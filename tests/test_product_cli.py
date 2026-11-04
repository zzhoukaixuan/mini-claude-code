"""M1 产品化验收：安装元数据、CLI 路由、延迟加载和 Fake Provider 链路。"""

from __future__ import annotations

import contextlib
import copy
import io
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from mini_claude_code import __version__
from mini_claude_code import backend
from mini_claude_code import cli


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "MODEL_ID",
    "FALLBACK_MODEL_ID",
)


class ScriptedMessages:
    """返回固定响应并记录请求，确保产品验收不访问网络。"""

    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(copy.deepcopy(kwargs))
        return self.response


def text_response(text: str) -> SimpleNamespace:
    """构造符合 Anthropic SDK 对象形态的纯文本响应。"""
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=4, output_tokens=2),
    )


class PackagingMetadataTests(unittest.TestCase):
    """验证项目能够暴露稳定的控制台命令。"""

    def test_wheel_contains_runtime_and_help_is_lazy(self) -> None:
        """在临时源码副本构建真实 wheel，从解包产物验证，不写入项目构建目录。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(PROJECT_ROOT / name, source / name)
            for name in ("mini_claude_code",):
                shutil.copytree(
                    PROJECT_ROOT / name, source / name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            environment = ProductCLITests._subprocess_environment_without_model_config()
            environment.pop("PYTHONPATH", None)
            build = subprocess.run(
                [sys.executable, "-X", "utf8", "-c",
                 "from setuptools.build_meta import build_wheel; build_wheel('dist')"],
                cwd=source, env=environment, capture_output=True, text=True,
                encoding="utf-8", timeout=120,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            wheel, = (source / "dist").glob("*.whl")
            installed = root / "installed"
            with zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())
                self.assertFalse(any(name.split('/')[0] in {'s11','s11e','s14','s18','s18e','s19','tests','examples'} for name in names))
                self.assertNotIn('mini_claude_code/agent/mcp/fake.py', names)
                for package in ("mini_claude_code",):
                    for path in (PROJECT_ROOT / package).rglob("*.py"):
                        self.assertIn(path.relative_to(PROJECT_ROOT).as_posix(), names)
                archive.extractall(installed)
            # -I 排除源码 PYTHONPATH；导入钩子确保帮助路径不加载任何课程运行时。
            script = (
                "import sys, importlib.abc; sys.path.insert(0, sys.argv[1])\n"
                "class Guard(importlib.abc.MetaPathFinder):\n"
                " def find_spec(self, fullname, path=None, target=None):\n"
                "  if fullname.split('.')[0] in ('s11e', 's14', 's18', 's18e', 's19') or fullname.startswith('mini_claude_code.agent'):\n"
                "   raise AssertionError('help imported runtime: ' + fullname)\n"
                "sys.meta_path.insert(0, Guard())\n"
                "from mini_claude_code.cli import main\n"
                "main(['chat', '--help'])\n"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", "-c", script, str(installed)],
                cwd=root, env=environment, capture_output=True, text=True,
                encoding="utf-8", timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Worktree", result.stdout)

    def test_pyproject_declares_console_script_and_package_version(self) -> None:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
            metadata = tomllib.load(stream)

        self.assertEqual(
            metadata["project"]["scripts"]["miniclaude"],
            "mini_claude_code.cli:main",
        )
        dependency_names = {
            requirement.split(">=", maxsplit=1)[0].casefold()
            for requirement in metadata["project"]["dependencies"]
        }
        self.assertEqual(
            dependency_names,
            {"anthropic", "python-dotenv", "pyyaml", "rich"},
        )
        included_packages = set(
            metadata["tool"]["setuptools"]["packages"]["find"]["include"]
        )
        self.assertEqual(included_packages, {"mini_claude_code", "mini_claude_code.*"})
        self.assertEqual(__version__, "19.1.0rc1")


class ProductCLITests(unittest.TestCase):
    """验证帮助、错误语义和 ask/chat 命令路由。"""

    @staticmethod
    def _subprocess_environment_without_model_config() -> dict[str, str]:
        environment = os.environ.copy()
        for name in CONFIG_NAMES:
            environment.pop(name, None)
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(PROJECT_ROOT)
            if not existing_pythonpath
            else os.pathsep.join((str(PROJECT_ROOT), existing_pythonpath))
        )
        # Windows 的默认管道编码随终端而变化；产品输出统一按 UTF-8 验收。
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        # 不读取测试机真实用户级配置；该路径不存在，测试也不在此写入。
        environment["HOME"] = str(PROJECT_ROOT / "tests" / "_absent_user_home")
        environment["USERPROFILE"] = environment["HOME"]
        return environment

    def test_module_help_works_outside_project_without_model_config(self) -> None:
        environment = self._subprocess_environment_without_model_config()
        with tempfile.TemporaryDirectory() as temporary_directory:
            completed = subprocess.run(
                [sys.executable, "-m", "mini_claude_code", "--help"],
                cwd=temporary_directory,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("MiniClaudeCode", completed.stdout)
        self.assertIn("ask", completed.stdout)
        self.assertIn("chat", completed.stdout)
        self.assertNotIn("Traceback", completed.stderr)

    def test_missing_configuration_has_friendly_exit_code(self) -> None:
        environment = self._subprocess_environment_without_model_config()
        with tempfile.TemporaryDirectory() as temporary_directory:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mini_claude_code",
                    "ask",
                    "你好",
                ],
                cwd=temporary_directory,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, cli.EXIT_USAGE_ERROR)
        self.assertIn("配置错误", completed.stderr)
        self.assertIn("MODEL_ID", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_missing_api_key_is_rejected_before_sdk_initialization(self) -> None:
        environment = self._subprocess_environment_without_model_config()
        environment["OPENROUTER_BASE_URL"] = "https://example.invalid/api"
        environment["MODEL_ID"] = "test-model"
        with tempfile.TemporaryDirectory() as temporary_directory:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mini_claude_code",
                    "ask",
                    "你好",
                ],
                cwd=temporary_directory,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, cli.EXIT_USAGE_ERROR)
        self.assertIn("OPENROUTER_API_KEY", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_ask_routes_options_and_prints_answer(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(backend, "run_once", return_value="完成") as run_once,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = cli.main(
                [
                    "ask",
                    "检查",
                    "项目",
                    "--permission-mode",
                    "strict",
                    "--no-memory",
                ]
            )

        self.assertEqual(exit_code, cli.EXIT_OK)
        self.assertEqual(stdout.getvalue().strip(), "完成")
        run_once.assert_called_once_with(
            "检查 项目",
            permission_mode="strict",
            memory_enabled=False,
        )

    def test_chat_delegates_to_existing_interactive_entry(self) -> None:
        with patch.object(backend, "run_chat", return_value=0) as run_chat:
            exit_code = cli.main(["chat"])

        self.assertEqual(exit_code, cli.EXIT_OK)
        run_chat.assert_called_once_with()

    def test_configuration_error_is_sanitized(self) -> None:
        secret = "should-never-appear"
        stderr = io.StringIO()
        with (
            patch.object(
                backend,
                "run_once",
                side_effect=backend.ProductConfigurationError(
                    "OPENROUTER_API_KEY 不能为空"
                ),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = cli.main(["ask", "测试"])

        self.assertEqual(exit_code, cli.EXIT_USAGE_ERROR)
        self.assertIn("配置错误", stderr.getvalue())
        self.assertNotIn(secret, stderr.getvalue())


class ProductFakeProviderTests(unittest.TestCase):
    """通过正式产品 Agent Loop 验证 ask 后端，但替换外部 Provider。"""

    def setUp(self):
        self.enterContext(patch('mini_claude_code.mcp_configuration.load_mcp_servers', return_value={}))

    def test_run_once_reaches_agent_loop_without_network(self) -> None:
        import mini_claude_code.agent.core.agent_loop as agent_loop_module

        scripted_messages = ScriptedMessages(text_response("Fake Provider 回答"))
        with (
            patch.object(
                agent_loop_module,
                "client",
                SimpleNamespace(messages=scripted_messages),
            ),
            patch.object(agent_loop_module, "rprint"),
            patch.object(
                agent_loop_module,
                "console",
                SimpleNamespace(print=lambda *_args, **_kwargs: None),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            answer = backend.run_once(
                "请回答测试问题",
                permission_mode="strict",
                memory_enabled=False,
            )

        self.assertEqual(answer, "Fake Provider 回答")
        self.assertEqual(len(scripted_messages.calls), 1)
        request = scripted_messages.calls[0]
        messages = cast(list[dict[str, object]], request["messages"])
        tools = cast(list[object], request["tools"])
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "请回答测试问题")
        self.assertTrue(tools)
        self.assertFalse({'connect_mcp','disconnect_mcp'} & {tool['name'] for tool in tools})

    def test_run_chat_delegates_to_product_runtime_without_network(self) -> None:
        """先选择历史，再由轻量入口请求加载当前版本配置。"""
        import mini_claude_code.agent.sessions.startup as startup

        def fake_start(argv, *, prepare_runtime):
            self.assertEqual(argv, [])
            prepare_runtime()
            return 0

        with (
            patch.object(backend, "_validate_configuration") as validate,
            patch.object(startup, "main", side_effect=fake_start) as run_chat,
        ):
            result = backend.run_chat()

        self.assertEqual(result, 0)
        validate.assert_called_once_with(env_file=None)
        run_chat.assert_called_once()


if __name__ == "__main__":
    unittest.main()
