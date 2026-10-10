#!/usr/bin/env bash
# Ubuntu 一键安装 my_agent（幂等：可重复跑，已做的步骤会跳过）
#
#   bash scripts/install-ubuntu.sh              # 安装并自检
#   bash scripts/install-ubuntu.sh --dry-run    # 只打印将要做什么，不改动系统
#   bash scripts/install-ubuntu.sh --no-browser # 跳过 Playwright 内核与系统依赖
#
# 需要 sudo 的只有两处：apt 系统包、Playwright 的浏览器依赖。脚本会明确提示。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

DRY_RUN=0
WITH_BROWSER=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-browser) WITH_BROWSER=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] $*"
  else
    echo "  + $*"
    "$@"
  fi
}

say() { echo; echo "=== $* ==="; }

say "1/6 环境检查"
if [ "$(uname -s)" != "Linux" ]; then
  echo "⚠ 这个脚本只针对 Linux（当前: $(uname -s)）。Windows 用 README 的 cmd 步骤。"
fi
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "✗ 找不到 $PY：sudo apt install -y python3 python3-venv python3-pip"
  exit 1
fi
# 代码里用了 PEP 604 注解（str | None），无 __future__ 兜底 → 必须 3.10+
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "✗ $("$PY" -V) 版本过低：需要 Python 3.10+（Ubuntu 22.04 自带 3.10，24.04 自带 3.12；"
  echo "  20.04 只有 3.8，请用 deadsnakes PPA 装 python3.11 后 PYTHON=python3.11 重跑）"
  exit 1
fi
echo "  ✓ $("$PY" -V) at $(command -v "$PY")"

say "2/6 系统包（需要 sudo：能编译依赖、录屏/OCR 等可选能力用到）"
if command -v apt-get >/dev/null 2>&1; then
  run sudo apt-get update -qq
  run sudo apt-get install -y python3-venv python3-pip git curl
else
  echo "  非 apt 系统，跳过（自行确保有 venv/pip/git）"
fi

say "3/6 Python 虚拟环境 .venv"
if [ -x .venv/bin/python ]; then
  echo "  ✓ 已存在，跳过创建"
else
  run "$PY" -m venv .venv
fi
VENV_PY=".venv/bin/python"
run "$VENV_PY" -m pip install --upgrade pip
run "$VENV_PY" -m pip install -r requirements.txt

say "4/6 浏览器内核（默认 Playwright；ego 后端需另装 dsh-ego-browser）"
if [ "$WITH_BROWSER" = "1" ]; then
  # --with-deps 会调 apt 装 chromium 依赖（需要 sudo）
  run "$VENV_PY" -m playwright install --with-deps chromium
  echo "  可选：中文网页截图更清晰 → sudo apt install -y fonts-noto-cjk"
else
  echo "  已按 --no-browser 跳过"
fi

say "5/6 配置文件 .env"
if [ -f .env ]; then
  echo "  ✓ .env 已存在（不动它）。新版新增的配置项见：bash scripts/upgrade.sh --check"
else
  run cp .env.example .env
  echo "  ⚠ 请编辑 .env 填入 LLM_API_KEY（以及可选端点/浏览器/QQ 等），再跑自检"
fi

say "6/6 环境自检（--doctor 会逐项报告依赖/端点/工具/沙箱/浏览器）"
run "$VENV_PY" main.py --doctor || true

cat <<'EOF'

下一步：
  1) 填 .env（至少 LLM_API_KEY）
  2) .venv/bin/python main.py --doctor        再查一遍
  3) .venv/bin/python main.py "你的任务"      跑第一个任务
  4) bash scripts/upgrade.sh                  以后升级（只快进，不做破坏性动作）

可选：
  bash scripts/install-hooks.sh               启用仓库钩子（同一天重复推送会被拦下）
  examples/systemd/my-agent.service           常驻服务模板 → /etc/systemd/system/ 后
                                              sudo systemctl daemon-reload && systemctl enable --now my-agent
EOF
