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
if [[ -z "${TWINE_PASSWORD:-}" ]]; then
    echo "错误: 未设置 TWINE_PASSWORD (PyPI API Token)"
    echo "  1. 在 https://pypi.org/manage/account/token/ 生成 token"
    echo "  2. export TWINE_PASSWORD=pypi-xxxx 或编辑本脚本填入"
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
rm -rf dist build src/*.egg-info

echo "==> 构建发行包 (sdist + wheel)..."
$PY -m build

echo "==> 校验发行包..."
$PY -m twine check dist/*

# ---------- 上传 ----------
REPO_ARGS=()
if [[ "$REPOSITORY" == "testpypi" ]]; then
    REPO_ARGS=(--repository testpypi)
fi

echo "==> 上传到 $REPOSITORY ..."
$PY -m twine upload "${REPO_ARGS[@]}" dist/*

echo "==> 发布完成！"
