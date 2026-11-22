"""自研单文件打包器（P3 全平台化）：bootloader（自研 C 引导器）+ 自研归档格式 v2。

同一套 C 引导器源码 + 归档格式，三平台产出：
- Windows : PE  (.exe)  —— mingw 交叉编译，或 zig cc（-Wl,--subsystem 选子系统）
- macOS   : Mach-O (.pexe；mem 模式 .mem.pexe) —— 本机 cc 编译 POSIX 分支；交叉用 zig cc
- Linux   : ELF   (.pexe；mem 模式 .mem.pexe) —— 本机 cc 编译 POSIX 分支；交叉用 zig cc（musl 静态）

跨平台: zig cc 自带全部目标平台的 libc/链接桩（musl/mingw/macOS tbd），
单工具链即可从任意主机交叉产出三平台任意架构的引导器——mem/pexe 格式
全平台交叉构建依赖 zig（brew install zig，或 PYTHUB_ZIG 指向 zig 可执行）。

参考 PyInstaller CArchive/bootloader 的思路独立实现，全程不依赖 PyInstaller：

- 归档格式 v2（见 bootloader/bootex.c 头部注释）:
    [ 引导器本体 ][ 文件数据区 ][ TOC ][ 96 字节 cookie ]
  每条目可选 zlib(DEFLATE) 压缩（Python 端 zlib.compress 压缩，C 端内嵌
  miniz/tinfl 解压）；cookie 含魔数、偏移、(数据区+TOC) 的 MD5（缓存键）
  与应用名（缓存按应用分组清理）。

- 引导器: 自研 C 源码随包分发（bootloader/bootex.c + vendored miniz tinfl
  解压器，仅 1 个 .c + 3 个 .h），Windows 分支首次打包时用 mingw-w64 交叉
  编译 PE，POSIX 分支用本机 cc 编译 Mach-O/ELF，产物缓存到 PYTHUB_CACHE_DIR
  ——构建机无需安装 Windows / MSVC / Visual Studio。

- stdlib zip 化（文件数骤减）: runtime/Lib 的 .py 打成单个 stdlib.zip，
  配 pythonXY._pth 引导（官方 embeddable 同款机制），TOC 条目数从 ~1600
  降到 ~20，首次解压显著提速；site-packages 与非 .py 文件保持散文件。

- 完整性校验（P2）: 运行时校验归档体 MD5（防单文件被篡改/截断）与缓存启动器
  MD5（防缓存目录被篡改），不一致时自动重新解压。
- 窗口子系统（P2）: pythub.toml [app] windowed=true 时编译 -mwindows PE，
  GUI 应用不闪控制台黑框，致命错误用 MessageBox 呈现。
- 缓存清理（P2）: cookie 携带应用名（归档格式 v3），运行时缓存按应用分组
  （<缓存>/exe/<应用>/<md5>）；引导器每次首次解压后 LRU 清理同应用旧 md5
  目录，并按缓存总大小上限（默认 4GB，可用 PYTHUB_CACHE_MAX_MB 覆盖）
  从最久未用的应用组开始驱逐。
"""

from __future__ import annotations

import hashlib
import io
import os
import platform as _platform_mod
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path
from typing import List, Optional, Tuple

from pythub.utils import PYTHUB_CACHE_DIR, find_executable, set_executable

MAGIC = b"PYTHUBEX"  # PythubEX 归档 magic（与 bootex.c 保持一致）
COOKIE_SIZE = 96
APP_FIELD_SIZE = 32
FLAG_DEFLATE = 1


def sanitize_app_name(name: str) -> str:
    """应用名净化为缓存目录安全名: 仅 [A-Za-z0-9._-]，其余替换为 _，截断 32 字节。

    该值写入 cookie 应用名字段，用作运行时缓存的分组目录名（LRU 清理的
    "同一应用"识别键），无需保留原始大小写以外的语义。"""
    cleaned = "".join(
        c if (c.isascii() and (c.isalnum() or c in "._-")) else "_"
        for c in (name or "")
    )
    cleaned = cleaned[:APP_FIELD_SIZE].lstrip(".") or "_app"
    return cleaned

# TOC 条目: path_len(u32), stored_size(u32), raw_size(u32), offset(u64), flags(u8)
_TOC_ITEM_FMT = "<IIIQB"

FLAG_DEFLATE = 1  # 条目经 zlib 压缩
FLAG_SYMLINK = 2  # 条目为符号链接，内容为链接目标串（POSIX 重建；Windows 回退为普通文件）
FLAG_MEM = 4  # 内存载荷条目: 引导器解压时不落盘，仅在内存中解出后直传解释器执行

# mem 模式 TOC 条目路径（与 bootex.c 保持一致）
MEM_BOOT_REL = "app.boot"  # Python 引导器源码
MEM_PAYLOAD_REL = "app.mem"  # 应用字节码（裸 marshal）

_BOOT_SOURCES = ("bootex.c", "miniz_tinfl.c")
_CROSS_GCC = {
    "x86_64": "x86_64-w64-mingw32-gcc",
    "aarch64": "aarch64-w64-mingw32-gcc",
}
_COMPILE_BASE = ["-O2", "-s", "-Wall"]

# zig cc 目标三元组（zig 自带全平台 libc/链接桩，无需目标平台 SDK）
_ZIG_TARGETS = {
    ("windows", "x86_64"): "x86_64-windows-gnu",
    ("windows", "aarch64"): "aarch64-windows-gnu",
    ("macos", "x86_64"): "x86_64-macos",
    ("macos", "aarch64"): "aarch64-macos",
    ("linux", "x86_64"): "x86_64-linux-musl",  # musl 静态链接，单文件无 glibc 依赖
    ("linux", "aarch64"): "aarch64-linux-musl",
}


def find_zig() -> Optional[str]:
    """查找 zig 工具链: PYTHUB_ZIG 环境变量优先，其次 PATH。"""
    env = os.environ.get("PYTHUB_ZIG")
    if env and Path(env).exists():
        return env
    return find_executable("zig")


def _compile_flags(console: bool) -> list:
    """console=True 控制台子系统（默认）; False 窗口子系统:
    -mwindows 无控制台窗口，GUI_APP 下致命错误用 MessageBox 呈现、状态输出静默。"""
    subsystem = ["-mconsole"] if console else ["-mwindows", "-DGUI_APP=1"]
    return _COMPILE_BASE + subsystem + ["-lshell32"]


def _check_path(name: str) -> None:
    """防路径穿越（与 bootex.c unsafe_path 同规则）: 拒绝绝对路径、盘符、.. 段。"""
    if not name:
        raise ValueError("归档条目路径为空")
    if name[0] in "/\\":
        raise ValueError(f"归档条目为绝对路径: {name!r}")
    if len(name) >= 2 and name[1] == ":" and name[0].isascii() and name[0].isalpha():
        raise ValueError(f"归档条目含盘符: {name!r}")
    for part in name.replace("\\", "/").split("/"):
        if part == "..":
            raise ValueError(f"归档条目含 .. 路径段: {name!r}")


def _bootloader_dir() -> Path:
    return Path(__file__).parent / "bootloader"


def _resolve_bootloader_dir() -> Path:
    """定位引导器源码目录。

    常规场景: 包安装目录下的 bootloader/。mem/内存执行场景（自打包产物中
    __file__ 为 <memory:...>，包目录不存在）: 从内存资源条目把源码物化到
    Pythub 缓存（md5 寻址，编译后即可复用）。"""
    bdir = _bootloader_dir()
    if all((bdir / s).exists() for s in _BOOT_SOURCES):
        return bdir
    from pythub.memres import mem_data, mem_data_names

    # 资源条目路径可能带包前缀（自打包: pythub/bootloader/xxx），
    # 匹配任意层级的 bootloader/ 目录并物化
    rels = [r for r in mem_data_names()
            if (r.startswith("bootloader/") or "/bootloader/" in r)
            and mem_data(r) is not None]
    if not rels:
        return bdir
    common = rels[0][:rels[0].rfind("bootloader/") + len("bootloader/")]
    key = hashlib.md5(
        b"".join(r.encode() + mem_data(r) for r in sorted(rels))
    ).hexdigest()[:12]
    target = PYTHUB_CACHE_DIR / "bootloader" / f"memsrc-{key}"
    for rel in sorted(rels):
        path = target / rel[len(common):]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(mem_data(rel))
    return target


def native_host_platform() -> Optional[str]:
    """本机平台名（pexe 只能在本机对应平台上生成）。"""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return None


def native_host_arch() -> str:
    """本机架构（归一为 x86_64/aarch64）。"""
    m = _platform_mod.machine()
    return {"arm64": "aarch64"}.get(m, m)


def ensure_bootloader(arch: str = "x86_64", console: bool = True, logger=None) -> Path:
    """交叉编译自研引导器并缓存（bootex + miniz 任一源码或编译标志变化自动重编译）。"""
    if arch not in _CROSS_GCC:
        raise ValueError(f"不支持的 Windows 架构: {arch}")
    bdir = _resolve_bootloader_dir()
    sources = [bdir / s for s in _BOOT_SOURCES]
    for s in sources:
        if not s.exists():
            raise RuntimeError(f"引导器源码缺失: {s}")
    flags = _compile_flags(console)
    key = hashlib.md5(
        b"".join(s.read_bytes() for s in sources) + repr(flags).encode()
    ).hexdigest()[:12]
    cache = PYTHUB_CACHE_DIR / "bootloader" / f"win-{arch}-{key}"
    out = cache / "bootex.exe"
    if out.exists():
        return out

    cc = find_executable(_CROSS_GCC[arch])
    if not cc:
        if find_zig():
            return ensure_bootloader_target("windows", arch, console, logger)
        raise RuntimeError(
            f"生成 .exe 需要交叉编译器 {_CROSS_GCC[arch]} 或 zig（用于编译自研 PE 引导器）。\n"
            "macOS: brew install mingw-w64 | Ubuntu: apt install gcc-mingw-w64-x86-64\n"
            "或安装 zig 后自动改用: brew install zig（PYTHUB_ZIG 可指定 zig 路径）"
        )
    cache.mkdir(parents=True, exist_ok=True)
    tmp_out = cache / f"bootex.tmp-{os.getpid()}"
    proc = subprocess.run(
        [cc, *flags, "-o", str(tmp_out), *[str(s) for s in sources]],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        shutil.rmtree(cache, ignore_errors=True)  # 失败清理缓存目录（防半成品残留）
        raise RuntimeError(f"引导器编译失败:\n{proc.stderr}")
    os.replace(tmp_out, out)  # 原子落位，并发构建安全
    if logger:
        logger.info("引导器编译完成: %s", out)
    return out


def _stdlib_zipify(
    entries: List[Tuple[str, bytes, bool]], python: str
) -> List[Tuple[str, bytes, bool]]:
    """stdlib zip 化: runtime/Lib 的 .py 合并为 runtime/stdlib.zip，
    并生成 pythonXY._pth（官方 embeddable 同款机制）引导解释器从 zip 导入。"""
    lib_prefix = "runtime/Lib/"
    lib_py = [e for e in entries if e[0].startswith(lib_prefix) and e[0].endswith(".py")]
    if not lib_py:
        return entries

    loose: List[Tuple[str, bytes, bool]] = []
    zip_items: List[Tuple[str, bytes]] = []
    for rel, data, is_link in lib_py:
        sub = rel[len(lib_prefix):]
        if sub.startswith("site-packages/"):
            loose.append((rel, data, is_link))  # 站内包保持散文件，路径语义不变
        else:
            zip_items.append((sub, data))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arc, data in sorted(zip_items):
            zf.writestr(arc, data)

    result = [e for e in entries if not (e[0].startswith(lib_prefix) and e[0].endswith(".py"))]
    result += loose  # site-packages .py 保持散文件
    result.append(("runtime/stdlib.zip", buf.getvalue(), False))

    # pythonXY._pth: 相对路径基于 runtime/（_pth 文件所在目录）
    pth_name = f"runtime/python{python.replace('.', '')}._pth"
    lines = ["stdlib.zip"]
    if any(rel.startswith("runtime/DLLs/") for rel, _, _ in result):
        lines.append("DLLs")
    # Lib 行: 有 site-packages 散文件或任何 Lib/ 下非 .py 条目（如 .pyd）时必须保留
    if loose or any(rel.startswith(lib_prefix) for rel, _, _ in result):
        lines.append("Lib")
    lines.append(".")  # runtime 根（pythonXY.dll 等所在）
    result.append((pth_name, ("\n".join(lines) + "\n").encode("ascii"), False))
    return result


def _collect_entries(bundle_dir: Path) -> List[Tuple[str, bytes, bool]]:
    """收集 bundle 全部条目为 (相对路径, 内容, 是否符号链接)。

    符号链接条目的"内容"为链接目标串（如 bin/python3 -> python3.12），
    归档中以 FLAG_SYMLINK 存储，引导器解压时重建（POSIX），避免跟随
    symlink 复制整个目标文件（python3.12 二进制会被重复打包 ~15MB）。"""
    entries: List[Tuple[str, bytes, bool]] = []
    for root, dirs, fnames in os.walk(bundle_dir):
        dirs.sort()
        rel_root = Path(root).relative_to(bundle_dir)
        for fn in sorted(fnames):
            rel = (rel_root / fn).as_posix()
            p = Path(root) / fn
            if p.is_symlink():
                entries.append((rel, os.readlink(p).encode("utf-8"), True))
            else:
                entries.append((rel, p.read_bytes(), False))
    return entries


def _write_archive(
    boot_bytes: bytes,
    bundle_dir: Path,
    out: Path,
    python: str,
    zipify: bool = True,
    app_name: str = "",
    mem_boot: bytes = b"",
    mem_payload: bytes = b"",
) -> Path:
    """引导器 + 自研归档拼装（Windows exe 与原生 pexe 共用）。

    app_name 写入 cookie 应用名字段（v3），引导器据此把运行时缓存分组到
    <缓存>/exe/<应用>/<md5>，实现同应用旧 md5 目录 LRU 清理。

    mem_boot / mem_payload 非空时（exe/pexe 的 mem 内存执行模式）追加两个
    FLAG_MEM 内存载荷条目: 引导器解压时跳过（不落盘），运行时在内存中解出——
    引导器源码经 PYTHUB_BOOT 环境变量注入、应用字节码经管道直传解释器执行。"""
    entries = _stdlib_zipify(_collect_entries(bundle_dir), python) if zipify \
        else _collect_entries(bundle_dir)
    entries.sort(key=lambda e: e[0])

    payload_offset = len(boot_bytes)
    blob = bytearray()
    toc_items = []
    for rel, data, is_link in entries:
        off = payload_offset + len(blob)
        comp = zlib.compress(data, 9)
        if len(comp) < len(data):
            stored, flags = comp, FLAG_DEFLATE
        else:
            stored, flags = data, 0
        if is_link:
            flags |= FLAG_SYMLINK
        blob += stored
        toc_items.append((rel, len(stored), len(data), off, flags))

    # mem 内存载荷条目（归档压缩存储；引导器只解到内存，不落盘）
    for rel, data in ((MEM_BOOT_REL, mem_boot), (MEM_PAYLOAD_REL, mem_payload)):
        if not data:
            continue
        off = payload_offset + len(blob)
        comp = zlib.compress(data, 9)
        stored = comp if len(comp) < len(data) else data
        flags = FLAG_MEM | (FLAG_DEFLATE if stored is comp else 0)
        blob += stored
        toc_items.append((rel, len(stored), len(data), off, flags))

    toc = bytearray(struct.pack("<I", len(toc_items)))
    for rel, stored, raw, off, flags in toc_items:
        pb = rel.encode("utf-8")
        toc += struct.pack(_TOC_ITEM_FMT, len(pb), stored, raw, off, flags)
        toc += pb

    toc_offset = payload_offset + len(blob)
    md5 = hashlib.md5(bytes(blob) + bytes(toc)).hexdigest()
    cookie = bytearray(MAGIC)
    cookie += struct.pack("<QQQ", payload_offset, toc_offset, len(toc))
    cookie += md5.encode("ascii").ljust(32, b"\0")
    app_field = sanitize_app_name(app_name).encode("ascii").ljust(APP_FIELD_SIZE, b"\0")
    assert len(app_field) == APP_FIELD_SIZE
    cookie += app_field
    assert len(cookie) == COOKIE_SIZE

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as f:
            f.write(boot_bytes)
            f.write(bytes(blob))
            f.write(bytes(toc))
            f.write(bytes(cookie))
        os.replace(tmp, out)  # 原子落位：中断不会留下损坏的产物
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    set_executable(out)
    return out


def make_exe(
    bundle_dir: Path,
    out: Path,
    arch: str = "x86_64",
    python: str = "3.12",
    logger=None,
    console: bool = True,
    app_name: str = "",
    mem_boot: bytes = b"",
    mem_payload: bytes = b"",
) -> Path:
    """把自包含目录打包为单文件 Windows .exe（自研归档 + 自研 PE 引导器）。

    - 条目按需 zlib 压缩（压缩无收益则原样存储）；
    - stdlib zip 化将 Lib/*.py 合并为单个 stdlib.zip，文件数骤减；
    - console=False 生成窗口子系统 PE（GUI 应用不闪黑框）；
    - app_name 写入 cookie，运行时缓存按应用分组并 LRU 清理旧版本；
    - mem_boot/mem_payload 非空时为 mem 内存执行模式（载荷不落盘，见
      _write_archive）。
    """
    boot = ensure_bootloader(arch, console, logger)
    return _write_archive(boot.read_bytes(), bundle_dir, out, python,
                          zipify=True, app_name=app_name,
                          mem_boot=mem_boot, mem_payload=mem_payload)


def ensure_bootloader_native(console: bool = True, logger=None) -> Path:
    """编译 POSIX 原生引导器（macOS Mach-O / Linux ELF）并缓存。

    引导器源码或编译标志变化自动重编译；仅本机平台（POSIX 分支用
    /proc/self/exe 或 argv0 定位自身，两端共用一套源码）。"""
    bdir = _resolve_bootloader_dir()
    sources = [bdir / s for s in _BOOT_SOURCES]
    for s in sources:
        if not s.exists():
            raise RuntimeError(f"引导器源码缺失: {s}")
    flags = ["-O2", "-s"] + ([] if console else ["-DGUI_APP=1"])
    key = hashlib.md5(
        b"".join(s.read_bytes() for s in sources) + repr(flags).encode()
    ).hexdigest()[:12]
    cache = PYTHUB_CACHE_DIR / "bootloader" / f"native-{sys.platform}-{key}"
    out = cache / "bootex"
    if out.exists():
        return out

    cc = find_executable("cc") or find_executable("gcc") or find_executable("clang")
    if not cc:
        raise RuntimeError("需要 C 编译器 cc/gcc/clang（用于编译 POSIX 引导器）")
    cache.mkdir(parents=True, exist_ok=True)
    tmp_out = cache / f"bootex.tmp-{os.getpid()}"
    proc = subprocess.run(
        [cc, *flags, "-o", str(tmp_out), *[str(s) for s in sources]],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        shutil.rmtree(cache, ignore_errors=True)
        raise RuntimeError(f"引导器编译失败:\n{proc.stderr}")
    os.replace(tmp_out, out)  # 原子落位，并发构建安全
    if proc.stderr.strip() and logger:
        logger.warning("引导器编译警告:\n%s", proc.stderr.strip())
    return out


def ensure_bootloader_target(
    platform: str, arch: str, console: bool = True, logger=None
) -> Path:
    """用 zig cc 交叉编译引导器到任意目标平台/架构并缓存。

    zig 自带全部目标平台的 libc 头与链接桩（musl/mingw/macOS tbd），
    无需目标平台 SDK 或交叉 sysroot；Linux 目标用 musl 静态链接
    （单文件，无 glibc 版本依赖）。Windows 子系统经 -Wl,--subsystem
    选择（zig cc 不识别 mingw 的 -mconsole/-mwindows 驱动参数）。"""
    target = _ZIG_TARGETS.get((platform, arch))
    if not target:
        raise ValueError(f"zig cc 不支持的目标平台/架构: {platform}/{arch}")
    bdir = _resolve_bootloader_dir()
    sources = [bdir / s for s in _BOOT_SOURCES]
    for s in sources:
        if not s.exists():
            raise RuntimeError(f"引导器源码缺失: {s}")
    if platform == "windows":
        subsystem = "-Wl,--subsystem,console" if console else "-Wl,--subsystem,windows"
        flags = ["-O2", "-s", "-Wall", subsystem] \
            + ([] if console else ["-DGUI_APP=1"]) + ["-lshell32"]
    else:
        flags = ["-O2", "-s"] + ([] if console else ["-DGUI_APP=1"])
    zig = find_zig()
    if not zig:
        raise RuntimeError(
            f"交叉编译 {platform}/{arch} 引导器需要 zig cc。\n"
            "macOS: brew install zig | Ubuntu: snap install zig --classic\n"
            "或设环境变量 PYTHUB_ZIG 指向 zig 可执行文件。"
        )
    key = hashlib.md5(
        b"".join(s.read_bytes() for s in sources)
        + repr(flags).encode() + target.encode()
    ).hexdigest()[:12]
    out_name = "bootex.exe" if platform == "windows" else "bootex"
    cache = PYTHUB_CACHE_DIR / "bootloader" / f"zig-{platform}-{arch}-{key}"
    out = cache / out_name
    if out.exists():
        return out

    cache.mkdir(parents=True, exist_ok=True)
    tmp_out = cache / f"{out_name}.tmp-{os.getpid()}"
    proc = subprocess.run(
        [zig, "cc", "-target", target, *flags, "-o", str(tmp_out),
         *[str(s) for s in sources]],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        shutil.rmtree(cache, ignore_errors=True)  # 失败清理缓存目录（防半成品残留）
        raise RuntimeError(
            f"zig cc 交叉编译引导器失败 ({platform}/{arch}):\n{proc.stderr}"
        )
    os.replace(tmp_out, out)  # 原子落位，并发构建安全
    if proc.stderr.strip() and logger:
        logger.warning("引导器编译警告:\n%s", proc.stderr.strip())
    return out


def make_native_exe(
    bundle_dir: Path,
    out: Path,
    platform: str = "linux",
    arch: Optional[str] = None,
    python: str = "3.12",
    logger=None,
    console: bool = True,
    app_name: str = "",
    mem_boot: bytes = b"",
    mem_payload: bytes = b"",
) -> Path:
    """单文件原生可执行（macOS Mach-O / Linux ELF），与 Windows exe 同款自研
    归档格式（PythubEX/PYTHUBEX + cookie + 双层完整性校验 + 原子解压 + 并发互斥）。

    - 本机平台+架构: 用本机 cc 编译（快速路径）；
    - 跨平台/跨架构: 用 zig cc 交叉编译（需安装 zig，见 ensure_bootloader_target）；
    - POSIX 解释器不支持 Windows 的 _pth 机制，stdlib 不 zip 化，条目保持
      散文件（与 .run 一致）；
    - app_name 写入 cookie，运行时缓存按应用分组并 LRU 清理旧版本；
    - mem_boot/mem_payload 非空时为 mem 内存执行模式（载荷不落盘，见
      _write_archive）。
    """
    host_platform = native_host_platform()
    host_arch = native_host_arch()
    cross = host_platform != platform or (arch and arch != host_arch)
    if cross:
        boot = ensure_bootloader_target(platform, arch or host_arch, console, logger)
    else:
        boot = ensure_bootloader_native(console, logger)
    if logger:
        logger.info("打包为原生单文件 (%s/%s, %s 子系统)",
                    platform, arch or host_arch,
                    "GUI" if not console else "console")
    return _write_archive(boot.read_bytes(), bundle_dir, out, python,
                          zipify=False, app_name=app_name,
                          mem_boot=mem_boot, mem_payload=mem_payload)


def parse_archive(path: Path) -> dict:
    """解析自研归档（自校验用）。"""
    data = path.read_bytes()
    if len(data) < COOKIE_SIZE:
        raise ValueError("文件过小")
    cookie = data[-COOKIE_SIZE:]
    if cookie[:8] != MAGIC:
        raise ValueError("cookie 校验失败，不是 Pythub 自研归档")
    payload_offset, toc_offset, toc_size = struct.unpack_from("<QQQ", cookie, 8)
    md5 = cookie[32:64].rstrip(b"\0").decode("ascii")
    app = cookie[64:96].rstrip(b"\0").decode("ascii")
    body = data[payload_offset:toc_offset + toc_size]
    if hashlib.md5(body).hexdigest() != md5:
        raise ValueError("归档内容 MD5 校验失败")
    count = struct.unpack_from("<I", data, toc_offset)[0]
    pos = toc_offset + 4
    entries: List[Tuple[str, int, int, int, int]] = []
    for _ in range(count):
        plen, stored, raw, off, flags = struct.unpack_from(_TOC_ITEM_FMT, data, pos)
        pos += struct.calcsize(_TOC_ITEM_FMT)
        name = data[pos:pos + plen].decode("utf-8")
        pos += plen
        _check_path(name)
        entries.append((name, off, stored, raw, flags))
    return {
        "bootloader_size": payload_offset,
        "entry_count": count,
        "md5": md5,
        "app": app,
        "entries": entries,
    }
