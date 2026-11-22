"""项目配置：pythub.toml 解析（3.11+ 用 tomllib，3.10 用内置简易解析器）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional


def _parse_toml_minimal(text: str) -> Dict[str, Any]:
    """极简 TOML 解析：仅支持 [section] 与 key = "value"/value（满足 pythub.toml 需求）。"""
    result: Dict[str, Any] = {}
    section = result
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            section = result
            for part in m.group(1).split("."):
                section = section.setdefault(part.strip(), {})
            continue
        m = re.match(r"^([\w-]+)\s*=\s*(.+)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith(('"', "'")) and val.endswith(('"', "'")):
                val = val[1:-1]
            elif val.startswith("["):
                items = re.findall(r'"([^"]*)"', val)
                val = items
            section[key] = val
    return result


def load_config(project_dir: Path) -> Dict[str, Any]:
    path = project_dir / "pythub.toml"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib

        return tomllib.loads(text)
    except ImportError:
        return _parse_toml_minimal(text)


def load_requirements(project_dir: Path) -> list:
    """读取 requirements.txt（每行一个依赖，忽略注释与空行）。"""
    path = project_dir / "requirements.txt"
    if not path.exists():
        return []
    reqs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            reqs.append(line)
    return reqs


def app_config(project_dir: Path, overrides: Optional[dict] = None) -> Dict[str, Any]:
    """合并 pythub.toml 的 [app] 段与命令行覆盖参数。"""
    cfg = load_config(project_dir).get("app", {})
    result = {
        "name": cfg.get("name", project_dir.name or "app"),
        "entry": cfg.get("entry", "main.py"),
        "version": cfg.get("version", "0.1.0"),
        "description": cfg.get("description", ""),
        "author": cfg.get("author", ""),
        "requirements": cfg.get("requirements", []),
        "windowed": bool(cfg.get("windowed", False)),
        "mem": bool(cfg.get("mem", False)),
        "chmod": bool(cfg.get("chmod", False)),
        "app_wrap": bool(cfg.get("app_wrap", False)),
    }
    for key, val in (overrides or {}).items():
        if val:
            result[key] = val
    return result
