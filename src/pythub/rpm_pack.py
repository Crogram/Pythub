"""RPM 包实现: 自研 cpio newc + rpm header 构造器（纯标准库，三端可交叉）。

rpm 结构（依 rpm 4.x 产物实测校准，参考样本: Fedora 40 bash x86_64/aarch64）:
    lead(96B) -> 签名 header(8 字节对齐收尾) -> 主 header -> gzip(cpio newc) 载荷

引擎选择:
    auto      构建机为 mac/linux 且存在 rpmbuild 时用 rpmbuild，否则自研
    self      始终自研（三端均可交叉生成，零工具链）
    rpmbuild  强制 rpmbuild（仅 mac/linux 构建机，windows 主机报错）
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pythub.formats import DEB_ARCH_MAP, sanitize_deb_name

DEFAULT_EMAIL = "open@crogram.org"

# lead archnum（实测: x86_64=1, aarch64=19；现代 rpm 以 header ARCH tag 为准）
RPM_ARCH_NUM = {"x86_64": 1, "i386": 1, "i686": 1, "aarch64": 19, "noarch": 255}
RPM_OS_LINUX = 1

# header 数据类型（include/rpm/rpmtag.h）
RPM_INT8 = 2
RPM_INT16 = 3
RPM_INT32 = 4
RPM_STRING = 6
RPM_BIN = 7
RPM_STRARR = 8
RPM_I18NSTRING = 9

# 依赖标志: RPMLIB(1<<24) | LESS(1<<1) | EQUAL(1<<3)（实测 Fedora 产物 0x100000A）
RPMSENSE_RPMLIB = 0x1000000
RPMSENSE_LESS = 0x2
RPMSENSE_EQUAL = 0x8

# 自研载荷的 rpmlib 能力声明（与 rpmbuild 产物声明一致）
_RPMLIB_REQS = [
    ("rpmlib(CompressedFileNames)", "3.0.4-1"),
    ("rpmlib(FileDigests)", "4.6.0-1"),
]


def sanitize_rpm_version(version: str) -> str:
    """rpm 版本号不允许 '-'；允许 [A-Za-z0-9.+~]，其余替换为点。"""
    ver = version.replace("_", ".")
    ver = re.sub(r"[^A-Za-z0-9.+~]", ".", ver).strip(".")
    return ver or "0.0.0"


def rpm_file_name(name: str, version: str, release: str, arch: str) -> str:
    pkg = sanitize_deb_name(name)
    ver = sanitize_rpm_version(version)
    rel = sanitize_rpm_version(release) or "1"
    return f"{pkg}-{ver}-{rel}.{arch}.rpm"


# ---------------------------------------------------------------- cpio newc

def _cpio_entry(name: str, data: bytes, mode: int, mtime: int, inode: int) -> bytes:
    """cpio newc 单条目: magic(6) + 13 个 8 位十六进制字段 + 名字/数据按 4 字节对齐。

    （GNU cpio newc 规范为 16 进制 ASCII，八进制会因现代 mtime 溢出 8 位而错位）
    """
    fields = (
        inode, mode, 0, 0, 1, mtime, len(data),
        0, 0, 0, 0, len(name) + 1, 0,
    )
    header = b"070701" + b"".join(f"{v:08X}".encode() for v in fields)
    entry = header + name.encode("utf-8") + b"\0"
    entry += b"\0" * (-len(entry) % 4)
    entry += data
    entry += b"\0" * (-len(data) % 4)
    return entry


def build_cpio(entries: list) -> bytes:
    """entries: [(relpath, kind, src)] -> cpio newc 字节流。

    kind: file（src 为 Path）/ dir（src 忽略）/ symlink（src 为目标字符串）。
    路径不带 './' 前缀（对应不声明 rpmlib(PayloadFilesHavePrefix)）。
    """
    out = bytearray()
    for i, (rel, kind, src) in enumerate(entries, 1):
        if kind == "dir":
            out += _cpio_entry(rel, b"", 0o40755, 0, i)
        elif kind == "symlink":
            out += _cpio_entry(rel, src.encode("utf-8"), 0o120777, 0, i)
        else:
            st = src.stat()
            data = src.read_bytes()
            mode = 0o100755 if (st.st_mode & 0o111) else 0o100644
            out += _cpio_entry(rel, data, mode, int(st.st_mtime), i)
    out += _cpio_entry("TRAILER!!!", b"", 0, 0, 0)
    out += b"\0" * (-(len(out) % 512) % 512)  # 载荷按 512 字节块收尾
    return bytes(out)


# ---------------------------------------------------------------- rpm header

class _Store:
    """header 数据区构造器（数值类型按宽度对齐，同 rpm lib/header.c）。"""

    def __init__(self):
        self.buf = bytearray()
        self.index: list = []  # (tag, type, offset, count)

    def _align(self, width):
        self.buf += b"\0" * (-len(self.buf) % width)

    def _add(self, tag, typ, raw, count):
        self.index.append((tag, typ, len(self.buf), count))
        self.buf += raw

    def int32(self, tag, values):
        values = values if isinstance(values, (list, tuple)) else [values]
        self._align(4)
        self._add(tag, RPM_INT32, struct.pack(f">{len(values)}I", *values), len(values))

    def int16(self, tag, values):
        values = values if isinstance(values, (list, tuple)) else [values]
        self._align(2)
        self._add(tag, RPM_INT16, struct.pack(f">{len(values)}H", *values), len(values))

    def int8(self, tag, values):
        values = values if isinstance(values, (list, tuple)) else [values]
        self._add(tag, RPM_INT8, struct.pack(f">{len(values)}B", *values), len(values))

    def string(self, tag, value):
        self._add(tag, RPM_STRING, value.encode("utf-8") + b"\0", 1)

    def strarr(self, tag, values):
        raw = b"".join(v.encode("utf-8") + b"\0" for v in values)
        self._add(tag, RPM_STRARR, raw, len(values))

    def i18n(self, tag, value):
        self._add(tag, RPM_I18NSTRING, value.encode("utf-8") + b"\0", 1)

    def bin(self, tag, value):
        self._add(tag, RPM_BIN, value, len(value))

    def render(self) -> bytes:
        self.index.sort(key=lambda e: e[0])  # rpm 要求索引按 tag 升序
        # 16 字节头: magic(3) + 版本(1) + 保留(4) + 索引数(4) + 数据区大小(4)
        hdr = struct.pack(">BBBBIII", 0x8E, 0xAD, 0xE8, 1, 0,
                          len(self.index), len(self.buf))
        idx = b"".join(struct.pack(">iIII", *e) for e in self.index)
        return hdr + idx + bytes(self.buf)


def _scan_bundle(bundle_dir: Path, pkg: str, name: str, windowed: bool, desktop: str):
    """枚举 bundle -> (cpio 条目, header 文件信息, 已安装大小)。

    header 文件信息: (relpath, kind, size, mode, mtime, md5hex, linkto)
    """
    entries = []
    paths = []
    installed = 0
    for p in sorted(bundle_dir.rglob("*")):
        rel = f"opt/{pkg}/{p.relative_to(bundle_dir).as_posix()}"
        if p.is_symlink():
            target = os.readlink(p)
            entries.append((rel, "symlink", target))
            paths.append((rel, "symlink", len(target), 0o120777, 0, "-", target))
            installed += len(target)
        elif p.is_dir():
            entries.append((rel, "dir", None))
            paths.append((rel, "dir", 0, 0o40755, 0, "-", ""))
        elif p.is_file():
            st = p.stat()
            md5 = hashlib.md5(p.read_bytes()).hexdigest()
            mode = 0o100755 if (st.st_mode & 0o111) else 0o100644
            entries.append((rel, "file", p))
            paths.append((rel, "file", st.st_size, mode, int(st.st_mtime), md5, ""))
            installed += st.st_size
    # /usr/bin/<name> -> /opt/<pkg>/<name>（绝对软链）
    link = f"/opt/{pkg}/{name}"
    entries.append((f"usr/bin/{name}", "symlink", link))
    paths.append((f"usr/bin/{name}", "symlink", len(link), 0o120777, 0, "-", link))
    # GUI 桌面入口
    if windowed and desktop:
        entries.append((f"usr/share/applications/{pkg}.desktop", "file", desktop))
        paths.append((f"usr/share/applications/{pkg}.desktop", "file",
                      len(desktop), 0o100644, 0,
                      hashlib.md5(desktop).hexdigest(), ""))
    return entries, paths, installed


def _build_header(pkg, ver, rel, arch, maintainer, description, license_,
                  group, paths, installed, buildhost) -> bytes:
    dirs: list = []          # 去重目录（含中间目录），尾部带 /
    dir_index: list = []
    basenames: list = []
    for f_rel, *_ in paths:
        parent = f_rel.rsplit("/", 1)[0] + "/"
        prefix = ""
        for seg in parent.split("/"):
            if seg:
                prefix += seg + "/"
                if prefix not in dirs:
                    dirs.append(prefix)
        dir_index.append(dirs.index(parent))
        basenames.append(f_rel.rsplit("/", 1)[-1])

    s = _Store()
    s.strarr(100, ["C"])                                       # HEADERI18NTABLE
    s.string(1000, pkg)                                        # NAME
    s.string(1001, ver)                                        # VERSION
    s.string(1002, rel)                                        # RELEASE
    s.i18n(1004, (description or pkg).splitlines()[0])         # SUMMARY
    s.i18n(1005, description or f"{pkg} (packaged by pythub)")  # DESCRIPTION
    s.int32(1006, int(time.time()))                            # BUILDTIME
    s.string(1007, buildhost)                                  # BUILDHOST
    s.int32(1009, installed)                                   # SIZE（已安装总大小）
    s.string(1014, license_ or "Unknown")                      # LICENSE
    s.string(1015, maintainer)                                 # PACKAGER
    s.i18n(1016, group)                                        # GROUP
    s.string(1021, "linux")                                    # OS
    s.string(1022, arch)                                       # ARCH
    s.int32(1028, [p[2] for p in paths])                       # FILESIZES
    s.int8(1029, [0] * len(paths))                             # FILESTATES
    s.int16(1030, [p[3] for p in paths])                       # FILEMODES
    s.int16(1033, [0] * len(paths))                            # FILERDEVS
    s.int32(1034, [p[4] for p in paths])                       # FILEMTIMES
    s.strarr(1035, [p[5] for p in paths])                      # FILEDIGESTS
    s.strarr(1036, [p[6] for p in paths])                      # FILELINKTOS
    s.int32(1037, [0] * len(paths))                            # FILEFLAGS
    s.strarr(1039, ["root"] * len(paths))                      # FILEUSERNAME
    s.strarr(1040, ["root"] * len(paths))                      # FILEGROUPNAME
    s.int32(1048, [RPMSENSE_RPMLIB | RPMSENSE_LESS | RPMSENSE_EQUAL] * len(_RPMLIB_REQS))
    s.strarr(1049, [n for n, _ in _RPMLIB_REQS])               # REQUIRENAME
    s.strarr(1050, [v for _, v in _RPMLIB_REQS])               # REQUIREVERSION
    s.int32(1095, [0] * len(paths))                            # FILEDEVICES
    s.int32(1096, list(range(1, len(paths) + 1)))              # FILEINODES
    s.int32(1116, dir_index)                                   # DIRINDEXES
    s.strarr(1117, basenames)                                  # BASENAMES
    s.strarr(1118, dirs)                                       # DIRNAMES
    s.string(1124, "cpio")                                     # PAYLOADFORMAT
    s.string(1125, "gzip")                                     # PAYLOADCOMPRESSOR
    s.string(1126, "9")                                        # PAYLOADFLAGS
    return s.render()


def _desktop_entry(name: str, pkg: str, description: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Exec=/usr/bin/{name}\n"
        "Terminal=false\n"
        f"Comment={(description or name).splitlines()[0]}\n"
        "Categories=Utility;\n"
    )


def _normalize_rpm_arch(arch: str) -> str:
    """deb 风格架构名还原为 rpm 架构名。"""
    deb_arch = DEB_ARCH_MAP.get(arch, arch)
    return {"amd64": "x86_64", "arm64": "aarch64", "i386": "i686"}.get(deb_arch, deb_arch)


def make_rpm(
    bundle_dir: Path,
    out_dir: Path,
    *,
    name: str,
    version: str,
    release: str = "1",
    arch: str = "x86_64",
    maintainer: str = "",
    description: str = "",
    license: str = "",  # noqa: A002 - rpm 术语
    group: str = "Applications/Other",
    windowed: bool = False,
    engine: str = "auto",
) -> Path:
    """组装 RPM 软件包（自研 cpio/header，或调用 rpmbuild）。

    布局与 .deb 一致: bundle -> /opt/<pkg>/、/usr/bin 软链、GUI 附桌面入口。
    自研路径纯标准库实现，三端均可交叉生成；rpmbuild 路径仅 mac/linux。
    """
    engine = _resolve_engine(engine)
    kwargs = dict(
        name=name, version=version, release=release, arch=arch,
        maintainer=maintainer, description=description, license=license,
        group=group, windowed=windowed)
    if engine == "rpmbuild":
        return _rpm_via_rpmbuild(bundle_dir, out_dir, **kwargs)
    return _rpm_self(bundle_dir, out_dir, **kwargs)


def _resolve_engine(engine: str) -> str:
    """解析引擎: auto -> 构建机 mac/linux 且有 rpmbuild 时用之，否则自研。"""
    engine = (engine or "auto").strip().lower()
    if engine not in ("auto", "self", "rpmbuild"):
        raise ValueError(f"未知的 rpm 引擎: {engine}（可选 auto/self/rpmbuild）")
    if engine == "rpmbuild":
        if sys.platform == "win32":
            raise SystemExit("rpmbuild 引擎不支持 Windows 构建机（请改用 self 引擎）")
        if not shutil.which("rpmbuild"):
            raise SystemExit(
                "未找到 rpmbuild，请先安装（macOS: brew install rpm；"
                "Linux: dnf install rpm-build / apt install rpm）或改用 self 引擎")
        return "rpmbuild"
    if engine == "auto" and sys.platform != "win32" and shutil.which("rpmbuild"):
        return "rpmbuild"
    return "self"


def _rpm_self(bundle_dir, out_dir, *, name, version, release, arch,
              maintainer, description, license, group, windowed) -> Path:
    """自研路径: cpio newc + gzip 载荷 + 手工构造 lead/签名/主 header。"""
    pkg = sanitize_deb_name(name)
    ver = sanitize_rpm_version(version)
    rel = sanitize_rpm_version(release) or "1"
    rpm_arch = _normalize_rpm_arch(arch)
    maintainer = maintainer.strip() or f"Pythub Packaging <{DEFAULT_EMAIL}>"
    if "@" not in maintainer:
        maintainer = f"{maintainer} <{DEFAULT_EMAIL}>"

    desktop = _desktop_entry(name, pkg, description) if windowed else ""
    entries, paths, installed = _scan_bundle(bundle_dir, pkg, name, windowed, desktop)

    payload = gzip.compress(build_cpio(entries), 9, mtime=0)
    header = _build_header(pkg, ver, rel, rpm_arch, maintainer, description,
                           license, group, paths, installed,
                           socket.gethostname() or "pythub")

    # 签名 header: SIZE / PAYLOADSIZE / MD5(header+payload) / SHA1 / SHA256(header)
    sig = _Store()
    sig.int32(1000, len(header) + len(payload))            # RPMSIGTAG_SIZE
    sig.int32(1007, len(payload))                          # RPMSIGTAG_PAYLOADSIZE
    sig.bin(1004, hashlib.md5(header + payload).digest())  # RPMSIGTAG_MD5
    sig.string(269, hashlib.sha1(header).hexdigest())      # RPMSIGTAG_SHA1HEADER
    sig.string(273, hashlib.sha256(header).hexdigest())    # RPMSIGTAG_SHA256HEADER
    sig_bytes = sig.render()
    sig_bytes += b"\0" * (-len(sig_bytes) % 8)  # 签名区按 8 字节对齐收尾（实测一致）

    # lead: 96 字节（布局依实测: magic4+major1+minor1+type2+archnum2+name66+os2+sig2+resv16）
    nv = f"{pkg}-{ver}-{rel}"[:66]
    lead = struct.pack(">IBBHH", 0xEDABEEDB, 3, 0, 0, RPM_ARCH_NUM.get(rpm_arch, 255))
    lead += nv.encode("ascii") + b"\0" * (66 - len(nv))
    lead += struct.pack(">HH", RPM_OS_LINUX, 5)
    lead += b"\0" * 16
    assert len(lead) == 96, len(lead)

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / rpm_file_name(name, version, rel, rpm_arch)
    if out.exists():
        out.unlink()
    with open(out, "wb") as f:
        f.write(lead + sig_bytes + header + payload)
    return out


_RPM_SPEC = """\
Name:           {pkg}
Version:        {ver}
Release:        {rel}
Summary:        {summary}
License:        {license}
Group:          {group}
BuildArch:      {arch}
Packager:       {maintainer}
AutoReqProv:    no

%description
{description}

%files
/opt/{pkg}
/usr/bin/{name}
"""


def _rpm_via_rpmbuild(bundle_dir, out_dir, *, name, version, release, arch,
                      maintainer, description, license, group, windowed) -> Path:
    """rpmbuild 路径: 预铺 buildroot + 最小 spec，--target 交叉构建。

    仅 mac/linux 构建机可用（rpmbuild 为纯用户态工具，不依赖 Linux 内核）。
    """
    pkg = sanitize_deb_name(name)
    ver = sanitize_rpm_version(version)
    rel = sanitize_rpm_version(release) or "1"
    rpm_arch = _normalize_rpm_arch(arch)
    maintainer = maintainer.strip() or f"Pythub Packaging <{DEFAULT_EMAIL}>"
    if "@" not in maintainer:
        maintainer = f"{maintainer} <{DEFAULT_EMAIL}>"

    out_dir = Path(out_dir)
    with tempfile.TemporaryDirectory(prefix="pythub-rpm-") as tmp:
        topdir = Path(tmp)
        # 预铺 buildroot（与自研路径一致的 FHS 布局）
        buildroot = topdir / "buildroot"
        shutil.copytree(bundle_dir, buildroot / "opt" / pkg, symlinks=True)
        usr_bin = buildroot / "usr" / "bin"
        usr_bin.mkdir(parents=True, exist_ok=True)
        os.symlink(f"/opt/{pkg}/{name}", usr_bin / name)
        if windowed:
            apps = buildroot / "usr" / "share" / "applications"
            apps.mkdir(parents=True, exist_ok=True)
            (apps / f"{pkg}.desktop").write_text(
                _desktop_entry(name, pkg, description), encoding="utf-8")

        spec = topdir / "SPECS" / f"{pkg}.spec"
        spec.parent.mkdir(parents=True, exist_ok=True)
        summary = (description or f"{name} (packaged by pythub)").splitlines()[0]
        desc = description or f"{name} (packaged by pythub)"
        spec.write_text(_RPM_SPEC.format(
            pkg=pkg, ver=ver, rel=rel, summary=summary, license=license or "Unknown",
            group=group, arch=rpm_arch, maintainer=maintainer, description=desc,
            name=name), encoding="utf-8")

        cmd = [
            "rpmbuild", "-bb",
            "--target", f"{rpm_arch}-pc-linux-gnu",
            "--define", f"_topdir {topdir}",
            "--define", "_rpmdir %_topdir/RPMS",
            "--define", "debug_package %{nil}",
            "--define", "__os_install_post %{nil}",
            "--define", "build_id_links none",
            "--buildroot", str(buildroot),
            str(spec),
        ]
        subprocess.run(cmd, check=True)

        built = out_dir / rpm_arch / f"{pkg}-{ver}-{rel}.{rpm_arch}.rpm"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / rpm_file_name(name, version, rel, rpm_arch)
        shutil.move(str(built), out)
        return out
