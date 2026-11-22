"""Pythub 命令行入口。

用法:
  pythub build <项目目录> [-p 平台...] [-f 格式...] [--arch] [--py] [-o 输出目录]
  pythub gui
  pythub init [目录]
  pythub analyze <项目目录>
  pythub formats
  pythub pypa build <项目目录> [-o dist]
  pythub pypa publish <文件...> [--repo pypi|testpypi] [--token ...]
  pythub cache [--prune] [--max-mb N]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from pythub import DEFAULT_FORMAT, PLATFORM_FORMATS, SUPPORTED_ARCHS, SUPPORTED_PYTHON, __version__
from pythub.engines import ENGINE_NAMES as _ENGINE_NAMES
from pythub.utils import get_logger


def _parse_targets(values: List[str], valid, kind: str) -> List[str]:
    result = []
    for v in values:
        if v == "all":
            result.extend(valid)
        else:
            if v not in valid:
                raise SystemExit(f"不支持的{kind}: {v}（可选: {', '.join(valid)}）")
            result.append(v)
    return list(dict.fromkeys(result)) or list(valid)


def cmd_build(args) -> int:
    from pythub.builder import build, spec_from_project

    logger = get_logger()
    project_dir = Path(args.path).resolve()
    if not project_dir.exists():
        raise SystemExit(f"项目目录不存在: {project_dir}")

    engine = (args.engine or "pythub").strip().lower()
    platforms = _parse_targets(args.platform, PLATFORM_FORMATS.keys(), "目标平台")
    fmts = args.format or []
    if not fmts or fmts == ["auto"]:
        fmts = [DEFAULT_FORMAT[p] for p in platforms]
    else:
        if engine in ("pythub", "zipapp"):
            valid = [f for p in platforms for f in PLATFORM_FORMATS[p]]
        else:
            from pythub.engines import ENGINES
            valid = list(ENGINES[engine]["fmts"])
        fmts = _parse_targets(fmts, valid, "输出格式")

    overrides = {
        "name": args.name,
        "entry": args.entry,
        "python": args.python,
        "arch": args.arch,
        "requirements": args.requirement,
        "output": args.output,
        "engine": args.engine,
        "windowed": args.windowed,
        "mem": getattr(args, "mem", False),
        "chmod": getattr(args, "chmod", False),
        "app_wrap": getattr(args, "app_wrap", False),
    }

    total = 0
    for p in platforms:
        for f in fmts:
            if engine in ("pythub", "zipapp") and f not in PLATFORM_FORMATS[p]:
                continue  # 三方引擎的格式校验交给 engines.build_engine
            spec = spec_from_project(project_dir, logger, overrides)
            spec.platform = p
            spec.arch = args.arch or spec.arch
            spec.python = args.python or spec.python
            spec.fmt = f
            spec.output = Path(args.output or "dist").resolve()
            if args.sign:
                spec.sign = True
            if args.sign_cert:
                spec.sign_cert = args.sign_cert
            if args.sign_key:
                spec.sign_key = args.sign_key
            artifacts = build(spec, logger)
            total += len(artifacts)
    logger.info("共生成 %d 个产物。", total)
    return 0


def cmd_gui(args) -> int:
    from pythub.gui import main

    return main(debug=getattr(args, "debug", False))


def cmd_init(args) -> int:
    base = Path(args.directory or ".").resolve() / "hello_app"
    base.mkdir(parents=True, exist_ok=True)
    (base / "main.py").write_text(_EXAMPLE_MAIN, encoding="utf-8")
    (base / "pythub.toml").write_text(_EXAMPLE_TOML, encoding="utf-8")
    print(f"示例工程已创建: {base}")
    print(f"试试: pythub build {base} -p all")
    return 0


def cmd_analyze(args) -> int:
    from pythub.analyzer import analyze

    project_dir = Path(args.path).resolve()
    cfg_dir = project_dir if project_dir.is_dir() else project_dir.parent
    entry = project_dir if project_dir.is_file() else cfg_dir / "main.py"
    from pythub.config import app_config

    cfg = app_config(cfg_dir)
    if entry.is_file() is False:
        entry = cfg_dir / cfg["entry"]
    info = analyze(entry, cfg_dir)
    print("导入的模块:")
    print("  标准库 :", ", ".join(info["stdlib"]) or "-")
    print("  第三方 :", ", ".join(info["thirdparty_modules"]) or "-")
    print("  发行包 :", ", ".join(info["thirdparty_packages"]) or "-")
    if info["unresolved"]:
        print("  未映射 :", ", ".join(info["unresolved"]),
              "（如需打包，请加入 requirements.txt）")
    return 0


def cmd_formats(_args) -> int:
    cur = None
    try:
        from pythub.runtime import current_platform

        cur = current_platform()
    except Exception:
        pass
    print("各平台可用输出格式（✱ 为默认格式）:")
    for p, fmts in PLATFORM_FORMATS.items():
        mark = "（当前平台）" if p == cur else ""
        print(f"  {p}{mark}: " + ", ".join(f"{f}{'✱' if f == DEFAULT_FORMAT[p] else ''}" for f in fmts))
    print("支持架构:", ", ".join(SUPPORTED_ARCHS))
    print("支持 Python 版本:", ", ".join(SUPPORTED_PYTHON))
    return 0


def cmd_pypa(args) -> int:
    from pythub.pypa import build_package, publish

    logger = get_logger()
    if args.pypa_cmd == "build":
        project_dir = Path(args.path).resolve()
        if not project_dir.exists():
            raise SystemExit(f"项目目录不存在: {project_dir}")
        out = Path(args.output or "dist").resolve()
        artifacts = build_package(project_dir, out, logger)
        print("发行包已生成:")
        for a in artifacts:
            print(f"  {a}")
        print(f"发布: pythub pypa publish {out}/*.whl --token <pypi-token>")
        return 0
    if args.pypa_cmd == "publish":
        from pythub.pypa import load_pypi_credentials, save_pypi_credentials

        files = [Path(f) for f in args.files]
        for f in files:
            if not f.exists():
                raise SystemExit(f"文件不存在: {f}")
        username = password = None
        if args.token:
            username, password = "__token__", args.token
        else:
            creds = load_pypi_credentials(args.repo)
            if creds:
                username, password = creds["username"], creds["password"]
                print(f"使用系统已配置凭据 ({creds['source']})")
            else:
                import getpass

                username = input("PyPI 用户名（API Token 认证直接填 __token__）: ").strip()
                password = getpass.getpass("密码/Token: ").strip()
                if not username or not password:
                    raise SystemExit("缺少凭据")
                if input("保存到 ~/.pypirc 以便下次直接使用? [y/N]: ").strip().lower() == "y":
                    path = save_pypi_credentials(username, password, args.repo)
                    print(f"已保存: {path}")
        from pythub.utils import set_file_logging

        set_file_logging(True)  # 发布日志强制落盘
        publish(files, repository=args.repo, username=username,
                password=password, logger=logger)
        return 0
    raise SystemExit("pypa 子命令: build / publish")


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def cmd_cache(args) -> int:
    import json as _json

    from pythub.utils import DEFAULT_CACHE_MAX_BYTES, PYTHUB_CACHE_DIR, cache_stats, prune_cache

    if args.max_mb is not None and args.max_mb <= 0:
        print("错误: --max-mb 必须为正整数（MB）", file=sys.stderr)
        return 1

    stats = cache_stats()
    total = sum(stats.values())
    pruned = None
    if args.prune:
        cap = (args.max_mb << 20) if args.max_mb else DEFAULT_CACHE_MAX_BYTES
        pruned = prune_cache(cap)
    if getattr(args, "json", False):
        print(_json.dumps({
            "dir": str(PYTHUB_CACHE_DIR),
            "stats": stats,
            "total_bytes": total,
            "pruned": [str(p) for p in pruned] if pruned else [],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"缓存目录: {PYTHUB_CACHE_DIR}")
    for sub, size in stats.items():
        print(f"  {sub}: {_human(size)}")
    print(f"  合计: {_human(total)}")
    if args.prune:
        if pruned:
            print(f"已按 LRU 清理 {len(pruned)} 个缓存条目:")
            for p in pruned:
                print(f"  - {p}")
        else:
            print("缓存未超上限，无需清理。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pythub",
        description="Pythub —— 开源的 Python 应用打包工具：一条命令，"
                    "把应用打包成免装 Python、可直接运行的产物（版权归属 CROGRAM）",
    )
    parser.add_argument("--version", action="version", version=f"pythub {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("build", help="打包项目（支持多平台/多版本/多架构交叉打包）")
    p.add_argument("path", help="项目目录（含 pythub.toml 或 main.py）")
    p.add_argument("-p", "--platform", action="append", default=[],
                   help="目标平台: macos/linux/windows/all，可多选")
    p.add_argument("-f", "--format", action="append", default=[],
                   help="输出格式（auto 为平台默认），可多选")
    p.add_argument("--arch", default=None, help="目标架构: x86_64/aarch64")
    p.add_argument("--py", "--python", dest="python", default=None,
                   help="目标 Python 版本: 3.10/3.11/3.12")
    p.add_argument("-o", "--output", default=None, help="输出目录（默认 dist）")
    p.add_argument("--name", default=None, help="应用名（默认取 pythub.toml 或目录名）")
    p.add_argument("--entry", default=None, help="入口脚本（默认 main.py）")
    p.add_argument("-r", "--requirement", action="append", default=[],
                   help="额外依赖（可多选）")
    p.add_argument("--engine", default="pythub",
                   choices=["pythub", "zipapp"] + list(_ENGINE_NAMES),
                   help="打包引擎: pythub（默认，自研，支持跨平台交叉；旧名 zipapp "
                        "保留为别名）；本机三方引擎: "
                        + "/".join(_ENGINE_NAMES))
    p.add_argument("--windowed", action="store_true", help="GUI 应用（不显示控制台）")
    p.add_argument("--mem", action="store_true",
                   help="开启 mem 内存执行模式（仅 exe/pexe）: 源码以字节码内嵌、"
                        "全程内存执行不落盘，产物后缀 .mem.exe / .mem.pexe")
    p.add_argument("--app-wrap", action="store_true",
                   help="mac 平台把 pexe 再次打包成 .app 壳（Finder 识别为应用，适合 GUI）")
    p.add_argument("--chmod", action="store_true",
                   help="为 run/pexe/pyz 等可执行产物自动添加执行权限（chmod +x）")
    p.add_argument("--sign", action="store_true",
                   help="给 .exe 附加 Authenticode 签名（需 osslsigncode；无证书时自动自签）")
    p.add_argument("--sign-cert", default=None, help="签名证书 PEM 路径（配合 --sign-key）")
    p.add_argument("--sign-key", default=None, help="签名私钥 PEM 路径（配合 --sign-cert）")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("gui", help="打开可视化操作界面（默认后台运行，不占控制台）")
    p.add_argument("--debug", action="store_true",
                   help="前台运行，日志输出到当前控制台（调试用；Ctrl+C 退出）")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("init", help="创建示例工程")
    p.add_argument("directory", nargs="?", default=None)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("analyze", help="分析项目依赖")
    p.add_argument("path", help="项目目录或入口脚本")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("formats", help="列出各平台可用格式")
    p.set_defaults(func=cmd_formats)

    p = sub.add_parser("cache", help="查看/清理本地构建缓存")
    p.add_argument("--prune", action="store_true", help="超过大小上限时按 LRU 清理")
    p.add_argument("--max-mb", type=int, default=None, help="缓存大小上限 MB（默认 4096）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出统计与清理结果（供 GUI/脚本消费）")
    p.set_defaults(func=cmd_cache)

    p = sub.add_parser("pypa", help="打包为 PyPI 发行包 / 发布到 PyPI")
    psub = p.add_subparsers(dest="pypa_cmd", required=True)
    pb = psub.add_parser("build", help="构建 sdist 与 wheel")
    pb.add_argument("path", help="项目目录")
    pb.add_argument("-o", "--output", default="dist")
    pb.set_defaults(func=cmd_pypa)
    pp = psub.add_parser("publish", help="发布到 PyPI")
    pp.add_argument("files", nargs="+", help="待上传的 .whl/.tar.gz")
    pp.add_argument("--repo", default="pypi", choices=["pypi", "testpypi"])
    pp.add_argument("--token", default=None,
                    help="PyPI API Token（不填则优先使用系统已配置凭据，其次交互输入）")
    pp.set_defaults(func=cmd_pypa)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"错误: {e}", file=sys.stderr)
        return 1


_EXAMPLE_MAIN = '''\
"""hello_app —— Pythub 示例工程。"""
import platform
import sys


def info():
    return f"Python {platform.python_version()} @ {platform.system()} {platform.machine()}"


def main():
    args = sys.argv[1:]
    if "--console" in args or not sys.stdout.isatty() and "--gui" not in args:
        pass
    try:
        import tkinter
    except ImportError:
        tkinter = None

    if tkinter and "--console" not in args:
        root = tkinter.Tk()
        root.title("Hello Pythub")
        root.geometry("420x180")
        tkinter.Label(root, text="Hello, Pythub!", font=("Helvetica", 20)).pack(pady=20)
        tkinter.Label(root, text=info()).pack()
        root.mainloop()
    else:
        print("Hello, Pythub!")
        print(info())


if __name__ == "__main__":
    main()
'''

_EXAMPLE_TOML = '''\
[app]
name = "hello-pythub"
entry = "main.py"
version = "0.1.0"
description = "Pythub 示例应用"
author = "CROGRAM"
requirements = []          # 第三方依赖，例如 ["requests"]
windowed = false           # GUI 应用在 PyInstaller 引擎下隐藏控制台
'''


if __name__ == "__main__":
    sys.exit(main())
