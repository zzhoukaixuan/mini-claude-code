"""从白名单源码构建本地 wheel/sdist 与 SHA-256 清单；不上传、不安装、不读取用户凭据。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]  # 固定源码根目录，不依赖启动目录


def build_release() -> Path:
    destination = ROOT / 'dist'
    with tempfile.TemporaryDirectory(prefix='miniclaude-build-') as temporary:
        staging = Path(temporary) / 'source'
        staging.mkdir()
        for name in ('pyproject.toml', 'README.md', 'MANIFEST.in', 'LICENSE'):
            shutil.copy2(ROOT / name, staging / name)
        for folder, extension in (('mini_claude_code', '.py'), ('docs', '.md')):
            for original in (ROOT / folder).rglob('*' + extension):
                if '__pycache__' in original.parts:
                    continue
                target = staging / original.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
        def build(command: str, cwd: Path) -> None:
            subprocess.run([sys.executable, '-X', 'utf8', '-c', command], cwd=cwd, check=True, timeout=120)
        build("from setuptools.build_meta import build_sdist; build_sdist('dist')", staging)
        sdist, = (staging / 'dist').glob('*.tar.gz')
        unpack = Path(temporary) / 'unpack'
        unpack.mkdir()
        with tarfile.open(sdist) as archive:
            for member in archive.getmembers():
                parts = Path(member.name).parts
                if len(parts) > 1 and parts[1] not in {
                    'mini_claude_code', 'mini_claude_code.egg-info', 'docs', 'README.md',
                    'pyproject.toml', 'MANIFEST.in', 'PKG-INFO', 'setup.cfg', 'LICENSE',
                }:
                    raise RuntimeError('源码发行包包含非白名单路径，停止构建')
            archive.extractall(unpack, filter='data')
        restored, = unpack.iterdir()
        build("from setuptools.build_meta import build_wheel; build_wheel('dist')", restored)
        wheel, = (restored / 'dist').glob('*.whl')
        with zipfile.ZipFile(wheel) as archive:
            if any(not (name.startswith('mini_claude_code/') or '.dist-info/' in name) for name in archive.namelist()):
                raise RuntimeError('wheel 包含非产品路径，停止构建')
        destination.mkdir(exist_ok=True)
        records = []
        # 不覆盖已有不同产物，避免无声替换同版本发布文件。
        for artifact in (sdist, wheel):
            target = destination / artifact.name
            if target.exists():
                raise FileExistsError(f'产物已存在，请使用新版本号或先人工归档：{target.name}')
        for artifact in (sdist, wheel):
            target = destination / artifact.name
            shutil.copy2(artifact, target)
            records.append({'file': target.name, 'bytes': target.stat().st_size,
                            'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
        manifest = destination / (wheel.stem + '.manifest.json')
        manifest.write_text(json.dumps({'status': 'local release candidate; not uploaded', 'artifacts': records},
                                       ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return manifest


if __name__ == '__main__':
    print(build_release())
