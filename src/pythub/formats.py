"""目标平台产物格式实现：启动器、.app、dmg、pkg、AppImage、单文件 .run、单文件 .exe、Debian/RPM 包。"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Optional

from pythub.exe_archive import (  # noqa: F401  自研单文件打包器
    find_zig,
    make_exe,
    make_native_exe,
    native_host_arch,
    native_host_platform,
)

from pythub.utils import find_executable, make_tar_gz, set_executable, zip_dir


def unix_launcher(main_name: str, python_rel: str, pyz_rel: str) -> str:
    return (
        "#!/bin/sh\n"
        'DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        f'exec "$DIR/{python_rel}" "$DIR/{pyz_rel}" "$@"\n'
    )


def app_launcher(main_name: str) -> str:
    return (
        "#!/bin/sh\n"
        'DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        'RES="$DIR/../Resources"\n'
        'exec "$RES/runtime/bin/python3" "$RES/app.pyz" "$@"\n'
    )


def windows_launcher(main_name: str) -> str:
    return (
        "@echo off\r\n"
        f'"%~dp0runtime\\python.exe" "%~dp0app.pyz" %*\r\n'
    )


def apprun_launcher() -> str:
    return (
        "#!/bin/sh\n"
        'DIR="$(dirname "$(readlink -f "$0")")"\n'
        'exec "$DIR/runtime/bin/python3" "$DIR/app.pyz" "$@"\n'
    )


INFO_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>{name}</string>
    <key>CFBundleDisplayName</key><string>{name}</string>
    <key>CFBundleIdentifier</key><string>{bundle_id}</string>
    <key>CFBundleExecutable</key><string>{name}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>{version}</string>
    <key>CFBundleVersion</key><string>{version}</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
</dict>
</plist>
"""


def build_app_bundle(
    dest: Path,
    name: str,
    version: str,
    runtime_src: Path,
    pyz_src: Path,
    logger: Optional[logging.Logger] = None,
    bundle_id: str = "",
) -> Path:
    """组装 macOS .app Bundle。bundle_id 为空则自动生成 org.crogram.<name>。"""
    app = dest / f"{name}.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    macos_dir = contents / "MacOS"
    res_dir = contents / "Resources"
    macos_dir.mkdir(parents=True)
    res_dir.mkdir(parents=True)

    ident = (bundle_id or "").strip()
    # 防御: 仅接受合法 Bundle ID 字符，非法（如误传提示文案）回退自动生成
    if ident and not re.fullmatch(r"[A-Za-z0-9.\-]+", ident):
        ident = ""
    ident = ident or "org.crogram." + "".join(
        ch if ch.isalnum() or ch in ".-" else "-" for ch in name.lower()
    )
    (contents / "Info.plist").write_text(
        INFO_PLIST_TEMPLATE.format(name=name, version=version, bundle_id=ident),
        encoding="utf-8"
    )
    (contents / "PkgInfo").write_text("APPL????", encoding="utf-8")
    shutil.copy2(pyz_src, res_dir / "app.pyz")
    # 保留符号链接（bin/python3 -> python3.12 等），避免目标文件被重复复制
    shutil.copytree(runtime_src, res_dir / "runtime", symlinks=True)
    launcher = macos_dir / name
    launcher.write_text(app_launcher(name), encoding="utf-8", newline="\n")
    set_executable(launcher)
    return app


def make_dmg(src_dir: Path, out: Path, name: str) -> Path:
    """hdiutil 打包 DMG（仅 macOS 可用）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["hdiutil", "create", "-volname", name, "-srcfolder", str(src_dir),
         "-ov", "-format", "UDZO", str(out)],
        check=True, capture_output=True,
    )
    return out


def make_pkg(src_dir: Path, out: Path, name: str, version: str) -> Path:
    """pkgbuild 打包 PKG（仅 macOS 可用），安装位置为 /Applications。

    pkgbuild 默认 --install-location 是 /，应用会被装到文件系统根目录导致"找不到应用"，
    因此显式指定安装到 /Applications。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["pkgbuild", "--root", str(src_dir), "--identifier", f"org.crogram.{name}",
         "--version", version, "--install-location", "/Applications", str(out)],
        check=True, capture_output=True,
    )
    return out


def make_appimage(
    staging: Path,
    out: Path,
    name: str,
    runtime_src: Path,
    pyz_src: Path,
    logger: Optional[logging.Logger] = None,
) -> Optional[Path]:
    """组装 AppDir 并调用 appimagetool 生成 AppImage。"""
    tool = find_executable("appimagetool")
    if not tool:
        return None
    appdir = staging / "AppDir"
    appdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pyz_src, appdir / "app.pyz")
    shutil.copytree(runtime_src, appdir / "runtime", symlinks=True)
    launcher = appdir / "AppRun"
    launcher.write_text(apprun_launcher(), encoding="utf-8", newline="\n")
    set_executable(launcher)
    desktop = appdir / f"{name}.desktop"
    desktop.write_text(
        f"[Desktop Entry]\nType=Application\nName={name}\n"
        f"Exec=AppRun\nIcon={name}\nCategories=Development;\n",
        encoding="utf-8",
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([tool, str(appdir), str(out)], check=True)
    return out


def make_exe_zip(bundle_dir: Path, out: Path) -> Path:
    """把自包含目录压缩为 .exe.zip（目录内含启动器，解压即用）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    zip_dir(out, bundle_dir)
    return out


# ---------------------------------------------------------------- 单文件 .run

RUN_HEADER_TEMPLATE = """#!/bin/sh
# Pythub 自解压单文件可执行（makeself 风格）
# 任意主机平台可交叉生成（引导头为纯文本脚本，无需任何工具链）；
# 目标机无需安装 Python，首次运行解压内嵌运行时到缓存目录，之后直接启动。
APP_NAME="{app}"
SKIP_BYTES="0000000000"
CACHE="$HOME/.cache/pythub/run/$APP_NAME/{md5}"
if [ ! -f "$CACHE/.pythub-run-ok" ]; then
    echo "首次运行，正在解压到 $CACHE ..."
    rm -rf "$CACHE"
    mkdir -p "$CACHE"
    # 用 $(( )) 算术展开取偏移: 空格补位既避免 BSD tail 的八进制陷阱，
    # 又保持引导头字节数恒定；$(( )) 不做字段分割，+ 与数字不会分开
    tail -c +$((SKIP_BYTES)) "$0" | tar xzf - -C "$CACHE" \\
        || {{ echo "错误: 自解压失败，文件可能已损坏" >&2; exit 1; }}
    [ -f "$CACHE/{app}" ] \\
        || {{ echo "错误: 启动器未生成，解压可能失败" >&2; exit 1; }}
    touch "$CACHE/.pythub-run-ok" 2>/dev/null
    # 缓存清理: 同一应用旧 md5 缓存目录 LRU 清理（仅保留当前版本）
    for _old in "$HOME/.cache/pythub/run/$APP_NAME"/*/; do
        [ "$_old" = "$CACHE/" ] || rm -rf "$_old" 2>/dev/null
    done
fi
exec "$CACHE/{app}" "$@"
"""

# 占位串含引号，确保不会与 md5 十六进制串混淆；替换后长度不变，保证字节偏移计算正确
_SKIP_PLACEHOLDER = '"0000000000"'


def make_run(bundle_dir: Path, out: Path, name: str) -> Path:
    """组装 Linux 单文件自解压可执行（.run）。

    结构: [ sh 引导头 ][ 追加的 tar.gz 数据 ]。引导头用 tail 按字节偏移
    切出自身尾部数据并解压到缓存目录，然后调起其中的启动器。
    """
    if any(c in name for c in '"\'`$\\'):
        raise ValueError(f"应用名称包含不安全字符: {name!r}")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(bundle_dir), arcname=".")
    payload = buf.getvalue()

    header = RUN_HEADER_TEMPLATE.format(app=name, md5=hashlib.md5(payload).hexdigest())
    # 数据从引导头之后第一个字节开始；tail -c +N 为 1 起始偏移。
    # 用空格右对齐补位: 未加引号的 $SKIP_BYTES 展开时 POSIX 字段分割会丢弃前导空格
    skip = len(header.encode("utf-8")) + 1
    header = header.replace(_SKIP_PLACEHOLDER, '"' + str(skip).rjust(10) + '"', 1)

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        f.write(header.encode("utf-8"))
        f.write(payload)
    set_executable(out)
    return out


# ---------------------------------------------------------------- Debian 包 (.deb)

# Debian 架构名映射（dpkg 惯例命名）
DEB_ARCH_MAP = {"x86_64": "amd64", "aarch64": "arm64", "i686": "i386"}

# dpkg 包名规范: 小写字母/数字 + . - +，且必须以字母数字开头
_DEB_NAME_OK = re.compile(r"[^a-z0-9+.\-]+")


def sanitize_deb_name(name: str) -> str:
    pkg = _DEB_NAME_OK.sub("-", name.lower()).strip("-")
    if not pkg or not pkg[0].isalnum():
        pkg = f"pythub-{pkg or 'app'}"
    return pkg


def sanitize_deb_version(version: str) -> str:
    """Debian 版本号允许 [A-Za-z0-9.+~:-]，其余字符替换；下划线习惯性替换为点。"""
    ver = version.replace("_", ".")
    ver = re.sub(r"[^A-Za-z0-9.+~:\-]+", "-", ver).strip("-")
    return ver or "0.0.0"


def deb_file_name(name: str, version: str, arch: str) -> str:
    pkg = sanitize_deb_name(name)
    ver = sanitize_deb_version(version)
    deb_arch = DEB_ARCH_MAP.get(arch, arch)
    return f"{pkg}_{ver}_{deb_arch}.deb"


def make_deb(
    bundle_dir: Path,
    out_dir: Path,
    *,
    name: str,
    version: str,
    arch: str = "x86_64",
    maintainer: str = "",
    description: str = "",
    section: str = "utils",
    windowed: bool = False,
) -> Path:
    """组装 Debian 软件包（.deb），纯标准库实现（ar 归档 + tar.gz），无需 dpkg。

    布局: bundle 内容 -> /opt/<pkg>/；/usr/bin/<name> 软链；GUI 应用附带
    /usr/share/applications/<pkg>.desktop。control 含 md5sums 与 Installed-Size。
    纯数据格式，任意主机平台均可交叉生成（与 .run 同属零工具链路线）。
    """
    pkg = sanitize_deb_name(name)
    ver = sanitize_deb_version(version)
    deb_arch = DEB_ARCH_MAP.get(arch, arch)
    maintainer = maintainer.strip() or "Pythub Packaging <open@crogram.org>"
    if "@" not in maintainer:
        # Debian 规范要求 Maintainer 为 "Name <email>"，缺邮箱时补默认地址
        maintainer = f"{maintainer} <open@crogram.org>"

    # ---- data.tar.gz: /opt/<pkg>/ 文件树 + /usr/bin 软链 (+ GUI 桌面入口) ----
    data_buf = io.BytesIO()
    md5s: list = []
    installed_size = 0

    def _root(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        ti.mtime = 0
        return ti

    with tarfile.open(fileobj=data_buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tf:
        for p in sorted(bundle_dir.rglob("*")):
            arc = f"opt/{pkg}/{p.relative_to(bundle_dir).as_posix()}"
            if p.is_symlink():
                ti = _root(tf.gettarinfo(str(p), arcname=arc))
                tf.addfile(ti)  # 链接按链接存储（md5sums 不登记链接）
                continue
            if p.is_dir():
                ti = _root(tf.gettarinfo(str(p), arcname=arc))
                ti.mode = 0o755
                tf.addfile(ti)
                continue
            if not p.is_file():
                continue
            ti = _root(tf.gettarinfo(str(p), arcname=arc))
            ti.mode = 0o755 if os.access(p, os.X_OK) else 0o644
            with open(p, "rb") as f:
                tf.addfile(ti, f)
            installed_size += p.stat().st_size
            md5s.append(f"{hashlib.md5(p.read_bytes()).hexdigest()}  {arc}")

        # /usr/bin/<name> -> /opt/<pkg>/<name>（绝对软链，卸载时随包删除）
        ti = tarfile.TarInfo(f"usr/bin/{name}")
        ti.type = tarfile.SYMTYPE
        ti.linkname = f"/opt/{pkg}/{name}"
        ti.mode = 0o777
        tf.addfile(_root(ti))

        # GUI 应用提供桌面入口（CLI 工具不生成，避免菜单噪音）
        if windowed:
            desktop = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={name}\n"
                f"Exec=/usr/bin/{name}\n"
                "Terminal=false\n"
                f"Comment={(description or name).splitlines()[0]}\n"
                "Categories=Utility;\n"
            )
            arc = f"usr/share/applications/{pkg}.desktop"
            data = desktop.encode("utf-8")
            ti = tarfile.TarInfo(arc)
            ti.size = len(data)
            ti.mode = 0o644
            tf.addfile(_root(ti), io.BytesIO(data))
            md5s.append(f"{hashlib.md5(data).hexdigest()}  {arc}")

    # ---- control.tar.gz: control + md5sums ----
    lines = [
        f"Package: {pkg}",
        f"Version: {ver}",
        f"Architecture: {deb_arch}",
        f"Maintainer: {maintainer}",
        f"Installed-Size: {installed_size // 1024 + 1}",
        f"Section: {section}",
        "Priority: optional",
    ]
    desc = (description or f"{name} (packaged by pythub)").strip().replace("\r\n", "\n")
    desc_lines = desc.split("\n")
    lines.append(f"Description: {desc_lines[0]}")
    for extra in desc_lines[1:]:
        lines.append(" ." if not extra.strip() else f" {extra}")
    control_text = "\n".join(lines) + "\n"

    control_buf = io.BytesIO()
    with tarfile.open(fileobj=control_buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tf:
        for member, data, mode in (
            ("control", control_text.encode("utf-8"), 0o644),
            ("md5sums", ("\n".join(md5s) + "\n").encode("utf-8"), 0o644),
        ):
            ti = tarfile.TarInfo(member)
            ti.size = len(data)
            ti.mode = mode
            tf.addfile(_root(ti), io.BytesIO(data))

    # ---- ar 归档: debian-binary + control.tar.gz + data.tar.gz ----
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / deb_file_name(name, version, arch)
    if out.exists():
        out.unlink()
    with open(out, "wb") as f:
        f.write(b"!<arch>\n")
        for header_name, data in (
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", control_buf.getvalue()),
            ("data.tar.gz", data_buf.getvalue()),
        ):
            # ar 成员头 60 字节: 名称16 + mtime12 + uid6 + gid6 + mode8 + size10 + "`\n"
            f.write(f"{header_name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}".encode())
            f.write(b"`\n")
            f.write(data)
            if len(data) % 2:
                f.write(b"\n")  # ar 成员按 2 字节对齐
    return out


def make_rpm(*args, **kwargs) -> Path:
    """RPM 包（自研 cpio/header 三端交叉，或 rpmbuild 引擎）——惰性转发到 rpm_pack。"""
    from pythub.rpm_pack import make_rpm as _make_rpm  # 规避循环导入

    return _make_rpm(*args, **kwargs)
