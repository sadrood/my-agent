"""
Team 多Agent协作模块（Manager-Worker 模式）。

Manager Agent 负责：
1. 分析用户任务
2. 拆解为子任务
3. 分派给合适的 Worker Agent
4. 汇总各 Worker 的产出
5. 必要时让 Reviewer 审查

Worker Agent 是带特定角色的 Agent 实例，专注执行子任务。

协作流程:
    User Task → Manager 拆解 → 分派子任务 → Worker 执行 → 结果汇总 → (可选) Reviewer 审查 → 最终交付
"""
import json
import threading
import time
import queue
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from agent.roles import (
    AgentRole, ALL_ROLES, GENERALIST, REVIEWER,
    MANAGER_ROLE_SELECTION_PROMPT,
)
from config import TEAM_CONFIG
from models.llm import LLM

# ================================================================
# 并行 Worker 的工具互斥锁（实战发现：角色级安全门整批回退过于保守，
# 改为按工具名串行化——LLM 推理并发，有状态工具调用互斥）
# ================================================================
_TOOL_LOCKS: Dict[str, threading.Lock] = {}
_TOOL_LOCKS_GUARD = threading.Lock()


def _get_tool_lock(tool_name: str) -> threading.Lock:
    """按工具名取进程级互斥锁（并行 Worker 共享同一工具实例时串行化调用）。"""
    with _TOOL_LOCKS_GUARD:
        lock = _TOOL_LOCKS.get(tool_name)
        if lock is None:
            lock = threading.Lock()
            _TOOL_LOCKS[tool_name] = lock
        return lock


@dataclass
class SubTask:
    """子任务定义。"""
    id: str
    description: str
    assigned_role: str          # 角色 key
    status: str = "pending"     # pending / running / completed / failed
    result: str = ""
    error: str = ""
    worker_name: str = ""
    independent: bool = False   # True=不依赖其他子任务结果，可并行执行（旧格式标记，兼容保留）
    depends_on: List[str] = field(default_factory=list)  # 依赖的子任务 id 列表（DAG 细粒度调度）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "assigned_role": self.assigned_role,
            "status": self.status,
            "result": self.result[:500] if self.result else "",
            "error": self.error,
            "independent": self.independent,
            "depends_on": list(self.depends_on),
        }


@dataclass
class TeamResult:
    """团队协作结果。"""
    success: bool
    final_answer: str
    subtasks: List[SubTask]
    review_feedback: str = ""
    total_time: float = 0.0


class WorkerAgent:
    """
    Worker Agent：带特定角色的执行单元。
    与主 Agent 共享工具管理器，但有独立的角色系统提示。
    """

    def __init__(self, name: str, role: AgentRole, llm: LLM, tool_manager):
        self.name = name
        self.role = role
        self.llm = llm
        self.tool_manager = tool_manager

    def execute(self, task: str, context: str = "", max_rounds: int = 10,
                emit: Optional[callable] = None) -> str:
        """
        执行一个子任务。

        Args:
            task: 子任务描述
            context: 上下文（之前的任务结果等）
            max_rounds: 最大交互轮数
            emit: 可选事件回调 emit(event_type, data)，用于 Dashboard 实时推送

        Returns:
            执行结果文本
        """
        messages = [
            {"role": "system", "content": self.role.system_prompt},
            {"role": "user", "content": (
                f"任务: {task}\n"
                + (f"上下文:\n{context}\n" if context else "")
                + f"\n可用的工具: {', '.join(self.role.tools)}\n"
                + "请完成这个子任务。如果需要使用工具，请说明工具名和参数。"
            )},
        ]

        result_parts = []
        tool_descriptions = self.tool_manager.get_all_descriptions()

        for _ in range(max_rounds):
            response = self.llm.chat(messages, temperature=self.role.temperature, max_tokens=2000)

            # 尝试解析工具调用
            tool_call = self._parse_tool_call(response)
            if tool_call:
                tool_name, tool_input = tool_call
                # 检查工具权限
                if tool_name not in self.role.tools and tool_name not in ("browser",) and "*" not in self.role.tools:
                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "user", "content": f"工具 '{tool_name}' 不在你的权限范围内。可用工具: {', '.join(self.role.tools)}。请用其他方式完成。"})
                    continue

                if emit:
                    emit("tool_call", {
                        "name": tool_name,
                        "input": str(tool_input)[:200] if not isinstance(tool_input, str) else tool_input[:200],
                        "worker": self.name,
                    })
                if self.tool_manager.is_parallel_safe(tool_name, {}):
                    tool_result = self.tool_manager.execute(tool_name, tool_input)
                else:
                    # 非并行安全工具（共享 Playwright/子进程等有状态实例）：
                    # 并行波次中按工具名互斥串行化，LLM 推理阶段仍然并发
                    with _get_tool_lock(tool_name):
                        tool_result = self.tool_manager.execute(tool_name, tool_input)
                output = tool_result.output if tool_result.success else f"错误: {tool_result.error}"
                if emit:
                    emit("tool_result", {
                        "name": tool_name,
                        "output": output[:300],
                        "success": tool_result.success,
                        "worker": self.name,
                    })
                result_parts.append(f"[{tool_name}] {output[:500]}")

                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": f"工具执行结果:\n{output}"})
            else:
                result_parts.append(response)
                break

        return "\n\n".join(result_parts)

    @staticmethod
    def _parse_tool_call(response: str) -> Optional[tuple]:
        """从 LLM 响应中解析工具调用。"""
        # 尝试 JSON 格式
        try:
            data = json.loads(response)
            if "tool" in data and "tool_input" in data:
                return (data["tool"], data["tool_input"])
        except json.JSONDecodeError:
            pass

        # 尝试 Markdown 代码块格式
        import re
        m = re.search(r'```json\s*\n(.*?)\n```', response, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                if "tool" in data:
                    return (data["tool"], data.get("tool_input", ""))
            except json.JSONDecodeError:
                pass

        # 尝试 TOOL: name PARAMS: input 格式
        m = re.search(r'TOOL:\s*(\w+)\s*\n?\s*PARAMS:\s*(.+)', response, re.IGNORECASE)
        if m:
            return (m.group(1).strip(), m.group(2).strip())

        return None


class Team:
    """
    Agent 团队（Manager-Worker 模式）。

    用法:
        team = Team(llm, tool_manager)
        result = team.run("调研2024年AI Agent发展趋势，写一份报告")
        print(result.final_answer)
    """

    def __init__(self, llm: LLM = None, tool_manager=None):
        self.llm = llm or LLM()
        self.tool_manager = tool_manager
        self.workers: Dict[str, WorkerAgent] = {}
        self._register_default_workers()
        # Dashboard 事件桥接（可选：dashboard 未安装时不影响团队模式运行）
        self._hub = None
        try:
            from dashboard.hub import get_dashboard_hub
            self._hub = get_dashboard_hub()
        except Exception:
            pass

    def _emit(self, event_type: str, data: dict):
        """向 Dashboard Hub 发送事件（静默失败）。"""
        if self._hub is not None:
            try:
                self._hub.emit(event_type, data)
            except Exception:
                pass

    def _register_default_workers(self):
        """注册默认 Worker。"""
        for key, role in ALL_ROLES.items():
            self.workers[key] = WorkerAgent(
                name=role.name,
                role=role,
                llm=self.llm,
                tool_manager=self.tool_manager,
            )

    def run(
        self,
        task: str,
        parallel: bool = False,
        enable_review: bool = True,
    ) -> TeamResult:
        """
        运行团队协作任务。

        Args:
            task: 用户任务
            parallel: 是否并行执行独立子任务
            enable_review: 是否启用 Reviewer 审查

        Returns:
            TeamResult
        """
        start_time = time.time()

        from agent.ui_theme import print_info, print_warning

        # Dashboard 事件：运行开始
        self._emit("run_start", {
            "mode": "team",
            "task": task,
            "goal": task,
            "agent": "Team",
            "timestamp": time.time(),
        })

        # 1. Manager 分析任务，选择角色，拆解子任务
        print_info(f"Team · Manager 分析任务: {task[:80]}", style="primary")
        subtasks = self._decompose_task(task)

        if not subtasks:
            self._emit("run_end", {
                "mode": "team",
                "status": "failed",
                "error": "无法拆解任务。",
                "total_time": time.time() - start_time,
            })
            return TeamResult(
                success=False,
                final_answer="无法拆解任务。",
                subtasks=[],
                total_time=time.time() - start_time,
            )

        print_info(f"Team · 拆解为 {len(subtasks)} 个子任务")
        for st in subtasks:
            print_info(f"    [{st.assigned_role}] {st.description[:60]}")

        # Dashboard 事件：计划（子任务总览，在前端渲染为步骤条）
        self._emit("plan", {
            "mode": "team",
            "steps": [st.to_dict() for st in subtasks],
            "summary": f"拆解为 {len(subtasks)} 个子任务",
        })

        # 2. 执行子任务（DAG 细粒度依赖调度）
        #    每轮收集「依赖的子任务全部已完成」的子任务为一批：
        #    - 批内 >=2 个且 parallel=True -> 用 _run_parallel_wave 并发执行
        #    - 否则按序 _execute_subtask
        #    上一批结果按子任务顺序累进 context（保持有序回喂），循环直到调度完。
        #    若某轮找不到任何可调度子任务但仍有未完成子任务（依赖环，或依赖了
        #    失败的子任务），把这些子任务标记 failed（error 写明依赖无法满足），
        #    继续调度其余任务，绝不死循环。
        context = ""
        id_to_task = {st.id: st for st in subtasks}

        while any(st.status == "pending" for st in subtasks):
            batch = [
                st for st in subtasks
                if st.status == "pending"
                and all(d in id_to_task and id_to_task[d].status == "completed"
                        for d in st.depends_on)
            ]
            if not batch:
                # 剩余未完成子任务不可调度：依赖环或依赖了失败/不存在的子任务
                for st in subtasks:
                    if st.status == "pending":
                        st.status = "failed"
                        st.error = "依赖无法满足（存在依赖环，或所依赖的子任务失败/不存在）"
                        print_warning(f"Team · {st.id} 依赖无法满足，标记失败")
                break
            if parallel and len(batch) >= 2:
                print_info(f"Team · 并行执行 {len(batch)} 个子任务"
                           f"（非并行安全工具自动串行化）", style="accent")
                context += self._run_parallel_wave(batch, context)
            else:
                for st in batch:
                    context += self._execute_subtask(st, context)

        # 3. Reviewer 审查（可选）
        review_feedback = ""
        if enable_review and len(subtasks) > 1:
            reviewer = self.workers.get("reviewer")
            if reviewer:
                print_info("Team · 审校进行质量审查", style="accent")
                completed_results = "\n\n".join(
                    f"[{st.worker_name}] {st.description}\n{st.result[:800]}"
                    for st in subtasks if st.status == "completed"
                )
                self._emit("step_start", {
                    "mode": "team",
                    "id": "review",
                    "title": "Reviewer · 质量审查",
                    "worker": reviewer.name,
                    "role": "reviewer",
                    "description": "审查各子任务执行结果并给出改进建议",
                })
                review_feedback = reviewer.execute(
                    task="审查以下子任务的执行结果，检查质量并给出改进建议",
                    context=f"原始任务: {task}\n\n各子任务结果:\n{completed_results}",
                    emit=lambda t, d: self._emit(t, {**d, "mode": "team", "subtask": "review"}),
                )
                self._emit("step_end", {
                    "mode": "team",
                    "id": "review",
                    "status": "completed",
                    "worker": reviewer.name,
                    "result": review_feedback[:300],
                })
                print_info("Team · 审校完成", style="success")

        # 4. Manager 汇总最终结果
        final_answer = self._synthesize(task, subtasks, review_feedback)

        self._emit("run_end", {
            "mode": "team",
            "status": "completed" if any(st.status == "completed" for st in subtasks) else "failed",
            "answer": final_answer,
            "total_time": time.time() - start_time,
            "subtask_count": len(subtasks),
        })

        return TeamResult(
            success=any(st.status == "completed" for st in subtasks),
            final_answer=final_answer,
            subtasks=subtasks,
            review_feedback=review_feedback,
            total_time=time.time() - start_time,
        )

    # ================================================================
    # 子任务执行（串行 / 并行共用）
    # ================================================================

    def _execute_subtask(self, st: SubTask, context: str) -> str:
        """
        执行单个子任务，返回追加到后续子任务上下文的片段（失败返回空串）。
        Dashboard 事件与状态更新与旧串行实现保持一致。
        """
        from agent.ui_theme import print_info, print_warning

        worker = self.workers.get(st.assigned_role)
        if worker is None:
            worker = self.workers["generalist"]

        st.status = "running"
        st.worker_name = worker.name

        # Dashboard 事件：子任务开始
        self._emit("step_start", {
            "mode": "team",
            "id": st.id,
            "title": f"{worker.name} · {st.description[:60]}",
            "worker": worker.name,
            "role": st.assigned_role,
            "description": st.description,
        })

        print_info(f"Team · {worker.name} 执行: {st.description[:60]}", style="accent")
        try:
            st.result = worker.execute(
                task=st.description,
                context=context,
                emit=lambda t, d, _s=st: self._emit(t, {**d, "mode": "team", "subtask": _s.id}),
            )
            st.status = "completed"
            self._emit("step_end", {
                "mode": "team",
                "id": st.id,
                "status": "completed",
                "worker": worker.name,
                "result": st.result[:300],
            })
            print_info(f"Team · {worker.name} 完成 ({len(st.result)} 字符)", style="success")
            return f"\n[{worker.name}] 完成: {st.result[:300]}\n"
        except Exception as e:
            st.status = "failed"
            st.error = str(e)
            self._emit("step_end", {
                "mode": "team",
                "id": st.id,
                "status": "failed",
                "worker": worker.name,
                "error": str(e),
            })
            print_warning(f"Team · {worker.name} 失败: {e}")
            return ""

    def _run_parallel_wave(self, subtasks: List[SubTask], context: str) -> str:
        """
        并发执行一批独立子任务，返回按子任务原顺序拼接的上下文片段
        （有序回喂，保证汇总阶段结果顺序确定）。

        用 daemon 线程并行（与 executor._run_tool_calls_parallel 同思路：
        ThreadPoolExecutor 的 with 退出会等待卡死的 worker 导致冻结）。
        join 带硬超时：超时的子任务标记失败，不拖死整个团队。
        """
        results: dict = {}

        def _run(st: SubTask):
            try:
                results[st.id] = self._execute_subtask(st, context)
            except BaseException as e:   # noqa: BLE001
                results[st.id] = ""

        timeout = TEAM_CONFIG.get("worker_timeout", 900)
        deadline = time.time() + timeout
        threads = []
        for st in subtasks:
            t = threading.Thread(target=_run, args=(st,), daemon=True,
                                 name=f"team-worker-{st.id}")
            t.start()
            threads.append(t)
        for t in threads:
            t.join(max(0.0, deadline - time.time()))

        for st, t in zip(subtasks, threads):
            if t.is_alive() and st.status == "running":
                st.status = "failed"
                st.error = f"Worker 执行超时（>{timeout:.0f}s）"
                self._emit("step_end", {
                    "mode": "team",
                    "id": st.id,
                    "status": "failed",
                    "worker": st.worker_name,
                    "error": st.error,
                })
                from agent.ui_theme import print_warning
                print_warning(f"Team · {st.worker_name} {st.error}")
        return "".join(results.get(st.id, "") for st in subtasks)

    def _decompose_task(self, task: str) -> List[SubTask]:
        """Manager 使用 LLM 拆解任务。"""
        role_descriptions = "\n".join(
            f"- {key}: {role.description}"
            for key, role in ALL_ROLES.items()
        )

        messages = [
            {"role": "system", "content": MANAGER_ROLE_SELECTION_PROMPT.format(
                role_descriptions=role_descriptions
            )},
            {"role": "user", "content": f"请分析以下任务:\n{task}"},
        ]

        try:
            response = self.llm.chat(messages, temperature=0.3, max_tokens=1500)
            data = self._parse_json(response)

            if not data:
                return self._fallback_decompose(task)

            primary = data.get("primary", "generalist")
            secondary = data.get("secondary", [])
            breakdown = data.get("task_breakdown", [task])

            subtasks = []
            explicit_depends: Dict[str, bool] = {}
            for item in breakdown:
                role = primary if not subtasks else (
                    secondary[len(subtasks) - 1] if len(subtasks) - 1 < len(secondary) else "generalist"
                )
                # 兼容三种格式：
                #   1) dict 含 depends_on（DAG 显式依赖，depends_on 缺省为 []）
                #   2) dict 含 independent 标记（旧格式，无 depends_on）
                #   3) 纯字符串（旧格式，等价于 conservative 依赖型）
                if isinstance(item, dict):
                    desc = str(item.get("description", "")).strip()
                    independent = bool(item.get("independent", False))
                    raw_depends = item.get("depends_on")
                    if raw_depends is not None:
                        depends = [str(d).strip() for d in raw_depends if str(d).strip()]
                        independent = not depends
                        has_explicit = True
                    else:
                        depends = []
                        has_explicit = False
                else:
                    desc = str(item).strip()
                    independent = False
                    depends = []
                    has_explicit = False
                if not desc:
                    continue
                st = SubTask(
                    id=f"task_{len(subtasks) + 1}",
                    description=desc,
                    assigned_role=role,
                    independent=independent,
                    depends_on=depends,
                )
                subtasks.append(st)
                explicit_depends[st.id] = has_explicit

            # 旧格式（无显式 depends_on）向后兼容：按旧两阶段语义合成依赖，
            # 保证「独立子任务先行、依赖型随后按序」的旧行为完全不变。
            #   independent=True  -> 无依赖（首批执行）
            #   independent=False -> 依赖全部独立子任务 + 列表中更靠前的旧格式依赖型
            #                        子任务，从而在 DAG 调度下等价于旧的两阶段顺序。
            independent_ids = [st.id for st in subtasks if st.independent]
            for i, st in enumerate(subtasks):
                if explicit_depends.get(st.id, False) or st.independent:
                    continue
                st.depends_on = list(independent_ids) + [
                    prev.id for prev in subtasks[:i]
                    if not prev.independent and not explicit_depends.get(prev.id, False)
                ]

            return subtasks

        except Exception as e:
            from agent.ui_theme import print_warning
            print_warning(f"Team · 任务拆解失败: {e}")
            return self._fallback_decompose(task)

    def _fallback_decompose(self, task: str) -> List[SubTask]:
        """回退：不拆解，直接分配给通用助手。"""
        return [SubTask(
            id="task_1",
            description=task,
            assigned_role="generalist",
        )]

    def _synthesize(self, task: str, subtasks: List[SubTask], review: str) -> str:
        """汇总子任务结果为最终答案。"""
        results_text = "\n\n---\n\n".join(
            f"### {st.worker_name} ({st.assigned_role})\n"
            f"状态: {st.status}\n"
            f"结果:\n{st.result[:1500]}"
            for st in subtasks
        )

        messages = [
            {"role": "system", "content": "你是一个任务汇总专家。请根据各子任务的执行结果，整合成一份完整的最终答案。"},
            {"role": "user", "content": (
                f"原始任务: {task}\n\n"
                f"子任务结果:\n{results_text}\n\n"
                + (f"审校建议:\n{review}\n\n" if review else "")
                + "请整合以上信息，输出最终答案。"
            )},
        ]

        try:
            return self.llm.chat(messages, temperature=0.5, max_tokens=3000)
        except Exception as e:
            return f"汇总失败: {str(e)}\n\n各子任务结果:\n{results_text}"

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        """从文本中解析 JSON。"""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            import re
            m = re.search(r'\{[\s\S]*\}', text)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
        return None
