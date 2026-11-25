#!/usr/bin/env zsh
# 发布 pythub 到 PyPI
# 用法: ./pypi.sh          # 正式发布到 PyPI
#       ./pypi.sh test     # 发布到 TestPyPI
set -euo pipefail

cd "$(dirname "$0")"

REPOSITORY="pypi"
if [[ "${1:-}" == "test" ]]; then
    REPOSITORY="testpypi"
fi

# ---------- PyPI Token ----------
# 优先读环境变量 TWINE_PASSWORD；为空则回退读取 pythub GUI 发布页保存的配置
# （~/.cache/pythub/settings.json -> publish.token）
if [[ -z "${TWINE_PASSWORD:-}" ]]; then
    SETTINGS="$HOME/.cache/pythub/settings.json"
    if [[ -f "$SETTINGS" ]] && command -v python3 &>/dev/null; then
        TOKEN_FROM_SETTINGS=$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get("publish", {}).get("token", "") or "")
except Exception:
    pass
' "$SETTINGS")
        if [[ -n "$TOKEN_FROM_SETTINGS" ]]; then
            TWINE_PASSWORD="$TOKEN_FROM_SETTINGS"
            echo "==> 使用 pythub GUI 发布页保存的 Token (settings.json)"
        fi
    fi
fi
if [[ -z "${TWINE_PASSWORD:-}" ]]; then
    echo "错误: 未设置 TWINE_PASSWORD (PyPI API Token)"
    echo "  1. 在 https://pypi.org/manage/account/token/ 生成 token"
    echo "  2. export TWINE_PASSWORD=pypi-xxxx 或编辑本脚本填入"
    echo "  3. 或在 pythub gui 发布页填入并保存 Token"
    exit 1
fi
TWINE_USERNAME="__token__"
export TWINE_USERNAME TWINE_PASSWORD

# ---------- 依赖检查 ----------
PY=python3.11
if ! command -v "$PY" &>/dev/null; then
    echo "错误: 未找到 $PY"
    exit 1
fi
for mod in build twine; do
    if ! "$PY" -m "$mod" --version &>/dev/null; then
        echo "错误: 缺少模块 $mod，请执行: $PY -m pip install build twine"
        exit 1
    fi
done

# ---------- 清理并构建 ----------
echo "==> 清理旧的构建产物..."
rm -rf dist build src/*.egg-info(N)

echo "==> 构建发行包 (sdist + wheel)..."
$PY -m build

echo "==> 校验发行包..."
$PY -m twine check dist/*(N)

# ---------- 上传 ----------
REPO_ARGS=()
if [[ "$REPOSITORY" == "testpypi" ]]; then
    REPO_ARGS=(--repository testpypi)
fi

echo "==> 上传到 $REPOSITORY ..."
$PY -m twine upload "${REPO_ARGS[@]}" dist/*(N)

echo "==> 发布完成！"
