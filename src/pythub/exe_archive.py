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

# embed 模式（需求 64: 内嵌解释器）TOC 条目（FLAG_MEM，引导器解压时不落盘，
# 由 embed 分支主动写到缓存: libpython 动态库 + stdlib zip，两个文件）
EMBED_PYLIB_REL = "app.pylib"  # PBS 运行时的 libpython 共享库
EMBED_STDLZ_REL = "app.stdlz"  # PBS 运行时的 stdlib（zipimport 从 zip 直接导入）
EMBED_STDLZF_REL = "app.stdlzf"  # stdlib frozen 字节码（static 模式, 全内存）
EMBED_TCLTK_REL = "app.tcltk"  # Tcl/Tk 数据目录 zip（应用引用 tkinter 时携带）
APP_WDLLS_REL = "app.wdlls"  # Windows static 第三方依赖 DLL zip（openssl/sqlite3）

# Windows static 跳过的扩展模块: 其第三方依赖（tcl86t/tk86t、libffi-8）的
# 导入库未随 PBS full 变体提供，无法静态链接（依赖 DLL 亦缺导入符号来源）
_WIN_STATIC_SKIP_EXTS = {"_tkinter", "_ctypes"}

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

# static 模式专用: PBS Linux 运行时按 glibc 构建，libpython3.x.a 引用
# glibc 特有符号（pwritev64/sendfile64/truncate64/posix_fadvise64 等 LFS
# 别名与 __isinff 等），musl 无这些符号——跨平台链接必须用 gnu 目标
# （zig 自带 glibc 桩，产物运行需目标机 glibc ≥ 构建所用版本）
_ZIG_GNU_TARGETS = {
    ("linux", "x86_64"): "x86_64-linux-gnu",
    ("linux", "aarch64"): "aarch64-linux-gnu",
    ("macos", "x86_64"): "x86_64-macos",
    ("macos", "aarch64"): "aarch64-macos",
    ("windows", "x86_64"): "x86_64-windows-gnu",
    ("windows", "aarch64"): "aarch64-windows-gnu",
}


def _default_compiler() -> str:
    """默认编译器设置（需求 37，工具管理页可改），auto/zig/mingw/cc。"""
    from pythub.tools import default_compiler

    return default_compiler()


def find_zig() -> Optional[str]:
    """查找 zig 工具链（多层回退）:

    1. PYTHUB_ZIG 环境变量（可以是完整路径，也可以是可执行文件名）;
    2. 当前进程 PATH（shutil.which）;
    3. 常见安装位置兜底 —— macOS GUI 从 Dock/Finder 启动时进程 PATH 只有
       /usr/bin:/bin:/usr/sbin:/sbin，不含 Homebrew 前缀；tarball 手动解压、
       版本管理器安装的 zig 也常不在 PATH 中；
    4. pythub 托管安装目录（工具管理页「安装 zig」的落位处）优先于 2/3。
    """
    env = os.environ.get("PYTHUB_ZIG")
    if env:
        found = shutil.which(env)  # 给的是命令名（如 "zig"）
        if not found and Path(env).is_file() and os.access(env, os.X_OK):
            found = env  # 给的是完整路径
        if found:
            return found

    # pythub 托管安装（工具管理页下载安装的 zig）
    managed = PYTHUB_CACHE_DIR / "tools" / "zig" / (
        "zig.exe" if sys.platform == "win32" else "zig"
    )
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)

    found = shutil.which("zig")
    if found:
        return found

    home = Path.home()
    candidates = [
        home / "zig" / "zig",            # 官方 tarball 默认解压位置
        home / ".zig" / "zig",           # zig 版本管理器（zvm 等）
        home / ".zigup" / "zig",         # zigup
        home / ".local" / "bin" / "zig",
        Path("/opt/homebrew/bin/zig"),   # Apple Silicon Homebrew
        Path("/usr/local/bin/zig"),      # Intel Homebrew / 手动安装
        Path("/opt/local/bin/zig"),      # MacPorts
        Path("/usr/local/zig/zig"),
        Path("/snap/bin/zig"),           # Linux snap
    ]
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


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

    # 默认编译器设置（需求 37）: 设为 zig 时优先用 zig cc 交叉编译
    zig = find_zig()
    prefer_zig = _default_compiler() == "zig"
    cc = find_executable(_CROSS_GCC[arch])
    if (prefer_zig and zig) or not cc:
        if zig:
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


# embed 模式 stdlib zip 内排除的目录（"用到哪个取哪个"：运行应用用不到的
# 教学示例/测试套件/打包工具一律不进产物；tkinter 保留——实验性支持 GUI 产物）
_EMBED_STDLIB_EXCLUDE = frozenset((
    "__pycache__", "site-packages", "test", "idlelib", "turtledemo",
    "pydoc_data", "lib2to3", "ensurepip", "__phello__",
))


def embed_pbs_files(runtime: Path, platform: str, pyver: str,
                    logger=None, include_tk: bool = False
                    ) -> Tuple[bytes, str, bytes, str, bytes]:
    """从 python-build-standalone 运行时取构件，准备 embed 内嵌模式（需求 64）。

    返回 (libpython 字节, 缓存落盘文件名, stdlib.zip 字节, pyver,
    tcltk.zip 字节)。tcltk 仅在 include_tk=True（应用引用 tkinter）且
    运行时含 tcl8.6/tk8.6 数据目录时非空——PBS 构建的 _tkinter 静态内嵌
    于 libpython，但 Tcl 数据查找路径指向其构建沙箱，必须随产物携带数据
    目录并在运行时显式设置 TCL_LIBRARY/TK_LIBRARY。

    embed 模式 = "Python 进内存"的 OS 层最佳近似:
    - 解释器以**共享库**形式内嵌进引导器归档（libpython.so/.dylib/.dll，
      取自 PBS 运行时），运行时由引导器进程 dlopen 加载——不再需要解压
      独立 python 可执行目录；
    - stdlib（含 lib-dynload 扩展模块）打包为单个 zip，运行时经 PYTHONPATH
      交给 zipimport **直接从 zip 导入**（不解压成目录树）；
    - 落盘从 mem 模式的整个 runtime 目录树（~55MB）降为 2 个文件（~15MB）。
    应用源码依旧字节码内存执行（同 mem）。libpython/stdlib/编译用解释器
    三者全部取自同一份 PBS 运行时，版本天然一致（pyver 即目标 Python 版本）。
    """
    pyver_nodot = pyver.replace(".", "")
    if platform == "windows":
        name = "libpython.dll"
        cands = [runtime / f"python{pyver_nodot}.dll", runtime / "python.dll",
                 runtime / "DLLs" / f"python{pyver_nodot}.dll"]
    elif platform == "macos":
        name = "libpython.dylib"
        cands = [runtime / "lib" / f"libpython{pyver}.dylib",
                 runtime / "lib" / "libpython.dylib"]
    else:
        name = "libpython.so"
        cands = [runtime / "lib" / f"libpython{pyver}.so.1.0",
                 runtime / "lib" / f"libpython{pyver}.so",
                 runtime / "lib" / "libpython.so.1.0"]
    pylib_path = next((c for c in cands if c.exists()), None)
    if not pylib_path:
        raise RuntimeError(
            "embed 内嵌模式需要 python-build-standalone 运行时的 libpython "
            f"共享库，在 {runtime} 下未找到（候选: "
            f"{', '.join(str(c) for c in cands)}）。")
    pylib = pylib_path.read_bytes()

    # stdlib 目录定位（unix: lib/python3.X；windows: Lib），缺失时自动发现
    if platform == "windows":
        stdlib_dir = runtime / "Lib"
    else:
        stdlib_dir = runtime / "lib" / f"python{pyver}"
        if not stdlib_dir.is_dir():
            stdlib_dir = next(
                (d for d in (runtime / "lib").glob("python3.*") if d.is_dir()),
                None,
            )
    if not stdlib_dir or not Path(stdlib_dir).is_dir():
        raise RuntimeError(f"运行时标准库目录缺失: {runtime}")
    stdlib_dir = Path(stdlib_dir)

    # stdlib 打包为 zip（按需取用: 排除 site-packages/test/idlelib 等运行
    # 应用用不到的内容；lib-dynload 扩展模块保留在 zip 内，zipimport 支持
    # 从 zip 加载）
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        n = 0
        for root, dirs, files in os.walk(stdlib_dir):
            dirs[:] = [d for d in dirs
                       if d not in _EMBED_STDLIB_EXCLUDE
                       and not d.startswith("config-")]
            for fn in sorted(files):
                p = Path(root) / fn
                if p.is_symlink() or p.suffix in (".pyc", ".pyo", ".a"):
                    continue
                arc = p.relative_to(stdlib_dir).as_posix()
                zf.write(p, arc)
                n += 1
    # Tcl/Tk 数据目录 zip（arcname 相对 runtime/lib，运行时释放到缓存根即为
    # cache/tcl8.6、cache/tk8.6、cache/tcl8）: 仅应用引用 tkinter 时携带
    # （"用到哪个取哪个"），否则不增加产物体积
    tcltk = b""
    if include_tk and platform != "windows":
        tkbuf = io.BytesIO()
        tkcnt = 0
        with zipfile.ZipFile(tkbuf, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=9) as zf:
            for d in ("tcl8.6", "tk8.6", "tcl8"):
                base = runtime / "lib" / d
                if not base.is_dir():
                    continue
                for root, dirs, files in os.walk(base):
                    dirs[:] = [x for x in dirs if x != "__pycache__"]
                    for fn in sorted(files):
                        p = Path(root) / fn
                        if p.is_symlink() or p.suffix in (".pyc", ".a"):
                            continue
                        zf.write(p, p.relative_to(runtime / "lib").as_posix())
                        tkcnt += 1
        if tkcnt:
            tcltk = tkbuf.getvalue()
            if logger:
                logger.info("  embed GUI 构件: tcl/tk 数据 %d 项 (%.1f MB)",
                            tkcnt, len(tcltk) / 1048576)

    if logger:
        logger.info("  embed 构件（PBS %s/%s）: %s (%.1f MB) + stdlib.zip "
                    "%d 项 (%.1f MB)",
                    platform, pyver, pylib_path.name, len(pylib) / 1048576,
                    n, len(buf.getvalue()) / 1048576)
    return pylib, name, buf.getvalue(), pyver, tcltk


def _write_archive(
    boot_bytes: bytes,
    bundle_dir: Path,
    out: Path,
    python: str,
    zipify: bool = True,
    app_name: str = "",
    mem_boot: bytes = b"",
    mem_payload: bytes = b"",
    embed_pylib: bytes = b"",
    embed_stdlz: bytes = b"",
    embed_stdlzf: bytes = b"",
    embed_tcltk: bytes = b"",
    embed_wdlls: bytes = b"",
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
    for rel, data in ((MEM_BOOT_REL, mem_boot), (MEM_PAYLOAD_REL, mem_payload),
                      (EMBED_PYLIB_REL, embed_pylib),
                      (EMBED_STDLZ_REL, embed_stdlz),
                      (EMBED_STDLZF_REL, embed_stdlzf),
                      (APP_WDLLS_REL, embed_wdlls)):
        if not data:
            continue
        off = payload_offset + len(blob)
        comp = zlib.compress(data, 9)
        stored = comp if len(comp) < len(data) else data
        flags = FLAG_MEM | (FLAG_DEFLATE if stored is comp else 0)
        blob += stored
        toc_items.append((rel, len(stored), len(data), off, flags))

    # Tcl/Tk 数据 zip（embed 模式 GUI, 应用引用 tkinter 时携带）: 常规条目
    # （非 FLAG_MEM）——引导器解压落盘到缓存目录，runner 从中释放出 tcl8.6/
    # tk8.6 真实文件目录并设置 TCL_LIBRARY/TK_LIBRARY（Tcl 需要真实文件路径）
    if embed_tcltk:
        off = payload_offset + len(blob)
        comp = zlib.compress(embed_tcltk, 9)
        stored = comp if len(comp) < len(embed_tcltk) else embed_tcltk
        flags = FLAG_DEFLATE if stored is comp else 0
        blob += stored
        toc_items.append((EMBED_TCLTK_REL, len(stored), len(embed_tcltk),
                          off, flags))

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

    # 默认编译器设置（需求 37）: 设为 zig 时本机编译也用 zig cc
    cmd: List[str]
    if _default_compiler() == "zig" and find_zig():
        cmd = [find_zig(), "cc"]  # type: ignore[list-item]
    else:
        cc = find_executable("cc") or find_executable("gcc") or find_executable("clang")
        if not cc:
            raise RuntimeError("需要 C 编译器 cc/gcc/clang（用于编译 POSIX 引导器）")
        cmd = [cc]
    cache.mkdir(parents=True, exist_ok=True)
    tmp_out = cache / f"bootex.tmp-{os.getpid()}"
    proc = subprocess.run(
        [*cmd, *flags, "-o", str(tmp_out), *[str(s) for s in sources]],
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
            "可在 GUI「工具管理」页一键安装 zig（pythub 自动托管、构建时自动识别）。\n"
            "或手动安装: macOS: brew install zig | Ubuntu: snap install zig --classic\n"
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


def _find_msvc_tools() -> Tuple[str, str]:
    """定位 MSVC 工具链（cl.exe / link.exe）。

    Windows static 解释器构件（PBS build/core objs）为 /GL 编译，仅官方
    MSVC 链接器可消费。查找顺序:
    1. 当前 PATH（VS 开发者命令行/已配置环境）;
    2. vswhere 定位最新 VS 安装（需 VC 工具集组件）。
    """
    cl = shutil.which("cl")
    link = shutil.which("link")
    if cl and link:
        return cl, link
    pf = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = (Path(pf) / "Microsoft Visual Studio" / "Installer"
               / "vswhere.exe")
    if vswhere.exists():
        proc = subprocess.run(
            [str(vswhere), "-latest", "-products", "*", "-requires",
             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            capture_output=True, text=True)
        vspath = (proc.stdout or "").strip().splitlines()
        if vspath:
            msvc = (Path(vspath[0]) / "VC" / "Tools" / "MSVC")
            if msvc.is_dir():
                vers = sorted(x for x in msvc.iterdir() if x.is_dir())
                if vers:
                    bin64 = vers[-1] / "bin" / "Hostx64" / "x64"
                    cl = str(bin64 / "cl.exe")
                    link = str(bin64 / "link.exe")
                    if Path(cl).exists() and Path(link).exists():
                        return cl, link
    raise RuntimeError(
        "Windows static 模式需要 MSVC Build Tools（cl.exe/link.exe）: PBS "
        "Windows 解释器构件为 /GL 编译，仅官方 MSVC 链接器可消费。请在安装了 "
        "C++ 工具集的 VS 环境（如 x64 Native Tools Command Prompt，或已配置 "
        "INCLUDE/LIB 的终端）中运行打包，或改用 mem/磁盘模式。")


def _gen_win_extinit(extmods: List[str]) -> Path:
    """生成 Windows static 扩展模块注册源码 pythub_extinit.c。

    PBS Windows 扩展模块（_ssl/_socket/pyexpat 等）以独立 .obj 静态链接，
    不在核心 config.obj 的 PyImport_Inittab 内——在 Py_Initialize 前逐个
    PyImport_AppendInittab 注册（仅允许 pre-init 调用）。"""
    buf = ["/* pythub 自动生成: Windows static 扩展模块注册表（勿手改） */",
           "#include <Python.h>", ""]
    for m in extmods:
        buf.append(f"extern PyObject *PyInit_{m}(void);")
    buf += ["", "void pythub_register_extensions(void)", "{",
            "    static const struct { const char *name;",
            "                          PyObject *(*init)(void); } tbl[] = {"]
    for m in extmods:
        buf.append(f'        {{"{m}", PyInit_{m}}},')
    buf += ["        {NULL, NULL},", "    };",
            "    for (int i = 0; tbl[i].name; i++)",
            "        PyImport_AppendInittab(tbl[i].name, tbl[i].init);", "}"]
    out_dir = PYTHUB_CACHE_DIR / "bootloader" / "src"
    out_dir.mkdir(parents=True, exist_ok=True)
    content = "\n".join(buf) + "\n"
    p = out_dir / f"pythub_extinit-{hashlib.md5(content.encode()).hexdigest()[:12]}.c"
    p.write_text(content)
    return p


def static_pbs_files(
    full_runtime: Path,
    platform: str,
    pyver: str,
    logger=None,
    include_tk: bool = False,
    compile_py: Optional[Path] = None,
) -> Tuple[List[Path], bytes, bytes, List[str], List[Path]]:
    """从 python-build-standalone **完整发行变体**（pgo-full, tar.zst）取
    static 内存模式（需求 64 第四档）构件。

    返回 (链接输入列表, stdlib frozen 字节码 blob, tcltk.zip 字节,
    扩展模块名列表, 需随归档携带的第三方 DLL 列表)。

    - POSIX: libpython3.x.a 静态库（PBS 构建把 zlib/ssl/sqlite3/tkinter 等
      119 个扩展模块全部静态编入），构建期直接链接进引导器二进制——运行期
      无 dlopen、无独立 libpython 文件（PyOxidizer 同款思路）；第三方依赖
      静态库在 build/lib/（crypto/ssl/expat/ffi/lzma/bz2/mpdec/sqlite3/
      tcl/tk/uuid）。
    - Windows: build/core/*.obj（core objs 为 /MT 静态 CRT、静态引用 Py
      符号）+ build/extensions/*/*.obj（扩展源码自带, 如 bz2/mpdecimal/
      expat），由 ensure_static_bootloader 直接链接——MSVC python312.lib
      只是 python312.dll 的导入库，不可用；openssl/sqlite3 为 DLL 导入，
      对应 DLL 随归档 app.wdlls 携带（_tkinter/_ctypes 缺 tcl/libffi 导入
      库暂不支持）。扩展 init 由 pythub_extinit.c 注册 PyImport_Inittab。
    - stdlib: 全部 .py 由**目标同版本解释器**编译为 marshal 字节码并打包
      成 frozen blob（引导器在 Py_Initialize 前注册 PyImport_FrozenModules，
      encodings 等从内存加载——零文件系统依赖；marshal 字节码与版本相关、
      与平台无关，可由任意同版本解释器生成）。
    - tcl/tk 数据: 应用引用 tkinter 时携带（Tcl 需要真实文件，唯一落盘项；
      Windows static 暂不支持 tkinter，忽略）。
    """
    install = full_runtime / "install"
    build = full_runtime / "build"
    if platform == "windows":
        # Windows: build/core objs 是唯一可静态链接的解释器构件
        core_dir = build / "core"
        if not core_dir.is_dir() or not list(core_dir.glob("*.obj")):
            raise RuntimeError(
                "full 变体运行时缺少 build/core/*.obj（Windows static 需要"
                "非 shared 的 pgo-full 变体）")
        extmods: List[str] = []
        wdlls: List[Path] = []
        libs: List[Path] = []
        dllmap = {"libcrypto": "libcrypto-3-x64.dll",
                  "libssl": "libssl-3-x64.dll",
                  "sqlite3": "sqlite3.dll"}
        for d in sorted((build / "extensions").iterdir()):
            if not d.is_dir() or d.name in _WIN_STATIC_SKIP_EXTS:
                continue
            extmods.append(d.name)
        for stem, dll in dllmap.items():
            p = build / "lib" / f"{stem}.lib"
            if p.exists() and (install / "DLLs" / dll).exists():
                wdlls.append(install / "DLLs" / dll)
        frozen_mods_log = f"扩展模块 {len(extmods)} 个"
    else:
        # POSIX: libpython{ver}.a 静态库
        # 位置: config-{ver}-{plat}/libpython{ver}.a 或 lib/libpython{ver}.a
        matches = sorted(install.glob(
            f"lib/python{pyver}/config-{pyver}-*/libpython{pyver}.a"))
        lib_a: Optional[Path] = matches[0] if matches else None
        if not lib_a:
            lib_a = install / "lib" / f"libpython{pyver}.a"
        if not lib_a.exists():
            raise RuntimeError(
                f"full 变体运行时缺少静态库 {lib_a.name}（需要 pgo-full 变体，"
                "当前缓存可能是 install_only 变体）")
        if not (build / "lib").is_dir():
            raise RuntimeError("full 变体运行时缺少 build/lib 第三方静态库目录")
        libs = [lib_a]
        extmods = []
        wdlls = []
        frozen_mods_log = lib_a.name

    # stdlib frozen blob: 用目标同版本解释器编译（marshal 字节码跨平台兼容）
    if compile_py is None:
        raise ValueError("static 模式需要 compile_py（同版本解释器路径）")
    # Windows PBS 布局 stdlib 在 install/Lib（大写），POSIX 在 lib/python{ver}
    if platform == "windows":
        stdlib_dir = install / "Lib"
    else:
        stdlib_dir = install / "lib" / f"python{pyver}"
    if not stdlib_dir.is_dir():
        raise RuntimeError(f"full 变体运行时缺少标准库: {stdlib_dir}")
    tmp_blob = PYTHUB_CACHE_DIR / "tmp"
    tmp_blob.mkdir(parents=True, exist_ok=True)
    blob_path = tmp_blob / f"stdlzf-{pyver}-{os.getpid()}.bin"
    # 排除: 与 mem/embed 裁剪规则一致（"用到哪个取哪个"）
    script = r'''
import marshal, os, struct, sys
base = sys.argv[1]
out = sys.argv[2]
EXC_DIRS = {"test", "idlelib", "lib2to3", "pydoc_data", "turtledemo",
            "ensurepip", "site-packages", "__pycache__", "lib-dynload",
            "config-3.12-darwin", "config", "venv"}
mods = []
for root, dirs, files in os.walk(base):
    dirs[:] = [d for d in dirs if d not in EXC_DIRS
               and not d.startswith("config-") and d != "__pycache__"]
    relroot = os.path.relpath(root, base)
    pkg = [] if relroot == "." else relroot.split(os.sep)
    for f in sorted(files):
        if not f.endswith(".py") or f.endswith("py.typed"):
            continue
        if f in ("__init__.py",):
            name = ".".join(pkg)
        else:
            name = ".".join(pkg + [f[:-3]])
        if not name or name in ("__main__", "antigravity", "this",
                                "__phello__"):
            continue
        p = os.path.join(root, f)
        try:
            code = compile(open(p, "rb").read(), p, "exec")
        except Exception:
            continue
        # is_package 仅对 __init__.py 为真——普通模块误标会令 FrozenImporter
        # 把 re._compiler 等文件当作包（去查其 __init__.py）导致导入崩溃
        mods.append((name, marshal.dumps(code), f == "__init__.py"))
blob = struct.pack("<I", len(mods))
for name, data, ispkg in mods:
    nb = name.encode()
    blob += struct.pack("<H", len(nb)) + nb
    blob += struct.pack("<B", 1 if ispkg else 0)
    blob += struct.pack("<I", len(data)) + data
open(out, "wb").write(blob)
print("frozen modules:", len(mods))
'''
    proc = subprocess.run(
        [str(compile_py), "-c", script, str(stdlib_dir), str(blob_path)],
        capture_output=True, text=True)
    if proc.returncode != 0 or not blob_path.exists():
        raise RuntimeError(
            f"stdlib frozen 字节码编译失败（{compile_py.name}）:\n{proc.stderr}")
    frozen_blob = blob_path.read_bytes()
    blob_path.unlink(missing_ok=True)

    # Tcl/Tk 数据目录 zip（同 embed: tcl8.6/tk8.6/tcl8, arcname 相对 lib）
    tcltk = b""
    if include_tk and platform != "windows":
        import io
        import zipfile
        tkbuf = io.BytesIO()
        tkcnt = 0
        with zipfile.ZipFile(tkbuf, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=9) as zf:
            for d in ("tcl8.6", "tk8.6", "tcl8"):
                base_d = install / "lib" / d
                if not base_d.is_dir():
                    continue
                for root, dirs, files in os.walk(base_d):
                    dirs[:] = [x for x in dirs if x != "__pycache__"]
                    for fn in sorted(files):
                        p = Path(root) / fn
                        if p.is_symlink() or p.suffix in (".pyc", ".a"):
                            continue
                        zf.write(p, p.relative_to(install / "lib").as_posix())
                        tkcnt += 1
        if tkcnt:
            tcltk = tkbuf.getvalue()
    if logger:
        msg = (f"  static 构件（PBS full {platform}/{pyver}）: "
               f"{frozen_mods_log} + frozen stdlib "
               f"{len(frozen_blob) / 1048576:.1f} MB")
        if tcltk:
            msg += f" + tcl/tk {len(tcltk) / 1048576:.1f} MB"
        if wdlls:
            msg += f" + 依赖 DLL {len(wdlls)} 个"
        logger.info(msg)
    return libs, frozen_blob, tcltk, extmods, wdlls


def ensure_static_bootloader(
    platform: str,
    arch: str,
    pyver: str,
    full_runtime: Path,
    console: bool = True,
    logger=None,
) -> Path:
    """编译 static 内存模式引导器: bootex.c（PYTHUB_STATIC 分支）与目标
    平台 PBS libpython3.x.a 及第三方静态库链接成单二进制——解释器直接
    成为引导器的一部分（PyOxidizer 同款思路），运行期零 dlopen。

    本机平台用系统 cc；跨平台用 zig cc（链接目标平台原生目标文件）。
    结果按源码+静态库内容哈希缓存。

    Windows 特殊: 没有可用的 libpython3.x.a（python312.lib 只是 DLL 导入库），
    解释器构件是 PBS build/core/*.obj——**全部为 /GL（MSVC 全程序优化）**
    编译，其 IR 为微软专有格式，仅官方 link.exe 可消费（LLVM/lld 多年
    未支持且微软明确不开源）。因此:
    - Windows 本机打包（MSVC Build Tools 的 cl.exe/link.exe 可用时）:
      cl /MT 编译 bootex.c + 生成的 pythub_extinit.c，link.exe 链接全部
      objs + 第三方 .lib——Windows static 的唯一可行路径；
    - Windows 交叉编译: 不可行，直接报错并引导用 mem/磁盘模式。
    扩展 init 表由 pythub_extinit.c 注册（PyImport_AppendInittab）。"""
    bdir = _resolve_bootloader_dir()
    sources = [bdir / s for s in _BOOT_SOURCES]
    for s in sources:
        if not s.exists():
            raise RuntimeError(f"引导器源码缺失: {s}")
    install = full_runtime / "install"
    build = full_runtime / "build"

    win_mode = platform == "windows"
    win_objs: List[Path] = []
    win_libs: List[Path] = []
    extinit_src: Optional[Path] = None
    if win_mode:
        host_platform = native_host_platform()
        if host_platform != "windows":
            raise RuntimeError(
                "Windows static 模式不支持交叉编译: PBS Windows 解释器核心"
                "构件（build/core/*.obj）全部为 /GL（MSVC 全程序优化）编译，"
                "其 IR 为微软专有格式，仅官方 link.exe 可消费（lld/zig 无法"
                "读取）。请在 Windows 上打包 Windows 目标，或改用 mem/磁盘"
                "模式（均支持交叉编译）。")
        core_objs = sorted((build / "core").glob("*.obj"))
        if not core_objs:
            raise RuntimeError(
                "full 变体运行时缺少 build/core/*.obj（Windows static 需要"
                "非 shared 的 pgo-full 变体）")
        ext_objs: List[Path] = []
        extmods: List[str] = []
        for d in sorted((build / "extensions").iterdir()):
            if not d.is_dir() or d.name in _WIN_STATIC_SKIP_EXTS:
                continue
            extmods.append(d.name)
            ext_objs += sorted(d.glob("*.obj"))
        # 第三方链接库: 导入库（crypto/ssl/sqlite3, DLL 随 app.wdlls 携带）
        # + 静态库（liblzma）
        for name in ("libcrypto", "libssl", "sqlite3", "liblzma"):
            p = build / "lib" / f"{name}.lib"
            if p.exists():
                win_libs.append(p)
        # 扩展 init 注册源码（打包期生成, 内容纳入缓存 key）
        extinit_src = _gen_win_extinit(extmods)
        win_objs = core_objs + ext_objs
    else:
        # libpython 静态库
        matches = sorted(install.glob(
            f"lib/python{pyver}/config-{pyver}-*/libpython{pyver}.a"))
        lib_a = matches[0] if matches else install / "lib" / f"libpython{pyver}.a"
        if not lib_a.exists():
            raise RuntimeError(f"缺少 libpython{pyver}.a（需 pgo-full 变体）")

    # POSIX: 第三方静态库（存在即链接——PBS 构建自带; 缺失的模块会在链接期
    # 报符号错误，此处按平台经验列表 + 通配兜底）。顺序即链接顺序（ld 单遍
    # 解析，被依赖的库须排在依赖者之后）: tk→tcl/X11，X11→xcb→Xau。
    third: List[Path] = []
    hacl: List[Path] = []
    if not win_mode:
        blib = build / "lib"
        for name in ("libcrypto", "libssl", "libexpat", "libffi", "liblzma",
                     "libbz2", "libmpdec", "libsqlite3", "libuuid",
                     "libtcl8.6", "libtk8.6", "libreadline", "libedit",
                     "libncursesw", "libncurses", "libpanelw", "libpanel",
                     "libmenuw", "libformw", "libz", "libdb",
                     "libX11", "libxcb", "libXau", "libffi-8"):
            for cand in sorted(blib.glob(f"{name}.a")):
                third.append(cand)
                break
        # Hacl 对象（个别 PBS 构建遗漏打包进 .a，补链接）
        hacl = sorted((build / "Modules" / "_hacl").glob("*.o"))

    host_platform = native_host_platform()
    host_arch = native_host_arch()
    cross = platform != host_platform or (arch and arch != host_arch)
    cmd: List[str]
    cl = link_exe = None
    if win_mode:
        cl, link_exe = _find_msvc_tools()
    elif cross:
        zig = find_zig()
        if not zig:
            raise RuntimeError(
                f"跨平台 static 引导器 ({platform}/{arch}) 需要 zig cc")
        gnu = _ZIG_GNU_TARGETS.get((platform, arch or host_arch), "")
        if not gnu:
            raise ValueError(f"zig cc 不支持的目标平台/架构: {platform}/{arch}")
        cmd = [zig, "cc", "-target", gnu]
    elif _default_compiler() == "zig" and find_zig():
        cmd = [find_zig(), "cc"]  # type: ignore[list-item]
    else:
        cc = (find_executable("cc") or find_executable("gcc")
              or find_executable("clang"))
        if not cc:
            raise RuntimeError("需要 C 编译器 cc/gcc/clang")
        cmd = [cc]

    if win_mode:
        # MSVC: /MT 静态 CRT（与 PBS objs 一致）; MS_NO_COREDLL 使 pyconfig.h
        # 走静态 API 声明（非 DLL 导入）
        flags = ["/nologo", "/O2", "/MT", "/DNDEBUG", "/DPYTHUB_STATIC=1",
                 "/DMS_NO_COREDLL=1", f"/I{install / 'include'}"]
    else:
        flags = ["-O2", "-s", "-DPYTHUB_STATIC=1",
                 f"-I{install / 'include' / f'python{pyver}'}"]
    # flags 纳入 key: 链接标志变化（如 -ObjC）必须触发重编译
    flags_sig = repr(flags).encode()
    key_material = [
        b"".join(s.read_bytes() for s in sources),
        repr(str(full_runtime) + pyver).encode(),
        flags_sig,
    ]
    if win_mode:
        # objs/导入库内容（前 1MB）+ 扩展 init 源码纳入 key
        key_material.append(b"".join(
            p.read_bytes()[:1 << 20] for p in win_objs + win_libs))
        key_material.append(extinit_src.read_bytes())
        sources = [*sources, extinit_src]
    else:
        key_material.append(
            b"".join(p.read_bytes()[:1 << 20] for p in third))
    key = hashlib.md5(b"".join(key_material)).hexdigest()[:12]
    out_name = "bootex.exe" if platform == "windows" else "bootex"
    cache = (PYTHUB_CACHE_DIR / "bootloader"
             / f"static-{platform}-{arch}-{pyver}-{key}")
    out = cache / out_name
    if out.exists():
        return out

    if win_mode:
        # 对象文件全部参与链接（.obj 无序性无影响）；导入库须排在其引用者
        # （_ssl.obj 等）之后。CRT/kernel32 等由 MSVC 默认库解析
        link = [*win_objs, *win_libs]
    else:
        link = [lib_a, *third, *hacl]
    if platform == "macos":
        # -ObjC: libtk8.6.a 的 TKApplication 类以 ObjC category 扩展
        # （tkProcessEvent: 等），不加载会被运行时 unrecognized selector 崩溃
        flags += ["-ObjC", "-lncurses", "-lpanel", "-lreadline", "-lz",
                  "-framework", "CoreFoundation",
                  "-framework", "ApplicationServices",
                  "-framework", "Cocoa",
                  "-framework", "Carbon",
                  "-framework", "IOKit",
                  "-framework", "QuartzCore",
                  "-framework", "SystemConfiguration"]
    elif platform == "linux":
        flags += ["-ldl", "-lm", "-lpthread", "-lutil"]

    cache.mkdir(parents=True, exist_ok=True)
    tmp_out = cache / f"{out_name}.tmp-{os.getpid()}"
    if win_mode:
        # MSVC 两步: cl 编译 bootex.c + pythub_extinit.c（/MT 与 PBS objs
        # 同 CRT），link.exe 链接全部 objs + 第三方 .lib（/GL objs 仅
        # link.exe 可消费）
        objs: List[Path] = []
        for src in sources:
            obj = cache / (src.stem + ".obj")
            proc = subprocess.run(
                [cl, *flags, "/c", str(src), f"/Fo{obj}"],
                capture_output=True, text=True)
            if proc.returncode != 0:
                shutil.rmtree(cache, ignore_errors=True)
                raise RuntimeError(
                    f"static 引导器编译失败 ({platform}/{arch}):\n"
                    f"{proc.stderr[-2000:]}")
            objs.append(obj)
        sub = "CONSOLE" if console else "WINDOWS"
        proc = subprocess.run(
            [link_exe, "/nologo", f"/SUBSYSTEM:{sub}", "/MACHINE:X64",
             f"/OUT:{tmp_out}", *[str(o) for o in objs],
             *[str(p) for p in link]],
            capture_output=True, text=True)
        if proc.returncode != 0:
            shutil.rmtree(cache, ignore_errors=True)
            raise RuntimeError(
                f"static 引导器链接失败 ({platform}/{arch}):\n"
                f"{proc.stderr[-2000:]}")
    else:
        proc = subprocess.run(
            [*cmd, *flags, "-o", str(tmp_out), *[str(s) for s in sources],
             *[str(p) for p in link]],
            capture_output=True, text=True)
        if proc.returncode != 0:
            shutil.rmtree(cache, ignore_errors=True)
            raise RuntimeError(
                f"static 引导器编译失败 ({platform}/{arch}):\n{proc.stderr[-2000:]}")
    os.replace(tmp_out, out)
    if proc.stderr.strip() and logger:
        logger.warning("static 引导器编译警告:\n%s", proc.stderr.strip())
    return out


def make_static_exe(
    out: Path,
    boot: Path,
    platform: str,
    arch: Optional[str],
    python: str,
    logger=None,
    console: bool = True,
    app_name: str = "",
    mem_boot: bytes = b"",
    mem_payload: bytes = b"",
    embed_stdlzf: bytes = b"",
    embed_tcltk: bytes = b"",
    embed_wdlls: bytes = b"",
) -> Path:
    """static 内存模式单文件可执行（需求 64 第四档）:

    解释器静态链接进入口二进制（boot 引导器），stdlib 以 frozen 字节码
    条目（app.stdlzf, FLAG_MEM）随归档携带，运行期全内存执行——除 GUI
    应用的 tcl/tk 数据目录外零落盘。无常规条目（runtime 目录树）。
    Windows 额外携带 app.wdlls（openssl/sqlite3 依赖 DLL zip）。"""
    import tempfile as _tempfile

    # static 归档无常规条目: 用空 bundle 目录 + mem/embed 条目拼装
    with _tempfile.TemporaryDirectory(prefix="pythub-static-") as td:
        return _write_archive(boot.read_bytes(), Path(td), out, python,
                              zipify=False, app_name=app_name,
                              mem_boot=mem_boot, mem_payload=mem_payload,
                              embed_stdlzf=embed_stdlzf,
                              embed_tcltk=embed_tcltk,
                              embed_wdlls=embed_wdlls)


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
    embed_pylib: bytes = b"",
    embed_stdlz: bytes = b"",
    embed_tcltk: bytes = b"",
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
                          mem_boot=mem_boot, mem_payload=mem_payload,
                          embed_pylib=embed_pylib, embed_stdlz=embed_stdlz,
                          embed_tcltk=embed_tcltk)


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
