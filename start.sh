#!/bin/bash

cd "$(dirname "$0")"

# 检查是否已运行
if [ -f .pid ]; then
    pid=$(cat .pid)
    if ps -p $pid > /dev/null 2>&1; then
        echo "服务已在运行 (PID: $pid)"
        exit 1
    fi
fi

# 选 Python 解释器：优先项目 venv，其次系统 python3
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    echo "错误：找不到 python（既无 .venv/bin/python 也无 python3）"
    exit 1
fi

# 让 SDK 走 claude CLI 读 keychain（自动刷新 OAuth），不要继承 stale 凭证
unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN
# 飞书 WebSocket 直连，避免 SOCKS 代理问题
unset ALL_PROXY HTTPS_PROXY HTTP_PROXY all_proxy https_proxy http_proxy
# 加载用户 PATH（确保能找到 claude CLI）
[ -f "$HOME/.shell_path" ] && source "$HOME/.shell_path"

# 后台启动
nohup "$PY" -m src.main_websocket > log.log 2>&1 &
echo $! > .pid

echo "服务已启动 (PID: $!)"
echo "日志: tail -f log.log"
