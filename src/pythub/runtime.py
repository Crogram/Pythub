"""嵌入式 Python 运行时管理。

从 python-build-standalone 项目下载目标平台/架构/版本的独立 CPython，
缓存于 ~/.cache/pythub/runtimes/，用于让产物自带解释器（目标机器免装 Python）。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple

from pythub import SUPPORTED_PYTHON
from pythub.utils import (
    PYTHUB_CACHE_DIR,
    ProgressLogger,
    download_file,
    find_executable,
    safe_extract,
)

RELEASE_TAG = "20241016"

PYTHON_PATCH_VERSIONS = {
    "3.10": "3.10.15",
    "3.11": "3.11.10",
    "3.12": "3.12.7",
}

# (platform, arch) -> python-build-standalone triplet
TRIPLETS = {
    ("macos", "x86_64"): "x86_64-apple-darwin",
    ("macos", "aarch64"): "aarch64-apple-darwin",
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("windows", "x86_64"): "x86_64-pc-windows-msvc-shared",
}


def detect_current() -> Tuple[str, str, str]:
    import platform
    import sys

    if sys.platform == "darwin":
        plat = "macos"
        mach = platform.machine()
        arch = "aarch64" if mach == "arm64" else ("x86_64" if mach == "x86_64" else mach)
    elif sys.platform == "win32":
        plat = "windows"
        arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    elif sys.platform.startswith("linux"):
        plat = "linux"
        mach = platform.machine()
        arch = "aarch64" if mach == "aarch64" else "x86_64"
    else:
        raise RuntimeError(f"不支持的平台: {sys.platform}")
    major, minor = sys.version_info[:2]
    pyver = f"{major}.{minor}"
    if pyver not in SUPPORTED_PYTHON:
        pyver = min(SUPPORTED_PYTHON, key=lambda v: abs(int(v.split(".")[1]) - minor))
    return plat, arch, pyver


def current_platform() -> str:
    return detect_current()[0]


def patch_version(pyver: str) -> str:
    if pyver not in PYTHON_PATCH_VERSIONS:
        raise ValueError(f"不支持的 Python 版本: {pyver}（可用: {', '.join(SUPPORTED_PYTHON)}）")
    return PYTHON_PATCH_VERSIONS[pyver]


def runtime_dir(platform: str, arch: str, pyver: str) -> Path:
    full = patch_version(pyver)
    return PYTHUB_CACHE_DIR / "runtimes" / f"cpython-{full}-{RELEASE_TAG}-{platform}-{arch}"


def fetch_runtime(
    platform: str,
    arch: str,
    pyver: str,
    logger: Optional[logging.Logger] = None,
) -> Path:
    """获取（必要时下载）目标平台嵌入式解释器目录。

    并发安全: 先在暂存目录完成下载/解压/瘦身并写入 .pythub-ok 标记，
    再原子 rename 到最终位置；若另一进程抢先完成（缓存已就位），直接
    复用其结果，避免半成品目录被并发进程误删/误用。"""
    triplet = TRIPLETS.get((platform, arch))
    if not triplet:
        raise ValueError(f"不支持的 目标平台/架构: {platform}/{arch}")
    full = patch_version(pyver)
    cache = runtime_dir(platform, arch, pyver)

    if (cache / ".pythub-ok").exists():
        # 旧版缓存未瘦身则就地瘦身
        marker = (cache / ".pythub-ok").read_text(encoding="utf-8")
        if "slim" not in marker:
            pruned = prune_runtime(cache, logger)
            if logger and pruned:
                logger.info("运行时缓存已瘦身: 移除 %s", ", ".join(pruned))
            (cache / ".pythub-ok").write_text(
                marker.rstrip("\n") + "\nslim\n", encoding="utf-8"
            )
        return cache

    # 优先使用官方 stripped 变体（无调试符号，体积显著更小，且跨平台无需本地 strip）
    archive_name = f"cpython-{full}+{RELEASE_TAG}-{triplet}-install_only_stripped.tar.gz"
    url = f"https://github.com/indygreg/python-build-standalone/releases/download/{RELEASE_TAG}/{archive_name}"

    import tempfile

    with tempfile.TemporaryDirectory(prefix="pythub-dl-") as td:
        tmp = Path(td)
        archive = tmp / archive_name
        if logger:
            logger.info("下载嵌入式 Python %s (%s/%s) ...", pyver, platform, arch)
        download_file(
            url,
            archive,
            logger=logger,
            progress=ProgressLogger(logger, f"Python {pyver}") if logger else None,
        )
        if logger:
            logger.info("解压运行时...")
        extract_runtime(archive, tmp / "extract")
        root = tmp / "extract" / "python"
        if not root.exists():
            raise RuntimeError(f"压缩包结构异常，缺少 python/ 目录: {archive_name}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        # 暂存目录（同父目录保证 rename 原子性），瘦身与标记先在暂存完成
        staging = cache.parent / f".{cache.name}.tmp-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.move(str(root), str(staging))
        pruned = prune_runtime(staging, logger)
        if logger and pruned:
            logger.info("运行时瘦身完成: 移除 %s", ", ".join(pruned))
        (staging / ".pythub-ok").write_text(f"{full}\n{triplet}\nslim\n", encoding="utf-8")

        # 就位: 另一并发进程可能已完成，双重检查后原子 rename
        if cache.exists():
            if (cache / ".pythub-ok").exists():
                shutil.rmtree(staging, ignore_errors=True)
                return cache
            shutil.rmtree(cache)  # 无标记的异常残留
        try:
            os.rename(staging, cache)
        except OSError:
            # rename 竞态失败: 若目标已完整就位则复用
            if (cache / ".pythub-ok").exists():
                shutil.rmtree(staging, ignore_errors=True)
                return cache
            raise

    return cache


# ---------------------------------------------------------------- 运行时瘦身

# 解压时直接跳过的成员（运行应用用不到的构建期内容；同时规避压缩包内符号链接循环）
_SKIP_TOP_DIRS = ("include", "share", "libs", "pypy")
_SKIP_STDLIB_DIRS = (
    "test", "idlelib", "lib2to3", "pydoc_data", "turtledemo", "ensurepip",
)
_SKIP_SITE_DIRS = ("pip", "setuptools", "pkg_resources", "wheel", "_distutils_hack")
_SKIP_SEGMENTS = ("__pycache__", "demos")


def _runtime_member_keep(name: str) -> bool:
    """判断 tar 压缩包成员是否保留（相对运行时根 python/ 过滤）。"""
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        return False
    if any(seg in _SKIP_SEGMENTS for seg in parts):
        return False
    if name.endswith(_PRUNE_FILE_SUFFIXES):
        return False
    rel = parts[1:] if parts[0] == "python" else parts
    if rel and rel[0] in _SKIP_TOP_DIRS:
        return False
    if len(rel) >= 2 and rel[0] in ("lib", "Lib") and rel[1].startswith("python3"):
        if len(rel) >= 3 and rel[2] in _SKIP_STDLIB_DIRS:
            return False
        if len(rel) >= 4 and rel[2] == "site-packages" and rel[3] in _SKIP_SITE_DIRS:
            return False
    return True


def extract_runtime(archive: Path, dest: Path) -> None:
    """解压运行时 tar 包，边解压边跳过无需保留的成员（防符号链接循环、省 IO）。"""
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        members = [m for m in tf.getmembers() if _runtime_member_keep(m.name)]
        tf.extractall(dest, members=members)


# 运行时根目录下可整体移除的目录（头文件/文档/静态链接库等构建期内容）
_PRUNE_ROOT_DIRS = ("include", "share", "libs", "pypy", "lib/pkgconfig")

# 标准库内可安全移除的目录（运行应用用不到）
_PRUNE_STDLIB_DIRS = (
    "test", "idlelib", "lib2to3", "pydoc_data", "turtledemo", "ensurepip",
    "site-packages/pip", "site-packages/setuptools", "site-packages/pkg_resources",
    "site-packages/wheel", "site-packages/_distutils_hack",
    "site-packages/setuptools-*.dist-info", "site-packages/pip-*.dist-info",
)

# bin 内无需保留的工具脚本（保留 python3 本体）
_BIN_PRUNE_PREFIXES = ("idle", "pydoc", "2to3", "pip", "wheel", "easy_install")

# 调试符号与静态库文件
_PRUNE_FILE_SUFFIXES = (".pdb", ".a", ".lib", ".pyo")

# setuptools 残留的 .pth（引用已移除的 _distutils_hack 会产生启动警告）
_PRUNE_PTH_FILES = ("distutils-precedence.pth",)


def _find_stdlib_dirs(runtime: Path) -> list:
    """定位标准库目录（unix: lib/python3.X；windows: Lib）。"""
    result = []
    lib = runtime / "lib"
    if lib.is_dir():
        result += [d for d in lib.iterdir() if d.is_dir() and d.name.startswith("python3.")]
    if (runtime / "Lib" / "os.py").exists():
        result.append(runtime / "Lib")
    return result


def prune_runtime(runtime: Path, logger=None) -> list:
    """就地瘦身运行时：移除调试符号、测试套件、pip/setuptools、文档、__pycache__，
    并 strip 二进制符号表（unix）。典型可减小 50%~80% 体积。返回移除内容类别列表。"""
    import subprocess

    is_windows = (runtime / "python.exe").exists()
    removed_kinds = []

    def rm_dir(d: Path) -> bool:
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            return True
        return False

    def rm_file(f: Path) -> bool:
        try:
            f.unlink()
            return True
        except OSError:
            return False

    # 1. 根目录构建期内容
    n = 0
    for d in _PRUNE_ROOT_DIRS:
        if rm_dir(runtime / d):
            n += 1
    if n:
        removed_kinds.append("include/share/静态库")

    # 2. 标准库内运行期用不到的包
    n = 0
    for stdlib in _find_stdlib_dirs(runtime):
        for pat in _PRUNE_STDLIB_DIRS:
            for d in stdlib.glob(pat):
                if rm_dir(d):
                    n += 1
    if n:
        removed_kinds.append("test/idlelib/pip等stdlib冗余包")

    # 3. bin 工具脚本（保留 python 本体）
    n = 0
    for bindir in (runtime / "bin", runtime / "Scripts"):
        if bindir.is_dir():
            for f in bindir.iterdir():
                if f.name.startswith(_BIN_PRUNE_PREFIXES):
                    n += rm_file(f)
    if n:
        removed_kinds.append("bin工具脚本")

    # 4. 调试符号文件（.pdb/.a/.lib）、setuptools 残留 .pth、__pycache__、tcl/tk demos
    for stdlib in _find_stdlib_dirs(runtime):
        sp = stdlib / "site-packages"
        if sp.is_dir():
            for name in _PRUNE_PTH_FILES:
                rm_file(sp / name)
    n = 0
    for root, dirs, files in os.walk(runtime):
        for d in list(dirs):
            if d == "__pycache__" or d == "demos":
                n += rm_dir(Path(root) / d)
                dirs.remove(d)
        for f in files:
            if f.endswith(_PRUNE_FILE_SUFFIXES):
                n += rm_file(Path(root) / f)
    if n:
        removed_kinds.append("调试符号/__pycache__")

    # 5. strip 二进制符号表（仅当运行时平台 == 当前主机平台时才可执行）
    strip = find_executable("strip")
    is_linux = bool(list((runtime / "lib").glob("libpython*.so*")))
    is_darwin = bool(list((runtime / "lib").glob("libpython*.dylib*")))
    host_match = (is_darwin and sys.platform == "darwin") or \
                 (is_linux and sys.platform.startswith("linux"))
    if strip and not is_windows and host_match:
        binaries = []
        bdir = runtime / "bin"
        if bdir.is_dir():
            binaries += [p for p in bdir.iterdir() if p.is_file() and not p.is_symlink()]
        binaries += list((runtime / "lib").glob("libpython*.dylib*"))
        binaries += list((runtime / "lib").glob("libpython*.so*"))
        for dyn in _find_stdlib_dirs(runtime):
            dynload = dyn / "lib-dynload"
            if dynload.is_dir():
                binaries += list(dynload.glob("*.so"))
        for b in binaries:
            subprocess.run([strip, "-x", str(b)], capture_output=True)
        if is_darwin and find_executable("codesign"):
            # darwin 二进制需重新 ad-hoc 签名（Apple Silicon 强制要求）
            for b in binaries:
                subprocess.run(["codesign", "--force", "--sign", "-", str(b)],
                               capture_output=True)
        removed_kinds.append("符号表(strip)")

    return removed_kinds


def interpreter_path(runtime: Path, platform: str) -> Path:
    if platform == "windows":
        return runtime / "python.exe"
    py3 = runtime / "bin" / "python3"
    return py3 if py3.exists() else runtime / "bin" / "python"
