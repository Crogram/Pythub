"""Pythub 打包引擎主流程。"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from pythub import DEFAULT_FORMAT, PLATFORM_FORMATS
from pythub.analyzer import analyze
from pythub.config import app_config, load_requirements
from pythub.deps import download_wheels, unpack_wheels
from pythub import formats
from pythub.runtime import fetch_runtime, interpreter_path
from pythub.utils import (
    ProgressLogger,
    copy_tree,
    make_tar_gz,
    make_zip,
    set_executable,
    temp_dir,
    zip_dir,
)

EXCLUDE_DIRS = {
    "__pycache__", ".git", ".hg", ".svn", ".venv", "venv", "env",
    "build", "dist", ".pythub", "node_modules", ".idea", ".vscode",
    ".mypy_cache", ".pytest_cache", ".tox", ".eggs",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd.bak"}

# 自研引擎名（zipapp 为旧名，永久别名）；其余引擎名走三方适配（engines.py）
SELF_ENGINES = {"pythub", "zipapp"}
DEFAULT_ENGINE = "pythub"

# 可执行产物后缀（chmod = true 时加执行权限；目录产物与分发归档不加）
EXEC_SUFFIXES = {".pexe", ".pyz", ".run", ".exe", ".appimage"}


def _tag(spec: "BuildSpec", arch: Optional[str] = None) -> str:
    """产物文件名标注: {name}-{version}-{python}-{platform}-{arch}（需求 36）。

    dist/ 下不再有第一级版本目录，所有最终产物平铺到 dist/，
    应用版本/Python/平台/架构差异直接体现在文件名上。
    应用版本统一去掉前导 v/V（如 v0.1.1 -> 0.1.1）。
    """
    ver = spec.version[1:] if spec.version[:1] in ("v", "V") else spec.version
    return f"{spec.name}-{ver}-{spec.python}-{spec.platform}-{arch or spec.arch}"


def _wrap_mac_app(pexe: Path, spec: "BuildSpec", logger: logging.Logger) -> Path:
    """把构建好的 mac pexe 包裹成最小 .app 壳（Finder 直接识别为应用）。

    结构: <name>{-pexe|-mem}.app/Contents/{Info.plist, MacOS/<pexe>}，pexe 以复制
    方式内嵌为 CFBundleExecutable（原 .pexe 产物保留不动）。壳名带 -pexe/-mem
    后缀，避免与 -f app（pythub 引擎版，同名 {name}.app）互相覆盖。仅 macOS 生效；
    经 Finder 启动的进程不挂接终端、stdout 不可见，适合 GUI 应用（CLI 请在
    终端运行或改用 .command 启动器）。
    """
    app = pexe.parent / (
        f"{_tag(spec)}{'-mem' if spec.mem else '-pexe'}.app"
    )
    if app.exists():
        shutil.rmtree(app)
    macos_dir = app / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    bin_name = pexe.name
    shutil.copy2(pexe, macos_dir / bin_name)
    (macos_dir / bin_name).chmod(0o755)
    ident = (spec.bundle_id or "").strip()
    # 防御: 仅接受合法 Bundle ID 字符，非法（如误传提示文案）回退自动生成
    if ident and not re.fullmatch(r"[A-Za-z0-9.\-]+", ident):
        ident = ""
    ident = ident or "org.crogram." + "".join(
        ch if ch.isalnum() or ch in ".-" else "-" for ch in spec.name.lower()
    )
    info = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>{bin_name}</string>
    <key>CFBundleIdentifier</key><string>{ident}</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleName</key><string>{spec.name}</string>
    <key>CFBundleDisplayName</key><string>{spec.name}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>{spec.version}</string>
    <key>CFBundleVersion</key><string>{spec.version}</string>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""
    (app / "Contents" / "Info.plist").write_text(info, encoding="utf-8")
    logger.info("  已打包 .app 壳: %s（内嵌 %s，Finder 可直接识别为应用）", app, bin_name)
    return app


def _apply_chmod(artifacts: List[Path], logger: logging.Logger) -> List[Path]:
    """需求 29: 为 run/pexe/pyz 等可执行产物自动添加执行权限（chmod = true）。

    只处理文件产物且后缀在 EXEC_SUFFIXES 内（.app 等目录产物、tar.gz/dmg/
    pkg/exe.zip 等分发归档不处理）；Windows 目标平台上属无意义操作但无害。
    mem 变体（.mem.exe / .mem.pexe）后缀仍在集合内，一并覆盖。
    """
    for a in artifacts:
        if a.is_file() and a.suffix.lower() in EXEC_SUFFIXES:
            a.chmod(a.stat().st_mode | 0o111)
            logger.info("  已添加执行权限 (+x): %s", a)
    return artifacts


@dataclass
class BuildSpec:
    project_dir: Path
    name: str = ""
    entry: str = ""
    version: str = "0.1.0"
    requirements: List[str] = field(default_factory=list)
    platform: str = "linux"
    arch: str = "x86_64"
    python: str = "3.12"
    fmt: str = ""
    output: Path = Path("dist")
    engine: str = "pythub"      # 自研引擎（旧名 zipapp，作为别名永久兼容）
    windowed: bool = False
    mem: bool = False           # mem 内存执行模式（仅 exe/pexe 生效）: 开启后源码以
                                # 字节码内嵌、全程内存执行不落盘，产物后缀
                                # .mem.exe / .mem.pexe；关闭则为常规磁盘执行
    sign: bool = False          # Authenticode 签名（仅 exe 格式生效）
    sign_cert: str = ""         # 签名证书 PEM 路径（空 = 自动自签）
    sign_key: str = ""          # 签名私钥 PEM 路径
    chmod: bool = False         # 自动为 run/pexe/pyz 等可执行产物添加执行权限
                                # （chmod +x，见 _apply_chmod）
    app_wrap: bool = False      # mac 平台把构建好的 pexe 再次包裹成 .app 壳
                                # （Finder 识别为应用，适合 GUI；原 .pexe 产物保留，
                                #  见 _wrap_mac_app）
    bundle_id: str = ""         # macOS .app 的 CFBundleIdentifier（pythub 引擎的
                                # app/dmg/pkg 与 app_wrap 壳、PyInstaller/py2app
                                # 引擎同步传递；空则自动生成 org.crogram.<name>）
    description: str = ""       # 应用描述（deb 包 Description / 桌面入口 Comment）
    maintainer: str = ""        # 维护者（deb 包 Maintainer，格式 "Name <email>"；
                                # 空则回退 pythub.toml [app].author，再空用内置默认）
    license: str = ""           # 许可证（rpm 包 License tag；空则 "Unknown"）
    rpm_engine: str = "auto"    # rpm 包引擎: auto（有 rpmbuild 则用之）/self（自研，
                                # 三端可交叉）/rpmbuild（仅 mac/linux 构建机）

    @property
    def target_fmt(self) -> str:
        return self.fmt or DEFAULT_FORMAT.get(self.platform, "dir")


def spec_from_project(
    project_dir: Path,
    logger: logging.Logger,
    overrides: Optional[dict] = None,
) -> BuildSpec:
    cfg = app_config(project_dir, overrides)
    reqs = list(cfg.get("requirements") or [])
    reqs += load_requirements(project_dir)
    if overrides and overrides.get("requirements"):
        reqs += list(overrides["requirements"])
    return BuildSpec(
        project_dir=project_dir,
        name=cfg["name"],
        entry=cfg["entry"],
        version=cfg["version"],
        requirements=reqs,
        platform=cfg.get("platform", "linux"),
        arch=cfg.get("arch", "x86_64"),
        python=cfg.get("python", "3.12"),
        fmt=cfg.get("fmt", ""),
        output=Path(cfg.get("output", "dist")),
        engine=str(cfg.get("engine") or "pythub").strip().lower(),
        windowed=bool(cfg.get("windowed", False)),
        mem=bool(cfg.get("mem", False)),
        sign=bool(cfg.get("sign", False)),
        sign_cert=str(cfg.get("sign_cert", "")),
        sign_key=str(cfg.get("sign_key", "")),
        chmod=bool(cfg.get("chmod", False)),
        app_wrap=bool(cfg.get("app_wrap", False)),
        bundle_id=str(cfg.get("bundle_id", "") or ""),
        description=str(cfg.get("description", "") or ""),
        maintainer=str(cfg.get("maintainer") or cfg.get("author") or ""),
        license=str(cfg.get("license", "") or ""),
        rpm_engine=str(cfg.get("rpm_engine") or "auto"),
    )


def build(spec: BuildSpec, logger: logging.Logger) -> List[Path]:
    """执行打包，返回产物文件/目录列表。"""
    # 三方引擎（本机平台）: 格式/平台校验由 engines 层完成，提前分发
    if spec.engine not in SELF_ENGINES:
        from pythub.engines import build_engine
        artifacts = build_engine(spec, logger)
        return _apply_chmod(artifacts, logger) if spec.chmod else artifacts

    if spec.platform not in PLATFORM_FORMATS:
        raise ValueError(f"不支持的平台: {spec.platform}")
    if spec.target_fmt not in PLATFORM_FORMATS[spec.platform]:
        raise ValueError(
            f"平台 {spec.platform} 不支持格式 {spec.target_fmt}，"
            f"可用: {', '.join(PLATFORM_FORMATS[spec.platform])}"
        )

    entry = (spec.project_dir / spec.entry).resolve()
    if not entry.exists():
        raise FileNotFoundError(f"入口脚本不存在: {entry}")

    if spec.target_fmt in ("exe", "pexe") and spec.mem:
        artifacts = _build_mem(spec, entry, logger)
        return _apply_chmod(artifacts, logger) if spec.chmod else artifacts

    logger.info("=" * 60)
    logger.info(
        "Pythub 开始打包: %s -> %s (%s/%s, Python %s, 引擎 %s)",
        spec.name, spec.target_fmt, spec.platform, spec.arch, spec.python, spec.engine,
    )

    # pexe 跨平台/架构: 安装了 zig 时支持全平台交叉编译引导器；无 zig 时
    # 架构与本机不符自动改用本机架构（而非直接报错），Apple Silicon 默认
    # 配置也可直接打包。必须在运行时下载之前解析，保证内嵌解释器与引导器
    # 架构一致。
    if spec.target_fmt == "pexe":
        host_arch = formats.native_host_arch()
        if spec.arch != host_arch and not formats.find_zig():
            logger.warning(
                "未安装 zig，pexe 仅支持本机架构（本机: %s），已自动改用（原目标: %s）",
                host_arch, spec.arch,
            )
            spec.arch = host_arch

    artifacts = _build_zipapp(spec, entry, logger)
    return _apply_chmod(artifacts, logger) if spec.chmod else artifacts


# ------------------------------------------------- pythub 引擎（旧名 zipapp）

def _build_zipapp(spec: BuildSpec, entry: Path, logger: logging.Logger) -> List[Path]:
    with temp_dir(f"pythub-{spec.name}-") as td:
        work = Path(td)

        # 1. 依赖分析
        logger.info("[1/6] 分析依赖...")
        info = analyze(entry, spec.project_dir)
        logger.info(
            "  标准库模块 %d 个，第三方模块 %d 个",
            len(info["stdlib"]), len(info["thirdparty_modules"]),
        )
        if info["unresolved"]:
            logger.warning(
                "  未能映射为发行包的模块（如需打包请写入 requirements.txt）: %s",
                ", ".join(info["unresolved"]),
            )
        reqs = list(dict.fromkeys(
            (spec.requirements or []) + info["thirdparty_packages"]
        ))

        # 2. 下载目标平台依赖 wheel（按目标平台过滤组件）
        wheels: List[Path] = []
        if reqs:
            logger.info("[2/6] 下载目标平台第三方组件...")
            wheels = download_wheels(
                reqs, spec.platform, spec.arch, spec.python, work / "wheels", logger
            )
            unpack_wheels(wheels, work / "lib")
            logger.info("  已集成 %d 个 wheel", len(wheels))
        else:
            logger.info("[2/6] 无第三方依赖，跳过")

        # 3. 获取目标平台嵌入式 Python 运行时
        logger.info("[3/6] 准备目标平台嵌入式 Python 运行时...")
        runtime = fetch_runtime(spec.platform, spec.arch, spec.python, logger)
        _check_interpreter(runtime, spec.platform)

        # 4. 生成 app.pyz
        logger.info("[4/6] 组装 app.pyz（源码 + 组件库 + 自举）...")
        pyz = work / "app.pyz"
        _build_pyz(spec, entry, work / "lib" if (work / "lib").exists() else None, pyz)

        # 5. 组装目标产物
        logger.info("[5/6] 组装平台产物 (%s) ...", spec.target_fmt)
        artifacts = _assemble(spec, runtime, pyz, work, logger)

        # 6. 完成
        logger.info("[6/6] 打包完成:")
        for a in artifacts:
            logger.info("  -> %s", a)
        return artifacts


def _check_interpreter(runtime: Path, platform: str) -> None:
    py = interpreter_path(runtime, platform)
    if not py.exists():
        raise RuntimeError(f"运行时解释器缺失: {py}")


# ------------------------------------------------------------------ mem 格式

def _mem_module_name(arc: str) -> str:
    """归档相对路径 -> 模块名（__init__ 即包）。"""
    parts = Path(arc).with_suffix("").parts
    if parts[-1] == "__init__":
        return ".".join(parts[:-1])
    return ".".join(parts)


def _find_host_python(pyver: str) -> Optional[str]:
    """查找本机指定版本的 Python 解释器（Windows 交叉编译 mem 时编译字节码用）。"""
    want = tuple(int(x) for x in pyver.split(".")[:2])
    candidates = []
    if sys.version_info[:2] == want:
        candidates.append(sys.executable)
    candidates.append(shutil.which(f"python{want[0]}{want[1]}"))
    candidates.append(shutil.which(f"python{want[0]}.{want[1]}"))
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def _build_mem(spec: BuildSpec, entry: Path, logger: logging.Logger) -> List[Path]:
    """exe/pexe 的 mem 内存执行模式: 单文件原生二进制，应用字节码全程内存执行。

    产物后缀 .mem.exe（Windows exe）/ .mem.pexe（macOS/Linux pexe）。产物 = 自研
    bootex 引导器 + 自研归档（嵌入式运行时 + 引导器源码 + 应用字节码两个
    FLAG_MEM 内存载荷条目）。运行时:

    产物 = 自研 bootex 引导器 + 自研归档（嵌入式运行时 + 引导器源码 +
    应用字节码两个 FLAG_MEM 内存载荷条目）。运行时:
    - 运行时解压到缓存目录（仅首次；解释器二进制必须落盘才能 exec，OS 限制）；
    - 引导器源码经 PYTHUB_BOOT 环境变量注入、应用字节码经 fd 3 管道直传
      解释器，全程内存执行，不产生任何临时 .py/.pyc 文件。

    编译使用目标运行时解释器（marshal/字节码与 CPython 版本强绑定）；
    目标运行时二进制在本机不可执行时（跨平台/跨架构），改用本机同版本
    Python 编译（marshal 跨平台兼容，同版本即可）。仅支持纯 Python 模块。
    引导器为自研 C 源码: 本机平台用 cc 快速编译，跨平台用 zig cc 交叉
    （全平台支持，Linux 目标 musl 静态链接）。
    """
    import json

    from pythub import mempack

    with temp_dir(f"pythub-mem-{spec.name}-") as td:
        work = Path(td)

        # 0. 平台/架构: zig cc 支持全平台交叉（Linux musl 静态 / macOS
        # Mach-O / Windows PE）；无 zig 时 POSIX 目标回退 mingw（仅 Windows）
        # 或本机平台+架构。交叉且缺工具链时提前报错。
        host_platform = formats.native_host_platform()
        host_arch = formats.native_host_arch()
        needs_cross = (host_platform != spec.platform or host_arch != spec.arch)
        if needs_cross and spec.platform != "windows" and not formats.find_zig():
            raise RuntimeError(
                f"交叉构建 mem 二进制到 {spec.platform}/{spec.arch} 需要 zig cc:\n"
                "  macOS: brew install zig | Linux: snap install zig --classic\n"
                "  或设环境变量 PYTHUB_ZIG 指向 zig 可执行文件。"
            )

        # 1. 依赖分析
        logger.info("[1/5] 分析依赖...")
        info = analyze(entry, spec.project_dir)
        reqs = list(dict.fromkeys(
            (spec.requirements or []) + info["thirdparty_packages"]
        ))
        wheels: List[Path] = []
        if reqs:
            logger.info("  下载目标平台第三方组件（仅纯 Python 模块）...")
            wheels = download_wheels(
                reqs, spec.platform, spec.arch, spec.python, work / "wheels", logger
            )
            unpack_wheels(wheels, work / "lib")
        else:
            logger.info("  无第三方依赖，跳过")

        # 2. 纯 Python 校验
        lib_dir = work / "lib" if (work / "lib").exists() else None
        ext_files = []
        if lib_dir:
            ext_files = [p for p in lib_dir.rglob("*")
                         if p.suffix in (".so", ".pyd", ".dylib")]
        if ext_files:
            raise ValueError(
                "mem 格式仅支持纯 Python 模块，检测到 C 扩展 "
                f"{', '.join(p.name for p in ext_files[:3])}；请改用 pythub 引擎其他格式"
            )
        data_files = [p for p in lib_dir.rglob("*")
                      if p.is_file() and p.suffix not in (".py", ".pyc", ".pyi")
                      and "dist-info" not in str(p)] if lib_dir else []
        if data_files:
            logger.warning(
                "  lib 目录存在 %d 个非代码文件，将作为内存资源条目打包"
                "（运行期经 pythub.memres 访问）",
                len(data_files),
            )

        # 3. 运行时（内嵌）+ 编译用解释器
        logger.info("[3/5] 准备目标运行时（内嵌 + 编译字节码）...")
        runtime = fetch_runtime(spec.platform, spec.arch, spec.python, logger)
        # 目标运行时二进制在本机可执行时直接用它编译字节码；否则（跨平台/
        # 跨架构）改用本机同版本 Python——marshal 字节码跨平台兼容，仅需
        # CPython 版本一致。Apple Silicon 经 Rosetta 可执行 x86_64 macOS
        # 运行时，视为可执行。
        rosetta = (spec.platform == "macos" and host_arch == "aarch64"
                   and spec.arch == "x86_64")
        runtime_executable = (host_platform == spec.platform
                              and (spec.arch == host_arch or rosetta))
        if runtime_executable:
            py = interpreter_path(runtime, spec.platform)
            _check_interpreter(runtime, spec.platform)
        else:
            py = _find_host_python(spec.python)
            if not py:
                raise RuntimeError(
                    f"交叉构建 {spec.platform}/{spec.arch} mem 格式需要本机安装 "
                    f"Python {spec.python}（用于编译字节码，版本必须一致）。"
                )
            logger.info(
                "  目标运行时不可在本机执行，使用本机 Python %s 编译字节码"
                "（marshal 跨平台兼容）", spec.python,
            )

        # 4. 生成模块清单并编译为单体裸 marshal 载荷
        pkg_prefix = ""
        if (spec.project_dir / "__init__.py").is_file():
            pkg_prefix = spec.project_dir.name
        files = []
        for rel in _project_files(spec.project_dir):
            if rel.suffix != ".py":
                continue  # mem 仅支持纯 Python 模块
            arc = f"{pkg_prefix}/{rel.as_posix()}" if pkg_prefix else rel.as_posix()
            files.append({
                "module": _mem_module_name(arc),
                "path": str(spec.project_dir / rel),
                "is_pkg": (spec.project_dir / rel).name == "__init__.py",
            })
        if lib_dir:
            for p in sorted(lib_dir.rglob("*.py")):
                rel = p.relative_to(lib_dir)
                files.append({
                    "module": _mem_module_name(rel.as_posix()),
                    "path": str(p),
                    "is_pkg": p.name == "__init__.py",
                })
        entry_rel = Path(spec.entry).as_posix()
        entry_module = _mem_module_name(
            f"{pkg_prefix}/{entry_rel}" if pkg_prefix else entry_rel
        )

        # 数据文件（非 .py）: 一并打包为内存资源条目（不落盘，
        # 运行期经 pythub.memres 访问，见 memres.materialize）
        data = []
        for rel in _project_files(spec.project_dir):
            if rel.suffix != ".py":
                arc = f"{pkg_prefix}/{rel.as_posix()}" if pkg_prefix else rel.as_posix()
                data.append({"rel": arc, "path": str(spec.project_dir / rel)})
        if lib_dir:
            for p in sorted(lib_dir.rglob("*")):
                if p.is_file() and p.suffix not in (".py", ".pyc", ".pyi") \
                        and "dist-info" not in str(p):
                    data.append({
                        "rel": f"lib/{p.relative_to(lib_dir).as_posix()}",
                        "path": str(p),
                    })
        if data:
            total = sum(Path(d["path"]).stat().st_size for d in data)
            logger.info("  内存资源条目 %d 个（%.1f KB）", len(data), total / 1024)

        manifest = work / "manifest.json"
        manifest.write_text(
            json.dumps({"files": files, "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
        helper = mempack.write_helper(work)
        payload_path = work / "payload.marshal"
        logger.info("[4/5] 编译 %d 个模块为内存字节码...", len(files))
        proc = subprocess.run(
            [str(py), str(helper), str(manifest), str(payload_path)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"mem 编译失败（运行时 {spec.python}）: {proc.stderr[-2000:] or proc.stdout[-2000:]}"
            )

        # 5. 组装单文件二进制（引导器 + 运行时 + 内存载荷）
        logger.info("[5/5] 组装单文件二进制（引导器 + 运行时 + 内存载荷）...")
        bundle = work / "bundle"
        bundle.mkdir(parents=True)
        # 保留符号链接（bin/python3 -> python3.X 等）：归档按链接存储
        shutil.copytree(runtime, bundle / "runtime", symlinks=True)

        spec.output.mkdir(parents=True, exist_ok=True)
        dest = spec.output / (
            f"{_tag(spec)}.mem.exe" if spec.platform == "windows"
            else f"{_tag(spec)}.mem.pexe"
        )
        if dest.exists():
            dest.unlink()

        logger.info("  mem 模式: 源码字节码内嵌、全程内存执行（不落盘）")
        boot_src = mempack.boot_runner_source(
            entry_module, info=_appinfo_dict(spec)).encode("utf-8")
        payload = payload_path.read_bytes()
        if spec.platform == "windows":
            formats.make_exe(bundle, dest, spec.arch, spec.python, logger,
                             console=not spec.windowed, app_name=spec.name,
                             mem_boot=boot_src, mem_payload=payload)
        else:
            formats.make_native_exe(bundle, dest, spec.platform, spec.arch,
                                    spec.python, logger,
                                    console=not spec.windowed,
                                    app_name=spec.name,
                                    mem_boot=boot_src, mem_payload=payload)
        os.chmod(dest, 0o755)
        logger.info("打包完成:")
        logger.info("  -> %s", dest)
        if spec.platform == "macos" and spec.app_wrap:
            return [dest, _wrap_mac_app(dest, spec, logger)]
        return [dest]


# 需求 63: 打包/运行信息注入。
# 方式: 不引入独立模块，直接在引导器中把信息挂到标准库 sys 模块上——
#   sys.pythub_packaged = True   （标志: 由 pythub 打包）
#   sys.pythub_info = {...}      （注入的信息字典，runtime 子字段动态探测）
# 应用代码无需 import 任何东西即可访问:
#   import sys
#   if getattr(sys, "pythub_packaged", False):
#       info = sys.pythub_info


def _appinfo_dict(spec: "BuildSpec") -> dict:
    """生成本次打包的静态信息字典（运行时字段由引导器动态探测补充）。"""
    import platform as _platform
    import pythub as _pkg

    return {
        "pythub_version": _pkg.__version__,
        "app": {
            "name": spec.name,
            "version": spec.version,
            "entry": spec.entry,
            "engine": spec.engine,
            "fmt": spec.fmt,
        },
        "target": {
            "platform": spec.platform,
            "arch": spec.arch,
            "python": spec.python,
            "mem": bool(spec.mem),
        },
        "build": {
            "python": "%d.%d.%d" % sys.version_info[:3],
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "host_platform": sys.platform,      # 原生值: darwin/win32/linux
            "host_os": {"darwin": "macos", "win32": "windows"}.get(
                sys.platform, "linux"),         # 规范化: macos/windows/linux
            "host_arch": {
                "x86_64": "x86_64",
                "amd64": "x86_64",
                "i386": "x86",
                "arm64": "aarch64",
            }.get(_platform.machine().lower(), _platform.machine().lower()),
        },
        "project_dir": str(spec.project_dir),
    }


def _build_pyz(spec: BuildSpec, entry: Path, lib_dir: Optional[Path], pyz: Path) -> None:
    """生成 zipapp：项目源码 + lib/ + 自举 __main__.py。"""
    entry_rel = str(Path(spec.entry).as_posix())
    # 项目目录本身是包（含 __init__.py）时，按 "<包名>/" 前缀布局打包，
    # 使包内 `import <包名>` 绝对导入可用（如用 pythub 打包 pythub 自身）
    pkg_prefix = ""
    if (spec.project_dir / "__init__.py").is_file():
        pkg_prefix = spec.project_dir.name
        if not entry_rel.startswith(pkg_prefix + "/"):
            entry_rel = f"{pkg_prefix}/{entry_rel}"
    elif entry_rel == "__main__.py":
        # 入口名与自举脚本冲突：改存 __entry__.py，避免自举覆盖入口造成自我递归
        entry_rel = "__entry__.py"
    bootstrap = f'''\
import os, sys, runpy

_zp = os.path.dirname(os.path.abspath(__file__))
_entry = {entry_rel!r}
_lib = os.path.join(_zp, "lib")
sys.path.insert(0, _zp)

# Pythub 注入打包/运行信息（需求 63: sys.pythub_packaged / sys.pythub_info）
sys.pythub_packaged = True
sys.pythub_info = _PYTHUB_INFO_REPR_
sys.pythub_info["runtime"] = {{
    "python": "%d.%d.%d" % sys.version_info[:3],
    "python_full": sys.version.split()[0],
    "implementation": sys.implementation.name,
    "executable": sys.executable,
    "exe_dir": os.path.dirname(os.path.abspath(sys.executable)),
    "app_origin": sys.argv[0] if sys.argv else "",
    "source": os.path.join(_zp, _entry),
    "source_on_disk": True,
    "cache_dir": os.environ.get("PYTHUB_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "pythub"),
}}

if os.path.isfile(_zp):
    # pyz 单文件模式
    import zipfile
    has_bin = False
    try:
        with zipfile.ZipFile(_zp) as _z:
            for _n in _z.namelist():
                if _n.startswith("lib/") and (_n.endswith(".so") or ".so." in _n or _n.endswith(".pyd")):
                    has_bin = True
                    break
    except OSError:
        pass
    if has_bin:
        # 含二进制扩展，解包到缓存目录后再导入
        cache = os.path.join(os.path.expanduser("~"), ".cache", "pythub", "pyzlib",
                             hashlib.md5((_zp + str(os.path.getmtime(_zp))).encode()).hexdigest())
        if not os.path.isdir(cache):
            os.makedirs(cache, exist_ok=True)
            with zipfile.ZipFile(_zp) as _z:
                _z.extractall(cache)
        _lib = os.path.join(cache, "lib")
    sys.path.insert(0, _lib)
    with zipfile.ZipFile(_zp) as _z:
        _src = _z.read(_entry).decode("utf-8")
    _g = {{"__name__": "__main__", "__file__": os.path.join(_zp, _entry)}}
    exec(compile(_src, _entry, "exec"), _g)
else:
    # 目录模式
    if os.path.isdir(_lib):
        sys.path.insert(0, _lib)
    runpy.run_path(os.path.join(_zp, _entry), run_name="__main__")
'''
    # 注意: 单文件模式里用到了 hashlib，统一顶部导入
    bootstrap = bootstrap.replace("import os, sys, runpy", "import os, sys, runpy, hashlib")
    # 需求 63: 静态打包信息以字面量内嵌进引导器
    bootstrap = bootstrap.replace("_PYTHUB_INFO_REPR_", repr(_appinfo_dict(spec)))

    files: List[Tuple[str, object, bool]] = []
    for rel in _project_files(spec.project_dir):
        arc = rel.as_posix()
        if pkg_prefix:
            arc = f"{pkg_prefix}/{arc}"
        if arc == "__main__.py":
            arc = "__entry__.py"  # 避免覆盖自举 __main__.py
        files.append((arc, spec.project_dir / rel, False))
    if lib_dir:
        for root, dirs, fnames in os.walk(lib_dir):
            rel_root = Path(root).relative_to(lib_dir)
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in fnames:
                p = Path(root) / fn
                arc = (Path("lib") / rel_root / fn).as_posix()
                files.append((arc, p, False))
    files.append(("__main__.py", bootstrap.encode("utf-8"), False))

    pyz.parent.mkdir(parents=True, exist_ok=True)
    with open(pyz, "wb") as f:
        f.write(b"#!/usr/bin/env python3\n")  # zipapp 惯例头
        with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, source, _exe in files:
                zi = zipfile.ZipInfo(str(arcname), date_time=(1980, 1, 1, 0, 0, 0))
                zi.external_attr = 0o644 << 16
                zi.compress_type = zipfile.ZIP_DEFLATED
                if isinstance(source, Path):
                    with open(source, "rb") as src:
                        zf.writestr(zi, src.read())
                else:
                    zf.writestr(zi, source)


def _project_files(project_dir: Path) -> List[Path]:
    result = []
    for root, dirs, fnames in os.walk(project_dir):
        rel_root = Path(root).relative_to(project_dir)
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith("*.egg-info")]
        dirs[:] = [d for d in dirs if not d.endswith(".egg-info")]
        for fn in fnames:
            if fn.endswith(tuple(EXCLUDE_SUFFIXES)):
                continue
            if fn == "pythub.toml" and rel_root == Path("."):
                continue
            result.append(rel_root / fn)
    return result


# ---------------------------------------------------------------- 产物组装

def _assemble(
    spec: BuildSpec,
    runtime: Path,
    pyz: Path,
    work: Path,
    logger: logging.Logger,
) -> List[Path]:
    fmt = spec.target_fmt
    arch = spec.arch  # pexe 架构已在 build() 阶段解析为本机架构
    # 需求 36: dist 下不再有第一级版本目录，最终产物直接平铺到 dist/，
    # 应用版本/Python/平台/架构差异直接体现在文件名上
    # （{name}-{version}-{python}-{platform}-{arch}，版本去前导 v/V）
    out = spec.output
    out.mkdir(parents=True, exist_ok=True)
    artifacts: List[Path] = []

    if fmt == "pyz":
        dest = out / f"{_tag(spec)}.pyz"
        shutil.copy2(pyz, dest)
        set_executable(dest)
        return [dest]

    # 需要 runtime 的格式：先组装自包含目录
    bundle = work / "bundle" / spec.name
    bundle.mkdir(parents=True)
    shutil.copy2(pyz, bundle / "app.pyz")
    # 保留符号链接（bin/python3 -> python3.12 等）：归档/压缩包按链接存储，
    # 避免目标文件被重复打包；Windows runtime 无符号链接，不受影响
    shutil.copytree(runtime, bundle / "runtime", symlinks=True)

    if spec.platform == "windows":
        launcher = bundle / f"{spec.name}.cmd"
        launcher.write_text(formats.windows_launcher(spec.name), encoding="utf-8", newline="\r\n")
    else:
        launcher = bundle / spec.name
        launcher.write_text(
            formats.unix_launcher(spec.name, "runtime/bin/python3", "app.pyz"),
            encoding="utf-8", newline="\n",
        )
        set_executable(launcher)

    if fmt == "dir":
        dest = out / _tag(spec)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(bundle, dest, symlinks=True)
        return [dest]

    if fmt == "exe.zip":
        dest = out / f"{_tag(spec)}.exe.zip"
        if dest.exists():
            dest.unlink()
        return [formats.make_exe_zip(bundle, dest)]

    if fmt == "exe":
        dest = out / f"{_tag(spec)}.exe"
        if dest.exists():
            dest.unlink()
        artifact = formats.make_exe(bundle, dest, spec.arch, spec.python, logger,
                                    console=not spec.windowed, app_name=spec.name)
        if spec.sign:
            from pythub.signing import sign_pe

            artifact = sign_pe(
                artifact,
                cert=Path(spec.sign_cert) if spec.sign_cert else None,
                key=Path(spec.sign_key) if spec.sign_key else None,
                logger=logger,
            )
        return [artifact]

    if fmt == "pexe":
        dest = out / f"{_tag(spec)}.pexe"
        if dest.exists():
            dest.unlink()
        artifact = formats.make_native_exe(bundle, dest, spec.platform, arch,
                                        spec.python, logger,
                                        console=not spec.windowed,
                                        app_name=spec.name)
        if spec.platform == "macos" and spec.app_wrap:
            return [artifact, _wrap_mac_app(artifact, spec, logger)]
        return [artifact]

    if fmt == "tar.gz":
        dest = out / f"{_tag(spec)}.tar.gz"
        if dest.exists():
            dest.unlink()
        make_tar_gz(dest, bundle)
        return [dest]

    if fmt == "run":
        dest = out / f"{_tag(spec)}.run"
        if dest.exists():
            dest.unlink()
        return [formats.make_run(bundle, dest, spec.name)]

    if fmt == "deb" and spec.platform == "linux":
        # Debian 包: bundle 铺 /opt/<pkg>/ + /usr/bin 软链，纯标准库交叉生成；
        # 文件名沿用 Debian 规范（{name}_{version}-{release}_{arch}.deb，
        # 版本/架构已在文件名标注，平台即 linux）
        dest = out / formats.deb_file_name(spec.name, spec.version, arch)
        if dest.exists():
            dest.unlink()
        return [formats.make_deb(
            bundle, out,
            name=spec.name, version=spec.version, arch=arch,
            maintainer=spec.maintainer, description=spec.description,
            windowed=spec.windowed,
        )]

    if fmt == "rpm" and spec.platform == "linux":
        # RPM 包: 自研 cpio/header（三端可交叉）或 rpmbuild（mac/linux 构建机可选）；
        # 文件名沿用 RPM 规范（{name}-{version}-{release}.{arch}.rpm）
        return [formats.make_rpm(
            bundle, out,
            name=spec.name, version=spec.version, arch=arch,
            maintainer=spec.maintainer, description=spec.description,
            license=spec.license, windowed=spec.windowed,
            engine=spec.rpm_engine,
        )]

    if spec.platform == "macos":
        staging = work / "stage"
        staging.mkdir(parents=True)
        app = formats.build_app_bundle(staging, spec.name, spec.version, runtime, pyz,
                                       logger, bundle_id=spec.bundle_id)
        if fmt == "app":
            dest = out / f"{_tag(spec)}.app"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(app, dest, symlinks=True)
            return [dest]
        if fmt == "dmg":
            dest = out / f"{_tag(spec)}.dmg"
            if dest.exists():
                dest.unlink()
            return [formats.make_dmg(staging, dest, spec.name)]
        if fmt == "pkg":
            dest = out / f"{_tag(spec)}.pkg"
            if dest.exists():
                dest.unlink()
            return [formats.make_pkg(staging, dest, spec.name, spec.version)]

    if fmt == "appimage" and spec.platform == "linux":
        dest = out / f"{_tag(spec)}.AppImage"
        if dest.exists():
            dest.unlink()
        result = formats.make_appimage(work / "stage", dest, spec.name, runtime, pyz, logger)
        if result:
            return [result]
        logger.warning("未找到 appimagetool，降级为单文件 .run（自解压可执行，无需任何工具）。")
        dest = out / f"{_tag(spec)}.run"
        if dest.exists():
            dest.unlink()
        return [formats.make_run(bundle, dest, spec.name)]

    raise ValueError(f"格式 {fmt} 不适用于平台 {spec.platform}")
