#!/usr/bin/env bash
# 一键升级：优先用仓库内 venv，其次系统 python3；具体流程交给 agent.upgrade
# （只做 git pull --ff-only，脏工作区默认拒绝，绝不 reset --hard）
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif [ -x venv/bin/python ]; then
  PY=venv/bin/python
else
  PY=python3
fi

echo "· 使用解释器: $PY"
exec "$PY" -m agent.upgrade "$@"
