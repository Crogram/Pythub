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

# 旧版静态映射（与 RELEASE_TAG=20241016 对应，保留以保证已有缓存目录兼容）；
# 3.13 起经上游 latest release 动态解析（见 resolve_runtime）
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

# static 模式 full 变体三元组覆盖: Windows 需要**非 shared**变体
# （x86_64-pc-windows-msvc, python.exe 静态链接核心）——其 build/core/*.obj
# 为 /MT 静态 CRT 且静态引用 Py 符号，可链接为无 python312.dll 的单文件
# 解释器；shared 变体的 objs 走 python312.dll 导入（__imp_Py*），不能用于
# 静态链接。其余平台 full 变体本身即静态库形态，无需覆盖。
TRIPLETS_FULL = {
    ("windows", "x86_64"): "x86_64-pc-windows-msvc",
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
    if pyver not in SUPPORTED_PYTHON:
        raise ValueError(f"不支持的 Python 版本: {pyver}（可用: {', '.join(SUPPORTED_PYTHON)}）")
    if pyver in PYTHON_PATCH_VERSIONS:
        return PYTHON_PATCH_VERSIONS[pyver]
    return resolve_runtime(pyver)[1]


# ---------------------------------------------------------------- 上游发布索引动态解析

# 上游 latest release 索引缓存（tag + 各 minor 最新补丁号），落盘后 24h 内复用；
# 网络不可用时回退到过期缓存，两者皆无才报错
_INDEX_CACHE = PYTHUB_CACHE_DIR / "runtime-index.json"
_INDEX_TTL = 24 * 3600


def _fetch_release_index() -> dict:
    """解析上游发布索引（各 minor 最新补丁号 + release tag）。

    GitHub API 优先，npmmirror 镜像兜底（大陆网络 GitHub API 常不可达）。
    返回 {"fetched": ts, "tag": ..., "patches": {"3.13": "3.13.x", ...}}。"""
    import json
    import time

    last_err: Optional[Exception] = None
    for fetcher in (_fetch_release_index_github, _fetch_release_index_npm):
        try:
            data = fetcher()
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        _INDEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _INDEX_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(_INDEX_CACHE)
        return data
    raise RuntimeError(
        "无法获取 python-build-standalone 发布索引\n"
        f"  GitHub API: {last_err}\n"
        "请检查网络后重试。") from last_err


GITHUB_RELEASE_BASE = (
    "https://github.com/astral-sh/python-build-standalone/releases/download"
)
# npmmirror（淘宝镜像）全量同步 python-build-standalone releases，
# URL 规律: {base}/{tag}/{文件名}，大陆网络下载速度快
NPM_MIRROR_BASE = "https://registry.npmmirror.com/-/binary/python-build-standalone"


def runtime_sources() -> list:
    """运行时下载源 base URL 列表（按顺序尝试）。

    显式配置（仅这一个源）: 环境变量 PYTHUB_PYTHON_MIRROR 或 settings.json
    的 runtime.mirror，值为目录形式的 base URL（如 npmmirror 镜像地址）。
    未配置时: GitHub 官方优先、npmmirror 镜像兜底。"""
    from pythub.utils import load_settings

    explicit = os.environ.get("PYTHUB_PYTHON_MIRROR") \
        or load_settings().get("runtime", {}).get("mirror")
    if explicit:
        return [str(explicit).rstrip("/")]
    return [GITHUB_RELEASE_BASE, NPM_MIRROR_BASE]


def _http_get_text(url: str, timeout: int = 30) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "pythub/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_get_json(url: str, timeout: int = 30):
    import json

    return json.loads(_http_get_text(url, timeout=timeout))


def _parse_cpython_full(name: str) -> Optional[Tuple[str, str]]:
    """从产物文件名 cpython-3.13.18+... 解析 (minor 版本, 完整补丁版本)。"""
    import re

    m = re.match(r"^cpython-(\d+)\.(\d+)\.(\d+)\+", name)
    if not m:
        return None
    return (
        f"{m.group(1)}.{m.group(2)}",
        f"{m.group(1)}.{m.group(2)}.{m.group(3)}",
    )


def _merge_patch(patches: dict, key: str, full: str) -> None:
    cur = patches.get(key)
    if cur is None or tuple(full.split(".")) > tuple(cur.split(".")):
        patches[key] = full


def _fetch_release_index_github() -> dict:
    import time

    release = _http_get_json(
        "https://api.github.com/repos/astral-sh/"
        "python-build-standalone/releases/latest")
    patches: dict = {}
    for asset in release.get("assets") or []:
        parsed = _parse_cpython_full(str(asset.get("name", "")))
        if parsed:
            _merge_patch(patches, *parsed)
    return {"fetched": time.time(),
            "tag": release.get("tag_name") or "", "patches": patches}


def _fetch_release_index_npm() -> dict:
    """npmmirror 镜像兜底: 列 tag 目录取最新，再列文件解析补丁号。"""
    import time

    listing = _http_get_json(f"{NPM_MIRROR_BASE}/")
    tags = [
        e for e in listing
        if str(e.get("name", "")).rstrip("/").isdigit()
    ]
    if not tags:
        raise RuntimeError("镜像 tag 目录为空")
    tag = max(tags, key=lambda e: str(e.get("date", "")))["name"].rstrip("/")
    patches: dict = {}
    for entry in _http_get_json(f"{NPM_MIRROR_BASE}/{tag}/"):
        parsed = _parse_cpython_full(str(entry.get("name", "")))
        if parsed:
            _merge_patch(patches, *parsed)
    return {"fetched": time.time(), "tag": tag, "patches": patches}


def _release_index(logger: Optional[logging.Logger] = None) -> dict:
    import json
    import time

    cached: Optional[dict] = None
    try:
        cached = json.loads(_INDEX_CACHE.read_text(encoding="utf-8"))
    except Exception:
        pass
    if cached and cached.get("fetched", 0) + _INDEX_TTL > time.time():
        return cached
    try:
        return _fetch_release_index()
    except Exception as e:  # noqa: BLE001
        if cached:
            if logger:
                logger.warning("发布索引刷新失败，回退本地缓存: %s", e)
            return cached
        raise RuntimeError(
            "无法获取 python-build-standalone 发布索引"
            "（网络不可用或 GitHub API 限流）: "
            f"{e}\n请检查网络后重试。") from e


def resolve_runtime(pyver: str) -> Tuple[str, str]:
    """解析 (release_tag, 完整补丁版本号)。

    3.10~3.12 走静态表（旧 release，保持既有缓存兼容）；其余版本经上游
    latest release 动态解析。"""
    if pyver in PYTHON_PATCH_VERSIONS:
        return RELEASE_TAG, PYTHON_PATCH_VERSIONS[pyver]
    idx = _release_index()
    full = idx.get("patches", {}).get(pyver)
    if not full:
        avail = ", ".join(
            sorted(idx.get("patches", {}),
                   key=lambda v: tuple(int(x) for x in v.split("."))))
        raise ValueError(
            f"上游 python-build-standalone（{idx.get('tag', '?')}）未提供 "
            f"Python {pyver} 的构建。可用版本: {avail or '未知'}")
    return idx["tag"], full


def runtime_dir(platform: str, arch: str, pyver: str, kind: str = "install_only") -> Path:
    tag, full = resolve_runtime(pyver)
    suffix = "-full" if kind == "full" else ""
    return PYTHUB_CACHE_DIR / "runtimes" / (
        f"cpython-{full}-{tag}-{platform}-{arch}{suffix}")


def fetch_runtime(
    platform: str,
    arch: str,
    pyver: str,
    logger: Optional[logging.Logger] = None,
    kind: str = "install_only",
) -> Path:
    """获取（必要时下载）目标平台嵌入式解释器目录。

    kind="install_only"（默认）: install_only_stripped/install_only 变体
    （tar.gz，npmmirror 镜像可用），目录布局为标准运行时树。

    kind="full": pgo-full 完整发行变体（tar.zst，仅 GitHub 提供），额外
    携带构建期构件——libpython3.x.a 静态库、include/ 头文件、第三方依赖
    静态库（crypto/ssl/expat/ffi/tcl/tk 等，位于 python/build/lib/），
    供 static 内存模式（需求 64）静态链接解释器使用。缓存布局:
    install/（标准运行时树）+ build/（构建期构件）。

    缓存保存**全量内容**（不预裁剪）；打包阶段由 builder 对 staging 副本
    按产物类型按需裁剪（prune_runtime），缓存本体始终完整（embed/static
    等模式需要头文件之外的完整构件）。

    并发安全: 先在暂存目录完成下载/解压并写入 .pythub-ok 标记（含 full），
    再原子 rename 到最终位置；若另一进程抢先完成（缓存已就位），直接
    复用其结果，避免半成品目录被并发进程误删/误用。"""
    triplet = TRIPLETS.get((platform, arch))
    if not triplet:
        raise ValueError(f"不支持的 目标平台/架构: {platform}/{arch}")
    if kind == "full":
        triplet = TRIPLETS_FULL.get((platform, arch), triplet)
    tag, full = resolve_runtime(pyver)
    # Windows full 用非 shared 变体（独立缓存目录, 避免与 shared 变体冲突）
    suffix = ("-full-ns" if (kind == "full" and platform == "windows")
              else "-full" if kind == "full" else "")
    cache = PYTHUB_CACHE_DIR / "runtimes" / (
        f"cpython-{full}-{tag}-{platform}-{arch}{suffix}")

    if (cache / ".pythub-ok").exists():
        marker = (cache / ".pythub-ok").read_text(encoding="utf-8")
        if "full" in marker:
            return cache
        # 旧版"瘦身"缓存已删减内容（新版打包需要全量构件），失效后重新下载
        if logger:
            logger.info("旧版瘦身运行时缓存已过时，重新下载完整运行时...")
        shutil.rmtree(cache, ignore_errors=True)

    if kind == "full":
        # 完整发行变体: pgo-full 为纯目标文件静态库（pgo+lto-full 的 .a 是
        # LLVM bitcode，旧版系统链接器无法解析）；上游仅 GitHub 提供，
        # 镜像源通常 404 会快速跳过
        variants = [
            f"cpython-{full}+{tag}-{triplet}-pgo-full.tar.zst",
        ]
    else:
        # 下载: stripped 变体优先（无调试符号，体积显著更小），缺失回退 install_only
        variants = [
            f"cpython-{full}+{tag}-{triplet}-install_only_stripped.tar.gz",
            f"cpython-{full}+{tag}-{triplet}-install_only.tar.gz",
        ]

    import tempfile

    with tempfile.TemporaryDirectory(prefix="pythub-dl-") as td:
        tmp = Path(td)
        if logger:
            logger.info("下载嵌入式 Python %s (%s/%s) ...", pyver, platform, arch)
        progress = ProgressLogger(logger, f"Python {pyver}") if logger else None
        archive: Optional[Path] = None
        last_err: Optional[Exception] = None
        for base in runtime_sources():
            # 正确路径形态: {源 base}/{release tag}/{文件名}
            src_base = f"{base}/{tag}"
            hit = False
            for name in variants:
                dest = tmp / name
                try:
                    # 上游若提供同名 .sha256 则下载后校验（astral 近期 release
                    # 已不再附带，获取失败静默跳过，不影响下载）
                    expected: Optional[str] = None
                    try:
                        expected = _http_get_text(f"{src_base}/{name}.sha256") \
                            .split()[0].strip().lower()
                    except Exception:  # noqa: BLE001
                        pass
                    download_file(
                        f"{src_base}/{name}",
                        dest,
                        logger=logger,
                        progress=progress,
                        expected_sha256=expected,
                    )
                    archive, hit = dest, True
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if logger:
                        logger.warning("下载源不可用（%s）: %s (%s)", base, name, e)
            if hit:
                break
        if archive is None:
            raise RuntimeError(
                f"运行时下载失败，已尝试源: {', '.join(runtime_sources())}\n"
                f"最后错误: {last_err}") from last_err

        if logger:
            logger.info("解压运行时...")
        extract_runtime(archive, tmp / "extract", logger=logger)
        root = tmp / "extract" / "python"
        if not root.exists():
            raise RuntimeError(f"压缩包结构异常，缺少 python/ 目录: {archive.name}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        # 暂存目录（同父目录保证 rename 原子性），瘦身与标记先在暂存完成
        staging = cache.parent / f".{cache.name}.tmp-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if kind == "full":
            # full 布局: python/{install,build,...}——整体保留（static 模式
            # 需要 install/ 运行时树 + build/lib/ 第三方静态库 + include/ 头文件）
            pass
        shutil.move(str(root), str(staging))
        # 缓存保存全量内容（不预裁剪）；裁剪延迟到打包阶段对 staging 副本按需进行
        (staging / ".pythub-ok").write_text(
            f"{full}\n{triplet}\nfull\n", encoding="utf-8")

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


# ---------------------------------------------------------------- 打包期按需裁剪

def extract_runtime(archive: Path, dest: Path, logger=None) -> None:
    """完整解压运行时 tar 包——不做任何成员过滤，缓存保存全量内容；
    裁剪延迟到打包阶段由 builder 对 staging 副本按产物类型按需进行。

    个别上游归档（如 PBS 部分构建的 share/terminfo）在文件成员之前携带
    指向自身/绝对路径的符号链接目录（或硬链接指向链接环成员），常规
    extractall 会因链接环触发 ELOOP；解压时逐成员容错: 删除构成环的符号
    链接组件后重试，仍失败（环上别名硬链接）则跳过该成员并告警——
    终态为真实文件目录，不影响解释器运行。"""
    import errno as _errno
    import subprocess
    import tarfile

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    actual = archive
    if str(archive).endswith(".tar.zst"):
        # zstd 压缩（PBS full 变体）: 标准库 tarfile 不支持，经系统 zstd
        # 预解压为 tar 后按常规流程处理
        import shutil as _sh

        zstd = _sh.which("zstd")
        if not zstd:
            raise RuntimeError(
                "解压 .tar.zst 需要 zstd 工具（brew install zstd / apt install zstd）")
        tar_tmp = archive.with_suffix(".tar")
        with open(tar_tmp, "wb") as out:
            r = subprocess.run([zstd, "-d", "-c", str(archive)], stdout=out,
                               stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError(f"zstd 解压失败: {r.stderr.decode(errors='replace')[:300]}")
        actual = tar_tmp

    skipped = []
    with tarfile.open(actual) as tf:
        for m in tf:
            try:
                tf.extract(m, dest)
                continue
            except OSError as e:
                if e.errno != _errno.ELOOP:
                    raise
            # ELOOP: 该成员路径上存在构成环的符号链接组件，删除后重试
            probe = dest / m.name
            while probe != dest and probe != probe.parent:
                probe = probe.parent
                if probe.is_symlink():
                    probe.unlink()
                    break
            try:
                tf.extract(m, dest)
                continue
            except OSError:
                skipped.append(m.name)  # 环上别名/硬链接: 跳过（正主文件已单独解出）
    if skipped and logger:
        logger.warning(
            "运行时归档含符号链接环，跳过 %d 个别名成员（不影响运行）: %s",
            len(skipped), ", ".join(skipped[:5]) + ("..." if len(skipped) > 5 else ""))


# 运行时根目录下可整体移除的目录（头文件/文档/静态链接库等构建期内容）
_PRUNE_ROOT_DIRS = ("include", "share", "libs", "pypy", "lib/pkgconfig")

# 标准库内可安全移除的目录（运行应用用不到）
_PRUNE_STDLIB_DIRS = (
    "test", "idlelib", "lib2to3", "pydoc_data", "turtledemo", "ensurepip",
    "__phello__", "__pycache__",
    "site-packages/pip", "site-packages/setuptools", "site-packages/pkg_resources",
    "site-packages/wheel", "site-packages/_distutils_hack",
    "site-packages/setuptools-*.dist-info", "site-packages/pip-*.dist-info",
)

# bin 内无需保留的工具脚本（保留 python 本体）
_BIN_PRUNE_PREFIXES = ("idle", "pydoc", "2to3", "pip", "wheel", "easy_install")

# bin 内无需保留的文件后缀（python3-config/python3.X-config: 构建扩展时才用）
_BIN_PRUNE_SUFFIXES = ("-config",)

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
    """就地裁剪运行时副本（打包期使用）：移除调试符号、测试套件、pip/setuptools、
    文档、__pycache__，并 strip 二进制符号表（unix）。典型可减小 50%~80% 体积。

    注意: 缓存本体保存全量内容（fetch_runtime 不裁剪），本函数作用于打包
    时复制到 staging/bundle 的副本，按产物类型按需取用、不用的不进产物。
    返回移除内容类别列表。"""
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
        # 构建配置目录（Makefile/libpython.a 等，编译扩展时才用）
        for d in stdlib.glob("config-*"):
            if rm_dir(d):
                n += 1
    if n:
        removed_kinds.append("test/idlelib/pip等stdlib冗余包")

    # 3. bin 工具脚本（保留 python 本体）
    n = 0
    for bindir in (runtime / "bin", runtime / "Scripts"):
        if bindir.is_dir():
            for f in bindir.iterdir():
                if f.name.startswith(_BIN_PRUNE_PREFIXES) or \
                        (f.name.endswith(_BIN_PRUNE_SUFFIXES)
                         and f.stem.startswith("python")):
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
