"""任务级 GUI Agent —— 让 agent 像主流桌面 agent 那样「给一句话，自己操作到完成」。

定位（与 tools/computer_use.py 的分工）：
    - computer 工具 = 动作级积木：screenshot / a11y / click / type / key / scroll / window。
      调用方必须自己看屏、自己算坐标、自己判断下一步。
    - 本模块 = 任务级循环：拿到自然语言目标后，内部反复
          「观察（a11y 树 + 截图）→ VLM 决策下一步动作 → 执行 computer 动作 → 再观察」
      直到 VLM 判定 done / call_user 或步数用尽。对外只暴露任务，不暴露坐标。

设计取舍（为什么这样接）：
    - 动作执行全部复用 DesktopTool.execute_json：截图/DPI/用户让步/键鼠注入都走现成实现，
      不重复造轮子，也不绕过它的安全挡板。
    - 观察以 a11y 树为主、截图为辅：a11y 给的是控件名 + 屏幕绝对坐标，比纯视觉估坐标稳；
      截图给 VLM 看整体界面状态和「目标完成了没」。
    - VLM 直接输出严格 JSON 动作（不是先写中文再解析），避免「看图→写描述→再抠坐标」失真。
    - 用户夺回鼠标（ownership=user）时立即暂停并如实报告，不自行抢回。
    - 只读观察不消耗动作额度；危险动作（click/type/key/scroll）才计入步数。

本模块不直接注册为工具；由 tools/gui_task.py 包成 BaseTool 接进工具系统。
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# 决策提示词
# ---------------------------------------------------------------------------

_DECIDE_SYSTEM = """你是一个电脑桌面操作 agent。你只能根据我提供的「无障碍树」和「屏幕截图」判断当前界面，然后决定下一步一个动作。

严格输出一个 JSON 对象（不要 markdown 代码块、不要解释、不要多余文字），格式：
{"reasoning":"一句话说明你看到什么、为什么这么做","action":{"action":"<动作名>", ...参数},"status":"continue"}

action 名和参数（与 computer 工具一致）：
- {"action":"click","x":<整数>,"y":<整数>,"button":"left|right|middle","double":<bool>}
- {"action":"type","text":"<要输入的文本，支持中文>"}
- {"action":"key","combo":"<ctrl+s / enter / alt+tab 等>"}
- {"action":"scroll","x":<整数>,"y":<整数>,"notches":<整数>}
- {"action":"window","title":"<要激活的窗口标题>"}
- {"action":"screenshot","question":"<需要再看清时问的问题>"}

坐标口径：优先用无障碍树里控件的屏幕绝对坐标（形如 (left,top)-(right,bottom)，点击取中心）；
只有树里没有目标时才用截图估坐标，并把中心点作为 x/y。

status 取值：
- "continue"：还要继续操作
- "done"：目标已完成（action 可为空对象 {}）
- "call_user"：需要人来做（登录、验证码、扫码、二选一等），在 reasoning 里说清要人做什么
- "fail"：确认无法完成，reasoning 里说明原因

规则：
1. 一次只做一个动作，做完系统会给你新的观察结果。
2. 目标是「%TASK%」。每步都要对照目标判断：完成了就立刻 status=done，不要多余操作。
3. 只操作目标相关界面，不要碰无关窗口、不要改与原任务无关的文件。
4. 如果上一步动作没有引起任何界面变化，换一种方式（比如改用 a11y 坐标或先激活窗口），不要重复同一个失败动作。
5. 遇到登录/密码/验证码/支付确认，一律 status=call_user，不要尝试猜测或绕过。"""

_OBSERVE_QUESTION = """这是当前屏幕截图。结合我同时给你的无障碍树，回答：当前前台是什么应用/界面？目标「%TASK%」是否已经完成？还有哪些可见的按钮/输入框与这个目标相关？简洁回答，不要给坐标（坐标我会从无障碍树取）。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class GUIStep:
    """一步循环的记录。"""
    index: int
    observation: str = ""          # 本步观察到的界面摘要（a11y + 截图分析）
    reasoning: str = ""            # VLM 为什么这么做
    action: Dict[str, Any] = field(default_factory=dict)
    result: str = ""               # 动作执行结果
    status: str = "continue"
    error: str = ""


@dataclass
class GUITaskState:
    """一次任务的可续跑状态。"""
    task: str
    steps: List[GUIStep] = field(default_factory=list)
    done: bool = False
    status: str = ""               # done / call_user / fail / stopped / max_steps
    summary: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "done": self.done,
            "status": self.status,
            "summary": self.summary,
            "step_count": len(self.steps),
            "steps": [vars(s) for s in self.steps],
        }


# ---------------------------------------------------------------------------
# VLM 决策解析（纯函数，便于单测）
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Optional[dict]:
    """从模型输出里抠出第一个 JSON 对象。容忍 ```json 围栏和前后废话。"""
    if not text:
        return None
    s = text.strip()
    # 去掉 markdown 围栏
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # 先整体试
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # 再找第一个 {...}（取到配对的右括号）
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


_ALLOWED_ACTIONS = {"click", "type", "key", "scroll", "window", "screenshot"}
_MUTATING = {"click", "type", "key", "scroll"}


def parse_decision(text: str) -> Dict[str, Any]:
    """把 VLM 文本解析成规范化决策。永远返回可安全使用的字典。

    返回: {"reasoning":str,"action":dict,"status":"continue|done|call_user|fail|invalid"}
    """
    obj = _extract_json(text)
    if obj is None:
        return {"reasoning": str(text or "")[:300], "action": {}, "status": "invalid"}

    status = str(obj.get("status", "continue")).strip().lower()
    if status not in {"continue", "done", "call_user", "fail"}:
        status = "continue"

    reasoning = str(obj.get("reasoning", ""))[:400]
    action = obj.get("action") or {}
    if not isinstance(action, dict):
        action = {}
    name = str(action.get("action", "")).strip().lower()
    if status == "continue" and name not in _ALLOWED_ACTIONS:
        # 决策无效：不执行任何动作，让上层记一轮错误并重试
        return {"reasoning": reasoning, "action": {}, "status": "invalid"}

    # 规整字段类型，避免模型输出字符串数字导致 OS 层异常
    clean: Dict[str, Any] = {"action": name}
    for k in ("x", "y", "notches"):
        if k in action:
            try:
                clean[k] = int(float(action[k]))
            except (TypeError, ValueError):
                pass
    if "button" in action:
        b = str(action["button"]).lower()
        clean["button"] = b if b in {"left", "right", "middle"} else "left"
    if "double" in action:
        clean["double"] = bool(action["double"])
    if "text" in action:
        clean["text"] = str(action["text"])
    if "combo" in action:
        clean["combo"] = str(action["combo"])
    if "title" in action:
        clean["title"] = str(action["title"])
    if "question" in action:
        clean["question"] = str(action["question"])

    # click 必须有坐标
    if name == "click" and not ("x" in clean and "y" in clean):
        return {"reasoning": reasoning, "action": {}, "status": "invalid"}
    # type 必须有文本
    if name == "type" and not clean.get("text"):
        return {"reasoning": reasoning, "action": {}, "status": "invalid"}
    # key 必须有 combo
    if name == "key" and not clean.get("combo"):
        return {"reasoning": reasoning, "action": {}, "status": "invalid"}

    return {"reasoning": reasoning, "action": clean, "status": status}


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

class GUISession:
    """一次任务级 GUI 循环。可多次 run（续跑），状态保存在 self.state。"""

    def __init__(
        self,
        computer_tool,                       # DesktopTool 实例（已实现 execute_json）
        vision_model=None,                   # VisionModel 实例；None 则内部懒加载
        max_steps: int = 30,
        use_screenshot: bool = True,
        a11y_max_elements: int = 120,
        a11y_max_depth: int = 8,
        stop_event=None,                     # threading.Event，外部可随时停
        event_sink: Optional[Callable[[str, dict], None]] = None,
    ):
        self.computer = computer_tool
        self._vision = vision_model
        self.max_steps = max(1, int(max_steps))
        self.use_screenshot = use_screenshot
        self.a11y_max_elements = a11y_max_elements
        self.a11y_max_depth = a11y_max_depth
        self.stop_event = stop_event
        self.event_sink = event_sink
        self.state: Optional[GUITaskState] = None

    def _emit(self, kind: str, data: dict) -> None:
        if self.event_sink is not None:
            try:
                self.event_sink(kind, data)
            except Exception:
                pass

    def _vision_or_load(self):
        if self._vision is not None:
            return self._vision
        try:
            from models.vision import VisionModel
            self._vision = VisionModel()
        except Exception:
            self._vision = None
        return self._vision

    def _observe(self, task: str) -> str:
        """采集当前界面：a11y 树为主，截图 + VLM 摘要为辅。"""
        parts: List[str] = []

        a11y = self.computer.execute_json({
            "action": "a11y",
            "max_elements": self.a11y_max_elements,
            "max_depth": self.a11y_max_depth,
        })
        if a11y.success and a11y.output.strip():
            parts.append("【无障碍树】\n" + a11y.output.strip())
        else:
            parts.append("【无障碍树】不可用：" + (a11y.error or "无输出"))

        if self.use_screenshot:
            shot = self.computer.execute_json({"action": "screenshot"})
            if shot.success and shot.output.strip():
                parts.append("【截图信息】\n" + shot.output.strip()[:1200])
                # screenshot 返回里已带 [截图: path]，把图片再喂给 VLM 做整体判断
                m = re.search(r"\[截图:\s*([^\]]+)\]", shot.output)
                path = m.group(1).strip() if m else None
                vision = self._vision_or_load()
                if path and vision is not None:
                    try:
                        with open(path, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode("ascii")
                        question = _OBSERVE_QUESTION.replace("%TASK%", task)
                        desc = vision.analyze(b64, question, max_tokens=500,
                                              system_prompt="你是桌面操作助手，回答简洁客观。")
                        parts.append("【视觉判断】\n" + str(desc).strip()[:500])
                    except Exception as e:
                        parts.append("【视觉判断】失败：" + str(e)[:120])
            else:
                parts.append("【截图】不可用：" + (shot.error or "无输出"))

        return "\n\n".join(parts)

    def _decide(self, task: str, observation: str, history: List[GUIStep]) -> Dict[str, Any]:
        vision = self._vision_or_load()
        if vision is None:
            return {"reasoning": "视觉模型不可用", "action": {}, "status": "fail"}

        recent = history[-3:]
        hist_lines = []
        for s in recent:
            hist_lines.append(
                f"- 第{s.index}步: 做了 {json.dumps(s.action, ensure_ascii=False)} → {s.result[:120]}"
            )
        prompt = (
            f"目标：{task}\n\n"
            f"最近的行动历史：\n" + ("\n".join(hist_lines) if hist_lines else "（无，这是第一步）") + "\n\n"
            f"当前界面观察：\n{observation[:4000]}\n\n"
            f"请决定下一步。严格输出一个 JSON 对象。"
        )
        system = _DECIDE_SYSTEM.replace("%TASK%", task)
        try:
            raw = vision.analyze("", prompt, max_tokens=700, system_prompt=system)
        except TypeError:
            # 兼容不带 system_prompt 的旧版 stub（单测里常见）
            try:
                raw = vision.analyze("", prompt, max_tokens=700)
            except Exception as e:
                return {"reasoning": f"决策调用失败: {e}", "action": {}, "status": "fail"}
        except Exception as e:
            return {"reasoning": f"决策调用失败: {e}", "action": {}, "status": "fail"}
        return parse_decision(str(raw))

    def start(self, task: str, window_title: str = "") -> GUITaskState:
        """开一个新任务（重置历史）。"""
        self.state = GUITaskState(task=task)
        if window_title:
            r = self.computer.execute_json({"action": "window", "title": window_title})
            self.state.steps.append(GUIStep(
                index=0,
                reasoning=f"激活目标窗口: {window_title}",
                action={"action": "window", "title": window_title},
                result=(r.output if r.success else r.error),
                status="continue",
            ))
        return self.state

    def run(self, task: str = "", window_title: str = "", max_steps: int = 0) -> GUITaskState:
        """跑任务直到完成/停住。task 为空则续跑当前 state。"""
        if task:
            self.start(task, window_title)
        if self.state is None:
            raise RuntimeError("没有任务：请先调用 start() 或传入 task。")
        state = self.state
        if state.done:
            return state

        cap = min(self.max_steps, max_steps) if max_steps else self.max_steps
        invalid_streak = 0

        while len([s for s in state.steps if s.status != "system"]) < cap:
            if self.stop_event is not None and self.stop_event.is_set():
                state.done = True
                state.status = "stopped"
                state.summary = "任务被用户停止。"
                self._emit("task_end", state.to_dict())
                return state

            step_no = len(state.steps) + 1

            # ---- 观察 ----
            obs = self._observe(state.task)
            self._emit("observation", {"step": step_no, "text": obs[:2000]})

            # ---- 决策 ----
            decision = self._decide(state.task, obs, state.steps)
            step = GUIStep(
                index=step_no,
                observation=obs[:3000],
                reasoning=decision.get("reasoning", ""),
                action=decision.get("action", {}),
                status=decision.get("status", "continue"),
            )

            # ---- 终态判定 ----
            if step.status in ("done", "call_user", "fail"):
                state.done = True
                state.status = step.status
                state.summary = step.reasoning or {
                    "done": "任务已完成。", "call_user": "需要用户介入。", "fail": "无法完成。"
                }[step.status]
                state.steps.append(step)
                self._emit("step", vars(step))
                self._emit("task_end", state.to_dict())
                return state

            if step.status == "invalid":
                invalid_streak += 1
                step.result = "决策无效（未输出合法动作 JSON），本步未执行任何操作。"
                state.steps.append(step)
                self._emit("step", vars(step))
                if invalid_streak >= 3:
                    state.done = True
                    state.status = "fail"
                    state.summary = "视觉模型连续 3 次没给出合法动作，已停止以免空转。"
                    self._emit("task_end", state.to_dict())
                    return state
                continue

            invalid_streak = 0

            # ---- 用户控制权检查（不自行抢回）----
            try:
                from tools.computer_use import ownership, HAND_BACK_HINT
                if ownership() == "user":
                    step.result = f"控制权在用户手里，未执行动作。{HAND_BACK_HINT}"
                    state.steps.append(step)
                    state.done = True
                    state.status = "call_user"
                    state.summary = step.result
                    self._emit("step", vars(step))
                    self._emit("task_end", state.to_dict())
                    return state
            except ImportError:
                pass

            # ---- 执行动作（复用 DesktopTool，安全挡板齐全）----
            result = self.computer.execute_json(step.action)
            step.result = (result.output if result.success else result.error) or ""
            if not result.success and step.action.get("action") in _MUTATING:
                # 用户让路/被接管 → 立即停，不重试
                if "控制权" in step.result or "让出" in step.result or "接管" in step.result:
                    step.result += "（已停止任务，不会自行抢回控制权）"
                    state.steps.append(step)
                    state.done = True
                    state.status = "call_user"
                    state.summary = step.result
                    self._emit("step", vars(step))
                    self._emit("task_end", state.to_dict())
                    return state
            state.steps.append(step)
            self._emit("step", vars(step))

        # 步数用尽
        state.done = True
        state.status = "max_steps"
        state.summary = f"达到步数上限（{cap}），任务可能未完成；可用 continue_task 继续。"
        self._emit("task_end", state.to_dict())
        return state

    def format_result(self, state: GUITaskState) -> str:
        """把任务结果渲染成给主 agent / 用户看的文本。"""
        status_cn = {
            "done": "完成", "call_user": "需要你介入", "fail": "失败",
            "stopped": "已停止", "max_steps": "步数用尽",
        }.get(state.status, state.status)
        lines = [f"[GUI 任务 {status_cn}] {state.summary}", ""]
        lines.append(f"目标：{state.task}")
        lines.append(f"执行步数：{len(state.steps)}")
        lines.append("")
        for s in state.steps:
            act = json.dumps(s.action, ensure_ascii=False) if s.action else "（无动作）"
            lines.append(f"  第{s.index}步 · {s.reasoning[:80]}")
            lines.append(f"    动作：{act}")
            if s.result:
                lines.append(f"    结果：{s.result[:160]}")
        if state.status == "max_steps":
            lines.append("")
            lines.append("提示：任务未确认完成，可让我「继续」接着做。")
        return "\n".join(lines)
