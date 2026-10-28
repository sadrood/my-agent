"""终端后台任务：job 注册表 + 日志文件的生命周期管理。

从 TerminalTool 抽出（god class 拆分）：只碰 self._jobs 与临时日志文件；
前台命令执行仍留在 TerminalTool。
"""
import os
import subprocess
import tempfile
import time

from tools.base import ToolResult
from tools.proc_util import kill_process_tree, new_process_group_kwargs


class TerminalJobsMixin:

    def _start_background(self, command: str) -> ToolResult:
        """后台启动命令：stdout/stderr 重定向到临时日志文件，立即返回任务 ID。"""
        out_path = ""
        f_out = None
        try:
            job_id = f"job-{int(time.time() * 1000)}-{len(self._jobs) + 1}"
            out_path = os.path.join(tempfile.gettempdir(), f"my_agent_bg_{job_id}.log")
            f_out = open(out_path, "w", encoding="utf-8", errors="replace")
            evicted = self._prune_jobs()   # 先按上限淘汰老任务（顺带关句柄、删日志）
            proc = subprocess.Popen(
                command, shell=True, stdout=f_out, stderr=subprocess.STDOUT,
                **new_process_group_kwargs(),
            )
        except Exception as e:
            # Popen 抛错时句柄既不 close、也没进 `_jobs`（后续 _prune_jobs 永远碰不到它）—— 每失败一次就漏一个句柄 + 一个空日志文件。
            if f_out is not None:
                try:
                    f_out.close()
                except Exception:
                    pass
            if out_path:
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            return ToolResult(success=False, output="",
                              error=f"后台启动失败: {str(e)[:200]}")
        self._jobs[job_id] = {
            "proc": proc, "command": command, "out_path": out_path,
            "started": time.time(), "handle": f_out,   # 保存句柄，结束/淘汰时关闭
        }
        # 淘汰运行中任务时不静默：被终止的任务本来还能用，必须让人/模型知道
        note = (f"\n注意：后台任务已达上限，已终止最老的 {len(evicted)} 个：{', '.join(evicted)}"
                if evicted else "")
        return ToolResult(
            success=True,
            output=(
                f"已启动后台任务 {job_id}（PID {proc.pid}）。\n"
                f'查看输出: 再调用 terminal 工具，command="bg output {job_id} 30"\n'
                f'结束任务: command="bg kill {job_id}"'
                + note
            ),
        )

    def _bg_dispatch(self, args: str) -> ToolResult:
        parts = args.split()
        sub = parts[0].lower() if parts else ""
        if sub == "list":
            return self._bg_list()
        if sub == "output":
            job_id = parts[1] if len(parts) > 1 else ""
            tail = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 20
            return self._bg_output(job_id, tail)
        if sub == "kill":
            return self._bg_kill(parts[1] if len(parts) > 1 else "")
        return ToolResult(
            success=False, output="",
            error="用法: bg list · bg output <任务ID> [行数] · bg kill <任务ID>",
        )

    def _bg_list(self) -> ToolResult:
        if not self._jobs:
            return ToolResult(success=True, output="（无后台任务）")
        lines = []
        for job_id, job in self._jobs.items():
            proc = job.get("proc")
            alive = bool(proc is not None and proc.poll() is None)
            status = "运行中" if alive else "已结束"
            lines.append(f"- {job_id}  [{status}]  {job['command'][:80]}")
        return ToolResult(success=True, output="后台任务:\n" + "\n".join(lines))

    @staticmethod
    def _is_finished(job: dict) -> bool:
        proc = job.get("proc")
        return proc is None or proc.poll() is not None

    def _prune_jobs(self, max_jobs: int = 20) -> list:
        """把任务表压回 max_jobs 以内，给即将启动的任务留一个位置；返回被终止的 id。

        已结束的先淘汰（关句柄、删日志）。仍超限时淘汰最老的**运行中**任务并终止它：
        运行中任务的日志正被写入、bg output 还要读，不终止就没法回收句柄和文件——
        只留一部分运行中任务却永远攒着句柄，才是真的泄漏。
        """
        if len(self._jobs) < max_jobs:
            return []
        excess = len(self._jobs) - max_jobs + 1
        ordered = sorted(self._jobs.items(), key=lambda kv: kv[1].get("started", 0))
        victims = [jid for jid, job in ordered if self._is_finished(job)][:excess]
        if len(victims) < excess:
            running = [jid for jid, job in ordered if not self._is_finished(job)]
            victims += running[:excess - len(victims)]
        killed = []
        for jid in victims:
            job = self._jobs.get(jid)
            if job is None:
                continue
            if not self._is_finished(job):
                kill_process_tree(job.get("proc"))
                killed.append(jid)
            self._finish_job(job)
            self._jobs.pop(jid, None)
        return killed

    @staticmethod
    def _release_job(job: dict):
        """关闭日志句柄（幂等）。"""
        fh = job.pop("handle", None)
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass

    @classmethod
    def _finish_job(cls, job: dict):
        """任务收尾：关句柄 + 删临时日志（幂等）。"""
        cls._release_job(job)
        path = job.get("out_path")
        if path:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    def _bg_output(self, job_id: str, tail: int = 20) -> ToolResult:
        job = self._jobs.get(job_id or "")
        if job is None:
            return ToolResult(
                success=False, output="",
                error=f"后台任务不存在: {job_id or '（空）'}（用 bg list 查看）",
            )
        # 只读末尾：旧实现 f.readlines() 会把整个日志读进内存，一个刷屏的后台任务（几万行/上百 MB）足以把内存打满。
        try:
            with open(job["out_path"], "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                block = min(size, 256 * 1024)      # 最多回读 256KB
                f.seek(size - block)
                raw = f.read(block)
            text = raw.decode("utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
        except Exception as e:
            return ToolResult(success=False, output="",
                              error=f"读取任务输出失败: {str(e)[:200]}")
        shown = "".join(lines[-tail:]) or "（暂无输出）"
        proc = job.get("proc")
        alive = bool(proc is not None and proc.poll() is None)
        suffix = "（任务仍在运行）" if alive else "（任务已结束）"
        return ToolResult(
            success=True,
            output=f"{job_id} 最近 {tail} 行 {suffix}:\n{shown.rstrip()}",
        )

    def _bg_kill(self, job_id: str) -> ToolResult:
        job = self._jobs.get(job_id or "")
        if job is None:
            return ToolResult(
                success=False, output="",
                error=f"后台任务不存在: {job_id or '（空）'}（用 bg list 查看）",
            )
        proc = job.get("proc")
        if proc is None or proc.poll() is not None:
            self._release_job(job)     # 幂等：关闭日志句柄（最常见的泄漏点）
            return ToolResult(success=True, output=(
                f"{job_id} 已结束（无需终止）。输出仍可查看: bg output {job_id}"))
        kill_process_tree(proc)     # 整棵杀掉；任何失败都在内部降级为 proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        # 关闭日志句柄（修掉"每个后台任务泄漏一个 fd"），但**保留日志文件**：杀掉任务后往往还要看输出排查原因。真正占地方的临时日志由_prune_jobs 在任务表超限时连同文件一起清掉（总量因此有界）。
        self._release_job(job)
        return ToolResult(
            success=True,
            output=f"{job_id} 已终止。输出仍可查看: bg output {job_id}",
        )
