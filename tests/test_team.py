"""
Team 多Agent协作测试：FakeLLM 脚本化，无网络。

覆盖：
- 串行模式（默认）行为不变：按序执行 + 上下文链
- 并行模式：独立子任务真并发（Barrier 验证），依赖型子任务继承其结果
- DAG 细粒度依赖调度：线性链、菱形（并行分支 + 汇合）、依赖环、失败下游
- 并行安全门：工具不并行安全时回退串行
- Worker 硬超时：超时子任务标记失败，不冻结团队
- Manager 拆解解析：dict 格式（depends_on / independent）与旧字符串格式兼容
"""
import json
import threading
import time

from agent.team import SubTask, Team
from agent.roles import ALL_ROLES


class FakeToolResult:
    def __init__(self, success=True, output="ok", error=""):
        self.success, self.output, self.error = success, output, error


class FakeToolManager:
    """最小工具管理器：可切换 is_parallel_safe 以测试并行安全门。"""

    def __init__(self, parallel_safe=True):
        self.parallel_safe = parallel_safe
        self.executed = []

    def get_all_descriptions(self):
        return "（无工具）"

    def is_parallel_safe(self, tool_name, arguments):
        return self.parallel_safe

    def execute(self, tool_name, tool_input):
        self.executed.append((tool_name, tool_input))
        return FakeToolResult(output=f"{tool_name} ok")


class FakeTeamLLM:
    """
    按消息内容路由的脚本化 LLM：
    - system 含「任务分配经理」→ 返回拆解 JSON
    - system 含「任务汇总专家」→ 返回最终答案
    - 其余视为 Worker 轮次：记录消息、统计并发度、可选 Barrier/睡眠
    """

    def __init__(self, decompose_json, worker_reply="完成",
                 barrier: threading.Barrier = None, worker_sleep: float = 0.0):
        self.decompose_json = decompose_json
        self.worker_reply = worker_reply
        self.barrier = barrier
        self.worker_sleep = worker_sleep
        self.worker_chats = []          # 每次 Worker chat 的 messages
        self._active = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def chat(self, messages, **kwargs):
        system = messages[0]["content"]
        if "任务分配经理" in system:
            return self.decompose_json
        if "任务汇总专家" in system:
            return "最终答案"

        with self._lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            self.worker_chats.append(messages)
            if self.barrier is not None:
                self.barrier.wait(timeout=10)
            if self.worker_sleep:
                time.sleep(self.worker_sleep)
        finally:
            with self._lock:
                self._active -= 1
        return self.worker_reply


def make_team(llm, tool_manager=None):
    team = Team(llm=llm, tool_manager=tool_manager or FakeToolManager())
    return team


def two_independent_json():
    """两个互不依赖的子任务（dict 格式）。"""
    return json.dumps({
        "primary": "writer",
        "secondary": ["researcher"],
        "reasoning": "测试",
        "task_breakdown": [
            {"description": "任务A", "independent": True},
            {"description": "任务B", "independent": True},
        ],
    }, ensure_ascii=False)


class TestDecomposeParsing:
    def test_dict_breakdown_parses_independent_flag(self):
        team = make_team(FakeTeamLLM(two_independent_json()))
        subtasks = team._decompose_task("总任务")
        assert [st.independent for st in subtasks] == [True, True]
        assert [st.description for st in subtasks] == ["任务A", "任务B"]
        assert subtasks[0].assigned_role == "writer"

    def test_string_breakdown_is_conservative_serial(self):
        """旧字符串格式：一律视为依赖型（不并行），保持向后兼容。"""
        llm = FakeTeamLLM(json.dumps({
            "primary": "writer",
            "task_breakdown": ["任务甲", "任务乙"],
        }, ensure_ascii=False))
        team = make_team(llm)
        subtasks = team._decompose_task("总任务")
        assert [st.independent for st in subtasks] == [False, False]

    def test_invalid_json_falls_back_to_single_task(self):
        team = make_team(FakeTeamLLM("不是JSON"))
        subtasks = team._decompose_task("总任务")
        assert len(subtasks) == 1
        assert subtasks[0].assigned_role == "generalist"


class TestSerialMode:
    def test_default_parallel_off_runs_in_order_with_context_chain(self):
        """parallel 缺省 False：按序执行，后序子任务能看到前序结果。"""
        decompose_json = json.dumps({
            "primary": "writer",
            "task_breakdown": ["任务A", "任务B"],
        }, ensure_ascii=False)

        class ChainedLLM(FakeTeamLLM):
            """第二个 Worker 返回不同回复，便于断言上下文链。"""
            def chat(self, messages, **kwargs):
                reply = super().chat(messages, **kwargs)
                if "任务汇总专家" not in messages[0]["content"] \
                        and "任务分配经理" not in messages[0]["content"] \
                        and len(self.worker_chats) >= 2:
                    return "结果B"
                return reply

        llm = ChainedLLM(decompose_json, worker_reply="结果A内容X")
        team = make_team(llm)
        result = team.run("总任务", enable_review=False)

        assert result.success is True
        assert llm.max_concurrent == 1                     # 串行：无并发
        assert result.final_answer == "最终答案"
        # 上下文链：第二个 Worker 的 user 消息里包含第一个的结果
        second_user = llm.worker_chats[1][1]["content"]
        assert "结果A内容X" in second_user


class TestParallelMode:
    def test_independent_subtasks_run_concurrently(self):
        """两个独立子任务并行执行：Barrier 验证真并发，二者都完成。"""
        barrier = threading.Barrier(2)   # 串行执行会等待超时 → BrokenBarrier
        llm = FakeTeamLLM(two_independent_json(), barrier=barrier)
        team = make_team(llm)

        t0 = time.time()
        result = team.run("总任务", parallel=True, enable_review=False)
        elapsed = time.time() - t0

        assert result.success is True
        assert all(st.status == "completed" for st in result.subtasks)
        assert llm.max_concurrent == 2                     # 真并发
        assert elapsed < 8                                 # 没有卡到 Barrier 超时

    def test_dependent_subtask_inherits_wave_results(self):
        """独立子任务先跑完（map），依赖型子任务随后继承其结果（reduce）。"""
        decompose_json = json.dumps({
            "primary": "writer",
            "task_breakdown": [
                {"description": "任务A", "independent": True},
                {"description": "任务B", "independent": False},
            ],
        }, ensure_ascii=False)

        class OrderedLLM(FakeTeamLLM):
            """第二个 Worker 返回不同回复，便于断言有序回喂。"""
            def chat(self, messages, **kwargs):
                reply = super().chat(messages, **kwargs)
                if "任务汇总专家" not in messages[0]["content"] \
                        and "任务分配经理" not in messages[0]["content"] \
                        and len(self.worker_chats) >= 2:
                    return "结果B"
                return reply

        llm = OrderedLLM(decompose_json, worker_reply="结果A内容X")
        team = make_team(llm)
        result = team.run("总任务", parallel=True, enable_review=False)

        assert result.success is True
        # 任务B 的上下文里包含任务A 的结果（有序回喂）
        second_user = llm.worker_chats[1][1]["content"]
        assert "结果A内容X" in second_user

    def test_unsafe_tool_manager_still_runs_parallel_wave(self):
        """非并行安全工具管理器：波次照常并发（工具调用在执行时串行化）。"""
        barrier = threading.Barrier(2)
        llm = FakeTeamLLM(two_independent_json(), barrier=barrier)
        team = make_team(llm, FakeToolManager(parallel_safe=False))
        result = team.run("总任务", parallel=True, enable_review=False)

        assert result.success is True
        assert all(st.status == "completed" for st in result.subtasks)
        assert llm.max_concurrent == 2      # LLM 推理阶段真并发，不整批回退

    def test_unsafe_tool_calls_serialized(self):
        """非并行安全工具：并行波次中按工具名互斥，执行期不重叠。"""
        json_two = json.dumps({
            "primary": "writer",
            "task_breakdown": [
                {"description": "任务A", "independent": True},
                {"description": "任务B", "independent": True},
            ],
        }, ensure_ascii=False)

        class ToolCallLLM:
            """Worker 第 1 轮返回工具调用，第 2 轮返回完成文本。"""

            def __init__(self):
                self.decompose_json = json_two
                self.replies = [
                    json.dumps({"tool": "file_write", "tool_input": "x"}),
                    json.dumps({"tool": "file_write", "tool_input": "y"}),
                ]
                self.barrier = threading.Barrier(2)
                self.max_concurrent = 0
                self._active = 0
                self._lock = threading.Lock()

            def chat(self, messages, **kwargs):
                system = messages[0]["content"]
                if "任务分配经理" in system:
                    return self.decompose_json
                if "任务汇总专家" in system:
                    return "最终答案"
                with self._lock:
                    self._active += 1
                    self.max_concurrent = max(self.max_concurrent, self._active)
                try:
                    if self.replies:
                        reply = self.replies.pop(0)
                    else:
                        reply = "完成"
                    if reply.startswith("{"):
                        self.barrier.wait(timeout=10)
                finally:
                    with self._lock:
                        self._active -= 1
                return reply

        class SlowUnsafeToolManager(FakeToolManager):
            """非并行安全工具：执行 0.3s 并统计执行期并发度。"""

            def __init__(self):
                super().__init__(parallel_safe=False)
                self._tool_active = 0
                self.max_tool_concurrent = 0
                self._tool_lock = threading.Lock()

            def execute(self, tool_name, tool_input):
                with self._tool_lock:
                    self._tool_active += 1
                    self.max_tool_concurrent = max(
                        self.max_tool_concurrent, self._tool_active)
                time.sleep(0.3)
                with self._tool_lock:
                    self._tool_active -= 1
                return FakeToolResult(output=f"{tool_name} ok")

        llm = ToolCallLLM()
        tm = SlowUnsafeToolManager()
        team = make_team(llm, tm)

        t0 = time.time()
        result = team.run("总任务", parallel=True, enable_review=False)
        elapsed = time.time() - t0

        assert result.success is True
        assert all(st.status == "completed" for st in result.subtasks)
        assert llm.max_concurrent == 2          # LLM 阶段并发
        assert tm.max_tool_concurrent == 1      # 工具执行互斥，不重叠
        assert elapsed >= 0.5                   # 两次 0.3s 串行执行的证据

    def test_worker_timeout_marks_failed_without_freezing(self, monkeypatch):
        """Worker 挂死：硬超时后标记失败，团队整体不冻结。"""
        from config import TEAM_CONFIG
        monkeypatch.setitem(TEAM_CONFIG, "worker_timeout", 0.5)

        llm = FakeTeamLLM(two_independent_json(), worker_sleep=5.0)
        team = make_team(llm)

        t0 = time.time()
        result = team.run("总任务", parallel=True, enable_review=False)
        elapsed = time.time() - t0

        assert result.success is False
        assert all(st.status == "failed" for st in result.subtasks)
        assert "超时" in result.subtasks[0].error
        assert elapsed < 4                                 # 没等满 5s 的睡眠


class TestDAGScheduling:
    """DAG 细粒度依赖调度：depends_on 显式声明子任务间的依赖关系。"""

    def test_linear_chain_a_to_b_runs_in_order_with_context(self):
        """线性链 A→B：即使 parallel=True 也按序执行，B 的上下文包含 A 的结果。"""
        decompose_json = json.dumps({
            "primary": "writer",
            "task_breakdown": [
                {"description": "任务A", "depends_on": []},
                {"description": "任务B", "depends_on": ["task_1"]},
            ],
        }, ensure_ascii=False)

        class ChainLLM(FakeTeamLLM):
            """第二个 Worker 返回不同回复，便于断言有序回喂。"""
            def chat(self, messages, **kwargs):
                reply = super().chat(messages, **kwargs)
                if "任务汇总专家" not in messages[0]["content"] \
                        and "任务分配经理" not in messages[0]["content"] \
                        and len(self.worker_chats) >= 2:
                    return "结果B内容"
                return reply

        llm = ChainLLM(decompose_json, worker_reply="结果A内容")
        team = make_team(llm)
        result = team.run("总任务", parallel=True, enable_review=False)

        assert result.success is True
        assert [st.status for st in result.subtasks] == ["completed", "completed"]
        assert llm.max_concurrent == 1                     # 链上每批只有 1 个，绝不并发
        second_user = llm.worker_chats[1][1]["content"]
        assert "结果A内容" in second_user

    def test_diamond_deps_branches_parallel_then_join(self):
        """菱形 A→(B,C)→D：B/C 并发（Barrier 证明），D 最后且上下文含 B、C 结果。"""
        decompose_json = json.dumps({
            "primary": "writer",
            "task_breakdown": [
                {"description": "任务A", "depends_on": []},
                {"description": "任务B", "depends_on": ["task_1"]},
                {"description": "任务C", "depends_on": ["task_1"]},
                {"description": "任务D", "depends_on": ["task_2", "task_3"]},
            ],
        }, ensure_ascii=False)
        barrier = threading.Barrier(2)   # 串行执行会等超时 → BrokenBarrier

        class DiamondLLM(FakeTeamLLM):
            """B/C 并行轮返回各自结果，D 的上下文应同时包含二者。"""
            def chat(self, messages, **kwargs):
                system = messages[0]["content"]
                if "任务分配经理" in system:
                    return self.decompose_json
                if "任务汇总专家" in system:
                    return "最终答案"
                user = messages[1]["content"]
                with self._lock:
                    self._active += 1
                    self.max_concurrent = max(self.max_concurrent, self._active)
                try:
                    self.worker_chats.append(messages)
                    if ("任务B" in user or "任务C" in user) and self.barrier is not None:
                        self.barrier.wait(timeout=10)
                    if "任务A" in user:
                        return "结果A内容"
                    if "任务B" in user:
                        return "结果B内容"
                    if "任务C" in user:
                        return "结果C内容"
                    return "结果D内容"
                finally:
                    with self._lock:
                        self._active -= 1

        llm = DiamondLLM(decompose_json, barrier=barrier)
        team = make_team(llm)

        t0 = time.time()
        result = team.run("总任务", parallel=True, enable_review=False)
        elapsed = time.time() - t0

        assert result.success is True
        assert [st.status for st in result.subtasks] == ["completed"] * 4
        assert llm.max_concurrent == 2                     # B/C 真并发
        assert elapsed < 8                                 # 没卡到 Barrier 超时
        # 调度顺序：A 第一批、D 最后执行
        assert "任务A" in llm.worker_chats[0][1]["content"]
        assert "任务D" in llm.worker_chats[-1][1]["content"]
        # D 的上下文包含 B 和 C 的结果（汇合 + 有序回喂）
        d_user = llm.worker_chats[-1][1]["content"]
        assert "结果B内容" in d_user and "结果C内容" in d_user

    def test_cyclic_deps_marked_failed_without_deadlock(self):
        """依赖环 A↔B：不死循环，两个子任务都标记 failed（依赖无法满足）。"""
        decompose_json = json.dumps({
            "primary": "writer",
            "task_breakdown": [
                {"description": "任务A", "depends_on": ["task_2"]},
                {"description": "任务B", "depends_on": ["task_1"]},
            ],
        }, ensure_ascii=False)

        llm = FakeTeamLLM(decompose_json)
        team = make_team(llm)
        t0 = time.time()
        result = team.run("总任务", parallel=True, enable_review=False)
        elapsed = time.time() - t0

        assert elapsed < 3                                 # 没有死循环
        assert all(st.status == "failed" for st in result.subtasks)
        assert all("依赖" in st.error for st in result.subtasks)
        assert llm.worker_chats == []                      # 无子任务真正执行过

    def test_downstream_of_failed_subtask_marked_failed(self):
        """依赖了 failed 子任务的下游：不可调度，同样标记 failed。"""
        decompose_json = json.dumps({
            "primary": "writer",
            "task_breakdown": [
                {"description": "任务A", "depends_on": []},
                {"description": "任务B", "depends_on": ["task_1"]},
            ],
        }, ensure_ascii=False)

        class ExplodingLLM(FakeTeamLLM):
            """第一个 Worker 直接抛异常（模拟 Worker 执行失败）。"""
            def chat(self, messages, **kwargs):
                reply = super().chat(messages, **kwargs)
                if "任务分配经理" not in messages[0]["content"] \
                        and "任务汇总专家" not in messages[0]["content"] \
                        and len(self.worker_chats) >= 1:
                    raise RuntimeError("worker 崩了")
                return reply

        llm = ExplodingLLM(decompose_json, worker_reply="结果A")
        team = make_team(llm)
        result = team.run("总任务", parallel=False, enable_review=False)

        assert result.subtasks[0].status == "failed"       # 执行本身失败
        assert "worker 崩了" in result.subtasks[0].error
        assert result.subtasks[1].status == "failed"       # 依赖不可满足
        assert "依赖" in result.subtasks[1].error

    def test_old_format_without_depends_on_behaves_as_before(self):
        """旧格式（independent 标记）无 depends_on：行为与改造前完全一致。"""
        decompose_json = json.dumps({
            "primary": "writer",
            "task_breakdown": [
                {"description": "任务A", "independent": True},
                {"description": "任务B", "independent": False},
            ],
        }, ensure_ascii=False)

        class OrderedLLM(FakeTeamLLM):
            def chat(self, messages, **kwargs):
                reply = super().chat(messages, **kwargs)
                if "任务汇总专家" not in messages[0]["content"] \
                        and "任务分配经理" not in messages[0]["content"] \
                        and len(self.worker_chats) >= 2:
                    return "结果B"
                return reply

        llm = OrderedLLM(decompose_json, worker_reply="结果A内容X")
        team = make_team(llm)

        # 拆解层面：独立子任务 depends_on 为空；依赖型子任务依赖全部独立子任务
        subtasks = team._decompose_task("总任务")
        assert [st.depends_on for st in subtasks] == [[], ["task_1"]]

        # 运行层面：依赖型子任务不参与并行波次，且继承独立子任务的结果
        result = team.run("总任务", parallel=True, enable_review=False)
        assert result.success is True
        assert [st.status for st in result.subtasks] == ["completed", "completed"]
        assert llm.max_concurrent == 1                     # 依赖型按序，不并发
        second_user = llm.worker_chats[1][1]["content"]
        assert "结果A内容X" in second_user

    def test_string_breakdown_synthesizes_serial_deps(self):
        """纯字符串旧格式：合成串行依赖，即使 parallel=True 也按序执行。"""
        llm = FakeTeamLLM(json.dumps({
            "primary": "writer",
            "task_breakdown": ["任务甲", "任务乙"],
        }, ensure_ascii=False))
        team = make_team(llm)
        subtasks = team._decompose_task("总任务")
        assert [st.independent for st in subtasks] == [False, False]
        assert [st.depends_on for st in subtasks] == [[], ["task_1"]]

        result = team.run("总任务", parallel=True, enable_review=False)
        assert result.success is True
        assert llm.max_concurrent == 1                     # 旧行为：字符串一律串行


class TestBackwardCompat:
    def test_subtask_dict_includes_independent(self):
        st = SubTask(id="task_1", description="d", assigned_role="writer")
        assert st.independent is False                     # 默认串行语义
        assert "independent" in st.to_dict()

    def test_subtask_dict_includes_depends_on(self):
        st = SubTask(id="task_1", description="d", assigned_role="writer")
        assert st.depends_on == []                         # 默认无依赖
        assert st.to_dict()["depends_on"] == []
        st2 = SubTask(id="task_2", description="d2", assigned_role="writer",
                      depends_on=["task_1"])
        assert st2.to_dict()["depends_on"] == ["task_1"]

    def test_run_result_shape_unchanged(self):
        llm = FakeTeamLLM(two_independent_json())
        team = make_team(llm)
        result = team.run("总任务", parallel=True, enable_review=False)
        # TeamResult 公开字段不变
        for attr in ("success", "final_answer", "subtasks",
                     "review_feedback", "total_time"):
            assert hasattr(result, attr)
        assert result.final_answer == "最终答案"
        # Worker 解析仍走各自角色的系统提示
        assert llm.worker_chats[0][0]["content"] == ALL_ROLES["writer"].system_prompt
