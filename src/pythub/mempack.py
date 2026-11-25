"""mem 格式辅助模块：内存执行引导器与字节码编译助手。

mem 格式产物为平台原生二进制可执行文件（自研 bootex 引导器 + 自研归档）：

- 二进制内嵌：嵌入式 Python 运行时（免装 Python）+ 引导器源码 + 应用字节码
  + 内存资源条目（应用的非代码数据文件，经 pythub.memres 访问）；
- 引导器源码、字节码与资源均为"内存载荷"归档条目（FLAG_MEM），启动时**不会**写入磁盘；
- 运行时：bootex 引导器将运行时解压到缓存目录（解释器二进制必须落盘才能 exec，
  这是操作系统限制，且仅在首次运行时发生），随后：
  1. 在内存中解压出引导器源码，经环境变量 ``PYTHUB_BOOT`` 注入解释器；
  2. 在内存中解压出应用字节码（裸 marshal），经 fd 3 管道直传解释器；
  3. 解释器以 ``-I -c 'import os;exec(os.environ["PYTHUB_BOOT"])'`` 启动，
     引导器从 fd 3 读取字节码、在内存中反序列化并执行——全程不产生任何
     临时 .py/.pyc 文件。
"""

from __future__ import annotations

from pathlib import Path

# 经环境变量 PYTHUB_BOOT 注入的最小加载器（bootex 引导器内置的 -c 一行码）
MINI_LOADER = "import os;exec(os.environ['PYTHUB_BOOT'])"

# 引导器源码：归档为内存条目 app.boot，运行时经 PYTHUB_BOOT 注入。
# 字节码经 fd 3 管道传入（Windows 为句柄值经命令行传入），全程内存。
BOOT_RUNNER_PROLOGUE_MEM = '''\
# -*- coding: utf-8 -*-
# Pythub mem 内存执行引导器（由 PYTHUB_BOOT 环境变量注入）
import os
import sys

os.environ.pop("PYTHUB_BOOT", None)

# Tcl/Tk 数据目录定位: PBS 构建的 libpython 静态内嵌 _tkinter，但 Tcl 数据
# 查找路径指向其构建沙箱（/tools/deps/...），需显式指向随产物释放的目录
_tclroot = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(sys.executable))), "runtime", "lib")
if os.path.isfile(os.path.join(_tclroot, "tcl8.6", "init.tcl")):
    os.environ.setdefault("TCL_LIBRARY", os.path.join(_tclroot, "tcl8.6"))
    os.environ.setdefault("TK_LIBRARY", os.path.join(_tclroot, "tk8.6"))
del _tclroot

_ENTRY = __PYTHUB_ENTRY__

if sys.platform == "win32":
    import msvcrt
    _fd = msvcrt.open_osfhandle(int(sys.argv[1]), 0)
    _argv = sys.argv[2:]
else:
    _fd = 3
    _argv = sys.argv[1:]

_chunks = []
while True:
    _chunk = os.read(_fd, 1 << 20)
    if not _chunk:
        break
    _chunks.append(_chunk)
os.close(_fd)

import marshal

# 载荷结构 {"modules": {模块名: (marshal字节码, 是否包)}, "data": {资源路径: bytes}}
_raw = marshal.loads(b"".join(_chunks))
'''

# 载荷还原与执行主体
BOOT_RUNNER_BODY = '''\
_codes = {name: (marshal.loads(blob), is_pkg)
          for name, (blob, is_pkg) in _raw["modules"].items()}
_module = _codes.get(_ENTRY)
if _module is None:
    sys.stderr.write("pythub mem: 入口模块 %r 缺失\\n" % _ENTRY)
    raise SystemExit(2)


class _MemLoader:
    """从内存字典加载 marshal 字节码。"""

    def __init__(self, fullname, code, is_pkg):
        self.fullname = fullname
        self.code = code
        self.is_pkg = is_pkg

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = "<memory:%s>" % self.fullname
        exec(self.code, module.__dict__)


class _MemFinder:
    def __init__(self, codes):
        self._codes = codes

    def find_spec(self, fullname, path=None, target=None):
        data = self._codes.get(fullname)
        if data is None:
            return None
        import importlib.util
        code, is_pkg = data
        spec = importlib.util.spec_from_loader(fullname, _MemLoader(fullname, code, is_pkg))
        if is_pkg:
            spec.submodule_search_locations = []
        return spec


sys.argv = _argv
sys.meta_path.insert(0, _MemFinder(_codes))

# Pythub 注入打包/运行信息（需求 63: sys.pythub_packaged / sys.pythub_info）
sys.pythub_packaged = True
sys.pythub_info = __PYTHUB_INFO__
sys.pythub_info["runtime"] = {
    "python": "%d.%d.%d" % sys.version_info[:3],
    "python_full": sys.version.split()[0],
    "implementation": sys.implementation.name,
    "executable": sys.executable,
    "exe_dir": os.path.dirname(os.path.abspath(sys.executable)),
    "app_origin": _argv[0] if _argv else "",
    "source": "<memory:%s>" % _ENTRY,
    "source_on_disk": False,
    "cache_dir": os.environ.get("PYTHUB_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "pythub"),
}

# 内存资源数据（非代码文件）: 注册为 __pythub_memdata__ 模块，经 pythub.memres 访问
if _raw.get("data"):
    import types
    _resmod = types.ModuleType("__pythub_memdata__")
    _resmod.DATA = _raw["data"]
    sys.modules["__pythub_memdata__"] = _resmod

_pkg = _ENTRY.rpartition(".")[0]
_globals = {"__name__": "__main__", "__file__": "<memory:%s>" % _ENTRY,
            "__package__": _pkg or None, "__builtins__": __builtins__}
exec(_module[0], _globals)
'''

# embed 模式 prologue（需求 64: 内嵌解释器）: 无 fd 3 管道，bootex 引导器
# 进程内 dlopen(libpython) 初始化解释器后经 PyRun_SimpleString 执行本源码。
# 应用载荷从自身归档（PythubEX cookie/TOC）解析；argv 经 base64 环境变量恢复。
BOOT_RUNNER_PROLOGUE_EMBED = '''\
# -*- coding: utf-8 -*-
# Pythub embed 内嵌解释器引导器（需求 64: 解释器以动态库形式进程内加载）
import os
import sys

os.environ.pop("PYTHUB_BOOT", None)
_SELF = os.environ.pop("PYTHUB_SELF", "")
_ARGV64 = os.environ.pop("PYTHUB_EMBED_ARGV", "")

import base64
import marshal
import struct
import zlib

# argv 恢复: C 端把 [自身路径, 用户参数...] 逐项 base64 后用逗号连接
_argv = [base64.b64decode(x).decode("utf-8", "surrogateescape")
         for x in _ARGV64.split(",")] if _ARGV64 else []

# Tcl/Tk 数据释放（应用引用 tkinter 时归档携带 app.tcltk 条目）: Tcl 需要
# 真实文件路径（不能走 zipimport），从缓存目录解出 tcl8.6/tk8.6 并设置环境
import zipfile as _zipfile

_CACHE_DIR = os.environ.get("PYTHUB_EXE_CACHE") or os.path.dirname(
    os.path.abspath(_SELF))
_TCLTK = os.path.join(_CACHE_DIR, "app.tcltk")
if os.path.isfile(_TCLTK) and not os.path.isdir(
        os.path.join(_CACHE_DIR, "tcl8.6")):
    try:
        with _zipfile.ZipFile(_TCLTK) as _zf:
            _zf.extractall(_CACHE_DIR)
    except Exception:  # noqa: BLE001 释放失败交由后续 import 报错
        pass
if os.path.isfile(os.path.join(_CACHE_DIR, "tcl8.6", "init.tcl")):
    os.environ.setdefault("TCL_LIBRARY", os.path.join(_CACHE_DIR, "tcl8.6"))
    os.environ.setdefault("TK_LIBRARY", os.path.join(_CACHE_DIR, "tk8.6"))

# 嵌入式解释器: sys.executable 指向进程内 dlopen 的 libpython 缓存副本。
# 引导器进程即解释器进程（无独立 python 可执行文件），CPython 嵌入式
# 初始化时默认把 sys.executable 填成引导器自身路径，需改为真实解释器——
# dlopen 进程内的 libpython 共享库（OS 物理限制: dlopen 必须以真实文件
# 为载体，故 libpython 以缓存文件形式存在，"内存执行"指进程内加载）
for _pylib_name in ("libpython.dylib", "libpython.so", "libpython.dll"):
    _pylib_path = os.path.join(_CACHE_DIR, _pylib_name)
    if os.path.exists(_pylib_path):
        sys.executable = _pylib_path
        break

# 从自身归档解析内存载荷 app.mem（PythubEX 格式: 尾部 96 字节 cookie + TOC）
with open(_SELF, "rb") as _f:
    _data = _f.read()
_tail = len(_data) - 96
if _data[_tail:_tail + 8] != b"PYTHUBEX":
    sys.stderr.write("pythub embed: 归档 cookie 校验失败\\n")
    raise SystemExit(2)
_toc_off = struct.unpack_from("<Q", _data, _tail + 16)[0]
_pos = _toc_off + 4
_cnt = struct.unpack_from("<I", _data, _toc_off)[0]
_payload = None
for _ in range(_cnt):
    _pl, _st, _rw, _o, _fl = struct.unpack_from("<IIIQB", _data, _pos)
    _pos += 21
    _name = _data[_pos:_pos + _pl].decode("utf-8")
    _pos += _pl
    if _name == "app.mem":
        _blob = _data[_o:_o + _st]
        if _fl & 1:
            _blob = zlib.decompress(_blob)
        _payload = _blob
        break
if _payload is None:
    sys.stderr.write("pythub embed: 归档缺少应用载荷 app.mem\\n")
    raise SystemExit(2)

# 载荷结构 {"modules": {模块名: (marshal字节码, 是否包)}, "data": {资源路径: bytes}}
_raw = marshal.loads(_payload)
_ENTRY = __PYTHUB_ENTRY__
'''

# embed 模式 body: 复用 mem body（_raw/_ENTRY/_argv 变量约定一致），仅把
# 尾部 exec 包装 SystemExit -> os._exit，把应用退出码回传给引导器进程。
_BOOT_RUNNER_BODY_EMBED = BOOT_RUNNER_BODY.replace(
    "exec(_module[0], _globals)\n",
    # 嵌入式解释器无 Py_Finalize 收尾（进程直接退出），Python 层 io 缓冲
    # 不会自动刷新——退出前显式 flush（os._exit 跳过一切清理，更需如此）
    "try:\n"
    "    exec(_module[0], _globals)\n"
    "except SystemExit as _e:\n"
    "    _rc = _e.code\n"
    "    sys.stdout.flush()\n"
    "    sys.stderr.flush()\n"
    "    os._exit(_rc if isinstance(_rc, int)\n"
    "             else (0 if _rc in (None, True) else 1))\n"
    "sys.stdout.flush()\n"
    "sys.stderr.flush()\n",
)


# static 模式 prologue（需求 64 第四档: 解释器静态链接进入口二进制）:
# stdlib 已由 C 引导器注册为 frozen 内存模块（encodings 等启动模块即从
# 内存加载，Py_Initialize 全程不访问文件系统 stdlib），本 prologue 只需
# 从自身归档解析应用载荷。sys.executable 保持 C 端默认值 = 入口二进制
# 路径——语义正确: 解释器机器码就在该二进制内。tcl/tk 数据释放与 embed
# 同款（Tcl 需要真实文件，是 static 模式唯一的落盘项）。
BOOT_RUNNER_PROLOGUE_STATIC = '''\
# -*- coding: utf-8 -*-
# Pythub static 静态内嵌引导器（需求 64: 解释器在入口二进制内, 零落盘）
import os
import sys

os.environ.pop("PYTHUB_BOOT", None)
_SELF = os.environ.pop("PYTHUB_SELF", "")
_ARGV64 = os.environ.pop("PYTHUB_EMBED_ARGV", "")

import base64
import marshal
import struct
import zlib

# argv 恢复: C 端把 [自身路径, 用户参数...] 逐项 base64 后用逗号连接
_argv = [base64.b64decode(x).decode("utf-8", "surrogateescape")
         for x in _ARGV64.split(",")] if _ARGV64 else []

# Tcl/Tk 数据释放（应用引用 tkinter 时归档携带 app.tcltk 条目）: Tcl 需要
# 真实文件路径，从缓存目录解出 tcl8.6/tk8.6 并设置环境
import zipfile as _zipfile

_CACHE_DIR = os.environ.get("PYTHUB_EXE_CACHE") or os.path.dirname(
    os.path.abspath(_SELF))
_TCLTK = os.path.join(_CACHE_DIR, "app.tcltk")
if os.path.isfile(_TCLTK) and not os.path.isdir(
        os.path.join(_CACHE_DIR, "tcl8.6")):
    try:
        with _zipfile.ZipFile(_TCLTK) as _zf:
            _zf.extractall(_CACHE_DIR)
    except Exception:  # noqa: BLE001 释放失败交由后续 import 报错
        pass
if os.path.isfile(os.path.join(_CACHE_DIR, "tcl8.6", "init.tcl")):
    os.environ.setdefault("TCL_LIBRARY", os.path.join(_CACHE_DIR, "tcl8.6"))
    os.environ.setdefault("TK_LIBRARY", os.path.join(_CACHE_DIR, "tk8.6"))

# Windows static 第三方依赖 DLL 释放（openssl/sqlite3 导入库对应 DLL）:
# 引导器已把缓存目录前置到 PATH，_ssl/_sqlite3 等静态扩展 import 期
# 经 PATH 搜索加载这些 DLL
if os.name == "nt":
    _WDLLS = os.path.join(_CACHE_DIR, "app.wdlls")
    if os.path.isfile(_WDLLS) and not os.path.isfile(
            os.path.join(_CACHE_DIR, "libcrypto-3-x64.dll")):
        try:
            with _zipfile.ZipFile(_WDLLS) as _zf:
                _zf.extractall(_CACHE_DIR)
        except Exception:  # noqa: BLE001 释放失败交由后续 import 报错
            pass
    os.environ["PATH"] = _CACHE_DIR + os.pathsep + os.environ.get("PATH", "")

# 从自身归档解析内存载荷 app.mem（PythubEX 格式: 尾部 96 字节 cookie + TOC）
with open(_SELF, "rb") as _f:
    _data = _f.read()
_tail = len(_data) - 96
if _data[_tail:_tail + 8] != b"PYTHUBEX":
    sys.stderr.write("pythub static: 归档 cookie 校验失败\\n")
    raise SystemExit(2)
_toc_off = struct.unpack_from("<Q", _data, _tail + 16)[0]
_pos = _toc_off + 4
_cnt = struct.unpack_from("<I", _data, _toc_off)[0]
_payload = None
for _ in range(_cnt):
    _pl, _st, _rw, _o, _fl = struct.unpack_from("<IIIQB", _data, _pos)
    _pos += 21
    _name = _data[_pos:_pos + _pl].decode("utf-8")
    _pos += _pl
    if _name == "app.mem":
        _blob = _data[_o:_o + _st]
        if _fl & 1:
            _blob = zlib.decompress(_blob)
        _payload = _blob
        break
if _payload is None:
    sys.stderr.write("pythub static: 归档缺少应用载荷 app.mem\\n")
    raise SystemExit(2)

# 载荷结构 {"modules": {模块名: (marshal字节码, 是否包)}, "data": {资源路径: bytes}}
_raw = marshal.loads(_payload)
_ENTRY = __PYTHUB_ENTRY__
'''


def boot_runner_source(entry: str, info=None, embed: bool = False,
                       static: bool = False) -> str:
    """生成 mem/embed/static 内存执行引导器源码（作为 app.boot 条目归档）。

    info: 打包信息字典（需求 63），经占位符内嵌进引导器，
    运行时挂载到 sys.pythub_packaged / sys.pythub_info。
    embed=True（需求 64: 内嵌解释器模式）: 引导器在 bootex 进程内经
    dlopen(libpython) 初始化的嵌入式解释器中执行——prologue 改为从
    自身归档解析内存载荷（无 fd 3 管道），应用参数经
    PYTHUB_EMBED_ARGV（base64）恢复，尾部包装 SystemExit 以 os._exit
    回传退出码。
    static=True（需求 64 第四档: 静态内嵌模式）: 解释器静态链接进入口
    二进制，stdlib 已由引导器注册为 frozen 内存模块（零落盘）——prologue
    与 embed 同款（从自身归档解析载荷），无需 libpython 路径覆写
    （sys.executable = 入口二进制，语义即"解释器在二进制内"）。"""
    if static:
        src = (BOOT_RUNNER_PROLOGUE_STATIC + _BOOT_RUNNER_BODY_EMBED)
    elif embed:
        src = (BOOT_RUNNER_PROLOGUE_EMBED + _BOOT_RUNNER_BODY_EMBED)
    else:
        src = BOOT_RUNNER_PROLOGUE_MEM + BOOT_RUNNER_BODY
    # 模板含大量 Python 花括号（字典推导等），用占位符替换而非 str.format
    return src.replace("__PYTHUB_ENTRY__", repr(entry)) \
              .replace("__PYTHUB_INFO__", repr(info or {}))


# 字节码编译助手：在目标运行时解释器中执行（字节码与 CPython 版本强绑定）。
# 读取清单（files: 模块 module/path/is_pkg；data: 资源 rel/path），
# 输出单体裸 marshal 文件 {"modules": {...}, "data": {...}}，由引导器内存还原。
COMPILE_HELPER = '''\
import io, json, marshal, sys

manifest, outpath = sys.argv[1], sys.argv[2]
with io.open(manifest, "r", encoding="utf-8") as f:
    manifest = json.load(f)

out = {"modules": {}, "data": {}}
for item in manifest["files"]:
    with io.open(item["path"], "r", encoding="utf-8") as f:
        src = f.read()
    code = compile(src, item["path"], "exec", dont_inherit=True)
    out["modules"][item["module"]] = (marshal.dumps(code), bool(item["is_pkg"]))
for item in manifest.get("data", []):
    with io.open(item["path"], "rb") as f:
        out["data"][item["rel"]] = f.read()

with io.open(outpath, "wb") as f:
    f.write(marshal.dumps(out))
'''


def write_helper(work) -> Path:
    """落盘编译助手脚本，返回其路径。"""
    path = Path(work) / "mem_compile_helper.py"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(COMPILE_HELPER)
    return path
