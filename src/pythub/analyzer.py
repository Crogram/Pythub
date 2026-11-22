"""依赖分析器：AST 解析导入，区分标准库/第三方，模块名→发行包名映射。"""

from __future__ import annotations

import ast
import importlib.metadata
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

# 常见"导入名 != 发行包名"的内置映射（本机未安装该包时 importlib.metadata 无法映射）
MODULE_DIST_OVERRIDES: Dict[str, str] = {
    "whois": "python-whois",
    "bs4": "beautifulsoup4",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "yaml": "pyyaml",
    "Crypto": "pycryptodome",
    "dotenv": "python-dotenv",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "xlsx": "xlsxwriter",
    "openpyxl": "openpyxl",
    "dateutil": "python-dateutil",
    "serial": "pyserial",
    "usb": "pyusb",
    "dns": "dnspython",
    "magic": "python-magic",
    "ldap": "python-ldap",
    "git": "gitpython",
    "slugify": "python-slugify",
    "socli": "socli",
    "dateparser": "dateparser",
    "execjs": "pyexecjs",
    "chardet": "chardet",
    "Markdown": "markdown",
    "websocket": "websocket-client",
    "jwt": "pyjwt",
    "redis": "redis",
    "matplotlib": "matplotlib",
    "numpy": "numpy",
    "pandas": "pandas",
    "requests": "requests",
    "aiohttp": "aiohttp",
    "flask": "flask",
    "django": "django",
}


def _local_top_modules(project_dir: Path) -> Set[str]:
    """项目内顶层模块：xxx.py 文件与 xxx/ 包目录。"""
    locals_: Set[str] = set()
    if not project_dir.is_dir():
        return locals_
    for child in project_dir.iterdir():
        if child.is_file() and child.suffix == ".py":
            locals_.add(child.stem)
        elif child.is_dir() and (child / "__init__.py").exists():
            locals_.add(child.name)
    return locals_


def collect_imports(entry: Path, project_dir: Path) -> Set[str]:
    """收集入口脚本 + 项目内所有 py 文件的顶层导入模块名。"""
    seen_files: Set[Path] = {entry.resolve()}
    todo: List[Path] = [entry.resolve()]
    imports: Set[str] = set()

    while todo:
        path = todo.pop()
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            raise RuntimeError(f"语法错误 {path}: {e}") from e

        for node in ast.walk(tree):
            names: List[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0 and node.module:
                    # 项目内相对导入：尝试追踪同目录模块
                    base = path.parent / (node.module.replace(".", "/") or "")
                    for cand in (base.with_suffix(".py"), base / "__init__.py"):
                        if cand.exists():
                            todo.append(cand)
                    continue
                if node.module:
                    names = [node.module]
            for name in names:
                top = name.split(".")[0]
                imports.add(top)
                # 项目内模块：继续解析其导入
                cand = project_dir / (top + ".py")
                pkg_init = project_dir / top / "__init__.py"
                for c in (cand, pkg_init):
                    if c.exists() and c.resolve() not in seen_files:
                        seen_files.add(c.resolve())
                        todo.append(c)

    return imports


def classify(imports: Set[str], project_dir: Path = None) -> Tuple[Set[str], Set[str]]:
    """返回 (stdlib, thirdparty)。项目本地模块不计入第三方。"""
    stdlib = {m for m in imports if m in getattr(sys, "stdlib_module_names", ())}
    thirdparty = imports - stdlib - {"__main__", "__future__"}
    if project_dir is not None:
        thirdparty -= _local_top_modules(project_dir)
        # 项目目录本身是包时，包名也属于本地模块（如用 pythub 打包 pythub 自身）
        if (project_dir / "__init__.py").is_file():
            thirdparty.discard(project_dir.name)
    return stdlib, thirdparty


def resolve_distributions(modules: Set[str]) -> Dict[str, str]:
    """模块名 → PyPI 发行包名（先查内置映射表，再依赖本机已安装元数据）。"""
    mapping = importlib.metadata.packages_distributions()
    result = {}
    for mod in modules:
        dists = mapping.get(mod)
        if dists:
            result[mod] = sorted(dists)[0]
        elif mod in MODULE_DIST_OVERRIDES:
            result[mod] = MODULE_DIST_OVERRIDES[mod]
    return result


def analyze(entry: Path, project_dir: Path) -> Dict[str, object]:
    imports = collect_imports(entry, project_dir)
    stdlib, thirdparty = classify(imports, project_dir)
    dists = resolve_distributions(thirdparty)
    unresolved = {m for m in thirdparty if m not in dists and m not in dists.values()}
    return {
        "imports": sorted(imports),
        "stdlib": sorted(stdlib),
        "thirdparty_modules": sorted(thirdparty),
        "thirdparty_packages": sorted(set(dists.values())),
        "unresolved": sorted(unresolved),
    }
