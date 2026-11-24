"""第三方打包引擎适配层。

在默认 pythub 引擎（自研、支持跨平台交叉打包；旧名 zipapp 保留为别名）之外，
适配本机平台的三方引擎:

- pyinstaller : PyInstaller        -> onefile/onedir/app/dir
- cx_freeze   : cx_Freeze          -> onedir/dir
- nuitka      : Nuitka             -> onefile/onedir/dir/app
- setuptools  : setuptools + pip   -> wheel
- py2exe      : py2exe（仅 Windows）-> onedir/dir
- py2app      : py2app（仅 macOS）  -> app/dir

三方引擎均在本机平台运行（不下载嵌入式运行时，产物内嵌本机 Python）；
跨平台请使用默认 pythub 引擎（--engine pythub）。

引擎可在 pythub.toml 中传参:

    [pyinstaller]
    # 具名选项（借鉴旧版 Pythub Builder）:
    icon = "app.ico"          # 图标（.ico/.icns/.png）
    version_file = "ver.rc"   # Windows VERSIONINFO 版本信息文件
    upx = false               # false 时禁用 UPX 压缩（默认启用，需已安装 UPX）
    # 透传参数:
    extra_args = ["--add-data", "data:.", "--hidden-import", "requests"]

    [nuitka]
    icon = "app.png"
    extra_args = ["--include-package", "requests"]

    [cx_freeze]
    icon = "app.ico"

    [py2app]
    icon = "app.icns"
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pythub.builder import BuildSpec
from pythub.config import load_config
from pythub.runtime import current_platform
from pythub.utils import temp_dir

# 引擎注册表: 第三方模块名（None = 无需安装）、支持平台、支持格式
ENGINES: Dict[str, dict] = {
    "pyinstaller": {"module": "PyInstaller", "fmts": ("onefile", "onedir", "app", "dir")},
    "cx_freeze": {"module": "cx_Freeze", "fmts": ("onedir", "dir")},
    "nuitka": {"module": "nuitka", "fmts": ("onefile", "onedir", "dir", "app")},
    "setuptools": {"module": None, "fmts": ("wheel",)},
    "py2exe": {"module": "py2exe", "platforms": ("windows",), "fmts": ("onedir", "dir")},
    "py2app": {"module": "py2app", "platforms": ("macos",), "fmts": ("app", "dir")},
}

ENGINE_NAMES: Tuple[str, ...] = (
    "pyinstaller", "cx_freeze", "nuitka", "setuptools", "py2exe", "py2app",
)


def engine_available(name: str) -> bool:
    mod = ENGINES[name]["module"]
    if mod is None:
        return True
    return importlib.util.find_spec(mod) is not None


def _normalize_engine(name: str) -> str:
    return (name or "").strip().lower().replace("-", "_")


def _engine_cfg(spec: BuildSpec, engine: str) -> dict:
    """读取 pythub.toml [engine] 段配置。"""
    cfg = load_config(spec.project_dir).get(engine, {})
    return cfg if isinstance(cfg, dict) else {}


def _engine_extra_args(spec: BuildSpec, engine: str) -> List[str]:
    """[engine] 段的 extra_args（透传底层工具）。"""
    args = _engine_cfg(spec, engine).get("extra_args", []) or []
    return [str(a) for a in args]


def _engine_named_args(spec: BuildSpec, engine: str) -> List[str]:
    """借鉴旧版 Pythub Builder 的具名配置项 → 引擎参数映射。

    支持: icon（图标 .ico/.icns/.png）、version_file（Windows VERSIONINFO .rc）、
    upx（bool，False 时禁用 UPX 压缩）。
    """
    cfg = _engine_cfg(spec, engine)
    out: List[str] = []
    plat = current_platform()

    icon = cfg.get("icon")
    vfile = cfg.get("version_file")
    upx = cfg.get("upx")

    if engine == "pyinstaller":
        if icon:
            out += ["--icon", str(_abs_path(spec.project_dir, icon))]
        if vfile:
            out += ["--version-file", str(_abs_path(spec.project_dir, vfile))]
        if upx is False:
            out.append("--noupx")
    elif engine == "cx_freeze":
        # cx_Freeze 7.x Executable(icon=...)
        # 由 _build_cx_freeze 处理，这里不产参数
        pass
    elif engine == "nuitka":
        if icon:
            p = _abs_path(spec.project_dir, icon)
            if plat == "windows":
                out.append(f"--windows-icon-from-ico={p}")
            elif plat == "macos":
                out.append(f"--macos-app-icon={p}")
    elif engine == "py2app":
        # iconfile 由 _build_py2app 处理
        pass
    return out


def _abs_path(project_dir: Path, p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (project_dir / path)


def _require_native(spec: BuildSpec) -> None:
    if spec.platform != current_platform():
        raise RuntimeError(
            f"引擎 {spec.engine} 仅支持本机平台 ({current_platform()})，"
            f"跨平台请使用默认 pythub 引擎（--engine pythub）。"
        )


def _run(cmd: List[str], logger: logging.Logger, cwd: Optional[Path] = None) -> None:
    logger.info("执行: %s%s", " ".join(cmd), f" (cwd={cwd})" if cwd else "")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None)
    if proc.returncode != 0:
        logger.error(proc.stdout[-3000:] if proc.stdout else "")
        raise RuntimeError(proc.stderr[-3000:] or f"命令失败（exit {proc.returncode}）")


def _resolve_artifact(out: Path, pats: List[str]) -> Optional[Path]:
    """按模式在输出目录查找产物（先精确后模糊）。"""
    for pat in pats:
        p = out / pat
        if p.exists():
            return p
    # 兜底: 目录里第一个近似命名（考虑引擎用入口脚本名命名的情况）
    for pat in pats:
        ext = Path(pat).suffix
        stem = Path(pat).stem
        for p in sorted(out.iterdir()):
            if (ext and p.name.endswith(ext)) or p.name.startswith(stem):
                return p
    return None


def _run_setup_script(spec: BuildSpec, work: Path, body: str,
                      cmd: List[str], logger: logging.Logger) -> None:
    """在临时目录生成 setup.py 并执行（cx_Freeze / py2exe / py2app 共用）。"""
    (work / "setup.py").write_text(body, encoding="utf-8")
    _run(cmd, logger, cwd=work)
    shutil.rmtree(work / "build", ignore_errors=True)


def _out_root(spec: BuildSpec) -> Path:
    """三方引擎的内部构建目录（隐藏，构建后由 _finalize 平铺到 dist/）。

    需求 36: dist/ 下不再有第一级版本目录，三方引擎先在隐藏目录内产出，
    最终产物经 _finalize 重命名后平铺到 dist/。
    """
    out = spec.output / (
        f".build-{spec.engine}-{_tag(spec)}"
    )
    out.mkdir(parents=True, exist_ok=True)
    return out


def _tag(spec: BuildSpec) -> str:
    """产物文件名标注: {name}-{version}-{python}-{platform}-{arch}（需求 36）。

    应用版本统一去掉前导 v/V（如 v0.1.1 -> 0.1.1）。
    """
    ver = spec.version[1:] if spec.version[:1] in ("v", "V") else spec.version
    return f"{spec.name}-{ver}-{spec.python}-{spec.platform}-{spec.arch}"


def _finalize(spec: BuildSpec, artifact: Optional[Path]) -> Optional[Path]:
    """把三方引擎产物平铺重命名到 dist/ 下（需求 36）。

    命名: 目录产物 -> dist/{name}-{version}-{python}-{platform}-{arch}/，
    文件产物 -> dist/{tag}{原后缀}（如 .exe / .app）。
    """
    if artifact is None:
        return None
    spec.output.mkdir(parents=True, exist_ok=True)
    tag = _tag(spec)
    if artifact.is_dir():
        dest = spec.output / tag
        if dest.exists():
            shutil.rmtree(dest)
    else:
        dest = spec.output / (tag + artifact.suffix)
        if dest.exists():
            dest.unlink()
    shutil.move(str(artifact), str(dest))
    # 构建目录已空则顺带移除
    try:
        artifact.parent.rmdir()
    except OSError:
        pass
    return dest


# ----------------------------------------------------------------- PyInstaller

def _build_pyinstaller(spec: BuildSpec, fmt: str, logger: logging.Logger) -> List[Path]:
    if not engine_available("pyinstaller"):
        raise RuntimeError("PyInstaller 引擎需要先安装: pip install pyinstaller")
    out = _out_root(spec)
    work = out / ".build-pyinstaller"
    work.mkdir(exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", spec.name,
        "--distpath", str(out),
        "--workpath", str(work / "work"),
        "--specpath", str(work / "spec"),
        "--onefile" if fmt == "onefile" else "--onedir",
    ]
    if spec.windowed and sys.platform in ("darwin", "win32"):
        cmd.append("--windowed")
    if sys.platform == "darwin" and spec.windowed and spec.bundle_id.strip():
        # 与 pythub 引擎同步: macOS .app 自定义 CFBundleIdentifier
        cmd.append(f"--osx-bundle-identifier={spec.bundle_id.strip()}")
    cmd += _engine_named_args(spec, "pyinstaller") + _engine_extra_args(spec, "pyinstaller")
    cmd.append(str((spec.project_dir / spec.entry).resolve()))

    _run(cmd, logger)
    shutil.rmtree(work, ignore_errors=True)

    if fmt == "onefile":
        a = _resolve_artifact(out, [spec.name, f"{spec.name}.exe"])
    elif sys.platform == "darwin" and spec.windowed:
        a = _resolve_artifact(out, [f"{spec.name}.app"])
    else:
        a = _resolve_artifact(out, [spec.name, f"{spec.name}.exe"])
    if not a:
        raise RuntimeError(f"未找到 PyInstaller 产物（输出目录: {out}）")
    return [_finalize(spec, a)]


# ------------------------------------------------------------------- cx_Freeze

def _build_cx_freeze(spec: BuildSpec, fmt: str, logger: logging.Logger) -> List[Path]:
    if not engine_available("cx_freeze"):
        raise RuntimeError("cx_Freeze 引擎需要先安装: pip install cx_Freeze")
    out = _out_root(spec)
    target = out / spec.name
    entry = (spec.project_dir / spec.entry).resolve()

    base = '"gui"' if (spec.windowed and sys.platform == "win32") else "None"
    icon = _engine_cfg(spec, "cx_freeze").get("icon")
    icon_arg = f", icon=r'{_abs_path(spec.project_dir, icon)}'" if icon else ""
    body = f"""\
from cx_Freeze import setup, Executable
setup(
    name={spec.name!r}, version={spec.version!r},
    executables=[Executable(script=r"{entry}",
                            target_name={spec.name!r}, base={base}{icon_arg})],
)
"""
    with temp_dir(f"pythub-cxfreeze-{spec.name}-") as td:
        _run_setup_script(
            spec, Path(td), body,
            [sys.executable, "setup.py", "build_exe",
             "--build-exe", str(target)] + _engine_extra_args(spec, "cx_freeze"),
            logger,
        )
    if not target.exists():
        raise RuntimeError(f"未找到 cx_Freeze 产物（输出目录: {out}）")
    return [_finalize(spec, target)]


# ---------------------------------------------------------------------- Nuitka

def _build_nuitka(spec: BuildSpec, fmt: str, logger: logging.Logger) -> List[Path]:
    if not engine_available("nuitka"):
        raise RuntimeError("Nuitka 引擎需要先安装: pip install nuitka")
    out = _out_root(spec)
    exe_name = f"{spec.name}.exe" if sys.platform == "win32" else spec.name

    cmd = [
        sys.executable, "-m", "nuitka",
        "--output-dir=" + str(out),
        "--output-filename=" + exe_name,
        "--remove-output",
    ]
    if fmt == "onefile":
        cmd.append("--onefile")
    else:
        cmd.append("--standalone")
    if spec.windowed:
        cmd.append("--disable-console")
    if fmt == "app" and sys.platform == "darwin":
        cmd.append("--macos-create-app-bundle")
    cmd += _engine_named_args(spec, "nuitka") + _engine_extra_args(spec, "nuitka")
    cmd.append(str((spec.project_dir / spec.entry).resolve()))

    _run(cmd, logger)
    if fmt == "app" and sys.platform == "darwin":
        a = _resolve_artifact(out, [f"{spec.name}.app"])
    elif fmt == "onefile":
        a = _resolve_artifact(out, [exe_name])
    else:
        a = _resolve_artifact(out, [f"{spec.name}.dist", exe_name])
    if not a:
        raise RuntimeError(f"未找到 Nuitka 产物（输出目录: {out}）")
    return [_finalize(spec, a)]


# ------------------------------------------------------------------- setuptools

def _build_setuptools(spec: BuildSpec, fmt: str, logger: logging.Logger) -> List[Path]:
    # wheel 文件名本身已含名称/版本/平台标注（dist 规范命名），
    # 直接平铺到 dist/（需求 36）
    out = spec.output
    out.mkdir(parents=True, exist_ok=True)
    proj = spec.project_dir

    if (proj / "setup.py").is_file() or (proj / "pyproject.toml").is_file():
        _run([sys.executable, "-m", "pip", "wheel", "--no-deps",
              "-w", str(out), str(proj)], logger)
    else:
        # 项目没有打包元数据: 生成最小 setup.py（包目录 -> packages，单脚本 -> py_modules）
        packages, py_modules = [], []
        for p in sorted(proj.iterdir()):
            if p.name.startswith("."):
                continue
            if p.is_dir() and (p / "__init__.py").is_file():
                packages.append(p.name)
            elif p.is_file() and p.suffix == ".py" and p.name != "setup.py":
                py_modules.append(p.stem)
        body = (
            "from setuptools import setup\n"
            f"setup(name={spec.name!r}, version={spec.version!r},\n"
            f"      packages={packages!r}, py_modules={py_modules!r},\n"
            f"      package_dir={{'': r'{proj}'}})\n"
        )
        with temp_dir(f"pythub-setuptools-{spec.name}-") as td:
            (Path(td) / "setup.py").write_text(body, encoding="utf-8")
            _run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                  "-w", str(out), td], logger)

    wheels = sorted(out.glob(f"{spec.name}-*.whl")) or sorted(out.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"未找到 setuptools 产物（输出目录: {out}）")
    return wheels


# ----------------------------------------------------------------------- py2exe

def _build_py2exe(spec: BuildSpec, fmt: str, logger: logging.Logger) -> List[Path]:
    if not engine_available("py2exe"):
        raise RuntimeError("py2exe 引擎需要先安装: pip install py2exe")
    out = _out_root(spec)
    entry = (spec.project_dir / spec.entry).resolve()

    # py2exe 0.12+ 用 freeze API；旧版回退 distutils 命令
    kind = "windows" if spec.windowed else "console"
    body = f"""\
import sys
try:
    from py2exe import freeze
    freeze({kind}=[{{"script": r"{entry}", "dest_base": {spec.name!r}}}],
           options={{"dist_dir": r"{out}"}})
except ImportError:
    from distutils.core import setup
    import py2exe  # noqa: F401
    setup({kind}=[{{"script": r"{entry}", "dest_base": {spec.name!r}}}],
          options={{"py2exe": {{"dist_dir": r"{out}"}}}}, zipfile=None)
"""
    with temp_dir(f"pythub-py2exe-{spec.name}-") as td:
        _run_setup_script(spec, Path(td), body,
                          [sys.executable, "setup.py"]
                          + _engine_extra_args(spec, "py2exe"), logger)
    a = _resolve_artifact(out, [f"{spec.name}.exe"])
    if not a:
        raise RuntimeError(f"未找到 py2exe 产物（输出目录: {out}）")
    return [_finalize(spec, a)]


# ----------------------------------------------------------------------- py2app

def _build_py2app(spec: BuildSpec, fmt: str, logger: logging.Logger) -> List[Path]:
    if not engine_available("py2app"):
        raise RuntimeError("py2app 引擎需要先安装: pip install py2app")
    out = _out_root(spec)
    entry = (spec.project_dir / spec.entry).resolve()

    icon = _engine_cfg(spec, "py2app").get("icon")
    opts = {"iconfile": str(_abs_path(spec.project_dir, icon))} if icon else {}
    if spec.bundle_id.strip():
        # 与 pythub 引擎同步: macOS .app 自定义 CFBundleIdentifier
        opts["plist"] = {"CFBundleIdentifier": spec.bundle_id.strip()}
    body = f"""\
from setuptools import setup
setup(
    app=[{{"script": r"{entry}", "dest_base": {spec.name!r}}}],
    options={{"py2app": {opts!r}}},
)
"""
    with temp_dir(f"pythub-py2app-{spec.name}-") as td:
        _run_setup_script(
            spec, Path(td), body,
            [sys.executable, "setup.py", "py2app", "--dist-dir", str(out)]
            + _engine_extra_args(spec, "py2app"),
            logger,
        )
    a = _resolve_artifact(out, [f"{spec.name}.app"])
    if not a:
        raise RuntimeError(f"未找到 py2app 产物（输出目录: {out}）")
    return [_finalize(spec, a)]
    return [a]


# ----------------------------------------------------------------------- 分发入口

def build_engine(spec: BuildSpec, logger: logging.Logger) -> List[Path]:
    """分发到指定三方引擎（本机平台）。"""
    engine = _normalize_engine(spec.engine)
    if engine not in ENGINES:
        raise ValueError(f"未知引擎: {spec.engine}，可用: {', '.join(ENGINE_NAMES)}")

    platforms = ENGINES[engine].get("platforms")
    if platforms and spec.platform not in platforms:
        raise ValueError(
            f"引擎 {engine} 仅支持平台: {', '.join(platforms)}（当前目标: {spec.platform}）"
        )
    _require_native(spec)

    fmts = ENGINES[engine]["fmts"]
    fmt = spec.target_fmt
    if fmt not in fmts:
        logger.warning(
            "引擎 %s 不支持格式 %s，已改用 %s（支持: %s）",
            engine, fmt, fmts[0], ", ".join(fmts),
        )
        fmt = fmts[0]

    return globals()[f"_build_{engine}"](spec, fmt, logger)
