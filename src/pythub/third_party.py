# -*- coding: utf-8 -*-
"""开源项目与许可证清单（需求 69）。

主界面「开源项目」标签页展示本项目用到的开源项目和工具及其许可证信息。
数据集中在此维护，GUI 与文档均可引用。

条目格式: (名称, 许可证, 主页/仓库, 用途说明)
"""

from typing import List, Tuple

Entry = Tuple[str, str, str, str]

# (分类, [条目, ...])
THIRD_PARTY: List[Tuple[str, List[Entry]]] = [
    # ------------------------------------------------ Python 运行时
    ("Python 运行时（打包产物内置）", [
        ("CPython",
         "PSF License 2.0",
         "https://github.com/python/cpython",
         "Python 解释器，打包产物内嵌的运行时主体"),
        ("python-build-standalone",
         "MPL-2.0",
         "https://github.com/astral-sh/python-build-standalone",
         "可再分发的独立构建 Python 运行时，Pythub 运行时缓存下载源"),
        ("Tcl/Tk",
         "Tcl/Tk License（BSD 风格）",
         "https://www.tcl.tk/software/tcltk/license.html",
         "tkinter 图形界面的底层图形库"),
    ]),
    # ------------------------------------------------ 运行时内置第三方库
    ("运行时内置第三方库（随运行时产物分发）", [
        ("OpenSSL",
         "Apache-2.0",
         "https://github.com/openssl/openssl",
         "ssl / hashlib 模块的 TLS 与加密实现"),
        ("libffi",
         "MIT",
         "https://github.com/libffi/libffi",
         "ctypes 模块的外部函数调用接口"),
        ("SQLite",
         "Public Domain",
         "https://www.sqlite.org",
         "sqlite3 模块的内嵌数据库引擎"),
        ("zlib",
         "zlib License",
         "https://github.com/madler/zlib",
         "zlib / gzip / zip 压缩"),
        ("bzip2",
         "bzip2 License（BSD 风格）",
         "https://www.sourceware.org/bzip2/",
         "bz2 模块压缩"),
        ("xz / liblzma",
         "0BSD（Public Domain 条款）",
         "https://tukaani.org/xz/",
         "lzma 模块压缩"),
        ("Expat",
         "MIT",
         "https://github.com/libexpat/libexpat",
         "XML 解析（pyexpat / ElementTree）"),
        ("mpdecimal",
         "BSD-3-Clause",
         "https://www.bytereef.org/mpdecimal/",
         "decimal 模块高精度十进制算术"),
        ("ncurses",
         "MIT（X11 风格）",
         "https://invisible-island.net/ncurses/",
         "curses 模块终端界面（Linux）"),
        ("libedit",
         "BSD-2-Clause",
         "https://www.thrysoee.dk/editline/",
         "readline 模块后端（macOS）"),
        ("Berkeley DB",
         "Sleepycat License",
         "https://www.oracle.com/database/berkeley-db/",
         "dbm.dumb 之外的 dbm 后端（可选）"),
    ]),
    # ------------------------------------------------ 可选打包引擎
    ("可选打包引擎（--engine 调用外部工具，不随 Pythub 分发）", [
        ("PyInstaller",
         "GPL-2.0（bootloader 附例外许可）",
         "https://github.com/pyinstaller/pyinstaller",
         "本机平台备选打包引擎"),
        ("Nuitka",
         "Apache-2.0",
         "https://github.com/Nuitka/Nuitka",
         "源码编译备选引擎"),
        ("cx_Freeze",
         "MIT",
         "https://github.com/marcelotduarte/cx_Freeze",
         "本机平台备选打包引擎"),
        ("py2exe",
         "MIT",
         "https://github.com/py2exe/py2exe",
         "Windows 平台备选打包引擎"),
        ("py2app",
         "MIT",
         "https://github.com/ronaldoussoren/py2app",
         "macOS 平台备选打包引擎"),
    ]),
    # ------------------------------------------------ 构建 / 工具链
    ("构建与工具链（打包过程调用，不随产物分发）", [
        ("zig",
         "MIT",
         "https://ziglang.org",
         "交叉编译器 / 链接器，编译自研 C 引导器 bootex"),
        ("mingw-w64",
         "多种宽松开源许可（公共领域等）",
         "https://www.mingw-w64.org",
         "Windows 目标交叉工具链"),
        ("osslsigncode",
         "GPL-3.0",
         "https://github.com/mtrojnar/osslsigncode",
         "Windows Authenticode 交叉签名（可选）"),
        ("appimagetool（AppImageKit）",
         "MIT（内含 GPL 的 squashfs 组件）",
         "https://github.com/AppImage/AppImageKit",
         "Linux AppImage 封装（可选，缺失时降级 .run）"),
        ("GNU binutils（strip）",
         "GPL-3.0+",
         "https://www.gnu.org/software/binutils/",
         "二进制瘦身，减小产物体积"),
        ("rpmbuild（RPM）",
         "GPL-2.0+",
         "https://github.com/rpm-software-management/rpm",
         "Linux RPM 包构建（可选）"),
        ("codesign / productbuild",
         "Apple 系统工具（随 Xcode 分发，非开源）",
         "https://developer.apple.com",
         "macOS 签名与 pkg 构建"),
    ]),
]

__all__ = ["THIRD_PARTY", "ACKNOWLEDGEMENT"]

# 致谢文案（需求 69: 随「开源许可」弹窗展示）
ACKNOWLEDGEMENT = (
    "谨向本页所列的所有开源项目及其作者、"
    "贡献者致以诚挚的谢意。"
)
