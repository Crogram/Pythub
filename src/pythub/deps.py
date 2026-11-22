"""跨平台依赖下载：为目标平台/架构/Python 版本下载对应 wheel。

即需求中"在 Mac 上打包时只打包 Mac 需要的组件，在 Windows 上打包时只打包
Windows 需要的组件" —— 通过 pip 的 --platform/--abi/--python-version 实现。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import List, Optional

# (platform, arch) -> pip 平台标签
PIP_PLATFORM_TAGS = {
    ("macos", "x86_64"): "macosx_11_0_x86_64",
    ("macos", "aarch64"): "macosx_11_0_arm64",
    ("windows", "x86_64"): "win_amd64",
    ("windows", "aarch64"): "win_arm64",
    ("linux", "x86_64"): "manylinux2014_x86_64",
    ("linux", "aarch64"): "manylinux2014_aarch64",
}


def abi_tag(pyver: str) -> str:
    minor = pyver.split(".")[1]
    return f"cp{pyver.split('.')[0]}{minor}"


def download_wheels(
    requirements: List[str],
    platform: str,
    arch: str,
    pyver: str,
    dest: Path,
    logger: Optional[logging.Logger] = None,
) -> List[Path]:
    """下载目标平台的二进制 wheel，返回 wheel 文件列表。"""
    if not requirements:
        return []
    tag = PIP_PLATFORM_TAGS.get((platform, arch))
    if not tag:
        raise ValueError(f"不支持的 目标平台/架构: {platform}/{arch}")

    dest.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "pip", "download",
        "--only-binary=:all:",
        "--platform", tag,
        "--abi", abi_tag(pyver),
        "--python-version", pyver,
        "--implementation", "cp",
        "--disable-pip-version-check",
        "-q",
        "-d", str(dest),
        *requirements,
    ]
    if logger:
        logger.info("下载目标平台依赖 (%s/%s py%s): %s", platform, arch, pyver, ", ".join(requirements))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(
            f"依赖下载失败（目标平台 {platform}/{arch}, Python {pyver}）:\n{err}\n"
            f"提示: 确认依赖存在对应平台的二进制 wheel，或移除该依赖。"
        )
    return sorted(dest.glob("*.whl"))


def unpack_wheels(wheels: List[Path], dest: Path) -> None:
    """把 wheel 解包进 lib 目录（处理 *.data/purelib 等目录）。"""
    dest.mkdir(parents=True, exist_ok=True)
    import shutil

    for whl in wheels:
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(dest)
        # 合并 .data 目录
        for data_dir in dest.glob("*.data"):
            for sub in data_dir.iterdir():
                if sub.name in ("purelib", "platlib"):
                    for item in sub.iterdir():
                        target = dest / item.name
                        if target.is_dir() and item.is_dir():
                            shutil.copytree(item, target, dirs_exist_ok=True)
                        else:
                            shutil.move(str(item), str(target))
            shutil.rmtree(data_dir, ignore_errors=True)
