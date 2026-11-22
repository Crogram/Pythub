"""mem 格式内存资源访问接口。

mem 格式将应用的非代码文件（数据文件等）一并打包为**内存资源条目**：
与字节码一样不落盘、随二进制内嵌、运行期在内存中读取。应用代码通过
本模块访问这些资源：

    from pythub.memres import mem_data, mem_data_names, materialize

    buf = mem_data("assets/logo.png")        # 读取单个资源（bytes）
    names = mem_data_names(prefix="assets/") # 列出资源
    d = materialize("assets")                # 需要真实文件时（如 C 库、
                                             # 子进程输入），一次性物化到缓存目录

资源路径即打包时的归档相对路径（项目文件保持项目内相对路径，第三方组件
数据带 ``lib/`` 前缀）。非 mem 格式运行时（zipapp/dir/exe 等）资源就是
真实文件，``materialize``/``mem_data`` 自动回退到包所在目录。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import List, Optional

_MEMDATA_MODULE = "__pythub_memdata__"


def _data() -> Optional[dict]:
    """内存资源字典（非 mem 环境返回 None）。"""
    import sys

    mod = sys.modules.get(_MEMDATA_MODULE)
    return getattr(mod, "DATA", None) if mod is not None else None


def mem_data_names(prefix: str = "") -> List[str]:
    """列出内存资源路径（可按前缀过滤）。非 mem 环境返回空列表。"""
    data = _data()
    if not data:
        return []
    return sorted(r for r in data if r.startswith(prefix))


def mem_data(rel: str) -> Optional[bytes]:
    """读取单个内存资源内容；资源不存在返回 None。"""
    data = _data()
    if not data:
        return None
    return data.get(rel)


def materialize(prefix: str = "", dest: Optional[Path] = None) -> Optional[Path]:
    """把内存资源（按前缀过滤）一次性物化到磁盘目录，返回该目录。

    适用于资源必须以真实文件存在的场景（如编译 C 引导器、作为子进程输入、
    第三方库坚持读文件）。默认落到 Pythub 缓存（内容 md5 寻址，重复调用复用）；
    传入 dest 则写到指定目录（目录不存在会自动创建，已有内容会被覆盖）。
    非 mem 环境返回 None（资源本来就是真实文件，无需物化）。
    """
    from pythub.utils import PYTHUB_CACHE_DIR

    data = _data()
    if not data:
        return None

    items = {rel: blob for rel, blob in data.items() if rel.startswith(prefix)}
    if not items:
        return None

    if dest is None:
        key = hashlib.md5(
            b"".join(rel.encode() + blob for rel, blob in sorted(items.items()))
        ).hexdigest()[:12]
        target = PYTHUB_CACHE_DIR / "memres" / key
    else:
        target = Path(dest)

    for rel, blob in items.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
        tmp.write_bytes(blob)
        shutil.move(str(tmp), str(path))  # 原子落盘，并发安全
    return target
