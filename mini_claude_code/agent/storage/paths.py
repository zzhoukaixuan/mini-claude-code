"""存储路径比较辅助：统一 Windows 扩展路径前缀后再执行边界判断。"""

from __future__ import annotations

import os
from pathlib import Path


def _comparison_path(path: Path) -> Path:
    """返回只用于比较的规范路径，不改变实际文件操作所使用的路径。"""
    path_text = str(path)
    if os.name == "nt":
        # Windows 在文件创建竞态中可能让 resolve() 对同一路径交替返回
        # ``E:\...`` 与 ``\\?\E:\...``。二者锚点字符串不同，Path 的
        # is_relative_to() 会误判越界，因此仅在比较时统一命名空间。
        if path_text.startswith("\\\\?\\UNC\\"):
            path_text = "\\\\" + path_text[8:]
        elif path_text.startswith("\\\\?\\"):
            path_text = path_text[4:]
        path_text = os.path.normcase(path_text)
    return Path(path_text)


def is_path_within_root(path: Path, root: Path) -> bool:
    """判断两个已解析路径的真实层级，同时兼容 Windows 扩展路径表示。"""
    if not isinstance(path, Path) or not isinstance(root, Path):
        raise TypeError("存储路径边界比较只接受 pathlib.Path")
    return _comparison_path(path).is_relative_to(_comparison_path(root))
