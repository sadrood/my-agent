#!/usr/bin/env bash
# ============================================================
#  my_agent 一次性引导脚本（Linux / macOS）。与 install_deps.bat
#  （Windows/cmd 版）步骤一一对应，可重复执行（幂等）。
#
#  用法：
#    bash install_deps.sh                # 全流程
#    bash install_deps.sh --no-desktop   # 只装 CLI 依赖（跳过 Node 桌面端）
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$PWD"
SKIP_DESKTOP=0
[ "${1:-}" = "--no-desktop" ] && SKIP_DESKTOP=1

echo "[1/5] 检查 Python（需要 3.11+）..."
PY=""
for cand in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "[ERROR] 没找到 Python 3.11+。" >&2
  echo "        Debian/Ubuntu: sudo apt install python3.11 python3.11-venv" >&2
  echo "        Fedora/RHEL:   sudo dnf install python3.11" >&2
  exit 1
fi
"$PY" --version

echo "[2/5] 创建虚拟环境 .venv ..."
if [ ! -x ".venv/bin/python" ]; then
  if ! "$PY" -m venv .venv; then
    echo "[ERROR] 创建 venv 失败。Debian/Ubuntu 上通常是缺 python3-venv：" >&2
    echo "        sudo apt install python3.11-venv" >&2
    exit 1
  fi
fi
VPY="$ROOT/.venv/bin/python"

echo "[3/5] 安装 Python 依赖（requirements.txt）..."
"$VPY" -m pip install --upgrade pip >/dev/null
"$VPY" -m pip install -r requirements.txt
echo "      Python 依赖装好了。"

echo "[4/5] 安装 Playwright chromium（浏览器工具用）..."
# 直连 CDN 在部分网络下很慢：默认走 npmmirror，可用环境变量覆盖。
export PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-https://npmmirror.com/mirrors/playwright}"
echo "      下载源: $PLAYWRIGHT_DOWNLOAD_HOST"
echo "      首次约 150MB（Ctrl+C 可跳过，只影响浏览器工具）。"
if ! "$VPY" -m playwright install chromium; then
  echo "      [WARN] chromium 下载失败或被跳过。重试："
  echo "        PLAYWRIGHT_DOWNLOAD_HOST=$PLAYWRIGHT_DOWNLOAD_HOST $VPY -m playwright install chromium"
fi
# Linux 上"装好了却起不来"几乎都是缺系统库（libnss3 / libatk / libgbm…）
if [ "$(uname -s)" = "Linux" ]; then
  echo "      提示：若启动报缺 .so，装系统依赖（需要 sudo）："
  echo "        sudo $VPY -m playwright install-deps chromium"
fi

if [ "$SKIP_DESKTOP" = "1" ]; then
  echo "[5/5] 跳过桌面端（--no-desktop）。CLI 不受影响。"
elif ! command -v node >/dev/null 2>&1; then
  echo "[5/5] 未找到 Node.js（桌面端需要 18+）。CLI 现在就能用。"
elif [ -d desktop ]; then
  echo "[5/5] 桌面端 Node 依赖（npm ci）..."
  export NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
  echo "      源: $NPM_REGISTRY"
  if (cd desktop && npm ci --no-audit --no-fund --registry="$NPM_REGISTRY"); then
    echo "      桌面端依赖就绪。"
  else
    echo "      [WARN] npm 安装失败（桌面端用不了，CLI 不受影响）。"
  fi
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "[NOTE] 已从 .env.example 生成 .env —— 填好 API key 再跑真实任务。"
fi

echo
echo "可选组件（缺了只是对应能力不可用，CLI 本身不受影响）："
command -v ffmpeg >/dev/null 2>&1 || \
  echo "  · ffmpeg 未安装：视频/音频工具需要。sudo apt install ffmpeg"
"$VPY" -c 'import rapidocr_onnxruntime' >/dev/null 2>&1 || \
  echo "  · 本地 OCR 后端未装（Linux 上系统 OCR 不存在）：$VPY -m pip install rapidocr-onnxruntime"
echo "  · 桌面操控（computer 工具）只有 Windows 实现，本平台不会注册该工具。"

echo
echo "引导完成。启动：$VPY main.py"
echo "（TEST_COMMAND 不用手动配：默认值已按平台选 .venv/bin/python）"
