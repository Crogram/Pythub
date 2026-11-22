"""PyPI 打包与发布：stdlib 实现 sdist/wheel 构建与 multipart 上传。"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import tarfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import List, Optional

from pythub.config import app_config, load_requirements

REPOSITORIES = {
    "pypi": "https://upload.pypi.org/legacy/",
    "testpypi": "https://test.pypi.org/legacy/",
}

_WHEEL_TAG = "py3-none-any"


def _normalize(name: str) -> str:
    return name.replace("-", "_").replace(".", "_").lower()


def build_package(project_dir: Path, out_dir: Path, logger: logging.Logger) -> List[Path]:
    """构建 sdist (.tar.gz) 与 wheel (.whl)。"""
    cfg = app_config(project_dir)
    name, version = cfg["name"], cfg["version"]
    nname = _normalize(name)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = _collect_project(project_dir)

    # ---- sdist ----
    sdist_name = f"{name}-{version}.tar.gz"
    sdist_path = out_dir / sdist_name
    with tarfile.open(sdist_path, "w:gz") as tf:
        pkg_info = _pkg_info(cfg)
        base = f"{name}-{version}"
        import io

        tf.addfile(
            tarfile.TarInfo(f"{base}/PKG-INFO"),
            io.BytesIO(pkg_info.encode("utf-8")),
        )
        for rel, abs_path in files:
            tf.add(abs_path, arcname=f"{base}/{rel.as_posix()}")
        reqs = load_requirements(project_dir)
        if reqs:
            import io

            content = "\n".join(reqs).encode("utf-8")
            ti = tarfile.TarInfo(f"{base}/requirements.txt")
            ti.size = len(content)
            tf.addfile(ti, io.BytesIO(content))

    # ---- wheel ----
    wheel_name = f"{nname}-{version}-{_WHEEL_TAG}.whl"
    wheel_path = out_dir / wheel_name
    records: List[tuple] = []
    payload: List[tuple] = []  # (arcname, bytes)
    for rel, abs_path in files:
        data = abs_path.read_bytes()
        arc = rel.as_posix()
        payload.append((arc, data))
        records.append((arc, data))
    dist_info = f"{nname}-{version}.dist-info"
    metadata = _pkg_info(cfg).encode("utf-8")
    wheel_meta = (
        "Wheel-Version: 1.0\nGenerator: pythub\nRoot-Is-Purelib: true\n"
        f"Tag: {_WHEEL_TAG}\n"
    ).encode("utf-8")
    payload.append((f"{dist_info}/METADATA", metadata))
    payload.append((f"{dist_info}/WHEEL", wheel_meta))
    payload.append((f"{dist_info}/top_level.txt", b""))
    # RECORD
    record_lines = []
    for arc, data in records:
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record_lines.append(f"{arc},sha256={digest},{len(data)}")
    record_lines.append(f"{dist_info}/METADATA,,")
    record_lines.append(f"{dist_info}/WHEEL,,")
    record_lines.append(f"{dist_info}/RECORD,,")
    payload.append((f"{dist_info}/RECORD", "\n".join(record_lines).encode("utf-8")))

    import zipfile

    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data in payload:
            zi = zipfile.ZipInfo(arc, date_time=(2024, 1, 1, 0, 0, 0))
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, data)

    logger.info("构建完成: %s / %s", sdist_path.name, wheel_path.name)
    return [sdist_path, wheel_path]


def _collect_project(project_dir: Path):
    """收集包文件：包目录（含 __init__.py）与顶层 py 文件。"""
    files = []
    exclude = {"__pycache__", ".git", "build", "dist", ".venv", "venv", ".pythub"}
    for root, dirs, fnames in os.walk(project_dir):
        rel_root = Path(root).relative_to(project_dir)
        dirs[:] = [d for d in dirs if d not in exclude and not d.endswith(".egg-info")]
        for fn in fnames:
            if fn.endswith((".pyc", ".pyo")) or fn == "pythub.toml":
                continue
            rel = rel_root / fn
            if rel_root == Path(".") and fn in ("requirements.txt",):
                continue
            files.append((rel, Path(root) / fn))
    return files


def _pkg_info(cfg: dict) -> str:
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {cfg['name']}",
        f"Version: {cfg['version']}",
        f"Summary: {cfg.get('description', '')}",
        f"Author: {cfg.get('author', '')}",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 凭据

def load_pypi_credentials(repository: str = "pypi") -> Optional[dict]:
    """读取系统已配置的 PyPI 凭据。

    优先级: 环境变量 (TWINE_USERNAME/TWINE_PASSWORD) > ~/.pypirc 对应仓库段。
    返回 {"username", "password", "source"}；未找到返回 None。
    """
    username = os.environ.get("TWINE_USERNAME")
    password = os.environ.get("TWINE_PASSWORD")
    if username and password:
        return {
            "username": username,
            "password": password,
            "source": "环境变量 TWINE_USERNAME/TWINE_PASSWORD",
        }
    pypirc = Path.home() / ".pypirc"
    if pypirc.is_file():
        import configparser

        cp = configparser.ConfigParser()
        cp.read(pypirc, encoding="utf-8")
        section = repository if cp.has_section(repository) else (
            "pypi" if cp.has_section("pypi") else None
        )
        if section:
            username = cp.get(section, "username", fallback=None)
            password = cp.get(section, "password", fallback=None)
            if username and password:
                return {
                    "username": username,
                    "password": password,
                    "source": f"~/.pypirc [{section}]",
                }
    return None


def save_pypi_credentials(username: str, password: str,
                          repository: str = "pypi") -> Path:
    """将凭据写入 ~/.pypirc（权限 600），保留已有配置段。"""
    import configparser

    pypirc = Path.home() / ".pypirc"
    cp = configparser.ConfigParser()
    if pypirc.is_file():
        cp.read(pypirc, encoding="utf-8")
    if not cp.has_section("distutils"):
        cp.add_section("distutils")
    servers = [s.strip() for s in
               cp.get("distutils", "index-servers", fallback="").split(",") if s.strip()]
    if repository not in servers:
        servers.append(repository)
        cp.set("distutils", "index-servers", ", ".join(servers))
    if not cp.has_section(repository):
        cp.add_section(repository)
    cp.set(repository, "username", username)
    cp.set(repository, "password", password)
    tmp = pypirc.with_suffix(".pypirc.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        cp.write(f)
    os.replace(tmp, pypirc)
    try:
        os.chmod(pypirc, 0o600)
    except OSError:
        pass
    return pypirc


# ---------------------------------------------------------------- 发布

def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def publish(
    files: List[Path],
    token: str = None,
    repository: str = "pypi",
    logger: logging.Logger = None,
    username: str = None,
    password: str = None,
    progress=None,
) -> bool:
    """上传发行文件到 PyPI（stdlib multipart 实现，逐文件上传）。

    认证二选一: token（API Token，等价 username=__token__）或 username+password。
    progress: 可选回调 progress(msg)，用于 GUI 状态栏实时反馈。
    """
    url = REPOSITORIES.get(repository)
    if not url:
        raise ValueError(f"未知仓库: {repository}（可选: {', '.join(REPOSITORIES)}）")
    if token:
        username, password = "__token__", token
    if not (username and password):
        raise ValueError("请提供 PyPI 凭据（API Token 或 用户名+密码）")

    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    user_disp = username if username != "__token__" else "__token__(API Token)"
    if logger:
        logger.info("开始发布 %d 个文件到 %s (%s)，认证用户: %s",
                    len(files), repository, url, user_disp)
    if progress:
        progress(f"开始上传 {len(files)} 个文件到 {repository}...")

    ok_count = 0
    for idx, f in enumerate(files, 1):
        size = f.stat().st_size
        if logger:
            logger.info("[%d/%d] 正在上传 %s (%s)...", idx, len(files), f.name, _fmt_size(size))
        if progress:
            progress(f"[{idx}/{len(files)}] 正在上传 {f.name} ({_fmt_size(size)})...")

        try:
            text = _upload_one(f, url, auth)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                detail = str(e)
            msg = f"[{idx}/{len(files)}] {f.name} 上传失败 HTTP {e.code}: {detail[:1000]}"
            if logger:
                logger.error("%s", msg)
            if progress:
                progress(msg[:80])
            raise RuntimeError(msg) from e

        ok_count += 1
        if logger:
            logger.info("[%d/%d] 上传成功 ✓ %s（服务器响应: %s）",
                        idx, len(files), f.name, text[:200])
        if progress:
            progress(f"[{idx}/{len(files)}] {f.name} 上传成功 ✓")

    summary = f"发布完成: {ok_count}/{len(files)} 个文件已上传到 {repository}"
    if logger:
        logger.info("%s", summary)
    if progress:
        progress(summary)
    return True


def _upload_one(f: Path, url: str, auth: str) -> str:
    """上传单个发行文件，返回服务器响应文本。"""
    boundary = uuid.uuid4().hex

    def field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    parts: List[bytes] = []
    parts.append(field(":action", "file_upload"))
    parts.append(field("protocol_version", "1"))

    data = f.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    ctype = "application/zip" if f.suffix == ".whl" else "application/gzip"
    parts.append(field("name", _dist_name_from(f)))
    parts.append(field("version", _dist_version_from(f)))
    parts.append(field("sha256_digest", digest))
    if f.suffix == ".whl":
        parts.append(field("filetype", "bdist_wheel"))
        parts.append(field("pyversion", "3"))
    else:
        parts.append(field("filetype", "sdist"))
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="content"; filename="{f.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Basic {auth}",
            "User-Agent": "pythub/0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8", "replace")


def _dist_name_from(path: Path) -> str:
    return path.name.rsplit("-", 2)[0] if path.suffix == ".whl" else path.name.rsplit("-", 1)[0]


def _dist_version_from(path: Path) -> str:
    if path.suffix == ".whl":
        return path.name.rsplit("-", 2)[1]
    return path.name.rsplit("-", 1)[1].rsplit(".tar", 1)[0]
