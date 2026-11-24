"""外部工具管理后端（需求 37）。

管理三类构建依赖工具，并提供 GUI「工具管理」页所需的状态/操作接口:

- 编译器: zig（下载安装/卸载/设为默认编译器）、cc、mingw 状态检测
- Python 运行时: 缓存列表 / 下载 / 移除 / 设为默认运行版本
- 打包工具: rpmbuild / appimagetool / strip / codesign 状态检测

所有管理操作（下载/安装/卸载/移除/设默认）写入**独立日志文件**
~/.cache/pythub/logs/tools-YYYYMMDD.log（按天分文件），与打包/发布日志分开。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pythub import SUPPORTED_ARCHS, SUPPORTED_PYTHON
from pythub.utils import (
    LOG_DIR,
    PYTHUB_CACHE_DIR,
    dir_size,
    download_file,
    load_settings,
    save_settings,
)

# pythub 托管的 zig 安装目录（GUI 安装 / find_zig 自动识别）
ZIG_MANAGED_DIR = PYTHUB_CACHE_DIR / "tools" / "zig"
_ZIG_INDEX_URL = "https://ziglang.org/download/index.json"

# 本机 (platform, arch) -> ziglang.org 发布清单键
_ZIG_HOST_KEYS = {
    ("macos", "x86_64"): "x86_64-macos",
    ("macos", "aarch64"): "aarch64-macos",
    ("linux", "x86_64"): "x86_64-linux",
    ("linux", "aarch64"): "aarch64-linux",
    ("windows", "x86_64"): "x86_64-windows",
}

# 默认编译器可选项（需求 37）
COMPILER_CHOICES = ("auto", "zig", "mingw", "cc")

_tools_file_handler: Optional[logging.FileHandler] = None


def tools_log_file_path() -> Path:
    """工具管理独立日志: logs/tools-YYYYMMDD.log（按天分文件）。"""
    return LOG_DIR / f"tools-{datetime.date.today():%Y%m%d}.log"


def get_tools_logger() -> logging.Logger:
    """工具管理专用日志器: 始终写入独立日志文件（需求 37）+ stdout。"""
    global _tools_file_handler
    logger = logging.getLogger("pythub.tools")
    if not logger.handlers:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("[%(levelname).1s] %(message)s"))
        logger.addHandler(stream)
    # 按天分文件: 日期变化（进程跨天运行）时自动切换到新日志文件
    expected = str(tools_log_file_path())
    if _tools_file_handler is None or _tools_file_handler.baseFilename != expected:
        if _tools_file_handler is not None:
            logger.removeHandler(_tools_file_handler)
            _tools_file_handler.close()
        Path(expected).parent.mkdir(parents=True, exist_ok=True)
        _tools_file_handler = logging.FileHandler(expected, encoding="utf-8")
        _tools_file_handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
        )
        logger.addHandler(_tools_file_handler)
    return logger


# ================================================================ 编译器


def _run_version(argv: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        if proc.returncode == 0:
            return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "未知版本"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def zig_status() -> Dict[str, object]:
    """zig 状态: found/path/version/managed（是否 pythub 托管安装）。"""
    from pythub.exe_archive import find_zig

    path = find_zig()
    if not path:
        return {"found": False, "path": "", "version": "", "managed": False}
    managed = Path(path).resolve().parent == ZIG_MANAGED_DIR.resolve()
    return {
        "found": True,
        "path": path,
        "version": _run_version([path, "version"]) or "未知版本",
        "managed": managed,
    }


def install_zig(
    progress: Optional[Callable[[float], None]] = None,
    logger: Optional[logging.Logger] = None,
) -> Path:
    """下载并安装最新稳定版 zig 到 pythub 托管目录（find_zig 自动识别）。

    仅用标准库: 读取 ziglang.org 发布清单 -> 下载对应本机平台压缩包
    -> 安全解压到 cache/tools/zig/ -> 验证 zig version。
    """
    from pythub.exe_archive import native_host_arch, native_host_platform
    from pythub.utils import safe_extract

    logger = logger or get_tools_logger()
    host = (native_host_platform(), native_host_arch())
    key = _ZIG_HOST_KEYS.get(host)
    if not key:
        raise RuntimeError(f"暂不支持为 {host[0]}/{host[1]} 自动安装 zig（请手动安装）")

    logger.info("获取 zig 最新稳定版清单...")
    req = urllib.request.Request(_ZIG_INDEX_URL, headers={"User-Agent": "pythub/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        index = json.loads(resp.read().decode("utf-8"))
    stable_ver = next((k for k in index if k != "master"), None)
    if not stable_ver or key not in index[stable_ver]:
        raise RuntimeError("无法解析 zig 发布清单（ziglang.org/index.json）")
    meta = index[stable_ver][key]
    url, size = meta["tarball"], int(meta.get("size") or 0)
    logger.info("开始下载 zig %s (%s, %s)...", stable_ver, key, f"{size / 1e6:.1f}MB" if size else "大小未知")

    suffix = ".zip" if sys.platform == "win32" else ".tar.xz"
    archive = PYTHUB_CACHE_DIR / "tools" / f"zig-{stable_ver}{suffix}"
    download_file(url, archive, logger, progress)

    logger.info("解压 zig 到 %s ...", ZIG_MANAGED_DIR)
    if ZIG_MANAGED_DIR.exists():
        shutil.rmtree(ZIG_MANAGED_DIR)
    with __import__("tempfile").TemporaryDirectory(prefix="pythub-zig-") as td:
        safe_extract(archive, Path(td))
        # 包内根目录为 zig-{host}-{ver}/，取出其内容平铺到托管目录
        roots = [p for p in Path(td).iterdir()]
        root = roots[0] if len(roots) == 1 else Path(td)
        shutil.copytree(root, ZIG_MANAGED_DIR, symlinks=True, dirs_exist_ok=True)
    archive.unlink(missing_ok=True)

    binary = ZIG_MANAGED_DIR / ("zig.exe" if sys.platform == "win32" else "zig")
    if not binary.is_file():
        raise RuntimeError(f"zig 安装异常: 未找到 {binary}")
    os.chmod(binary, 0o755)
    ver = _run_version([str(binary), "version"])
    logger.info("zig 安装完成 ✓ (%s): %s", ver, binary)
    return binary


def uninstall_zig(logger: Optional[logging.Logger] = None) -> None:
    """卸载 pythub 托管安装的 zig（不影响系统其他位置安装的 zig）。"""
    logger = logger or get_tools_logger()
    if not ZIG_MANAGED_DIR.exists():
        logger.warning("pythub 托管的 zig 未安装（%s 不存在）", ZIG_MANAGED_DIR)
        return
    shutil.rmtree(ZIG_MANAGED_DIR)
    logger.info("pythub 托管 zig 已卸载 ✓（%s）", ZIG_MANAGED_DIR)


def default_compiler() -> str:
    """当前默认编译器设置（auto/zig/mingw/cc），默认 auto（自动探测）。"""
    return load_settings().get("tools", {}).get("default_compiler", "auto")


def set_default_compiler(name: str, logger: Optional[logging.Logger] = None) -> None:
    """设置默认编译器并落盘（build.tab 打包时 exe/pexe 引导器按此优先选择）。"""
    if name not in COMPILER_CHOICES:
        raise ValueError(f"未知编译器: {name}（可选: {', '.join(COMPILER_CHOICES)}）")
    settings = load_settings()
    settings.setdefault("tools", {})["default_compiler"] = name
    save_settings(settings)
    (logger or get_tools_logger()).info("默认编译器已设为 %s ✓", name)


# ================================================================ Python 运行时


def list_runtimes() -> List[Dict[str, object]]:
    """列出已缓存的嵌入式运行时（目录名 + 大小）。"""
    root = PYTHUB_CACHE_DIR / "runtimes"
    items: List[Dict[str, object]] = []
    if not root.exists():
        return items
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / ".pythub-ok").exists():
            continue
        items.append({"name": child.name, "path": child, "size": dir_size(child)})
    return items


def remove_runtime(path: Path, logger: Optional[logging.Logger] = None) -> None:
    """移除指定运行时缓存目录。"""
    logger = logger or get_tools_logger()
    path = Path(path)
    if PYTHUB_CACHE_DIR / "runtimes" not in path.parents or not path.is_dir():
        raise ValueError(f"不是有效的运行时缓存目录: {path}")
    shutil.rmtree(path)
    logger.info("运行时已移除 ✓: %s", path.name)


def download_runtime(
    platform: str,
    arch: str,
    pyver: str,
    progress: Optional[Callable[[float], None]] = None,
    logger: Optional[logging.Logger] = None,
) -> Path:
    """下载（或复用）指定平台/架构/版本的嵌入式 Python 运行时。"""
    from pythub.runtime import fetch_runtime

    logger = logger or get_tools_logger()
    if pyver not in SUPPORTED_PYTHON:
        raise ValueError(f"不支持的 Python 版本: {pyver}（可用: {', '.join(SUPPORTED_PYTHON)}）")
    if (platform, arch) not in {
        ("macos", "x86_64"), ("macos", "aarch64"),
        ("linux", "x86_64"), ("linux", "aarch64"),
        ("windows", "x86_64"),
    }:
        raise ValueError(f"不支持的 平台/架构: {platform}/{arch}")
    logger.info("准备运行时: Python %s (%s/%s)...", pyver, platform, arch)
    path = fetch_runtime(platform, arch, pyver, logger)
    logger.info("运行时就绪 ✓: %s", path)
    return path


def default_python() -> str:
    """默认 Python 运行版本（打包页初始值），默认 3.12。"""
    ver = load_settings().get("build", {}).get("default_python", "3.12")
    return ver if ver in SUPPORTED_PYTHON else "3.12"


def set_default_python(pyver: str, logger: Optional[logging.Logger] = None) -> None:
    """设置默认 Python 运行版本并落盘。"""
    if pyver not in SUPPORTED_PYTHON:
        raise ValueError(f"不支持的 Python 版本: {pyver}（可用: {', '.join(SUPPORTED_PYTHON)}）")
    settings = load_settings()
    settings.setdefault("build", {})["default_python"] = pyver
    save_settings(settings)
    (logger or get_tools_logger()).info("默认 Python 版本已设为 %s ✓", pyver)


# ================================================================ 打包工具


# (标识, 展示名, 探测命令名列表, 安装提示)
_PACKAGING_TOOLS = (
    ("appimagetool", "appimagetool（AppImage 封装）", ("appimagetool",),
     "下载: https://appimage.org （放入 PATH；缺失时自动降级为 .run 自解压格式）"),
    ("strip", "strip（产物瘦身）", ("strip",),
     "macOS/Linux 通常随 Xcode Command Line Tools / binutils 提供"),
    ("codesign", "codesign（macOS 应用签名）", ("codesign",),
     "随 Xcode Command Line Tools 提供（xcode-select --install）"),
    ("cc", "cc/gcc/clang（本机 C 编译）", ("cc", "gcc", "clang"),
     "macOS: xcode-select --install | Linux: apt install build-essential"),
)


def packaging_tools_status() -> List[Dict[str, object]]:
    """打包工具链状态（found/path/hint），供工具管理页展示。"""
    from pythub.utils import find_executable

    result: List[Dict[str, object]] = []
    for key, label, names, hint in _PACKAGING_TOOLS:
        path = next((p for n in names if (p := find_executable(n))), None)
        result.append({
            "key": key, "label": label, "found": bool(path),
            "path": path or "", "hint": hint,
        })
    return result


# ================================================================ rpmbuild（需求 39）


def _find_brew() -> Optional[str]:
    from pythub.utils import find_executable

    found = find_executable("brew")
    if found:
        return found
    for cand in (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew")):
        if cand.is_file():
            return str(cand)
    return None


def _rpmbuild_pm_commands() -> Optional[Dict[str, List[str]]]:
    """按平台探测可用的包管理器，返回安装/卸载 rpmbuild 的命令。

    macOS: brew install rpm；Linux: apt/dnf/yum/zypper（需 sudo）。
    """
    from pythub.utils import find_executable

    if sys.platform == "darwin":
        brew = _find_brew()
        if brew:
            return {
                "install": [brew, "install", "rpm"],
                "uninstall": [brew, "uninstall", "rpm"],
            }
        return None
    if sys.platform == "linux":
        pm = find_executable("dnf") or find_executable("yum")
        if pm:
            return {
                "install": ["sudo", pm, "install", "-y", "rpm-build"],
                "uninstall": ["sudo", pm, "remove", "-y", "rpm-build"],
            }
        if find_executable("apt-get"):
            return {
                "install": ["sudo", "apt-get", "install", "-y", "rpm"],
                "uninstall": ["sudo", "apt-get", "remove", "-y", "rpm"],
            }
        if find_executable("zypper"):
            return {
                "install": ["sudo", "zypper", "--non-interactive", "install", "rpm"],
                "uninstall": ["sudo", "zypper", "--non-interactive", "remove", "rpm"],
            }
    return None


def rpmbuild_status() -> Dict[str, object]:
    """rpmbuild 状态: found/path/可通过包管理器托管安装。"""
    from pythub.utils import find_executable

    path = find_executable("rpmbuild")
    return {
        "found": bool(path),
        "path": path or "",
        "can_manage": _rpmbuild_pm_commands() is not None,
    }


def install_rpmbuild(logger: Optional[logging.Logger] = None) -> str:
    """通过系统包管理器安装 rpmbuild（需求 39，工具管理页一键安装）。

    macOS 用 brew（免 sudo）；Linux 经 sudo 调用 apt/dnf/yum/zypper，
    如当前用户无免密 sudo 权限则失败并提示手动执行命令。
    """
    from pythub.utils import find_executable

    logger = logger or get_tools_logger()
    existing = find_executable("rpmbuild")
    if existing:
        logger.info("rpmbuild 已安装，无需重复安装: %s", existing)
        return f"rpmbuild 已安装 ✓\n路径: {existing}"

    cmds = _rpmbuild_pm_commands()
    if not cmds:
        hint = ("未找到可用的包管理器。请手动安装 rpmbuild:\n"
                "  macOS: 先安装 Homebrew（https://brew.sh），然后 brew install rpm\n"
                "  Fedora: dnf install rpm-build | Ubuntu: apt install rpm")
        logger.warning("rpmbuild 自动安装失败: %s", hint)
        raise RuntimeError(hint)

    cmd = cmds["install"]
    logger.info("开始安装 rpmbuild: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    path = find_executable("rpmbuild")
    if proc.returncode != 0 or not path:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        logger.error("rpmbuild 安装失败: %s", detail)
        raise RuntimeError(
            "rpmbuild 安装失败（Linux 下可能需要输入 sudo 密码，建议在终端手动执行）:\n"
            f"  {' '.join(cmd)}\n{detail}")
    logger.info("rpmbuild 安装完成 ✓: %s", path)
    return f"rpmbuild 安装完成 ✓\n路径: {path}"


def uninstall_rpmbuild(logger: Optional[logging.Logger] = None) -> str:
    """通过系统包管理器卸载 rpmbuild（需求 39）。"""
    from pythub.utils import find_executable

    logger = logger or get_tools_logger()
    path = find_executable("rpmbuild")
    if not path:
        logger.warning("rpmbuild 未安装，无需卸载")
        return "rpmbuild 未安装，无需卸载"

    cmds = _rpmbuild_pm_commands()
    if not cmds:
        raise RuntimeError(
            "未找到安装 rpmbuild 所用的包管理器，请用原安装方式手动卸载")
    cmd = cmds["uninstall"]
    logger.info("开始卸载 rpmbuild: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        logger.error("rpmbuild 卸载失败: %s", detail)
        raise RuntimeError(
            f"rpmbuild 卸载失败，请手动执行:\n  {' '.join(cmd)}\n{detail}")
    logger.info("rpmbuild 已卸载 ✓（原路径: %s）", path)
    return f"rpmbuild 已卸载 ✓\n（缺失时 rpm 格式仍可自动改用自研引擎）"


# ================================================================ mingw（Windows PE 交叉编译）


# mingw 交叉 gcc 探测名（x86_64 优先）
_MINGW_GCC = ("x86_64-w64-mingw32-gcc", "aarch64-w64-mingw32-gcc")


def _mingw_pm_commands() -> Optional[Dict[str, List[str]]]:
    """按平台探测可用的包管理器，返回安装/卸载 mingw-w64 的命令。"""
    from pythub.utils import find_executable

    if sys.platform == "darwin":
        brew = _find_brew()
        if brew:
            return {
                "install": [brew, "install", "mingw-w64"],
                "uninstall": [brew, "uninstall", "mingw-w64"],
            }
        return None
    if sys.platform == "linux":
        pm = find_executable("dnf") or find_executable("yum")
        if pm:
            return {
                "install": ["sudo", pm, "install", "-y", "mingw64-gcc"],
                "uninstall": ["sudo", pm, "remove", "-y", "mingw64-gcc"],
            }
        if find_executable("apt-get"):
            return {
                "install": ["sudo", "apt-get", "install", "-y",
                            "gcc-mingw-w64-x86-64"],
                "uninstall": ["sudo", "apt-get", "remove", "-y",
                              "gcc-mingw-w64-x86-64"],
            }
    return None


def mingw_status() -> Dict[str, object]:
    """mingw 状态: found/paths/version/can_manage（需求: 编译器页托管管理）。"""
    from pythub.utils import find_executable

    paths = [p for n in _MINGW_GCC if (p := find_executable(n))]
    return {
        "found": bool(paths),
        "paths": paths,
        "version": _run_version([paths[0], "--version"]) if paths else "",
        "can_manage": _mingw_pm_commands() is not None,
    }


def install_mingw(logger: Optional[logging.Logger] = None) -> str:
    """通过系统包管理器安装 mingw-w64（Windows PE 交叉编译）。"""
    from pythub.utils import find_executable

    logger = logger or get_tools_logger()
    existing = find_executable(_MINGW_GCC[0]) or find_executable(_MINGW_GCC[1])
    if existing:
        logger.info("mingw-w64 已安装，无需重复安装: %s", existing)
        return f"mingw-w64 已安装 ✓\n路径: {existing}"

    cmds = _mingw_pm_commands()
    if not cmds:
        hint = ("未找到可用的包管理器。请手动安装 mingw-w64:\n"
                "  macOS: 先安装 Homebrew（https://brew.sh），然后 brew install mingw-w64\n"
                "  Ubuntu/Debian: apt install gcc-mingw-w64-x86-64\n"
                "  Fedora: dnf install mingw64-gcc")
        logger.warning("mingw-w64 自动安装失败: %s", hint)
        raise RuntimeError(hint)

    cmd = cmds["install"]
    logger.info("开始安装 mingw-w64: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    path = find_executable(_MINGW_GCC[0]) or find_executable(_MINGW_GCC[1])
    if proc.returncode != 0 or not path:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        logger.error("mingw-w64 安装失败: %s", detail)
        raise RuntimeError(
            "mingw-w64 安装失败（Linux 下可能需要输入 sudo 密码，建议在终端手动执行）:\n"
            f"  {' '.join(cmd)}\n{detail}")
    logger.info("mingw-w64 安装完成 ✓: %s", path)
    return (f"mingw-w64 安装完成 ✓\n路径: {path}\n"
            "Windows PE 格式将优先使用它编译引导器（缺失时自动改用 zig）")


def uninstall_mingw(logger: Optional[logging.Logger] = None) -> str:
    """通过系统包管理器卸载 mingw-w64。"""
    from pythub.utils import find_executable

    logger = logger or get_tools_logger()
    path = find_executable(_MINGW_GCC[0]) or find_executable(_MINGW_GCC[1])
    if not path:
        logger.warning("mingw-w64 未安装，无需卸载")
        return "mingw-w64 未安装，无需卸载"

    cmds = _mingw_pm_commands()
    if not cmds:
        raise RuntimeError(
            "未找到安装 mingw-w64 所用的包管理器，请用原安装方式手动卸载")
    cmd = cmds["uninstall"]
    logger.info("开始卸载 mingw-w64: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        logger.error("mingw-w64 卸载失败: %s", detail)
        raise RuntimeError(
            f"mingw-w64 卸载失败，请手动执行:\n  {' '.join(cmd)}\n{detail}")
    logger.info("mingw-w64 已卸载 ✓（原路径: %s）", path)
    return "mingw-w64 已卸载 ✓\n（Windows PE 交叉编译将自动改用 zig）"


# ================================================================ cc（系统原生编译器）


# 本机 cc 探测名（clang/gcc 及常见带版本后缀名）
_CC_NAMES = ("cc", "gcc", "clang", "gcc-15", "gcc-14", "gcc-13")


def _cc_pm_commands() -> Optional[Dict[str, List[str]]]:
    """按平台探测可用的包管理器，返回安装/卸载本机 C 编译器的命令。"""
    from pythub.utils import find_executable

    if sys.platform == "darwin":
        brew = _find_brew()
        if brew:
            return {
                "install": [brew, "install", "gcc"],
                "uninstall": [brew, "uninstall", "gcc"],
            }
        return None
    if sys.platform == "linux":
        pm = find_executable("dnf") or find_executable("yum")
        if pm:
            return {
                "install": ["sudo", pm, "install", "-y", "gcc"],
                "uninstall": ["sudo", pm, "remove", "-y", "gcc"],
            }
        if find_executable("apt-get"):
            return {
                "install": ["sudo", "apt-get", "install", "-y", "build-essential"],
                "uninstall": ["sudo", "apt-get", "remove", "-y", "build-essential"],
            }
    return None


def cc_status() -> Dict[str, object]:
    """cc 状态: found/paths/version/can_manage（需求: 编译器页托管管理）。"""
    from pythub.utils import find_executable

    paths = [p for n in _CC_NAMES if (p := find_executable(n))]
    return {
        "found": bool(paths),
        "paths": paths,
        "version": _run_version([paths[0], "--version"]) if paths else "",
        "can_manage": _cc_pm_commands() is not None,
    }


def install_cc(logger: Optional[logging.Logger] = None) -> str:
    """通过系统包管理器安装本机 C 编译器（POSIX 本机编译引导器兜底）。"""
    from pythub.utils import find_executable

    logger = logger or get_tools_logger()
    existing = next((p for n in _CC_NAMES if (p := find_executable(n))), None)
    if existing:
        logger.info("cc 已安装，无需重复安装: %s", existing)
        return f"cc 已安装 ✓\n路径: {existing}"

    cmds = _cc_pm_commands()
    if not cmds:
        hint = ("未找到可用的包管理器。请手动安装 C 编译器:\n"
                "  macOS: xcode-select --install（或先安装 Homebrew 后 brew install gcc）\n"
                "  Ubuntu/Debian: apt install build-essential\n"
                "  Fedora: dnf install gcc")
        logger.warning("cc 自动安装失败: %s", hint)
        raise RuntimeError(hint)

    cmd = cmds["install"]
    logger.info("开始安装 cc: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    path = next((p for n in _CC_NAMES if (p := find_executable(n))), None)
    if proc.returncode != 0 or not path:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        logger.error("cc 安装失败: %s", detail)
        raise RuntimeError(
            "cc 安装失败（Linux 下可能需要输入 sudo 密码，建议在终端手动执行）:\n"
            f"  {' '.join(cmd)}\n{detail}")
    logger.info("cc 安装完成 ✓: %s", path)
    return (f"cc 安装完成 ✓\n路径: {path}\n"
            "POSIX 本机编译将可使用系统原生编译器（默认编译器选 cc 时优先）")


def uninstall_cc(logger: Optional[logging.Logger] = None) -> str:
    """通过系统包管理器卸载本机 C 编译器。"""
    from pythub.utils import find_executable

    logger = logger or get_tools_logger()
    path = next((p for n in _CC_NAMES if (p := find_executable(n))), None)
    if not path:
        logger.warning("cc 未安装，无需卸载")
        return "cc 未安装，无需卸载"

    cmds = _cc_pm_commands()
    if not cmds:
        raise RuntimeError(
            "未找到安装 cc 所用的包管理器，请用原安装方式手动卸载")
    cmd = cmds["uninstall"]
    logger.info("开始卸载 cc: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        logger.error("cc 卸载失败: %s", detail)
        raise RuntimeError(
            f"cc 卸载失败，请手动执行:\n  {' '.join(cmd)}\n{detail}")
    logger.info("cc 已卸载 ✓（原路径: %s）", path)
    return ("cc 已卸载 ✓\n"
            "（系统自带的 clang/cc 可能仍可用，状态以实际探测为准；"
            "缺失时 POSIX 本机编译将自动改用 zig / mingw）")
