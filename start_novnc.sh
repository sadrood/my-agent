#!/bin/bash
# noVNC 启动脚本：Xvfb + x11vnc + websockify
# 安全：viewonly 只读 + 密码认证，外部只能看不能操作

DISPLAY_NUM=99
VNC_PORT=5900
WEB_PORT=8081
WIDTH=1280
HEIGHT=720
DEPTH=24

# VNC 密码（存在 ~/.vnc/passwd，用 vncpasswd 生成）
mkdir -p ~/.vnc
if [ ! -f ~/.vnc/passwd ]; then
    x11vnc -storepasswd orca2026 ~/.vnc/passwd
fi

# 启动 Xvfb 虚拟显示
echo "[noVNC] 启动 Xvfb :${DISPLAY_NUM} (${WIDTH}x${HEIGHT}x${DEPTH})"
Xvfb :${DISPLAY_NUM} -screen 0 ${WIDTH}x${HEIGHT}x${DEPTH} -ac +extension RANDR &
XVFB_PID=$!
sleep 1

# 启动 x11vnc 抓取虚拟屏幕（viewonly 只读 + 密码认证）
echo "[noVNC] 启动 x11vnc（端口 $VNC_PORT，只读+密码）"
x11vnc -display :${DISPLAY_NUM} -rfbport $VNC_PORT -rfbauth ~/.vnc/passwd -viewonly -forever -shared -bg -quiet
sleep 1

# 启动 websockify 转成 WebSocket
echo "[noVNC] 启动 websockify（端口 $WEB_PORT → $VNC_PORT）"
websockify --web=/usr/share/novnc/ $WEB_PORT localhost:$VNC_PORT &
WS_PID=$!

echo "[noVNC] 就绪：浏览器打开 http://<公网IP>:${WEB_PORT}/vnc.html"
echo "[noVNC] VNC 密码：orca2026"

# 等待退出信号
wait $WS_PID
