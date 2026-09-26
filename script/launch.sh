#!/bin/bash
# cursorAdmin · 用本目录 venv 启动
# 用法：
#   ./script/launch.sh              启动桌面应用
#   ./script/launch.sh start        同上
#   ./script/launch.sh preview      浏览器预览（演示数据，默认 43147）
#   ./script/launch.sh preview 8080 指定端口预览
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

VENV="./venv"
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

if [ "$(uname -s)" = "Darwin" ]; then
  REQ="requirements-mac.txt"
else
  REQ="requirements.txt"
fi

pick_python() {
  if [ -n "${PYTHON:-}" ]; then
    echo "$PYTHON"
    return
  fi
  if [ -x /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 ]; then
    echo /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11
    return
  fi
  if command -v python3.11 >/dev/null 2>&1; then
    command -v python3.11
    return
  fi
  command -v python3
}

ensure_venv() {
  if [ ! -x "$VENV/bin/python" ]; then
    local py
    py="$(pick_python)"
    if [ -z "$py" ]; then
      echo "[X] 找不到 Python。请安装 3.11：https://www.python.org/downloads/macos/"
      exit 1
    fi
    echo "[launch] 建立虚拟环境 $VENV （$("$py" --version 2>&1)）"
    "$py" -m venv "$VENV"
  fi
  if ! "$VENV/bin/python" -c "import webview, requests, websocket" >/dev/null 2>&1; then
    echo "[launch] 安装依赖 $REQ ..."
    "$VENV/bin/python" -m pip install -U pip -i "$PIP_MIRROR"
    "$VENV/bin/python" -m pip install -r "$REQ" -i "$PIP_MIRROR"
  fi
}

usage() {
  cat <<'EOF'
用法:
  ./script/launch.sh              启动桌面应用
  ./script/launch.sh start        同上
  ./script/launch.sh preview      浏览器预览（演示数据，默认端口 43147）
  ./script/launch.sh preview 8080 指定端口预览
EOF
}

CMD="${1:-start}"
case "$CMD" in
  start|run)
    ensure_venv
    echo "启动 cursorAdmin …（关闭窗口即退出）"
    exec "$VENV/bin/python" -m app
    ;;
  preview)
    ensure_venv
    PORT="${2:-43147}"
    echo "预览 http://127.0.0.1:${PORT}/  （演示数据，Ctrl+C 退出）"
    exec "$VENV/bin/python" -m app.preview_server --port "$PORT"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "未知命令：$CMD"
    usage
    exit 1
    ;;
esac
