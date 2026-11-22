"""Authenticode 交叉签名（需求 14）: 基于 osslsigncode 给 Windows .exe 附加
数字签名；未提供证书时可用 openssl 自动生成自签名证书。

说明:
- 自签名证书可减少部分杀软误报，但不会获得系统信任；正式分发请使用受信
  代码签名证书（或配合 -t 时间戳服务）。
- 仅标准库实现；osslsigncode / openssl 为可选外部依赖，仅在启用签名时需要。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from pythub.utils import PYTHUB_CACHE_DIR, find_executable

CERT_DIR = PYTHUB_CACHE_DIR / "certs"


def find_sign_tool() -> str:
    """定位 osslsigncode，缺失时给出安装指引。"""
    tool = find_executable("osslsigncode")
    if not tool:
        raise RuntimeError(
            "Authenticode 签名需要 osslsigncode。\n"
            "macOS: brew install osslsigncode | Ubuntu: apt install osslsigncode"
        )
    return tool


def ensure_selfsigned_cert(
    common_name: str = "Pythub Self-Signed",
    logger=None,
) -> Tuple[Path, Path]:
    """生成（或复用）自签名代码签名证书，缓存在 cache/pythub/certs/。"""
    cert = CERT_DIR / "selfsigned.crt"
    key = CERT_DIR / "selfsigned.key"
    if cert.exists() and key.exists():
        return cert, key

    openssl = find_executable("openssl")
    if not openssl:
        raise RuntimeError("未找到 openssl（自动生成自签名证书需要）")
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_key = key.with_name(key.name + ".tmp")
    tmp_crt = cert.with_name(cert.name + ".tmp")
    proc = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(tmp_key), "-out", str(tmp_crt),
         "-days", "3650",
         "-subj", f"/CN={common_name}/O=CROGRAM"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"自签名证书生成失败:\n{proc.stderr}")
    os.replace(tmp_key, key)
    os.replace(tmp_crt, cert)
    try:
        key.chmod(0o600)
    except OSError:
        pass
    if logger:
        logger.info("已生成自签名证书: %s（正式分发请使用受信代码签名证书）", cert)
    return cert, key


def sign_pe(
    target: Path,
    cert: Optional[Path] = None,
    key: Optional[Path] = None,
    timestamp_url: Optional[str] = None,
    logger=None,
) -> Path:
    """给 PE（.exe）附加 Authenticode 签名，成功后原子替换原文件。

    cert/key 缺省时自动生成（复用）自签名证书；timestamp_url 提供 RFC3161
    时间戳服务地址（可选）。"""
    if bool(cert) != bool(key):
        raise ValueError("sign_cert 与 sign_key 必须成对提供（或不提供以自动自签）")
    tool = find_sign_tool()
    if cert is None:
        cert, key = ensure_selfsigned_cert(logger=logger)

    out = target.with_name(f"{target.name}.signed.tmp")
    cmd = [tool, "sign", "-certs", str(cert), "-key", str(key)]
    if timestamp_url:
        cmd += ["-t", timestamp_url]
    # 注意: osslsigncode 2.x 对 --in/--out 双横线形式解析异常（误报
    # "Overwriting an existing file"），必须使用单横线 -in/-out
    cmd += ["-in", str(target), "-out", str(out)]
    if logger:
        logger.info("Authenticode 签名: %s%s", target,
                    f"（时间戳: {timestamp_url}）" if timestamp_url else "")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        Path(out).unlink(missing_ok=True)
        raise RuntimeError(
            f"osslsigncode 签名失败:\n{proc.stderr or proc.stdout}".strip()
        )
    os.replace(out, target)
    return target
