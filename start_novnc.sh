#!/bin/bash
# noVNC 启动脚本：Xvfb + x11vnc + websockify
# 安全：viewonly 只读 + 密码认证 + x11vnc 只监听本机（外部只能经 websockify 看，不能操作）
#
# 用法：VNC_PASSWORD=<你的密码> bash start_novnc.sh     # 首次用它生成 ~/.vnc/passwd
# 之后复用 ~/.vnc/passwd；密码绝不写进脚本（本仓库公开）。
#
# ⚠ 暴露面：websockify 的网页端口会监听所有网卡 → 只在安全组放行可信来源，
#    或改成只绑 127.0.0.1 走 SSH 隧道（ssh -L 8081:127.0.0.1:8081）。
set -euo pipefail

DISPLAY_NUM=99
VNC_PORT=5900
WEB_PORT=8081
WIDTH=1280
HEIGHT=720
DEPTH=24

mkdir -p ~/.vnc
if [ -f ~/.vnc/passwd ]; then
    echo "[noVNC] 复用已有的 ~/.vnc/passwd（要换密码：x11vnc -storepasswd 覆盖它）"
else
    : "${VNC_PASSWORD:?首次运行请设置 VNC_PASSWORD，例如 VNC_PASSWORD=xxx bash start_novnc.sh}"
    x11vnc -storepasswd "$VNC_PASSWORD" ~/.vnc/passwd
    chmod 600 ~/.vnc/passwd
    echo "[noVNC] 已用 VNC_PASSWORD 生成 ~/.vnc/passwd"
fi

# 启动 Xvfb 虚拟显示
echo "[noVNC] 启动 Xvfb :${DISPLAY_NUM} (${WIDTH}x${HEIGHT}x${DEPTH})"
Xvfb :${DISPLAY_NUM} -screen 0 ${WIDTH}x${HEIGHT}x${DEPTH} -ac +extension RANDR &
XVFB_PID=$!
sleep 1

# 启动 x11vnc 抓取虚拟屏幕（-localhost：只接受本机连接，外部一律走 websockify）
echo "[noVNC] 启动 x11vnc（只监听 127.0.0.1:${VNC_PORT}，只读+密码）"
x11vnc -display :${DISPLAY_NUM} -localhost -rfbport $VNC_PORT -rfbauth ~/.vnc/passwd \
    -viewonly -forever -shared -bg -quiet
sleep 1

# 启动 websockify 转成 WebSocket
echo "[noVNC] 启动 websockify（端口 $WEB_PORT → 127.0.0.1:$VNC_PORT）"
websockify --web=/usr/share/novnc/ $WEB_PORT localhost:$VNC_PORT &
WS_PID=$!

stop_all() {
    kill "$WS_PID" "$XVFB_PID" 2>/dev/null || true
}
trap stop_all EXIT INT TERM

echo "[noVNC] 就绪：浏览器打开 http://127.0.0.1:${WEB_PORT}/vnc.html（或经隧道/反代访问）"
echo "[noVNC] 密码即 ~/.vnc/passwd 里那个（脚本不打印）"

wait $WS_PID
