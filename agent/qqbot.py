"""QQ 机器人桥（官方 API，走腾讯 `qq-botpy` SDK）：用 QQ 私聊 / 群内 @ 驱动本 agent。

安全是硬编码的，不给配置绕过：
- 私聊直接可用；**群聊默认关闭**（进群=群里任何人可远程使唤这台电脑），
  开了也要过"群白名单 + 发言人白名单"两道；
- `QQBOT_ALLOW_FROM` 白名单（openid，逗号分隔）——为空时拒绝所有人并在日志里提示怎么加；
- 每个 QQ 用户**独立会话**，互不串上下文；
- 权限档只允许 `ask` / `block`，**不接受 full**（来自聊天通道的请求不允许提权）；
- AppID/AppSecret 只从环境变量读，日志与回执都不打印密钥。

平台硬约束（决定了实现形态）：
- 被动回复有效期 **单聊 60 分钟 / 群聊 5 分钟**，超窗口只能改用主动消息，而主动消息
  **每个用户/每个群每月仅 4 条**，极其稀缺——所以窗口要按场景分开算，别动不动就降级；
- 同一条消息最多回复 **5 次**（2026-01 更新说明调整为 4 次），超出会导致整条发送失败；
- 同一 `msg_id` 下 `msg_seq` 必须递增，重复会发送失败——分块回复靠它区分；
- 新机器人默认只在**沙箱**：`QQBOT_SANDBOX` 连错环境会"连上了却收不到任何消息"。

启动：`python -m agent.qqbot`（需要 `pip install qq-botpy`）
"""
import asyncio
import hashlib
import os
import threading
import time
from typing import Dict, List, Optional

#: QQ 单条消息安全长度（官方上限更大，这里留足余量并保证可读）
CHUNK_CHARS = 800
#: 一轮任务的等待上限（秒）：到点给 agent 置停止信号，优雅收尾而不是硬杀
TASK_TIMEOUT = 1800
#: 审批等待上限（秒）
APPROVE_TIMEOUT = 180
#: 被动回复有效期（官方：单聊 60 分钟 / 群聊 5 分钟）。各留一分钟余量，超了才改主动消息。
#: 主动消息极稀缺（单聊/群聊各每月 4 条），能被动回复就别动用它。
PASSIVE_REPLY_WINDOW_C2C = 3540.0
PASSIVE_REPLY_WINDOW_GROUP = 240.0
#: 同一条消息最多回复几条（官方写 5 次；2026-01 更新说明调整为 4 次，取更严的）
MAX_REPLIES_PER_MESSAGE = 4
#: 允许的权限档（不给 full：聊天通道不允许提权）
ALLOWED_PRESETS = ("ask", "block")

#: 会话模式：task = 发什么都当任务执行；chat = 直接说话只聊天（干活要 /do）
ALLOWED_MODES = ("task", "chat")
#: 短问候一律走对话、不当任务执行（"你好"被当成目标去开浏览器是最容易踩的坑）
CHITCHAT_HINTS = ("你好", "您好", "hi", "hello", "hiya", "在吗", "在么", "在不在", "哈喽",
                  "嗨", "早", "早安", "晚安", "谢谢", "thanks", "thank you", "辛苦了")
CHITCHAT_MAX_CHARS = 12


def looks_like_chitchat(text: str) -> bool:
    """是不是一句寒暄：短 + 命中问候词。任务描述通常更长，不会被误判。"""
    body = str(text or "").strip().lower()
    if not body or len(body) > CHITCHAT_MAX_CHARS:
        return False
    return any(hint in body for hint in CHITCHAT_HINTS)


def _chunk(text: str, limit: int = CHUNK_CHARS) -> List[str]:
    """把长回复切成多条（QQ 单条有长度限制），保留换行边界。"""
    body = str(text or "").strip()
    if not body:
        return ["（没有输出）"]
    out, buf = [], ""
    for line in body.splitlines(keepends=True):
        if len(buf) + len(line) > limit and buf:
            out.append(buf.rstrip())
            buf = ""
        while len(line) > limit:                    # 单行超长：硬切
            out.append(line[:limit])
            line = line[limit:]
        buf += line
    if buf.strip():
        out.append(buf.rstrip())
    return out or ["（没有输出）"]


async def _log_send_errors(coro, seq: int, passive: bool) -> None:
    """等待发送协程，把失败记进日志，别让它变成"从未被取回的异常"。"""
    try:
        await coro
    except Exception as e:                          # noqa: BLE001
        from agent import run_log
        run_log.log(f"QQ 回复发送失败（第 {seq} 块，被动={passive}）："
                    f"{type(e).__name__}: {str(e)[:160]}", "warn")


def _chunks_capped(text: str, limit: int = MAX_REPLIES_PER_MESSAGE) -> List[str]:
    """切块并卡住总条数：平台对同一条消息的回复次数有上限，多出来的只能丢。

    超限继续发会整条失败（平台报超频），不如把尾部收成一句提示，
    让用户知道结果被截断、可以去哪取完整的。
    """
    limit = max(1, int(limit))
    pieces = _chunk(text)
    if len(pieces) <= limit:
        return pieces
    dropped = sum(len(p) for p in pieces[limit:])
    kept = pieces[:limit]
    kept[-1] = f"{kept[-1]}\n…（内容过长，已省略约 {dropped} 字；完整结果可用 /status 取）"
    return kept


class QQBotConfig:
    """桥配置：默认读 config.QQBOT_CONFIG（来自 .env），也可注入 dict（测试用）。"""

    def __init__(self, env: Optional[dict] = None):
        if env is None:
            from config import QQBOT_CONFIG
            env = QQBOT_CONFIG
        self.app_id = str(env.get("QQBOT_APP_ID") or env.get("app_id") or "").strip()
        self.secret = str(env.get("QQBOT_APP_SECRET") or env.get("app_secret") or "").strip()
        raw_allow = env.get("QQBOT_ALLOW_FROM") or env.get("allow_from") or ""
        if isinstance(raw_allow, (list, tuple)):
            self.allow_from = [str(x).strip() for x in raw_allow if str(x).strip()]
        else:
            self.allow_from = [x.strip() for x in str(raw_allow).split(",") if x.strip()]
        raw_groups = env.get("QQBOT_ALLOW_GROUPS") or env.get("allow_groups") or ""
        if isinstance(raw_groups, (list, tuple)):
            self.allow_groups = [str(x).strip() for x in raw_groups if str(x).strip()]
        else:
            self.allow_groups = [x.strip() for x in str(raw_groups).split(",") if x.strip()]
        preset = str(env.get("QQBOT_PERMISSION") or env.get("permission") or "ask").strip().lower()
        self.preset = preset if preset in ALLOWED_PRESETS else "ask"
        self.max_chars = int(env.get("QQBOT_MAX_CHARS") or env.get("max_chars") or 2000)
        self.per_minute = int(env.get("QQBOT_RATE_PER_MINUTE") or env.get("rate_per_minute") or 6)
        self.task_timeout = int(env.get("QQBOT_TASK_TIMEOUT")
                                or env.get("task_timeout") or TASK_TIMEOUT)
        # 新机器人默认只存在于沙箱：连错环境会"连上了却收不到任何消息"，所以默认沙箱
        raw_env = str(env.get("QQBOT_SANDBOX", env.get("sandbox", "true"))).strip().lower()
        self.sandbox = raw_env not in ("false", "0", "no", "off")
        allow_group = env.get("QQBOT_ALLOW_GROUP", env.get("allow_group", False))
        self.allow_group = (allow_group is True
                            or str(allow_group).strip().lower() == "true")
        self.workspace = str(env.get("QQBOT_WORKSPACE") or env.get("workspace") or "").strip()
        # ---- 实时画面 / 截图（A+B）----
        # 面板地址：桥把画面推给它，面板的「实时画面」与取图端点才看得到非本进程的浏览器
        self.dashboard_url = str(env.get("QQBOT_DASHBOARD_URL")
                                 or env.get("dashboard_url") or "").strip()
        self.frame_token = str(env.get("QQBOT_DASHBOARD_TOKEN")
                               or env.get("dashboard_token") or "").strip()
        # 给你点开看的链接（留空则用 dashboard_url）
        self.live_url = str(env.get("QQBOT_LIVE_URL") or env.get("live_url") or "").strip()
        # 任务结束/收到 /shot 时把截图发到 QQ；公网可取图的地址是备用上传通道
        raw_send = env.get("QQBOT_SEND_FRAME", env.get("send_frame", True))
        self.send_frame = not (raw_send is False
                               or str(raw_send).strip().lower() in ("false", "0", "no", "off"))
        self.public_frame_url = str(env.get("QQBOT_PUBLIC_FRAME_URL")
                                    or env.get("public_frame_url") or "").strip()
        self.push_seconds = max(0.5, float(env.get("QQBOT_FRAME_PUSH_SECONDS")
                                           or env.get("frame_push_seconds") or 2.0))
        # ---- 对话 / 任务 ----
        # 默认模式：task = 发什么都当任务；chat = 直接说话只聊天（寒暄两种模式下都走对话）
        mode = str(env.get("QQBOT_DEFAULT_MODE") or env.get("default_mode") or "task").strip().lower()
        self.default_mode = mode if mode in ALLOWED_MODES else "task"
        self.chat_turns = max(1, int(env.get("QQBOT_CHAT_TURNS") or env.get("chat_turns") or 10))
        self.chat_max_tokens = max(64, int(env.get("QQBOT_CHAT_MAX_TOKENS")
                                           or env.get("chat_max_tokens") or 1000))
        # 闲聊引擎：agent（默认）= 与任务同一个会话（上下文共享、落盘、可用工具）；
        # llm = 轻量纯聊天（不落盘、不带工具，省 token）
        engine = str(env.get("QQBOT_CHAT_ENGINE")
                     or env.get("chat_engine") or "agent").strip().lower()
        self.chat_engine = engine if engine in ("agent", "llm") else "agent"

    def frame_url(self) -> str:
        """给 QQ 抓图用的带 token 地址（需面板能被公网/QQ 服务器访问）。"""
        if not (self.dashboard_url and self.frame_token):
            return ""
        return f"{self.dashboard_url.rstrip('/')}/api/frame.png?token={self.frame_token}"

    def live_link(self) -> str:
        """给你点开看实时画面的链接。"""
        return self.live_url or self.dashboard_url

    def __repr__(self) -> str:                      # 绝不把 secret 带进日志
        return (f"QQBotConfig(app_id={'*' * len(self.app_id)}, secret=***, "
                f"allow_from={len(self.allow_from)} 个, 群={len(self.allow_groups)} 个, "
                f"preset={self.preset}, per_minute={self.per_minute}, "
                f"group={self.allow_group}, mode={self.default_mode}, "
                f"env={'沙箱' if self.sandbox else '正式'})")

    def ready(self) -> bool:
        return bool(self.app_id and self.secret)


class UserSession:
    """一个 QQ 用户 ↔ 一个会话（独立 Agent 实例，权限档锁死在 ask/block）。"""

    def __init__(self, openid: str, preset: str = "ask", verbose: bool = False,
                 approver=None, task_timeout: float = TASK_TIMEOUT,
                 mode: str = "task", chat_turns: int = 10, chat_max_tokens: int = 1000):
        self.openid = openid
        self.preset = preset
        self.mode = mode if mode in ALLOWED_MODES else "task"
        self.chat_turns = max(1, int(chat_turns))
        self.chat_max_tokens = max(64, int(chat_max_tokens))
        self.chat_history: list = []
        self.agent = None
        self.lock = threading.Lock()
        self.last_run_at = 0.0
        self.turns = 0
        self.last_output = ""
        self.task_timeout = task_timeout       # 秒；<=0 表示不限时
        self.timed_out = False
        self._approver = approver
        self._verbose = verbose

    def session_name(self) -> str:
        digest = hashlib.sha256(self.openid.encode("utf-8")).hexdigest()[:10]
        return f"qq-{digest}"

    def ensure_agent(self):
        """懒建 Agent：权限档在这里强制设定，且不允许被环境变量提权。"""
        if self.agent is not None:
            return self.agent
        from agent.agent import Agent, AgentConfig
        config = AgentConfig(
            verbose=self._verbose,
            stream_enabled=False,
            approval_policy="on-request",       # 需要确认的操作一定问
            approval_honor_on_request=True,     # 且必须尊重"必须人工确认"
            approver=self._approver,            # 疑问推到 QQ，由用户回 y/a/n
            session_name=self.session_name(),
        )
        agent = Agent(config=config)
        try:
            agent.approval.apply_preset(self.preset)   # ask / block（不含 full）
        except Exception:                              # noqa: BLE001
            pass
        self.agent = agent
        return agent

    def run(self, goal: str) -> str:
        """执行一条任务（同一用户串行，避免并发改同一工作区）。

        超时不硬杀线程（杀不掉，还会留下操作同一工作区的孤儿线程），
        而是给 agent 的 stop_event 置位——它在下一个检查点优雅收尾，已完成成果保留。
        """
        with self.lock:
            agent = self.ensure_agent()
            self.last_run_at = time.time()
            self.turns += 1
            self.timed_out = False
            stop_event = threading.Event()
            timer = None
            if self.task_timeout and self.task_timeout > 0:
                timer = threading.Timer(self.task_timeout, stop_event.set)
                timer.daemon = True
                timer.start()
            try:
                self.last_output = str(agent.run(goal, keep_session=True,
                                                 stop_event=stop_event) or "")
                if stop_event.is_set():
                    self.timed_out = True
                    self.last_output = (f"任务超过 {int(self.task_timeout)} 秒，已中断"
                                        "（已完成的成果保留）。\n\n") + self.last_output
            except Exception as e:                     # noqa: BLE001
                self.last_output = f"执行失败：{type(e).__name__}: {str(e)[:200]}"
            finally:
                if timer is not None:
                    timer.cancel()
            return self.last_output

    def screenshot(self) -> tuple:
        """抓当前页面一帧，返回 (base64, 格式)；拿不到就返回 ("", "png")。"""
        agent = self.agent
        if agent is None:
            return "", "png"
        try:
            tool = agent.tool_manager.get_tool("browser")
        except Exception:                            # noqa: BLE001
            tool = None
        if tool is None or not hasattr(tool, "execute"):
            return "", "png"
        try:
            result = tool.execute("screenshot_base64")
        except Exception:                            # noqa: BLE001
            return "", "png"
        if not getattr(result, "success", False):
            return "", "png"
        meta = getattr(result, "metadata", None) or {}
        return str(meta.get("screenshot_base64") or ""), "png"

    def chat(self, text: str) -> str:
        """纯对话：只调 LLM 聊天，不注册工具、不起浏览器、不写盘。

        走 agent 的同一个 LLM 实例（沿用端点/备用链/限流重试），但不进工具循环。
        """
        from models.prompts import QQ_CHAT_SYSTEM_PROMPT
        agent = self.ensure_agent()
        self.chat_history.append({"role": "user", "content": text})
        messages = [{"role": "system", "content": QQ_CHAT_SYSTEM_PROMPT},
                    *self.chat_history[-self.chat_turns * 2:]]
        reply = str(agent.llm.chat(messages, max_tokens=self.chat_max_tokens) or "").strip()
        self.chat_history.append({"role": "assistant", "content": reply})
        return reply


class QQBotBridge:
    """把 QQ 私聊消息接到 agent；审批通过"回复 y/a/n"完成。"""

    def __init__(self, config: Optional[QQBotConfig] = None, runner=None):
        self.config = config or QQBotConfig()
        self.sessions: Dict[str, UserSession] = {}
        self._recent: Dict[str, List[float]] = {}
        self._pending: Dict[str, dict] = {}
        self._always: Dict[str, set] = {}            # 用户选了"始终允许"的工具
        self._ctx: Dict[str, dict] = {}              # 会话 → 发送上下文（api/msg_id/loop/收信时刻）
        self._seq: Dict[str, dict] = {}             # 会话 → {msg_id, next}：同一 msg_id 下 msg_seq 持续递增
        self._api = None                            # botpy 的 message._api（兼容单会话调用）
        self._msg_id = ""
        self._loop = None
        self.approve_timeout = APPROVE_TIMEOUT       # 审批等待上限（测试可调小）
        self._run_task = runner                     # 可注入（测试/替换执行体）

    # ---------------- 门控 ----------------

    @staticmethod
    def scope_key(user_openid: str, group_openid: str = "") -> str:
        """会话/限流/审批都按这个键隔离：群里的每个人各自一份上下文。"""
        return f"g:{group_openid}:{user_openid}" if group_openid else f"c:{user_openid}"

    @staticmethod
    def norm_key(key: str) -> str:
        """把裸 openid 也当成私聊键（调用方少一层心智负担）。"""
        value = str(key or "")
        return value if value[:2] in ("c:", "g:") else f"c:{value}"

    def remember_context(self, key: str, message, loop=None) -> None:
        """记下这条消息的发送上下文：api、msg_id、收信时刻。

        必须按会话存：群聊与私聊并发时，实例级字段会被后到的消息覆盖，
        审批问题就会发进别人的窗口。
        """
        key = self.norm_key(key)
        self._api = getattr(message, "_api", None)
        self._msg_id = str(getattr(message, "id", "") or "")
        self._loop = loop
        self._ctx[key] = {"api": self._api, "msg_id": self._msg_id,
                          "loop": loop, "recv_at": time.time()}

    def _ctx_for(self, key: str) -> dict:
        """取该会话的发送上下文；无记录时回退实例字段（兼容直接调用发送的场景）。"""
        return self._ctx.get(self.norm_key(key)) or {
            "api": self._api, "msg_id": self._msg_id,
            "loop": self._loop, "recv_at": 0.0}

    def _next_seq(self, key: str, msg_id) -> int:
        """同一 msg_id 下持续递增的 msg_seq。

        回执、正式回复、审批提问共用一条计数：否则回执占了 seq=1，
        紧随其后的回复又从 1 开始，会被平台按「msg_id+msg_seq 重复」去重拒收。
        换了 msg_id（新一条来信）则重新从 1 计数。
        """
        key = self.norm_key(key)
        state = self._seq.get(key)
        if not state or state.get("msg_id") != msg_id:
            state = {"msg_id": msg_id, "next": 1}
            self._seq[key] = state
        seq = state["next"]
        state["next"] = seq + 1
        return seq

    def _reply_plan(self, key: str, ctx: dict) -> tuple:
        """回报策略：还在被动窗口内就带 msg_id 回复，超了才改主动消息。

        窗口按场景不同（官方：单聊 60 分钟 / 群聊 5 分钟）。超窗口后仍带原 msg_id 会被
        平台拒掉，用户只看到"已收到"却永远等不到结果。主动消息每月仅 4 条，是最后退路。
        """
        group_openid, _ = self.split_key(self.norm_key(key))
        window = PASSIVE_REPLY_WINDOW_GROUP if group_openid else PASSIVE_REPLY_WINDOW_C2C
        fresh = (time.time() - float(ctx.get("recv_at") or 0.0)) <= window
        return (ctx.get("msg_id") if fresh else None), fresh

    def allowed(self, user_openid: str, group_openid: str = "") -> bool:
        """私聊：本人在白名单即可；群聊：群要在群的名单里，且发言人也要在白名单里。"""
        if not user_openid or user_openid not in self.config.allow_from:
            return False
        if not group_openid:
            return True
        return (self.config.allow_group
                and group_openid in self.config.allow_groups)

    def rate_ok(self, key: str, consume: bool = False) -> bool:
        now = time.time()
        hits = [t for t in self._recent.get(key, []) if now - t < 60]
        if len(hits) >= self.config.per_minute:
            self._recent[key] = hits
            return False
        if consume:
            hits.append(now)
            self._recent[key] = hits
        return True

    def trim(self, text: str) -> str:
        return str(text or "").strip()[: self.config.max_chars]

    def plan(self, key: str, text: str) -> str:
        """纯判定（无副作用）：reject / approval / rate / slash / task —— 给回执用。"""
        if key in self._pending:
            return "approval"
        if not self.rate_ok(key):
            return "rate"
        return "slash" if str(text or "").strip().startswith("/") else "task"

    def reject_hint(self, user_openid: str, group_openid: str = "") -> str:
        """被门控拦下时回什么：先说清是**哪一道**名单没过，并把要加的那串回显出来。"""
        if user_openid not in self.config.allow_from:
            return ("这台机器人的调用白名单里没有你。请把下面这串加进 .env 的 "
                    f"QQBOT_ALLOW_FROM 后重启：\n{user_openid}")
        if group_openid:
            if not self.config.allow_group:
                return ("群聊默认关闭。确认要在群里用它的话，把 .env 的 "
                        "QQBOT_ALLOW_GROUP=true 打开。")
            return (f"这个群不在群白名单里。要放行请把下面这串加进 .env 的 "
                    f"QQBOT_ALLOW_GROUPS 后重启：\n{group_openid}")
        return "这台机器人不接受来自该来源的消息。"

    # ---------------- 审批 ----------------

    def begin_approval(self, key: str, request) -> bool:
        """发审批请求并等用户回 y/a/n（超时=拒绝）；此前选过"始终允许"的直接放行。"""
        key = self.norm_key(key)
        tool = str(getattr(request, "tool_name", "") or "?")
        if tool and tool in self._always.get(key, set()):
            return True
        if self._ctx_for(key).get("api") is None:
            return False                            # 没法问 → 保守拒绝
        event = threading.Event()
        self._pending[key] = {"event": event, "allow": False, "tool": tool}
        reason = str(getattr(request, "reason", "") or "")
        command = str(getattr(request, "command", "") or "")[:300]
        body = (f"⚠ 需要批准 · {tool}\n{reason}\n$ {command}\n\n"
                "回复 y=允许一次 / a=本会话始终允许这个工具 / n=拒绝")
        try:
            self._send_sync(key, body)
        except Exception:                            # noqa: BLE001
            pass
        event.wait(self.approve_timeout)
        state = self._pending.pop(key, {})
        if state.get("allow") and state.get("always") and tool:
            self._always.setdefault(key, set()).add(tool)
        return bool(state.get("allow"))

    def approver_for(self, key: str):
        """给 UserSession 用的审批回调（Agent 侧只认 request）。"""
        return lambda request: self.begin_approval(key, request)

    def resolve_approval(self, key: str, text: str) -> bool:
        """把用户这条回复当成审批答复；返回是否消费掉了它。"""
        key = self.norm_key(key)
        state = self._pending.get(key)
        if not state:
            return False
        answer = str(text or "").strip().lower()
        if answer in ("y", "yes", "允许", "好", "嗯"):
            state["allow"] = True
        elif answer in ("a", "always", "始终", "一直"):
            state["allow"] = True
            state["always"] = True
            tool = str(state.get("tool") or "")
            if tool:
                # 立刻记下来：这样"始终允许"从下一条命令起就生效，不必等当前这次跑完
                self._always.setdefault(key, set()).add(tool)
        else:
            state["allow"] = False
        state["event"].set()
        return True

    # ---------------- 发送 ----------------

    def _send_sync(self, key: str, text: str) -> None:
        """把回复按长度切块发出去（群聊走 post_group_message，私聊走 post_c2c_message）。

        分块必须递增 msg_seq：官方规定「相同 msg_id + msg_seq 重复发送会失败」，
        恒定用默认值会让第 2 块起全被平台拒掉。
        """
        key = self.norm_key(key)
        ctx = self._ctx_for(key)
        api = ctx.get("api")
        if api is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        loop = running if running is not None else ctx.get("loop")
        if loop is None:                             # 没有循环就先别造协程，免得留下"未被等待"告警
            from agent import run_log
            run_log.log(f"QQ 回复无处可发（没有事件循环）：会话 {key[:12]}…，已丢弃", "warn")
            return
        msg_id, fresh = self._reply_plan(key, ctx)
        group_openid, user_openid = self.split_key(key)
        for piece in _chunks_capped(text):
            seq = self._next_seq(key, msg_id)
            if group_openid:
                coro = api.post_group_message(group_openid=group_openid, msg_type=0,
                                              msg_id=msg_id, msg_seq=seq, content=piece)
            else:
                coro = api.post_c2c_message(openid=user_openid, msg_type=0, msg_id=msg_id,
                                            msg_seq=seq, content=piece)
            guarded = _log_send_errors(coro, seq, fresh)
            try:
                if running is not None:
                    running.create_task(guarded)
                else:                                # 审批在别的线程里等 → 丢回主循环
                    asyncio.run_coroutine_threadsafe(guarded, loop)
            except Exception as e:                   # noqa: BLE001
                from agent import run_log
                run_log.log(f"QQ 回复派发失败（第 {seq} 块）："
                            f"{type(e).__name__}: {str(e)[:160]}", "warn")

    @staticmethod
    def split_key(key: str) -> tuple:
        """把 scope key 拆回 (group_openid, user_openid)。"""
        parts = str(key or "").split(":", 2)
        if len(parts) == 3 and parts[0] == "g":
            return parts[1], parts[2]
        return "", (parts[1] if len(parts) > 1 else str(key or ""))

    # ---------------- 实时画面 / 截图（A+B） ----------------

    def ack_text(self) -> str:
        """收到任务时的回执：把"实时画面在哪看"一并说清（A）。"""
        base = "已收到，开始执行…（跑久了可用 /status 查看）"
        link = self.config.live_link()
        return base + (f"\n实时画面：{link}" if link else "")

    def _start_frame_pusher(self, key: str) -> Optional[threading.Event]:
        """任务期间把画面推给面板（面板是另一个进程，看不到本进程的浏览器）。"""
        if not (self.config.dashboard_url and self.config.frame_token):
            return None
        stop = threading.Event()
        threading.Thread(target=self._push_loop, args=(key, stop), daemon=True,
                         name="qq-frame-push").start()
        return stop

    def _push_loop(self, key: str, stop: threading.Event) -> None:
        import httpx
        url = self.config.dashboard_url.rstrip("/") + "/api/frame"
        headers = {"X-Frame-Token": self.config.frame_token}
        while not stop.is_set():
            session = self.sessions.get(key)
            data, fmt = session.screenshot() if session else ("", "png")
            if data:
                try:
                    httpx.post(url, json={"data": data, "format": fmt},
                               headers=headers, timeout=5.0)
                except Exception:                    # noqa: BLE001
                    pass                             # 推不过去不影响任务本身
            stop.wait(self.config.push_seconds)

    def schedule_frame(self, key: str) -> None:
        """把"发一张当前截图到 QQ"排进事件循环（同步调用点用）。"""
        ctx = self._ctx_for(key)
        loop = ctx.get("loop") or self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.send_frame(key), loop)
        except Exception:                            # noqa: BLE001
            pass

    async def send_frame(self, key: str) -> None:
        """把当前页面截图发进 QQ 会话；哪条上传通道可用会自动选，失败会说明原因。"""
        from agent import run_log
        key = self.norm_key(key)
        ctx = self._ctx_for(key)
        api = ctx.get("api")
        if api is None:
            return
        session = self.sessions.get(key)
        data, fmt = session.screenshot() if session else ("", "png")
        if not data:
            await self._send_plain(key, "现在没有可用画面（浏览器没打开或任务已经结束）。")
            return
        group_openid, user_openid = self.split_key(key)
        media = None
        tried = []
        media = await self._upload_base64(api, user_openid, group_openid, data)
        tried.append("base64 直传" + ("成功" if media else "失败"))
        if media is None and self.config.public_frame_url:
            media = await self._upload_url(api, user_openid, group_openid,
                                           self.config.public_frame_url)
            tried.append("公网 URL" + ("成功" if media else "失败"))
        if media is None:
            await self._send_plain(
                key, "截图拿到了，但没有可用的上传通道（" + "、".join(tried) + "）。\n"
                     "用 `python -m agent.qqbot --probe-media` 探测；"
                     "或配置 QQBOT_PUBLIC_FRAME_URL 指向能被 QQ 服务器抓取的图片地址。")
            return
        try:
            if group_openid:
                await api.post_group_message(group_openid=group_openid, msg_type=7,
                                             media=media)
            else:
                await api.post_c2c_message(openid=user_openid, msg_type=7, media=media)
        except Exception as e:                        # noqa: BLE001
            run_log.log(f"QQ 发图失败：{type(e).__name__}: {str(e)[:160]}", "warn")
            await self._send_plain(key, f"图片上传成功但发送失败：{type(e).__name__}。")

    async def _send_plain(self, key: str, text: str) -> None:
        await self._send(key, text)

    async def _upload_base64(self, api, user_openid: str, group_openid: str, data: str):
        """把 base64 截图直接传给平台（官方文档未明确此字段，故失败即降级）。"""
        try:
            from botpy.http import Route
            route = (Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid)
                     if group_openid else
                     Route("POST", "/v2/users/{openid}/files", openid=user_openid))
            resp = await api._http.request(route, json={"file_type": 1, "file_data": data,
                                                        "srv_send_msg": False})
        except Exception:                            # noqa: BLE001
            return None
        return self._media_of(resp)

    async def _upload_url(self, api, user_openid: str, group_openid: str, url: str):
        """备用通道：让平台自己去抓一个公网可达的图片地址。"""
        try:
            from botpy.http import Route
            route = (Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid)
                     if group_openid else
                     Route("POST", "/v2/users/{openid}/files", openid=user_openid))
            resp = await api._http.request(route, json={"file_type": 1, "url": url,
                                                       "srv_send_msg": False})
        except Exception:                            # noqa: BLE001
            return None
        return self._media_of(resp)

    @staticmethod
    def _media_of(resp):
        """从上传响应里取可再次发送的 media 对象。"""
        if resp is None:
            return None
        if isinstance(resp, dict):
            return resp if resp.get("file_info") or resp.get("file_uuid") else None
        return resp if (getattr(resp, "file_info", None)
                        or getattr(resp, "file_uuid", None)) else None

    def probe_media(self) -> str:
        """说明两条上传通道的现状（真正的探测在会话内用 /shot 完成）。"""
        cfg = self.config
        lines = ["截图上传通道：",
                 "· base64 直传：官方 SDK 未暴露此字段，代码会先试这条，失败自动降级",
                 "· 公网 URL 通道：" + (f"已配置 {cfg.public_frame_url}"
                                      if cfg.public_frame_url else
                                      "未配置 QQBOT_PUBLIC_FRAME_URL（QQ 服务器抓不到内网地址）"),
                 "",
                 "实际验证：机器人跑起来后，在 QQ 里发一句「/shot」，"
                 "它会逐条尝试并回报哪条能用。"]
        return "\n".join(lines)

    # ---------------- 事件 ----------------

    async def handle_c2c(self, message) -> None:
        """botpy 的 on_c2c_message_create 入口：先回执，再执行，最后把结论发回去。"""
        author = getattr(message, "author", None)
        openid = str(getattr(author, "user_openid", "") or "")
        text = str(getattr(message, "content", "") or "").strip()
        key = self.scope_key(openid)
        self.remember_context(key, message, asyncio.get_running_loop())
        if not self.allowed(openid):
            await self._send(key, self.reject_hint(openid))
            from agent import run_log
            run_log.log(f"QQ 私聊被拒（不在白名单）: openid 尾号 {openid[-6:]}", "warn")
            return
        if self.plan(key, text) == "task":
            await self._send(key, self.ack_text())
        reply = await asyncio.to_thread(self.handle_text, openid, text)
        if reply:
            await self._send(key, reply)

    async def handle_group_message(self, message) -> None:
        """QQ 群 @机器人：群白名单 + 发言人白名单都通过才执行（每个人独立上下文）。"""
        from agent import run_log
        group_openid = str(getattr(message, "group_openid", "") or "")
        author = getattr(message, "author", None)
        member = str(getattr(author, "member_openid", "") or "")
        text = str(getattr(message, "content", "") or "").strip()
        key = self.scope_key(member, group_openid)
        self.remember_context(key, message, asyncio.get_running_loop())
        if not self.allowed(member, group_openid):
            run_log.log(f"QQ 群消息被拒：群 {group_openid[-6:]} 成员 {member[-6:]}", "warn")
            await self._send(key, self.reject_hint(member, group_openid))
            return
        if self.plan(key, text) == "task":
            await self._send(key, self.ack_text())
        reply = await asyncio.to_thread(self.handle_text, member, text, group_openid)
        if reply:
            await self._send(key, reply)

    async def _send(self, key: str, payload: str) -> None:
        """按 scope key 发消息（群/私聊自动分流）；单条失败只记日志，不炸掉事件处理。"""
        key = self.norm_key(key)
        ctx = self._ctx_for(key)
        api = ctx.get("api")
        if api is None:
            return
        msg_id, fresh = self._reply_plan(key, ctx)
        group_openid, user_openid = self.split_key(key)
        for piece in _chunks_capped(payload):
            seq = self._next_seq(key, msg_id)
            try:
                if group_openid:
                    await api.post_group_message(group_openid=group_openid, msg_type=0,
                                                 msg_id=msg_id, msg_seq=seq, content=piece)
                else:
                    await api.post_c2c_message(openid=user_openid, msg_type=0,
                                               msg_id=msg_id, msg_seq=seq, content=piece)
            except Exception as e:                   # noqa: BLE001
                from agent import run_log
                run_log.log(f"QQ 回复发送失败（第 {seq} 块，被动={fresh}）："
                            f"{type(e).__name__}: {str(e)[:160]}", "warn")

    def handle_text(self, openid: str, text: str, group_openid: str = "") -> str:
        """核心：门控 → 审批答复 → 对话 / 任务。返回要发回去的文本（空=不回）。"""
        from agent import run_log
        if not openid:
            return ""
        key = self.scope_key(openid, group_openid)
        if not self.allowed(openid, group_openid):
            run_log.log(f"QQ 消息被拒（不在白名单）: 用户尾号 {openid[-6:]}"
                        + (f"，群尾号 {group_openid[-6:]}" if group_openid else ""), "warn")
            return self.reject_hint(openid, group_openid)
        if self.resolve_approval(key, text):
            return "收到，已按你的答复处理。"
        if not self.rate_ok(key, consume=True):
            return f"消息太频繁了（上限 {self.config.per_minute} 条/分钟），稍后再试。"
        goal = self.trim(text)
        if not goal:
            return "发一句话告诉我要做什么。"
        if goal.lower().startswith("/shot"):
            # 要一张当前画面：图本身异步发出去，这里只做确认（成功/失败都会再发一条）
            self.schedule_frame(key)
            return ""

        session = self._session_for(key)
        lowered = goal.lower()

        # ---- 对话 / 任务的显式切换 ----
        if lowered.startswith("/chat"):
            rest = goal[len("/chat"):].strip()
            if rest:
                return self._chat_once(session, key, rest)
            session.mode = "chat"
            return "已切到对话模式：直接说话就是聊天，要干活用 /do <任务>。"
        if lowered.startswith("/do") or lowered.startswith("/task"):
            cut = 3 if lowered.startswith("/do") else 5
            rest = goal[cut:].strip()
            if rest:
                return self._execute_task(session, key, rest)
            session.mode = "task"
            return "已切到任务模式：发什么都当成任务执行（寒暄仍会当聊天回你）。"
        if goal.startswith("/"):
            return self._slash(key, goal)

        # ---- 默认路由：寒暄一律走对话；否则按会话模式 ----
        if looks_like_chitchat(goal) or session.mode == "chat":
            return self._chat_once(session, key, goal)
        return self._execute_task(session, key, goal)

    def _session_for(self, key: str) -> "UserSession":
        return self.sessions.setdefault(
            key, UserSession(key, self.config.preset, approver=self.approver_for(key),
                             task_timeout=self.config.task_timeout,
                             mode=self.config.default_mode,
                             chat_turns=self.config.chat_turns,
                             chat_max_tokens=self.config.chat_max_tokens))

    def _chat_once(self, session: "UserSession", key: str, text: str) -> str:
        """闲聊：默认走**同一个 agent 会话**（上下文与任务共享、落盘、需要时仍可用工具）。

        这样才像 QQ 上的聊天助手：聊过的内容，之后发任务时它还记得；
        代价是每条闲聊也会过一次工具循环（可用 QQBOT_CHAT_ENGINE=llm 换成轻量纯聊天）。
        """
        from agent import run_log
        from models.prompts import QQ_CHAT_HINT
        run_log.log(f"QQ 聊天：会话 {session.session_name()}，{len(text)} 字，"
                    f"引擎 {self.config.chat_engine}")
        if self.config.chat_engine == "llm":
            try:
                return session.chat(text) or "（没有回复）"
            except Exception as e:                    # noqa: BLE001
                run_log.log(f"QQ 对话失败：{type(e).__name__}: {str(e)[:160]}", "warn")
                return f"对话出错了：{type(e).__name__}。稍后再试，或用 /do <任务> 直接执行。"
        try:
            # 连寒暄也带提示：否则同一条 agent 会话里"你好"又会被当成任务去"完成"
            return self._execute_task(session, key, f"{text}\n\n{QQ_CHAT_HINT}", kind="chat")
        except Exception as e:                        # noqa: BLE001
            run_log.log(f"QQ 聊天失败：{type(e).__name__}: {str(e)[:160]}", "warn")
            return f"聊天出错了：{type(e).__name__}。稍后再试。"

    def _execute_task(self, session: "UserSession", key: str, goal: str,
                      kind: str = "task") -> str:
        """执行路径：走完整 agent 循环（含工具、审批、检查点）。

        kind="chat" 时是闲聊：不推帧、不自动发截图（页面本来就没动）。
        """
        from agent import run_log
        run_log.log(f"QQ {'任务' if kind == 'task' else '聊天'}：会话 {session.session_name()}，"
                    f"{len(goal)} 字，档位 {session.preset}")
        runner = self._run_task or session.run
        pusher = self._start_frame_pusher(key) if kind == "task" else None
        try:
            output = runner(goal)
        finally:
            if pusher is not None:
                pusher.set()
        if kind == "task" and self.config.send_frame and self._run_task is None:
            self.schedule_frame(key)                 # B：任务收尾把画面发到 QQ
        return output or "（没有输出）"

    def _slash(self, key: str, goal: str) -> str:
        cmd = goal.split()[0].lower()
        session = self.sessions.get(key)
        if cmd in ("/help", "/?", "/h"):
            return ("可用：直接说话=聊天（寒暄），/do <任务> 执行一次任务，/chat 切对话模式，"
                    "/task 切任务模式，/status 看状态，/new 开新会话，/shot 发一张当前画面。"
                    "需要批准时会问你，回 y/a/n。")
        if cmd == "/status":
            if not session:
                return "还没有会话（发一条任务就建）。"
            tail = (session.last_output or "").strip().replace("\n", " ")[-160:]
            return (f"会话 {session.session_name()} · 模式 {session.mode}/{self.config.chat_engine} · 档位 {session.preset} · "
                    f"已完成 {session.turns} 轮 · 上次 "
                    f"{time.strftime('%H:%M:%S', time.localtime(session.last_run_at))}"
                    + (f"\n上次结果尾部：{tail}" if tail else ""))
        if cmd == "/new":
            if session:
                session.agent = None
                session.turns = 0
                session.chat_history = []
            return "已开新会话。"
        return f"未知指令 {cmd}（可用：/status /new /help）"


def _build_client(bridge: QQBotBridge):
    """创建 botpy 客户端（惰性导入：没装 SDK 时不拖垮其它功能）。"""
    import botpy

    class _Client(botpy.Client):
        async def on_ready(self):
            from agent import run_log
            where = "沙箱" if bridge.config.sandbox else "正式"
            run_log.log(f"QQ 机器人已连接（{where}环境）：{self.robot.name}（{bridge.config}）")
            if bridge.config.sandbox:
                run_log.log("当前连的是沙箱：只有开放平台「沙箱配置」里列出的账号/群能收到回复；"
                            "机器人上线后把 QQBOT_SANDBOX 设为 false 才会走正式环境")

        async def on_c2c_message_create(self, message):
            await bridge.handle_c2c(message)

        async def on_group_at_message_create(self, message):
            # QQ 群的 @机器人 事件（注意：on_at_message_create 是**频道**的事件，不是群）
            await bridge.handle_group_message(message)

        async def on_at_message_create(self, message):
            from agent import run_log
            run_log.log("收到频道(guild)消息：本桥不支持频道，已忽略", "warn")

    intents = botpy.Intents(public_messages=True)
    return _Client(intents=intents, is_sandbox=bool(bridge.config.sandbox))


def main(argv: Optional[list] = None) -> int:
    """`python -m agent.qqbot`：连上 QQ，把私聊消息转给 agent。"""
    import argparse
    from agent import run_log
    parser = argparse.ArgumentParser(prog="python -m agent.qqbot",
                                     description="QQ 机器人桥：用 QQ 私聊驱动 agent")
    parser.add_argument("--probe-media", action="store_true", dest="probe_media",
                        help="查看截图上传通道现状（真正验证在会话内用 /shot）")
    # 只在显式传参（或 __main__ 传 sys.argv[1:]）时解析：main() 也会被测试/宿主直接调用，
    # 那时去解析 pytest 的 argv 会直接报"无法识别的参数"。
    args = parser.parse_args(list(argv) if argv is not None else [])
    config = QQBotConfig()
    if args.probe_media:
        print(QQBotBridge(config).probe_media())
        return 0
    if not config.ready():
        print("缺少 QQBOT_APP_ID / QQBOT_APP_SECRET（写进 .env，别提交）。")
        return 2
    if not config.allow_from:
        print("⚠ QQBOT_ALLOW_FROM 为空：现在**所有人都会被拒绝**。"
              "先给对方发一条消息，把日志里回显的 openid 加进白名单。")
    where = "沙箱" if config.sandbox else "正式"
    print(f"连接环境：{where}（{'QQBOT_SANDBOX=true' if config.sandbox else 'QQBOT_SANDBOX=false'}）"
          + ("—— 只有沙箱配置里列出的账号/群能收到回复" if config.sandbox else ""))
    if config.workspace:
        os.chdir(config.workspace)
    run_log.start(tag="qqbot", extra={"qqbot": repr(config)})
    bridge = QQBotBridge(config)
    client = _build_client(bridge)
    run_log.log(f"QQ 桥启动（{where}环境），等待消息")
    client.run(appid=config.app_id, secret=config.secret)
    return 0


if __name__ == "__main__":      # pragma: no cover - 长驻入口
    import sys as _sys
    raise SystemExit(main(_sys.argv[1:]))
