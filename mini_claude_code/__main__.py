"""模块启动入口：支持执行 ``python -m mini_claude_code``。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
