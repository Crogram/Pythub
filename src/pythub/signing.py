"""Authenticode 交叉签名（需求 14）: 基于 osslsigncode 给 Windows .exe 附加
数字签名；未提供证书时可用 openssl 自动生成自签名证书。

说明:
- 自签名证书可减少部分杀软误报，但不会获得系统信任；正式分发请使用受信
  代码签名证书（或配合 -t 时间戳服务）。
- 仅标准库实现；osslsigncode / openssl 为可选外部依赖，仅在启用签名时需要。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from pythub.utils import PYTHUB_CACHE_DIR, find_executable

CERT_DIR = PYTHUB_CACHE_DIR / "certs"

# Mach-O 魔数（32/64 位 + FAT 通用/64），用于识别 Contents/MacOS 下需签名的内嵌二进制
_MACHO_MAGICS = {
    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",   # MH_MAGIC / MH_MAGIC_64
    b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",   # 字节序相反变体
    b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf",   # FAT / FAT 64
}


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
    pfx: Optional[Path] = None,
    pfx_pass: str = "",
    logger=None,
) -> Path:
    """给 PE（.exe）附加 Authenticode 签名，成功后原子替换原文件。

    三种证书来源（优先级从高到低）:
    1. pfx: PKCS#12 单文件证书（.p12/.pfx，CA 颁发的正式代码签名证书常见格式），
       配合 pfx_pass 密码；
    2. cert+key 成对 PEM；
    3. 都缺省时自动生成（复用）自签名证书。
    timestamp_url 提供 RFC3161 时间戳服务地址（可选；建议正式证书都加上，
    否则证书过期后签名随之失效）。"""
    tool = find_sign_tool()
    out = target.with_name(f"{target.name}.signed.tmp")
    if pfx is not None:
        cmd = [tool, "sign", "-pkcs12", str(pfx)]
        if pfx_pass:
            cmd += ["-pass", pfx_pass]
    else:
        if bool(cert) != bool(key):
            raise ValueError("sign_cert 与 sign_key 必须成对提供（或不提供以自动自签）")
        if cert is None:
            cert, key = ensure_selfsigned_cert(logger=logger)
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


# ---------------------------------------------------------------- macOS codesign


def codesign_tool() -> str:
    """定位 codesign（仅 macOS 构建机可用），缺失返回空串。"""
    if find_executable("codesign") and sys.platform == "darwin":
        return find_executable("codesign")
    return ""


def list_codesign_identities() -> List[Tuple[str, str]]:
    """列出钥匙串中有效的代码签名身份（需求 58）。

    执行 `security find-identity -v -p codesign`，解析返回 [(hash, name)]；
    无 codesign（非 mac 构建机）或无有效身份返回空列表。
    """
    if not codesign_tool():
        return []
    proc = subprocess.run(
        ["security", "find-identity", "-v", "-p", "codesign"],
        capture_output=True, text=True,
    )
    out: List[Tuple[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        m = re.match(r'\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"(.+)"', line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def sign_macos_app(
    target: Path,
    identity: str = "",
    entitlements: Optional[str] = None,
    logger=None,
) -> Path:
    """codesign 对 macOS 产物签名（需求 58）：.app / app_wrap 壳。

    identity 为空时使用 ad-hoc（'-'）临时签名；.app 为目录产物，先对
    Contents/MacOS 下内嵌的 Mach-O（运行时二进制、引导器、依赖库）逐个签名，
    再签 .app 外壳，保证外层签名校验通过。identity 签名失败会抛异常终止构建。
    entitlements: 权限声明 plist 路径（可选，--entitlements）。
    """
    tool = codesign_tool()
    if not tool:
        raise RuntimeError(
            "macOS codesign 签名需要 macOS 构建机（未找到 codesign）。"
            "交叉平台无法完成 codesign 签名，请在 mac 上构建或关闭签名选项。"
        )
    ident = identity or "-"
    target = Path(target)
    extra = ["--entitlements", str(entitlements)] if entitlements else []

    def _run_sign(path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [tool, "--force", "--sign", ident] + extra + [str(path)],
            capture_output=True, text=True,
        )

    # 内嵌 Mach-O 先签（外壳签名会 seal 资源与内层代码）
    macos_dir = target / "Contents" / "MacOS"
    if target.is_dir() and macos_dir.is_dir():
        for f in sorted(macos_dir.rglob("*")):
            if not (f.is_file() and not f.is_symlink()):
                continue
            try:
                with open(f, "rb") as fh:
                    if fh.read(4) not in _MACHO_MAGICS:
                        continue
            except OSError:
                continue
            proc = _run_sign(f)
            if proc.returncode != 0 and logger:
                # 单个内嵌二进制失败不阻断（如资源不完整），外壳签名兜底校验
                logger.warning(
                    "  codesign 内嵌二进制签名警告: %s\n%s",
                    f.name, (proc.stderr or "").strip(),
                )

    proc = _run_sign(target)
    if proc.returncode != 0:
        raise RuntimeError(
            f"codesign 签名失败（identity={ident if ident != '-' else 'ad-hoc'}）:\n"
            f"{proc.stderr or proc.stdout}".strip()
        )
    if logger:
        logger.info("  已签名（codesign %s）: %s",
                    ident if ident != "-" else "ad-hoc", target)
    return target


# ------------------------------------------------------------ deb/rpm GPG 签名


def gpg_tool() -> str:
    """定位 gpg，缺失返回空串。"""
    return find_executable("gpg") or ""


def list_gpg_keys() -> List[Tuple[str, str]]:
    """列出 gpg 私钥（需求 62: deb/rpm 签名用）。

    解析 `gpg --list-secret-keys --with-colons`，返回 [(keyid, name)]；
    无 gpg 或无私钥返回空列表。
    """
    gpg = gpg_tool()
    if not gpg:
        return []
    proc = subprocess.run(
        [gpg, "--list-secret-keys", "--with-colons"],
        capture_output=True, text=True,
    )
    out: List[Tuple[str, str]] = []
    keyid = ""
    for line in (proc.stdout or "").splitlines():
        parts = line.split(":")
        if parts[0] == "sec" and len(parts) > 4:
            keyid = parts[4]
        elif parts[0] == "uid" and keyid and len(parts) > 9:
            # uid 字段形如 "Name <email>"，取姓名部分
            name = parts[9].split(" <")[0].strip()
            if name:
                out.append((keyid, name))
            keyid = ""
    return out


def sign_deb(target: Path, gpg_key: str = "", logger=None) -> Path:
    """dpkg-sig 对 .deb 签名（需求 62）。gpg_key 空 = 使用 gpg 默认密钥。

    dpkg-sig 仅 Debian/Ubuntu 系提供（交叉构建机普遍没有，此时调用方告警跳过）。
    """
    tool = find_executable("dpkg-sig")
    if tool:
        cmd = [tool, "--sign", "builder"]
        if gpg_key:
            cmd += ["-k", gpg_key]
        cmd.append(str(target))
        tool_name = "dpkg-sig"
    else:
        # dpkg-sig 在新版 Debian 已弃用，备选 debsigs
        tool = find_executable("debsigs")
        if not tool:
            raise RuntimeError(
                "deb 签名需要 dpkg-sig 或 debsigs 工具"
                "（apt install dpkg-sig / debsigs）")
        cmd = [tool, "--sign", "origin"]
        if gpg_key:
            cmd += ["--keyid", gpg_key]
        cmd.append(str(target))
        tool_name = "debsigs"
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{tool_name} 签名失败:\n{(proc.stderr or proc.stdout).strip()}"
        )
    if logger:
        logger.info("  已签名（%s %s）: %s",
                    tool_name, gpg_key or "默认密钥", Path(target).name)
    return Path(target)


def sign_rpm(target: Path, gpg_key: str = "", logger=None) -> Path:
    """rpmsign（rpm --addsign）对 .rpm 签名（需求 62）。gpg_key 必填。

    rpm 可通过 brew/apt 跨平台安装；GPG 目录取 GNUPGHOME 或 ~/.gnupg。
    """
    rpm = find_executable("rpm")
    if not rpm:
        raise RuntimeError("rpm 签名需要 rpm 工具（brew install rpm / apt install rpm）")
    if not gpg_key:
        raise RuntimeError("rpm 签名需要在「签名管理」中选择 GPG 密钥")
    gnupg = os.environ.get("GNUPGHOME") or str(Path.home() / ".gnupg")
    proc = subprocess.run(
        [rpm, "--addsign",
         "--define", f"_gpg_name {gpg_key}",
         "--define", f"_gpg_path {gnupg}",
         str(target)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"rpm --addsign 签名失败:\n{(proc.stderr or proc.stdout).strip()}"
        )
    if logger:
        logger.info("  已签名（rpm --addsign %s）: %s", gpg_key, Path(target).name)
    return Path(target)


# ------------------------------------------------------------ macOS 开发者证书


def login_keychain() -> str:
    """登录钥匙串路径。"""
    return str(Path.home() / "Library" / "Keychains" / "login.keychain-db")


def import_macos_cert(path, password: str = "", logger=None) -> str:
    """导入 macOS 开发者证书（.p12/.pfx/.cer/.crt）到登录钥匙串。

    从 Apple Developer 账号下载的证书通常是 .p12。-T 授权 codesign /
    productbuild 免密使用私钥（否则每次签名都会弹钥匙串密码框）。
    证书已存在时按成功处理。返回 security 输出文本。
    """
    if sys.platform != "darwin":
        raise RuntimeError("macOS 开发者证书导入仅支持 macOS 构建机")
    sec = find_executable("security")
    if not sec:
        raise RuntimeError("未找到 security 工具（macOS 系统自带，不应缺失）")
    cmd = [sec, "import", str(path), "-k", login_keychain(),
           "-T", "/usr/bin/codesign", "-T", "/usr/bin/productbuild"]
    if password:
        cmd += ["-P", password]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = ((proc.stderr or "") + (proc.stdout or "")).strip()
    if proc.returncode != 0 and "already exists" not in out \
            and "errSecDuplicateItem" not in out:
        raise RuntimeError(f"证书导入失败:\n{out}")
    if logger:
        logger.info("已导入开发者证书: %s", path)
    return out or "证书已存在（跳过重复导入）"


# ------------------------------------------------------------ macOS 公证


def notarize_available() -> bool:
    """公证工具链是否可用（macOS 构建机 + xcrun notarytool，Xcode 13+ CLT）。"""
    if sys.platform != "darwin":
        return False
    return bool(find_executable("xcrun"))


def notarize_file(
    target: Path,
    apple_id: str,
    password: str,
    team_id: str = "",
    logger=None,
) -> Path:
    """公证 macOS 产物并 staple（需求 65: Gatekeeper 分发要求）。

    流程: notarytool submit --wait 上传公证（.app 会先打成 zip 上传，
    .dmg/.pkg 直接上传）→ stapler staple 把票据钉到产物上 →
    stapler validate 校验。需 Apple ID + 应用专用密码（App 专用密码，
    非 Apple ID 登录密码）；team_id 可选（账号属多个团队时必须）。
    """
    if not notarize_available():
        raise RuntimeError(
            "公证需要 macOS 构建机与 Xcode Command Line Tools"
            "（xcrun notarytool，Xcode 13+）")
    if not apple_id or not password:
        raise RuntimeError("公证需要 Apple ID 与应用专用密码")
    target = Path(target)
    xcrun = find_executable("xcrun")

    # .app 目录产物需打包为 zip 上传（Apple 公证接口要求）
    tmp_zip: Optional[Path] = None
    submit_target = target
    if target.is_dir():
        tmp_dir = Path(tempfile.mkdtemp(prefix="pythub-notarize-"))
        tmp_zip = tmp_dir / f"{target.name}.zip"
        shutil.make_archive(str(tmp_zip.with_suffix("")), "zip",
                            root_dir=target.parent, base_dir=target.name)
        submit_target = tmp_zip
    try:
        cmd = [xcrun, "notarytool", "submit", str(submit_target),
               "--apple-id", apple_id, "--password", password, "--wait"]
        if team_id:
            cmd += ["--team-id", team_id]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            raise RuntimeError(
                f"公证提交失败:\n{(proc.stderr or proc.stdout).strip()}")
        if logger:
            logger.info("  公证提交完成（notarytool）: %s", target)

        proc = subprocess.run(
            [xcrun, "stapler", "staple", str(target)],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"stapler staple 失败:\n{(proc.stderr or proc.stdout).strip()}")
        subprocess.run(
            [xcrun, "stapler", "validate", str(target)],
            capture_output=True, text=True, timeout=600,
        )  # validate 失败不阻断（票据已钉上）
        if logger:
            logger.info("  已公证并 staple: %s", target)
        return target
    finally:
        if tmp_zip and tmp_zip.exists():
            shutil.rmtree(tmp_zip.parent, ignore_errors=True)
