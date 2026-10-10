# 升级到新版本

> 命令总览见 [COMMANDS.md](COMMANDS.md)；这一节只讲升级本身。

项目内置了自我升级（只做 `git pull --ff-only`，**不会** `reset --hard`/合并/强推）：

```cmd
my-agent --upgrade-check        :: 只看有没有新版本，不改动任何东西
my-agent --upgrade              :: 升级（代码 + 依赖），工作区脏则拒绝
my-agent --upgrade --upgrade-stash   :: 脏工作区时自动 stash，升级后恢复
python -m agent.upgrade         :: 等价入口（服务器上可直接这么跑）
bash scripts/upgrade.sh --check :: 自动挑 .venv/bin/python 的包装脚本
```

会话内也可用：`/upgrade`（真升级）、`/upgrade check`（只看）。

**安全边界**：
- 只快进；本地有远端没有的提交时**拒绝升级**并给出两条路（`--rebase` 或先 `git branch` 备份再处理），
  升级器**永不**替你 `reset --hard`；
- 工作区有未提交改动时拒绝（避免你的改动被卷进 pull），除非显式 `--upgrade-stash`；
- 升级前记录 `.env` 的哈希、升级后比对，确认你的密钥文件没被动过（`.env` 本就 gitignore）；
- 升级后列出 `.env.example` 里**新增的配置项**（你的 `.env` 不会被 pull 改动，需要手动补，不补有默认值）；
- `pip install -r requirements.txt` 失败会明确报出，并提示手动重跑（不会静默"升级成功"）。

**升级完记得重启服务**（systemd/pm2/nohup）—— 正在运行的进程仍是旧代码。另外第一次启动可能有一次
models.dev 模型目录的后台拉取；被中断的旧运行可用 `python -m agent.recover --orphans` 查看。
