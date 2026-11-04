"""产品发布边界：源码隔离、只读诊断、独立发行包安装，无真实 API。"""

import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from mini_claude_code import doctor
from mini_claude_code.configuration import ConfigurationError, ModelConnection
from tests import test_product_cli

ROOT = Path(__file__).resolve().parents[1]


class ProductReleaseTests(unittest.TestCase):
    def test_public_tree_contains_only_product_sources(self):
        self.assertFalse((ROOT/'s19').exists())
        self.assertFalse((ROOT/'learn-claude-code').exists())
        self.assertFalse((ROOT/'LEARNING_PLAN.md').exists())
        self.assertIn('Copyright (c) 2026 zzhoukaixuan', (ROOT/'LICENSE').read_text())
        self.assertFalse((ROOT/'mini_claude_code/agent/mcp/fake.py').exists())

    def test_product_has_no_course_imports(self):
        for path in (ROOT/'mini_claude_code').rglob('*.py'):
            tree=ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                modules = ([node.module or ''] if isinstance(node,ast.ImportFrom) and node.level == 0
                           else [alias.name for alias in node.names] if isinstance(node,ast.Import) else [])
                for module in modules:
                    self.assertFalse(module.split('.')[0] in {'s11','s11e','s14','s18','s18e','s19'},(path,module))

    def test_doctor_is_read_only_and_hides_credentials(self):
        connection=ModelConnection('https://example.invalid/api','test-doctor-secret','fake-model',source='test')
        output=io.StringIO()
        with patch.object(doctor,'resolve_connection',return_value=connection), \
                patch.object(doctor,'load_mcp_servers',return_value={}), \
                patch('socket.socket.connect',side_effect=AssertionError('no network')), redirect_stdout(output):
            status=doctor.run_doctor(as_json=True)
        report=json.loads(output.getvalue())
        self.assertEqual(status,0)
        self.assertFalse(report['network_tested'])
        self.assertNotIn(connection.api_key,output.getvalue())
        self.assertNotIn(connection.base_url,output.getvalue())

    def test_doctor_reports_invalid_config_without_raw_exception(self):
        output=io.StringIO()
        with patch.object(doctor,'resolve_connection',side_effect=ConfigurationError('secret-in-error')), \
                patch.object(doctor,'load_mcp_servers',return_value={}), redirect_stdout(output):
            self.assertEqual(doctor.run_doctor(as_json=True),1)
        self.assertNotIn('secret-in-error',output.getvalue())

    def test_product_fake_matches_real_interface(self):
        from tests.support.mcp_fake import create_fake_docs_client
        from mini_claude_code.agent.mcp.registry import MCPRegistry
        registry=MCPRegistry([],{})
        registry.register('demo',create_fake_docs_client())
        _, handlers=registry.assemble_tool_pool()
        self.assertIn('fake-docs',handlers['mcp__demo__get_version']().content)
        registry.disconnect('demo')

    def test_sdist_wheel_installed_without_course_or_source_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source=root/'source'; source.mkdir()
            for name in ('pyproject.toml','README.md','LICENSE'):
                shutil.copy2(ROOT/name,source/name)
            shutil.copytree(ROOT/'mini_claude_code',source/'mini_claude_code',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            # 这些毒丸若进入发行包即失败；不接触用户真实配置或课程文件。
            for name in ('s19','tests','.memory'):
                (source/name).mkdir()
                (source/name/'secret_fixture.py').write_text('raise RuntimeError("not distributable")',encoding='utf-8')
            (source/'.env').write_text('DO_NOT_DISTRIBUTE=fake-secret',encoding='utf-8')
            env=test_product_cli.ProductCLITests._subprocess_environment_without_model_config()
            env.pop('PYTHONPATH',None)
            def run(args,cwd=root,timeout=120):
                result=subprocess.run(args,cwd=cwd,env=env,capture_output=True,text=True,encoding='utf-8',timeout=timeout)
                self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
                return result
            run([sys.executable,'-X','utf8','-c',"from setuptools.build_meta import build_sdist; build_sdist('dist')"],source)
            archive,=(source/'dist').glob('*.tar.gz')
            unpack=root/'unpack'; unpack.mkdir()
            with tarfile.open(archive) as package:
                names = {Path(member.name).parts[-1] for member in package.getmembers()}
                self.assertTrue({'README.md', 'LICENSE', 'pyproject.toml'} <= names)
                for member in package.getmembers():
                    parts=Path(member.name).parts
                    if len(parts)>1:
                        self.assertNotIn(parts[1],{'s19','tests','.memory','.env','examples'})
                package.extractall(unpack,filter='data')
            restored,=unpack.iterdir()
            run([sys.executable,'-X','utf8','-c',"from setuptools.build_meta import build_wheel; build_wheel('dist')"],restored)
            wheel,=(restored/'dist').glob('*.whl')
            with zipfile.ZipFile(wheel) as package:
                self.assertTrue(all(name.startswith('mini_claude_code/') or '.dist-info/' in name for name in package.namelist()))
                licenses = [name for name in package.namelist() if name.endswith('/LICENSE')]
                self.assertEqual(len(licenses), 1)
                self.assertIn(b'Copyright (c) 2026 zzhoukaixuan', package.read(licenses[0]))
            # 独立 venv、复用本机依赖以离线运行；Guard 阻止借用旧 editable finder 或课程包。
            run([sys.executable,'-m','venv','--system-site-packages',str(root/'venv')])
            python=root/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            run([str(python),'-m','pip','install','--no-index','--no-deps','--ignore-installed',str(wheel)])
            script = '''
import importlib.abc, importlib.util, os, sys, pathlib
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('s11','s11e','s14','s18','s18e','s19'):
            raise AssertionError('course import: '+fullname)
sys.meta_path.insert(0,Guard())
import mini_claude_code
assert pathlib.Path(mini_claude_code.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
from mini_claude_code.cli import main
try: main(['--help'])
except SystemExit as error: assert error.code == 0
from unittest.mock import patch
from types import SimpleNamespace as NS
os.environ.update(OPENROUTER_API_KEY='test-key',OPENROUTER_BASE_URL='http://127.0.0.1:1',MODEL_ID='test-model')
from mini_claude_code import backend
with patch('mini_claude_code.mcp_configuration.load_mcp_servers',return_value={}):
    import mini_claude_code.agent.core.agent_loop as agent
response=NS(stop_reason='end_turn',content=[NS(type='text',text='wheel-ok')],usage=NS(input_tokens=1,output_tokens=1))
with patch.object(agent.client.messages,'create',return_value=response), patch('socket.socket.connect',side_effect=AssertionError('network')):
    assert backend.run_once('hello',memory_enabled=False)=='wheel-ok'
assert importlib.util.find_spec('mini_claude_code.agent.mcp.fake') is None
print('isolated-wheel-runtime-ok')
'''
            result=run([str(python),'-I','-X','utf8','-c',script])
            self.assertIn('isolated-wheel-runtime-ok',result.stdout)

    def test_readme_local_links_exist(self):
        import re
        text = (ROOT/'README.md').read_text(encoding='utf-8')
        for target in re.findall(r'\]\(([^)]+)\)', text):
            if '://' not in target and not target.startswith('#'):
                self.assertTrue((ROOT/target.split('#')[0]).exists(), target)
        self.assertNotIn('scripts/build_release.py', text)
        self.assertNotIn('examples/mcp_http_server.py', text)
