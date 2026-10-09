"""QQ 机器人桥（官方 API，走腾讯 `qq-botpy` SDK）：用 QQ 私聊驱动本 agent。

安全是硬编码的，不给配置绕过：
- **只响应私聊**（群消息一律忽略）；
- `QQBOT_ALLOW_FROM` 白名单（openid，逗号分隔）——为空时拒绝所有人并在日志里提示怎么加；
- 每个 QQ 用户**独立会话**，互不串上下文；
- 权限档只允许 `ask` / `block`，**不接受 full**（来自聊天通道的请求不允许提权）；
- AppID/AppSecret 只从环境变量读，日志与回执都不打印密钥。

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
#: 一轮任务的等待上限（秒）
TASK_TIMEOUT = 1800
#: 审批等待上限（秒）
APPROVE_TIMEOUT = 180
#: 允许的权限档（不给 full：聊天通道不允许提权）
ALLOWED_PRESETS = ("ask", "block")


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
        allow_group = env.get("QQBOT_ALLOW_GROUP", env.get("allow_group", False))
        self.allow_group = (allow_group is True
                            or str(allow_group).strip().lower() == "true")
        self.workspace = str(env.get("QQBOT_WORKSPACE") or env.get("workspace") or "").strip()

    def __repr__(self) -> str:                      # 绝不把 secret 带进日志
        return (f"QQBotConfig(app_id={'*' * len(self.app_id)}, secret=***, "
                f"allow_from={len(self.allow_from)} 个, 群={len(self.allow_groups)} 个, "
                f"preset={self.preset}, per_minute={self.per_minute}, "
                f"group={self.allow_group})")

    def ready(self) -> bool:
        return bool(self.app_id and self.secret)


class UserSession:
    """一个 QQ 用户 ↔ 一个会话（独立 Agent 实例，权限档锁死在 ask/block）。"""

    def __init__(self, openid: str, preset: str = "ask", verbose: bool = False,
                 approver=None):
        self.openid = openid
        self.preset = preset
        self.agent = None
        self.lock = threading.Lock()
        self.last_run_at = 0.0
        self.turns = 0
        self.last_output = ""
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
        """执行一条任务（同一用户串行，避免并发改同一工作区）。"""
        with self.lock:
            agent = self.ensure_agent()
            self.last_run_at = time.time()
            self.turns += 1
            try:
                self.last_output = str(agent.run(goal, keep_session=True) or "")
            except Exception as e:                     # noqa: BLE001
                self.last_output = f"执行失败：{type(e).__name__}: {str(e)[:200]}"
            return self.last_output


class QQBotBridge:
    """把 QQ 私聊消息接到 agent；审批通过"回复 y/a/n"完成。"""

    def __init__(self, config: Optional[QQBotConfig] = None, runner=None):
        self.config = config or QQBotConfig()
        self.sessions: Dict[str, UserSession] = {}
        self._recent: Dict[str, List[float]] = {}
        self._pending: Dict[str, dict] = {}
        self._always: Dict[str, set] = {}            # 用户选了"始终允许"的工具
        self._api = None                            # botpy 的 message._api（发送用）
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
        if self._api is None:
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
        """把回复按长度切块发出去（群聊走 post_group_message，私聊走 post_c2c_message）。"""
        api, msg_id = self._api, getattr(self, "_msg_id", "")
        if api is None:
            return
        group_openid, user_openid = self.split_key(key)
        for piece in _chunk(text):
            if group_openid:
                coro = api.post_group_message(group_openid=group_openid, msg_type=0,
                                              msg_id=msg_id, content=piece)
            else:
                coro = api.post_c2c_message(openid=user_openid, msg_type=0, msg_id=msg_id,
                                            content=piece)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(coro)
            else:                                    # 审批在别的线程里等 → 丢到主循环
                asyncio.run_coroutine_threadsafe(coro, self._loop)

    @staticmethod
    def split_key(key: str) -> tuple:
        """把 scope key 拆回 (group_openid, user_openid)。"""
        parts = str(key or "").split(":", 2)
        if len(parts) == 3 and parts[0] == "g":
            return parts[1], parts[2]
        return "", (parts[1] if len(parts) > 1 else str(key or ""))

    # ---------------- 事件 ----------------

    async def handle_c2c(self, message) -> None:
        """botpy 的 on_c2c_message_create 入口：先回执，再执行，最后把结论发回去。"""
        author = getattr(message, "author", None)
        openid = str(getattr(author, "user_openid", "") or "")
        self._api = getattr(message, "_api", None)
        self._msg_id = str(getattr(message, "id", "") or "")
        self._loop = asyncio.get_running_loop()
        text = str(getattr(message, "content", "") or "").strip()
        key = self.scope_key(openid)
        if not self.allowed(openid):
            await self._send(openid, self.reject_hint(openid))
            from agent import run_log
            run_log.log(f"QQ 私聊被拒（不在白名单）: openid 尾号 {openid[-6:]}", "warn")
            return
        if self.plan(key, text) == "task":
            await self._send(openid, "已收到，开始执行…（跑久了可用 /status 查看）")
        reply = await asyncio.to_thread(self.handle_text, openid, text)
        if reply:
            await self._send(openid, reply)

    async def handle_group_message(self, message) -> None:
        """QQ 群 @机器人：群白名单 + 发言人白名单都通过才执行（每个人独立上下文）。"""
        from agent import run_log
        group_openid = str(getattr(message, "group_openid", "") or "")
        author = getattr(message, "author", None)
        member = str(getattr(author, "member_openid", "") or "")
        text = str(getattr(message, "content", "") or "").strip()
        self._api = getattr(message, "_api", None)
        self._msg_id = str(getattr(message, "id", "") or "")
        self._loop = asyncio.get_running_loop()
        if not self.allowed(member, group_openid):
            run_log.log(f"QQ 群消息被拒：群 {group_openid[-6:]} 成员 {member[-6:]}", "warn")
            await self._send(self.scope_key(member, group_openid),
                             self.reject_hint(member, group_openid))
            return
        key = self.scope_key(member, group_openid)
        if self.plan(key, text) == "task":
            await self._send(key, "已收到，开始执行…（跑久了可用 /status 查看）")
        reply = await asyncio.to_thread(self.handle_text, member, text, group_openid)
        if reply:
            await self._send(key, reply)

    async def _send(self, key: str, payload: str) -> None:
        """按 scope key 发消息（群/私聊自动分流）。"""
        group_openid, user_openid = self.split_key(key)
        for piece in _chunk(payload):
            if group_openid:
                await self._api.post_group_message(group_openid=group_openid, msg_type=0,
                                                   msg_id=self._msg_id, content=piece)
            else:
                await self._api.post_c2c_message(openid=user_openid, msg_type=0,
                                                 msg_id=self._msg_id, content=piece)

    def handle_text(self, openid: str, text: str, group_openid: str = "") -> str:
        """核心：门控 → 审批答复 → 新任务。返回要发回去的文本（空=不回）。"""
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
        if goal.startswith("/"):
            return self._slash(key, goal)
        session = self.sessions.setdefault(
            key, UserSession(key, self.config.preset, approver=self.approver_for(key)))
        run_log.log(f"QQ 任务：用户尾号 {openid[-6:]}"
                    + (f"，群尾号 {group_openid[-6:]}" if group_openid else "")
                    + f"，{len(goal)} 字，会话 {session.session_name()}，档位 {session.preset}")
        runner = self._run_task or session.run
        output = runner(goal)
        return output or "（没有输出）"

    def _slash(self, key: str, goal: str) -> str:
        cmd = goal.split()[0].lower()
        session = self.sessions.get(key)
        if cmd in ("/help", "/?", "/h"):
            return ("可用：直接发任务；/status 看状态；/new 开新会话。"
                    "需要批准时会问你，回 y/a/n。")
        if cmd == "/status":
            if not session:
                return "还没有会话（发一条任务就建）。"
            tail = (session.last_output or "").strip().replace("\n", " ")[-160:]
            return (f"会话 {session.session_name()} · 档位 {session.preset} · "
                    f"已完成 {session.turns} 轮 · 上次 "
                    f"{time.strftime('%H:%M:%S', time.localtime(session.last_run_at))}"
                    + (f"\n上次结果尾部：{tail}" if tail else ""))
        if cmd == "/new":
            if session:
                session.agent = None
                session.turns = 0
            return "已开新会话。"
        return f"未知指令 {cmd}（可用：/status /new /help）"


def _build_client(bridge: QQBotBridge):
    """创建 botpy 客户端（惰性导入：没装 SDK 时不拖垮其它功能）。"""
    import botpy

    class _Client(botpy.Client):
        async def on_ready(self):
            from agent import run_log
            run_log.log(f"QQ 机器人已连接：{self.robot.name}（{bridge.config}）")

        async def on_c2c_message_create(self, message):
            await bridge.handle_c2c(message)

        async def on_group_at_message_create(self, message):
            # QQ 群的 @机器人 事件（注意：on_at_message_create 是**频道**的事件，不是群）
            await bridge.handle_group_message(message)

        async def on_at_message_create(self, message):
            from agent import run_log
            run_log.log("收到频道(guild)消息：本桥不支持频道，已忽略", "warn")

    intents = botpy.Intents(public_messages=True)
    return _Client(intents=intents)


def main() -> int:
    """`python -m agent.qqbot`：连上 QQ，把私聊消息转给 agent。"""
    from agent import run_log
    config = QQBotConfig()
    if not config.ready():
        print("缺少 QQBOT_APP_ID / QQBOT_APP_SECRET（写进 .env，别提交）。")
        return 2
    if not config.allow_from:
        print("⚠ QQBOT_ALLOW_FROM 为空：现在**所有人都会被拒绝**。"
              "先给对方发一条消息，把日志里回显的 openid 加进白名单。")
    if config.workspace:
        os.chdir(config.workspace)
    run_log.start(tag="qqbot", extra={"qqbot": repr(config)})
    bridge = QQBotBridge(config)
    client = _build_client(bridge)
    run_log.log("QQ 桥启动，等待私聊消息")
    client.run(appid=config.app_id, secret=config.secret)
    return 0


if __name__ == "__main__":      # pragma: no cover - 长驻入口
    raise SystemExit(main())
