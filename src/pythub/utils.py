"""通用工具：日志、下载、压缩、文件操作。仅使用标准库实现。"""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, List, Optional

PYTHUB_CACHE_DIR = Path(
    os.environ.get("PYTHUB_CACHE", Path.home() / ".cache" / "pythub")
)

CHUNK_SIZE = 256 * 1024

# 持久化设置与日志落盘（需求 17/18）
SETTINGS_FILE = PYTHUB_CACHE_DIR / "settings.json"
LOG_DIR = PYTHUB_CACHE_DIR / "logs"
_file_handler: Optional[logging.FileHandler] = None


def load_settings() -> dict:
    """读取持久化设置（cache/pythub/settings.json），缺失/损坏返回空 dict。"""
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    """原子写入设置（临时文件 + rename），避免并发/中断产生损坏的 JSON。"""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_name(f"{SETTINGS_FILE.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, SETTINGS_FILE)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def log_file_path() -> Path:
    """当天日志文件：cache/pythub/logs/pythub-YYYYMMDD.log（按天分文件）。"""
    return LOG_DIR / f"pythub-{datetime.date.today():%Y%m%d}.log"


def set_file_logging(enabled: bool, name: str = "pythub") -> Optional[Path]:
    """开关"日志落盘"：给 logger 挂/摘 FileHandler，返回当前日志文件路径。

    开关状态持久化到 cache/pythub/settings.json，下次启动 get_logger
    会自动恢复；幂等，可重复调用。
    """
    global _file_handler
    logger = logging.getLogger(name)
    if enabled:
        if _file_handler is None:
            path = log_file_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            _file_handler = logging.FileHandler(path, encoding="utf-8")
            _file_handler.setFormatter(
                logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
            )
            logger.addHandler(_file_handler)
        settings = load_settings()
        settings["log_to_file"] = True
        save_settings(settings)
        return log_file_path()
    if _file_handler is not None:
        logger.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
    settings = load_settings()
    settings["log_to_file"] = False
    save_settings(settings)
    return None


# ---------------- 本地构建缓存维护（需求 14: 缓存清理） ----------------
# 构建机缓存顶层子目录（运行时用户缓存 exe/、run/ 由引导器自管理，不在此列）
BUILD_CACHE_SUBDIRS = ("runtimes", "pyzlib", "bootloader")

DEFAULT_CACHE_MAX_BYTES = 4 << 30  # 4GB


def dir_size(path: Path) -> int:
    """目录递归占用字节数（不跟随符号链接）。"""
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for f in files:
            fp = Path(root) / f
            if fp.is_symlink():
                continue
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total


def cache_stats() -> dict:
    """构建机缓存按顶层子目录（runtimes/pyzlib/bootloader）的占用统计。"""
    stats = {}
    for sub in BUILD_CACHE_SUBDIRS:
        d = PYTHUB_CACHE_DIR / sub
        if d.exists():
            stats[sub] = dir_size(d)
    return stats


def cache_max_bytes() -> int:
    """缓存大小上限（bytes）: settings.json 的 cache.max_mb（单位 MB）。

    未配置/非法值时回退 DEFAULT_CACHE_MAX_BYTES（4096 MB = 4GB）。"""
    mb = load_settings().get("cache", {}).get("max_mb")
    try:
        mb = int(mb)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        mb = 0
    if mb <= 0:
        mb = DEFAULT_CACHE_MAX_BYTES >> 20
    return mb << 20


def set_cache_max_mb(mb: int, logger: Optional[logging.Logger] = None) -> None:
    """设置缓存大小上限（MB）并持久化到 settings.json 的 cache.max_mb。"""
    if mb <= 0:
        raise ValueError("缓存上限必须为正整数（MB）")
    settings = load_settings()
    settings.setdefault("cache", {})["max_mb"] = mb
    save_settings(settings)
    if logger:
        logger.info("缓存大小上限已设为 %d MB", mb)


def prune_cache(max_bytes: Optional[int] = None) -> List[Path]:
    """把构建机缓存裁剪到大小上限（LRU: 按缓存条目 mtime 从旧到新删除）。

    条目 = runtimes/pyzlib/bootloader 下的各版本/md5 缓存目录；返回被删除
    的条目路径列表。不触碰运行时用户缓存（exe/、run/，由引导器自行清理）
    与 settings/logs。max_bytes 省略时读取 settings.json 的 cache.max_mb
    （未配置则为默认 4GB）。"""
    if max_bytes is None:
        max_bytes = cache_max_bytes()
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须为正数（防呆: 0 会清空全部缓存）")
    entries = []  # (mtime, path, size)
    total = 0
    for sub in BUILD_CACHE_SUBDIRS:
        d = PYTHUB_CACHE_DIR / sub
        if not d.exists():
            continue
        for child in d.iterdir():
            try:
                mtime = child.stat().st_mtime
                size = dir_size(child)
            except OSError:
                continue
            entries.append((mtime, child, size))
            total += size
    pruned: List[Path] = []
    for mtime, child, size in sorted(entries, key=lambda e: e[0]):
        if total <= max_bytes:
            break
        shutil.rmtree(child, ignore_errors=True)
        pruned.append(child)
        total -= size
    return pruned


def get_logger(name: str = "pythub") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("[%(levelname).1s] %(message)s")
        )
        logger.addHandler(handler)
    # 无论是否已有 handler（如 GUI 已挂载队列 handler），都确保 INFO 级别生效
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    # 日志落盘（需求 18）：按持久化设置自动恢复（幂等，已挂载则跳过）
    if _file_handler is None and load_settings().get("log_to_file"):
        set_file_logging(True, name)
    return logger


class ProgressLogger:
    """把进度回调转为日志输出（0~1）。"""

    def __init__(self, logger: logging.Logger, label: str):
        self.logger = logger
        self.label = label
        self._last = -1

    def __call__(self, ratio: float) -> None:
        pct = int(ratio * 100)
        if pct != self._last and pct % 5 == 0:
            self._last = pct
            self.logger.info("下载 %s: %d%%", self.label, pct)


def download_file(
    url: str,
    dest: Path,
    logger: Optional[logging.Logger] = None,
    progress: Optional[Callable[[float], None]] = None,
    expected_sha256: Optional[str] = None,
) -> Path:
    """下载文件到指定路径。

    - 断点续传: 利用 .part 已有字节数发 HTTP Range 请求，从断点继续；
      服务端不支持（返回非 206）时整段重下
    - 最多尝试 3 次；HTTP 404/410 不重试（资源不存在，重试无意义）
    - expected_sha256 给出时下载完成后校验，不匹配则删除产物并报错
    """
    import hashlib
    import urllib.error

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        offset = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": "pythub/0.1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers),
                    timeout=60) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                resumed = bool(offset) and code == 206
                total = int(resp.headers.get("Content-Length") or 0)
                if resumed:
                    total += offset
                done = offset if resumed else 0
                with open(tmp, "ab" if resumed else "wb") as f:
                    while True:
                        chunk = resp.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress and total:
                            progress(done / total)
            if expected_sha256:
                h = hashlib.sha256()
                with open(tmp, "rb") as f:
                    for block in iter(lambda: f.read(1 << 20), b""):
                        h.update(block)
                actual = h.hexdigest()
                if actual != expected_sha256:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"下载文件 sha256 校验失败\n"
                        f"  URL: {url}\n  期望: {expected_sha256}\n  实际: {actual}")
            tmp.replace(dest)
            return dest
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"资源不存在: {url} ({e.code})") from e
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
        if logger:
            logger.warning(
                "下载中断（第 %d/3 次），%s: %s (%s)",
                attempt, "将断点续传" if tmp.exists() else "重新下载",
                url, last_err)
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"下载失败: {url}\n{last_err}")


def is_frozen_unix_executable(path: Path) -> bool:
    """pyz/app 内的启动器需要可执行权限。"""
    return path.suffix in {"", ".sh", ".pyz", ".AppImage", ".app"}


def set_executable(path: Path) -> None:
    try:
        path.chmod(0o755)
    except OSError:
        pass


def make_executable_script(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    set_executable(path)


def copy_tree(
    src: Path,
    dest: Path,
    exclude_dirs: Optional[Iterable[str]] = None,
    exclude_suffixes: Optional[Iterable[str]] = None,
) -> None:
    """复制目录树，忽略常见垃圾目录。"""
    exclude_dirs = set(exclude_dirs or [])
    exclude_suffixes = set(exclude_suffixes or [])
    dest.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        dirs[:] = [
            d
            for d in dirs
            if d not in exclude_dirs and d != "__pycache__"
        ]
        for d in dirs:
            (dest / rel_root / d).mkdir(parents=True, exist_ok=True)
        for fn in files:
            if any(fn.endswith(s) for s in exclude_suffixes):
                continue
            shutil.copy2(Path(root) / fn, dest / rel_root / fn)


def make_zip(zip_path: Path, files: List[tuple]) -> None:
    """files: [(arcname, Path_or_bytes, is_executable)]"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, source, executable in files:
            zi = zipfile.ZipInfo(str(arcname))
            zi.external_attr = ((0o755 if executable else 0o644) << 16)
            zi.compress_type = zipfile.ZIP_DEFLATED
            if isinstance(source, Path):
                zi.date_time = (1980, 1, 1, 0, 0, 0)
                with open(source, "rb") as f:
                    zf.writestr(zi, f.read())
            else:
                zi.date_time = (1980, 1, 1, 0, 0, 0)
                zf.writestr(zi, source)


def make_tar_gz(tar_path: Path, src_dir: Path) -> None:
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(src_dir, arcname=src_dir.name)


def safe_extract(archive: Path, dest: Path) -> None:
    """安全解压 zip/tar/tar.xz（防路径穿越）。"""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            _check_members(zf.namelist())
            zf.extractall(dest)
    elif archive.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as tf:
            _check_members(tf.getnames())
            tf.extractall(dest)
    elif archive.name.endswith((".tar.xz", ".txz")):
        # zig 等官方发布使用 tar.xz（stdlib lzma 解码）
        with tarfile.open(archive, "r:xz") as tf:
            _check_members(tf.getnames())
            tf.extractall(dest)
    elif archive.name.endswith(".tar.bz2"):
        with tarfile.open(archive, "r:bz2") as tf:
            _check_members(tf.getnames())
            tf.extractall(dest)
    else:
        raise ValueError(f"不支持的压缩格式: {archive}")


def _check_members(names: Iterable[str]) -> None:
    for name in names:
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"压缩包内包含不安全路径: {name}")


def zip_dir(zip_path: Path, src_dir: Path) -> None:
    files: List[tuple] = []
    for root, dirs, fnames in os.walk(src_dir):
        rel = Path(root).relative_to(src_dir)
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in fnames:
            p = Path(root) / fn
            arc = src_dir.name / rel / fn
            exe = os.access(p, os.X_OK) and not fn.endswith((".py", ".txt", ".json"))
            files.append((str(arc), p, exe))
    make_zip(zip_path, files)


def find_executable(name: str) -> Optional[str]:
    """在 PATH 中查找可执行文件。"""
    return shutil.which(name)


def temp_dir(prefix: str = "pythub-") -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix=prefix)
