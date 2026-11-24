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


def boot_runner_source(entry: str, info=None) -> str:
    """生成 mem 内存执行引导器源码（作为 app.boot 条目归档）。

    info: 打包信息字典（需求 63），经占位符内嵌进引导器，
    运行时挂载到 sys.pythub_packaged / sys.pythub_info。"""
    # 模板含大量 Python 花括号（字典推导等），用占位符替换而非 str.format
    src = (BOOT_RUNNER_PROLOGUE_MEM + BOOT_RUNNER_BODY) \
        .replace("__PYTHUB_ENTRY__", repr(entry)) \
        .replace("__PYTHUB_INFO__", repr(info or {}))
    return src


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
