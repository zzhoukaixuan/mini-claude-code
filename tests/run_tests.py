"""从临时工作区运行离线产品测试，避免读取真实配置或写入用户项目。"""

import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix='miniclaude-tests-') as directory:
        previous_directory = Path.cwd()
        try:
            os.chdir(directory)
            os.environ.update(OPENROUTER_API_KEY='test-key',
                              OPENROUTER_BASE_URL='http://127.0.0.1:1', MODEL_ID='test-model')
            suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        finally:
            # Windows 不允许删除进程当前目录，先离开测试工作区再清理。
            os.chdir(previous_directory)
    sys.exit(not result.wasSuccessful())
