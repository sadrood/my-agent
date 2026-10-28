# 固定任务集评测

在隔离工作区里逐个运行真实任务，用**机器可判定**的验收脚本打分，输出 `pass@1` / `pass@k`。

## 运行

```bash
.venv/Scripts/python eval/run.py --list
.venv/Scripts/python eval/run.py --task fix-off-by-one --runs 3
.venv/Scripts/python eval/run.py --runs 3 --no-fallback --model glm-5.2
```

- 每个任务在 `eval/results/<时间>/workspaces/` 下有自己的工作区副本，agent 的 cwd 就是那里。
- 会话、rollout、运行日志、上下文存储全部写进本次评测目录，不碰仓库的 `memory/`。
- 子进程以仓库 `.env` 为准（会剥掉 `ANTHROPIC_*`，避免 Claude Code 的环境变量把模型顶掉）。
- `--no-fallback` 禁用备用模型链，适合做可复现的横向对比。
- 退出码：全部尝试通过为 0，否则 1（可直接接 CI）。

## 指标

- `pass@1`：所有尝试中通过的比例。
- `pass@k`：每个任务 k 次尝试**全部通过**的任务比例。
- `agent 自报成功`：agent 自己返回 `ok=true` 的比例（只作参考，不参与判定）。

## 任务格式

```
eval/tasks/<id>/
  task.json
  workspace/        # 初始文件，复制到隔离工作区
  check.py          # 可选，自定义验收脚本
```

`task.json`：

```json
{
  "id": "fix-off-by-one",
  "goal": "修复 calc.py，让 pytest 通过。",
  "timeout_seconds": 300,
  "max_ops": 20,
  "checks": [
    {"name": "pytest", "type": "command", "cmd": ["{python}", "-m", "pytest", "-q"]}
  ]
}
```

支持的检查类型：

| type | 字段 | 通过条件 |
|---|---|---|
| `command` | `cmd`, `expect_exit`(默认 0), `timeout` | 退出码等于期望值 |
| `file_contains` | `path`, `text`, `ignore_case` | 文件包含文本 |
| `file_not_contains` | `path`, `text`, `ignore_case` | 文件不包含文本 |
| `file_equals` | `path`, `text`, `strip`(默认 true) | 文件内容完全一致 |
| `file_exists` / `file_absent` | `path` | 文件存在 / 不存在 |
| `custom` | `script`(默认 check.py), `timeout` | 脚本以工作区为参数，退出 0 |

`{python}` 会替换成运行评测的解释器路径。所有检查项必须全部通过，任务才算通过。

## 注意

- 这是**评测隔离**，不是安全沙箱：任务来自本仓库，属于可信输入；不要直接跑不可信来源的 workspace。
- 一次任务的成本和耗时取决于模型与限流，长任务请设置合理的 `timeout_seconds` 与 `max_ops`。
