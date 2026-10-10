#!/usr/bin/env bash
# 启用仓库自带的 git 钩子（目前只有 pre-push：同一天重复推送会被拦下）
#
#   bash scripts/install-hooks.sh            # 启用
#   bash scripts/install-hooks.sh --uninstall # 关闭
#
# 用 core.hooksPath 指向仓库内的 .githooks/，所以钩子随仓库分发、每个克隆跑一次即可。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--uninstall" ]; then
  git config --unset core.hooksPath || true
  echo "✓ 已关闭仓库钩子（回到默认 .git/hooks）"
  exit 0
fi

chmod +x .githooks/* 2>/dev/null || true
git config core.hooksPath .githooks
echo "✓ 已启用仓库钩子: core.hooksPath = .githooks"
echo "  规则：同一天已在远端推过提交时，再次 push 会被拦下（紧急例外：ALLOW_MULTI_PUSH_TODAY=1）"
