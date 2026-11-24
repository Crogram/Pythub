"""Pythub —— 开源的基于 Python 的 Python 应用打包工具。

版权归属开源组织 CROGRAM (https://crogram.org)
"""

__author__ = "CROGRAM"
__url__ = "https://crogram.org"
__project__ = "crogram-pythub"  # 与 pyproject.toml [project].name 一致
_FALLBACK_VERSION = "0.1.1"


def _resolve_version() -> str:
    """版本号与 pyproject.toml 保持一致: 已安装包元数据 > 源码树 pyproject.toml > 兜底值。"""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _meta_version

    try:
        return _meta_version(__project__)
    except PackageNotFoundError:
        pass
    try:  # 源码直接运行（未安装）时从仓库根的 pyproject.toml 读取
        import tomllib
        from pathlib import Path

        pp = Path(__file__).resolve().parents[2] / "pyproject.toml"
        with open(pp, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        pass
    return _FALLBACK_VERSION


__version__ = _resolve_version()

SUPPORTED_PLATFORMS = ("macos", "linux", "windows")
SUPPORTED_ARCHS = ("x86_64", "aarch64")
SUPPORTED_PYTHON = ("3.10", "3.11", "3.12", "3.13", "3.14", "3.15", "3.16")

# 各平台可用输出格式（按优先级排序）
# exe:  自研原生单文件 PE（Windows；免装 Python；仅纯 Python 模块）
# pexe: 自研原生单文件可执行（macOS Mach-O / Linux ELF；免装 Python；仅纯 Python 模块）
# deb:  Debian 软件包（Linux；进系统包管理器可卸载；纯标准库生成、全平台交叉）
# rpm:  RPM 软件包（Linux；自研 cpio/header 纯标准库生成、全平台交叉；mac/linux
#       构建机亦可选 rpmbuild 引擎，见 BuildSpec.rpm_engine）
# mem:  exe/pexe 的执行模式开关（非输出类型，见 BuildSpec.mem）——开启后源码以
#       字节码形式内嵌、全程内存执行不落盘，产物后缀 .mem.exe / .mem.pexe；
#       数据文件随内存资源条目内嵌，经 pythub.memres 访问；支持自打包
PLATFORM_FORMATS = {
    "macos": ["app", "dmg", "pkg", "tar.gz", "pexe", "dir", "pyz"],
    "linux": ["pexe", "tar.gz", "run", "appimage", "deb", "rpm", "dir", "pyz"],
    "windows": ["exe", "exe.zip", "dir", "pyz"],
}

DEFAULT_FORMAT = {"macos": "app", "linux": "pexe", "windows": "exe"}
