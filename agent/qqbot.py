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
import re
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
#: 短问句同样是闲聊："你叫什么名字""你会什么""今天几号"。
#: 这类问题走任务通道的代价是：先回一句"已收到，开始执行…"，收尾还去抓一张画面 —— 用户
#: 只是想问句话。真任务（"把临时文件删掉""重启服务"）通常更长或带动作词，不会被卷进来；
#: 万一卷进来也不要紧：对话通道走的是同一个 agent 循环，工具照样能用，只是不来回执。
CHITCHAT_QUESTION_HINTS = ("什么", "谁", "哪", "吗", "呢", "几", "怎么", "为什么",
                           "多少", "是不是", "能不能", "有没有")
CHITCHAT_MAX_CHARS = 12


def looks_like_chitchat(text: str) -> bool:
    """是不是一句闲聊：短 + （问候词 或 问句词）。任务描述通常更长，不会被误判。"""
    body = str(text or "").strip().lower()
    if not body or len(body) > CHITCHAT_MAX_CHARS:
        return False
    if any(hint in body for hint in CHITCHAT_HINTS):
        return True
    return any(hint in body for hint in CHITCHAT_QUESTION_HINTS)


#: 补发图片时识别的扩展名
_IMAGE_EXTS = ("png", "jpg", "jpeg", "gif", "webp", "bmp")

#: 补发**音频**时识别的扩展名。agent 用tts / voice_clone 工具生成的语音文件
#: 落在这个目录，之前 _IMAGE_EXTS 不含这些扩展名 → mp3 压根没进补发流程，
#: 用户收到的只是一串本地路径。现在按后缀分流：图片 file_type=1、音频 file_type=3。
_AUDIO_EXTS = ("mp3", "wav", "silk", "ogg", "m4a", "aac", "amr")

#: 后缀 → 官方 file_type（1=图片 2=视频 3=语音 4=文件）。见官方《富媒体消息概述》。
_MEDIA_FILE_TYPES = {
    "png": 1, "jpg": 1, "jpeg": 1, "gif": 1, "webp": 1, "bmp": 1,
    "mp4": 2,
    "mp3": 3, "wav": 3, "silk": 3, "ogg": 3, "m4a": 3, "aac": 3, "amr": 3,
}

#: 语音合成的文本上限（字）。语音是给人听的结论，交接单/长报告念出来是噪音——
#: 超过这个长度就不念（agent 已生成的音频不受此限，那是它自己决定要发的内容）。
MAX_VOICE_CHARS = 200


def _first_existing_audio(text: str) -> tuple:
    """在回复文本里找出第一个**真实存在**的音频文件。

    返回 (绝对路径, 文本里的原始片段)；找不到返回 (None, "")。
    agent 的 tts / voice_clone 工具产出 mp3 后会把路径写进回复，
    这里用来直接发那个文件——保住它选的音色，不拿文字重新合成。
    """
    root = _project_root()
    for raw in _find_image_paths(text):
        cand = raw if os.path.isabs(raw) else os.path.join(root, raw)
        cand = os.path.normpath(cand)
        ext = cand.rsplit(".", 1)[-1].lower() if "." in cand else ""
        if ext not in _AUDIO_EXTS:
            continue
        try:
            if os.path.isfile(cand) and os.path.getsize(cand) >= 1024:
                return cand, raw
        except OSError:                            # noqa: BLE001
            continue
    return None, ""

#: 「这条回复要带语音」的内传哨兵。/v 前缀 → handle_text 在文本前打这个标记，
#: 调用方（handle_c2c / handle_group_message）见到就调 send_voice，再把标记剥掉。
#: 走文本而不是返回值元组，是为了让 handle_text 的既有签名和测试保持不变。
_VOICE_FLAG = "VOICE"


def _project_root() -> str:
    """项目根目录（惰性导入：config 依赖较重，别在模块加载时就拖进来）。"""
    try:
        from config import PROJECT_ROOT
        return PROJECT_ROOT
    except Exception:                                   # noqa: BLE001
        return os.getcwd()


def _find_image_paths(text: str) -> List[str]:
    """从回复文本里挑出本地**媒体**路径（图片+音频；按出现顺序、去重、保持原样）。

    旧实现用 `(?:/[A-Za-z0-9_.\\-]+)+\\.(png|...)`，在 Windows 上有三个洞：
      · `D:/aaa/...png` 里的盘符被吃掉，只剩 `/aaa/...` → open() 找不到文件
      · `D:\\aaa\\...png` 反斜杠路径匹配出半截
      · 只给文件名（`img-xxx.png`）完全匹配不到
    所以这里显式处理盘符、正反斜杠与纯文件名三种写法。

    另外**音频也必须收**：agent 的tts / voice_clone 工具产出mp3 路径，
    用户要的是"把音频发过来"，只回一串本地路径等于没发。

    三轮匹配后按「规范化后的绝对路径」去重，避免 `D:/a/b.png` 又被第②轮
    从中间抠出 `/a/b.png` 重复一遍。
    """
    # 先摘掉 URL：`https://x/a.png` 会被下面的路径正则当成好几条"本地路径"，
    # 白占补发名额且都打不开（实测一个 URL 能产出 3 条假路径）。
    text = re.sub(r"https?://\S+", " ", str(text or ""))
    exts = _IMAGE_EXTS + _AUDIO_EXTS
    out: List[str] = []
    seen = set()
    root = _project_root()

    def _add(p: str) -> None:
        p = p.strip().strip('"\'')
        if not p:
            return
        norm = os.path.normpath(p if os.path.isabs(p) else os.path.join(root, p))
        key = norm.lower()
        if key in seen:            # 同一条路径已收录（挡掉盘符路径的二次匹配）
            return
        seen.add(key)
        out.append(p)

    for m in re.finditer(r"[A-Za-z]:[\\/][^\s'\"<>|]+?\.(?:%s)" % "|".join(exts),
                         text, flags=re.I):
        _add(m.group(0))
    for m in re.finditer(r"(?<![\w.])/[^\s'\"<>|]+?\.(?:%s)" % "|".join(exts),
                         text, flags=re.I):
        _add(m.group(0))
    # 纯文件名 / 相对路径：必须真的带分隔目录才算"路径"，
    # 否则 D:/aaa/img-20261013.png 里的文件名会被当成相对路径再收一遍，
    # 白白吃掉"最多补发 3 张"的配额。
    for m in re.finditer(r"(?<![\w\\/])[\w.\-]+(?:[\\/][\w.\-]+)+\.(?:%s)" % "|".join(exts),
                         text, flags=re.I):
        _add(m.group(0))
    # 只有不含斜杠的孤零零文件名时也收（相对项目根能找到的常见产物名）
    for m in re.finditer(r"(?<![\w./\\-])[\w\-]+\.(?:%s)" % "|".join(exts),
                         text, flags=re.I):
        _add(m.group(0))
    return out


def _sent_placeholder(raw: str) -> str:
    """已发出去的媒体在文字里留一句人话占位（区分图片 / 语音）。"""
    ext = raw.rsplit(".", 1)[-1].lower() if "." in raw else ""
    if ext in _AUDIO_EXTS:
        return "（语音已发上）"
    if ext == "mp4":
        return "（视频已发上）"
    return "（图片已发上图）"


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


#: Markdown 发送开关。官方 2026-04-23 起「单聊/群聊自定义 Markdown 已对所有机器人开放，
#: 无需申请模板、频道除外」——所以默认走真正的富文本（msg_type=2），星号渲染成粗体。
#: 置 False 则退回"剥掉标记发纯文本"。
USE_MARKDOWN = True


def _md_to_text(text: str) -> str:
    """把 LLM 常见的 Markdown 标记转成 QQ 纯文本友好的形式（USE_MARKDOWN=False 时才走）。

    QQ 官方富文本(msg_type=2)只支持固定模板，不接受任意 Markdown，
    所以这里主动去掉符号噪音：
      - ``**粗体**`` / `__粗体__`  → 粗体内容本身
      - ``*斜体*`` / `_斜体_`      → 斜体内容本身
      - ``~~删除线~~``             → 删除线内容本身
      - ` ```代码块``` `           → 保留缩进，去掉围栏
      - 行内 `code`               → 去掉反引号
      - ``[文字](url)``            → 文字（url）
    只做**去标记**，不改文字内容。
    """
    s = str(text or "")
    if not s:
        return s
    # 代码块：去围栏，保留内容
    s = re.sub(r"^```[a-zA-Z0-9_+-]*\s*$", "", s, flags=re.M)
    # 链接：[文字](url) → 文字（url）
    s = re.sub(r"\[([^\]]*)\]\(([^)]+)\)", r"\1（\2）", s)
    # 强调：**粗**、__粗__ → 粗
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    # 删除线：~~删~~ → 删
    s = re.sub(r"~~([^~]+)~~", r"\1", s)
    # 斜体：*斜*、_斜_ → 斜（放在加粗之后，避免吃掉已处理的内容）
    s = re.sub(r"\*([^*\n]+)\*", r"\1", s)
    s = re.sub(r"(?<![\w])_([^_\n]+)_(?![\w])", r"\1", s)
    # 行内代码：`code` → code
    s = re.sub(r"`([^`\n]+)`", r"\1", s)
    # 行首标题标记：### 标题 → 【标题】
    s = re.sub(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$", r"【\1】", s, flags=re.M)
    # 引用：> 内容 → │ 内容
    s = re.sub(r"^\s{0,3}>\s?(.*)$", r"│ \1", s, flags=re.M)
    # 水平线：--- / *** / ___ → 空
    s = re.sub(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", "", s, flags=re.M)
    # 列表符号规范化：* item / + item → • item
    s = re.sub(r"^(\s*)[*+]\s+", r"\1• ", s, flags=re.M)
    return s


#: Markdown 单条内容上限（字符）。富文本要序列化成 json，实际限制比纯文本更紧；
#: 这里跟随 CHUNK_CHARS 保持一致，让切块/封顶行为与纯文本路径同口径（测试也依赖这点）。
MD_CHUNK_CHARS = CHUNK_CHARS


#: Markdown 尾块超限时统一的截断提示（与 _chunks_capped 措辞一致）。
_MD_TRUNCATED = "\n…（内容过长，已省略后续；完整结果可用 /status 取）"


def _cap_markdown(pieces: List[str], limit: int = MAX_REPLIES_PER_MESSAGE) -> List[str]:
    """Markdown 版条数封顶：超出的直接丢，并给最后一块加省略提示。

    刻意**不**复用 _chunks_capped——它内部走 _chunk 按固定字数硬切，
    会把 ```围栏劈开、把标题和正文拦腰截断。这里只裁块数，不动块内结构。
    """
    limit = max(1, int(limit))
    if len(pieces) <= limit:
        return pieces
    dropped_chars = sum(len(p) for p in pieces[limit:])
    kept = pieces[:limit]
    kept[-1] = f"{kept[-1]}{_MD_TRUNCATED}"
    return kept


def _chunks_markdown(text: str, limit: int = MD_CHUNK_CHARS,
                     max_pieces: int = MAX_REPLIES_PER_MESSAGE) -> List[str]:
    """按**行边界**切分 Markdown，且绝不把标记劈成两半。

    和 _chunk 的区别：Markdown 里 ``` 是成对围栏、`**` 是成对强调，砍在中间会
    让渲染错乱。这里按行累计，围栏状态带着走；单行超长才硬切。

    max_pieces：最多回几条——调用方按"这条来信还剩几次回复额度"传入
    （回执与补发的图片也吃同一份额度）。
    """
    body = str(text or "").strip()
    if not body:
        return ["（没有输出）"]
    # 超长围栏块（单行就超限）：整体作为一个代码块发，宁可超长也不劈开——
    # 劈开会让渲染错乱，比超限更难读。
    fence = re.search(r"(```.*?```)", body, flags=re.S)
    if fence and len(fence.group(1)) > limit:
        head = body[: fence.start()].strip()
        tail = body[fence.end():].strip()
        out = [head] if head else []
        out.append(fence.group(1))
        if tail:
            out.extend(_chunks_markdown(tail, limit, max_pieces))
            return _cap_markdown([x for x in out if x.strip()], max_pieces) or ["（没有输出）"]
        return _cap_markdown([x for x in out if x.strip()], max_pieces) or ["（没有输出）"]
    out, buf, in_fence = [], "", False
    for line in body.splitlines(keepends=True):
        if re.match(r"^\s{0,3}```", line):
            in_fence = not in_fence
        if len(buf) + len(line) > limit and buf.strip():
            if in_fence:                      # 别让收尾停在未闭合的围栏里
                buf += "\n```\n"
            out.append(buf.rstrip())
            buf = ""
        if len(line) > limit:                # 单行超长（不在围栏内）：硬切
            while len(line) > limit:
                out.append(line[:limit])
                line = line[limit:]
        buf += line
    if buf.strip():
        if in_fence:                          # 收尾补围栏
            buf += "\n```"
        out.append(buf.rstrip())
    # 同一条消息最多回 MAX_REPLIES_PER_MESSAGE 条，超了整条发送失败（平台报超频）。
    # 用 _cap_markdown 只裁块数、不裁块内结构，避免把围栏/标题劈开。
    return _cap_markdown([x for x in out if x.strip()], max_pieces) or ["（没有输出）"]


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


def _int_or(env: dict, key: str, alias: str, default: int) -> int:
    """取整数配置：**0 是合法值**，不能像 `or` 那样被默认值吃掉；空串/缺失才回默认。"""
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        raw = env.get(alias)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


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
        # 并发闸门：同时最多跑几个 agent 循环（2 核机器上别让它无限并发）
        self.max_concurrent_tasks = max(1, int(env.get("QQBOT_MAX_CONCURRENT_TASKS")
                                              or env.get("max_concurrent_tasks") or 2))
        # 下面两个 0 是**有意义**的值（0 = 不等待 / 不回收），不能用 `or` 兜底——0 是假值会被默认值吃掉
        self.task_queue_timeout = max(0, int(_int_or(env, "QQBOT_TASK_QUEUE_TIMEOUT",
                                                     "task_queue_timeout", 30)))
        self.session_ttl = max(0, int(_int_or(env, "QQBOT_SESSION_TTL",
                                              "session_ttl", 7200)))
        self.task_timeout = int(env.get("QQBOT_TASK_TIMEOUT")
                                or env.get("task_timeout") or TASK_TIMEOUT)
        # 新机器人默认只存在于沙箱：连错环境会"连上了却收不到任何消息"，所以默认沙箱
        raw_env = str(env.get("QQBOT_SANDBOX", env.get("sandbox", "true"))).strip().lower()
        self.sandbox = raw_env not in ("false", "0", "no", "off")
        allow_group = env.get("QQBOT_ALLOW_GROUP", env.get("allow_group", False))
        self.allow_group = (allow_group is True
                            or str(allow_group).strip().lower() == "true")
        # 任务回执开关：QQBOT_ACK=false 时不发「已收到，开始执行」
        ack = env.get("QQBOT_ACK") or env.get("ack") or ""
        self.ack_enabled = str(ack).strip().lower() not in ("false", "0", "no", "off")
        # 语音回复开关：QQBOT_VOICE_REPLY=true 时回复后同时发一条语音（TTS）
        voice = env.get("QQBOT_VOICE_REPLY") or env.get("voice_reply") or ""
        self.voice_reply = str(voice).strip().lower() in ("1", "true", "yes", "on")
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
        #: 当前这一轮任务的停止信号（供 `/stop` 从**别的线程**置位）。
        #: 每轮 run() 建新的，跑完清空；为 None 表示这台会话当前没有任务在跑。
        self._current_stop_event = None
        #: 本轮是被用户 /stop 停的（与"超时"区分，给出不同文案）
        self.stopped_by_user = False
        #: /stop 幂等标记：已按过就别重复回"收到了"
        self._stop_pressed = False
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

        同一根 stop_event 也供 `/stop` 使用：桥在**别的线程**里拿到它并 .set()，
        效果与超时一致（都是"下一个检查点收尾"），只是文案不同。
        """
        with self.lock:
            agent = self.ensure_agent()
            self.last_run_at = time.time()
            self.turns += 1
            self.timed_out = False
            self.stopped_by_user = False
            self._stop_pressed = False
            stop_event = threading.Event()
            self._current_stop_event = stop_event       # 暴露给 /stop
            timer = None
            if self.task_timeout and self.task_timeout > 0:
                timer = threading.Timer(self.task_timeout, stop_event.set)
                timer.daemon = True
                timer.start()
            try:
                self.last_output = str(agent.run(goal, keep_session=True,
                                                 stop_event=stop_event) or "")
                if stop_event.is_set():
                    if self.stopped_by_user:
                        self.last_output = ("（已按 /stop 停止，已完成成果保留）\n\n"
                                            ) + self.last_output
                    else:
                        self.timed_out = True
                        self.last_output = (f"任务超过 {int(self.task_timeout)} 秒，已中断"
                                            "（已完成的成果保留）。\n\n") + self.last_output
            except Exception as e:                     # noqa: BLE001
                self.last_output = f"执行失败：{type(e).__name__}: {str(e)[:200]}"
            finally:
                if timer is not None:
                    timer.cancel()
                self._current_stop_event = None         # 本轮结束，/stop 该回"没有在跑的"
            return self.last_output

    def screenshot(self, only_live: bool = False) -> tuple:
        """抓当前页面一帧，返回 (base64, 格式)；拿不到就返回 ("", "png")。

        only_live=True 只照"已经开着的"页面：抓图会顺手启动浏览器，而"任务收尾顺带发张
        状态图"这种事不该为了拍一张空白页把 Chromium 拉起来（用户会收到一张白图）。
        """
        agent = self.agent
        if agent is None:
            return "", "png"
        try:
            tool = agent.tool_manager.get_tool("browser")
        except Exception:                            # noqa: BLE001
            tool = None
        if tool is None or not hasattr(tool, "execute"):
            return "", "png"
        if only_live and not getattr(tool, "has_live_page", lambda: False)():
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
        #: 待发的语音文本（按会话键）：/v 前缀或 /v 重播时入队，调用方消费
        self._pending_voice: Dict[str, str] = {}
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
        #: 并发闸门：同时执行的任务数上限（信号量按配置建好）
        self._slots = threading.BoundedSemaphore(max(1, int(self.config.max_concurrent_tasks)))
        self._running: set = set()                  # 正在执行任务的会话键（不参与空闲回收）
        self._last_active: Dict[str, float] = {}    # 会话 → 最近活动时间
        self._last_prune = 0.0                      # 上次回收的时间（限频）

    # ---------------- 门控 ----------------

    @staticmethod
    def scope_key(user_openid: str, group_openid: str = "") -> str:
        """会话/限流/审批都按这个键隔离。
        群聊按群共享上下文：群里所有人共用一份对话，任务串行排队执行。
        """
        return f"g:{group_openid}" if group_openid else f"c:{user_openid}"

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

    def _remaining_replies(self, key: str) -> int:
        """这条来信还剩几次被动回复额度。

        被动窗口内(msg_id 有效)时,**回执、补发的图片/语音、正文文字共用同一份额度**:
        平台规定同一条消息最多回 MAX_REPLIES_PER_MESSAGE 次,超了整条发送失败。
        超窗口(改主动消息)或本条还没回过时,视为满额。
        """
        key = self.norm_key(key)
        msg_id, _fresh = self._reply_plan(key, self._ctx_for(key))
        if not msg_id:
            return MAX_REPLIES_PER_MESSAGE
        state = self._seq.get(key) or {}
        if state.get("msg_id") != msg_id:
            return MAX_REPLIES_PER_MESSAGE
        used = max(0, int(state.get("next", 1)) - 1)
        return max(0, MAX_REPLIES_PER_MESSAGE - used)

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
        """纯判定（无副作用）：approval / rate / slash / chat / task —— 给回执用。

        必须和默认路由（`handle_text` 里那条 `looks_like_chitchat(...) or mode == "chat"`）
        得出同一个结论，否则会出现"实际按聊天回答了，却先发一条'已收到，开始执行…'"——
        回执只在真的要开任务时才有意义。
        """
        if key in self._pending:
            return "approval"
        if not self.rate_ok(key):
            return "rate"
        body = str(text or "").strip()
        if body.startswith("/"):
            return "slash"
        session = self.sessions.get(self.norm_key(key))
        if looks_like_chitchat(body) or (session is not None and session.mode == "chat"):
            return "chat"
        return "task"

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

    def _handle_stop(self, key: str, text: str) -> Optional[str]:
        """处理 `/stop`、`/stop all`。返回回复文本；不是停命令就返回 None。

        语义：
          · **按会话隔离**——在哪个会话发就停哪个（群里发停群任务、私聊发停私聊任务），
            不会误杀另一个会话的任务；`/stop all` 才是全停兜底。
          · **只停当前、保留排队**——同会话里排在后面的任务照常执行
            （用户要的是"这条不要了"，不是"这个会话别干了"）。

        必须在会话锁**之前**调用：正在跑的任务占着 session.lock，
        走常规路径的消息会阻塞排队，那样永远停不掉正在跑的那个。
        置位是线程安全的（threading.Event），且是"下一个检查点收尾"的优雅停止，
        不是硬杀线程。
        """
        cmd = str(text or "").strip().lower()
        if cmd not in ("/stop", "/stopall", "/stop all", "/停", "/停止", "/全停"):
            return None
        from agent import run_log

        def _mark(session) -> bool:
            """给一个会话置停止信号；返回是否真的置上了（没在跑就不置）。"""
            ev = getattr(session, "_current_stop_event", None)
            if ev is None or ev.is_set():
                return False
            session.stopped_by_user = True
            session._stop_pressed = True
            ev.set()
            return True

        if cmd in ("/stopall", "/stop all", "/全停"):
            hit = [k for k, s in list(self.sessions.items()) if _mark(s)]
            if not hit:
                return "现在没有在跑的任务。"
            run_log.log(f"QQ 收到 /stop all：已对 {len(hit)} 个会话置停止信号", "warn")
            return f"已对 {len(hit)} 个会话置停止信号（下一个检查点收尾，成果保留）。"

        session = self.sessions.get(self.norm_key(key))
        if session is None or getattr(session, "_current_stop_event", None) is None:
            return "这个会话现在没有在跑的任务。"
        if getattr(session, "_stop_pressed", False):
            return "已经喊停了，它在下一个检查点收尾（已完成成果保留）。"
        _mark(session)
        run_log.log(f"QQ 收到 /stop：会话 {session.session_name()} 置停止信号", "warn")
        return "收到，已喊停——它在下一个检查点收尾（已完成成果保留）。"

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
        # 官方 2026-04-23 起单聊/群聊支持自定义 Markdown：msg_type=2 传 markdown.content，
        # ** / # 等会真正渲染成粗体与标题。传了 markdown 时 content 必须为空（见官方接口文档）。
        if USE_MARKDOWN:
            pieces = _chunks_markdown(text)
        else:
            pieces = _chunks_capped(_md_to_text(text))
        for piece in pieces:
            seq = self._next_seq(key, msg_id)
            if USE_MARKDOWN:
                md = {"content": piece}
                if group_openid:
                    coro = api.post_group_message(group_openid=group_openid, msg_type=2,
                                                  msg_id=msg_id, msg_seq=seq, markdown=md)
                else:
                    coro = api.post_c2c_message(openid=user_openid, msg_type=2, msg_id=msg_id,
                                                msg_seq=seq, markdown=md)
            elif group_openid:
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
        """把 scope key 拆回 (group_openid, user_openid)。
        群聊键 g:群openid（共享会话，无用户维度）。
        """
        s = str(key or "")
        if s.startswith("g:"):
            return s[2:], ""
        if s.startswith("c:"):
            return "", s[2:]
        return "", s

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
        link_sent = False
        while not stop.is_set():
            session = self.sessions.get(key)
            data, fmt = session.screenshot(only_live=True) if session else ("", "png")
            if data:
                if not link_sent:
                    link_sent = True
                    novnc_url = (self.config.live_link() or self.config.dashboard_url or "").rstrip("/") + "/vnc.html"
                    self._send_sync(key, "🌐 agent 正在操作浏览器，点这里实时查看画面：\n" + novnc_url)
                try:
                    httpx.post(url, json={"data": data, "format": fmt},
                               headers=headers, timeout=5.0)
                except Exception:                    # noqa: BLE001
                    pass                             # 推不过去不影响任务本身
            stop.wait(self.config.push_seconds)

    def schedule_frame(self, key: str, only_live: bool = False) -> None:
        """把"发一张当前截图到 QQ"排进事件循环（同步调用点用）。"""
        ctx = self._ctx_for(key)
        loop = ctx.get("loop") or self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.send_frame(key, only_live=only_live), loop)
        except Exception:                            # noqa: BLE001
            pass

    async def send_frame(self, key: str, only_live: bool = False) -> None:
        """把当前页面截图发进 QQ 会话；哪条上传通道可用会自动选，失败会说明原因。

        only_live=True 是"任务收尾顺带发一张"：没开着浏览器就静默跳过（不启动浏览器、
        也不发消息），别为了一条无关任务给用户塞一张空白页截图。
        """
        from agent import run_log
        key = self.norm_key(key)
        ctx = self._ctx_for(key)
        api = ctx.get("api")
        if api is None:
            return
        session = self.sessions.get(key)
        data, fmt = session.screenshot(only_live=only_live) if session else ("", "png")
        if not data:
            if only_live:
                return
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

    async def send_local_images(self, key: str, text: str) -> List[str]:
        """把回复文字里提到的本地**媒体**文件直接发进 QQ（画图 / tts 的产物）。

        agent 生成的图片/音频默认只回一个本地路径；这里扫描回复里的媒体路径，
        找到就把**文件本体**也发过去，用户不用登录服务器取文件。
        按后缀选官方 file_type（图片=1 / 语音=3），这样 tts、voice_clone
        生成的 mp3 会被当语音发出去，而不是留在文字里当路径。
        返回成功发出的**原始文本片段**列表：调用方据此把文字里的路径替换掉
        （发失败则保留文字兜底）。

        注意：返回的是文本里**原样出现**的片段，而不是解析后的绝对路径——
        调用方要用它做 `reply.replace(...)`，两者必须一致才对得上。
        """
        import base64
        import os
        from agent import run_log
        key = self.norm_key(key)
        ctx = self._ctx_for(key)
        api = ctx.get("api")
        if api is None:
            return []
        # (原始片段, 解析后的绝对路径) 成对，后面用原始片段去替换文字
        pairs: List[tuple] = []
        seen_abs = set()
        root = _project_root()
        for raw in _find_image_paths(str(text or "")):
            # 盘符路径按原样用；相对/纯文件名相对项目根解析
            cand = raw if os.path.isabs(raw) else os.path.join(root, raw)
            cand = os.path.normpath(cand)
            if cand in seen_abs:
                continue
            seen_abs.add(cand)
            ext = cand.rsplit(".", 1)[-1].lower() if "." in cand else ""
            if _MEDIA_FILE_TYPES.get(ext) is None:
                continue                        # 扩展名不认识就别乱发
            # 只有**真实存在**的文件才占补发名额：回复里常只是"提了一句路径"，
            # 先切 3 再逐个试打开会让真图被不存在的路径挤掉。
            try:
                if not os.path.isfile(cand):
                    continue
                size = os.path.getsize(cand)
            except OSError:
                continue
            pairs.append((raw, cand, size))
        if not pairs:
            return []
        group_openid, user_openid = self.split_key(key)
        sent: List[str] = []
        for raw, path, size in pairs[:3]:        # 最多补发 3 个，避免刷屏
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            ftype = _MEDIA_FILE_TYPES.get(ext) or 1
            if ftype == 3 and size < 1024:
                run_log.log(f"QQ 补发语音跳过（音频过小 {size} 字节）：{path}", "warn")
                continue                          # TTS 偶尔产出 0 字节文件
            # 额度：给正文文字至少留 1 条，别让图把结论挤掉（超了整条发送失败）
            if self._remaining_replies(key) <= 1:
                run_log.log(f"QQ 补发媒体跳过（回复额度要留给正文）：{path}", "warn")
                break
            kind = "语音" if ftype == 3 else "图片"
            try:
                with open(path, "rb") as f:
                    data = base64.b64encode(f.read()).decode("ascii")
            except OSError as e:               # noqa: BLE001
                run_log.log(f"QQ 补发{kind}跳过（读不到 {path}）：{type(e).__name__}", "warn")
                continue
            media = await self._upload_base64(api, user_openid, group_openid, data,
                                              file_type=ftype)
            if media is None:
                run_log.log(f"QQ 补发{kind}失败（{path}）：无可用上传通道", "warn")
                continue
            # 必须带 msg_id/msg_seq 走**被动回复**：群主动消息每月只有 4 条，
            # 不带 msg_id 的富媒体会被当主动消息直接拒掉（单聊同理）。
            msg_id, _fresh = self._reply_plan(key, ctx)
            seq = self._next_seq(key, msg_id)
            try:
                if group_openid:
                    await api.post_group_message(group_openid=group_openid, msg_type=7,
                                                 media=media, msg_id=msg_id, msg_seq=seq)
                else:
                    await api.post_c2c_message(openid=user_openid, msg_type=7, media=media,
                                               msg_id=msg_id, msg_seq=seq)
                sent.append(raw)                    # 回原始片段，调用方据此替换文字
            except Exception as e:              # noqa: BLE001
                run_log.log(f"QQ 补发图片失败（{path}）：{type(e).__name__}: {str(e)[:120]}",
                            "warn")
        return sent

    async def send_voice(self, key: str, text: str) -> bool:
        """把回复**内容**发成语音进 QQ（file_type=3）。

        两条路径，优先级不同：
          ① **agent 已生成的音频**：回复里带 mp3/wav 路径时，直接把那个文件发出去——
             这样保留 agent 选定的音色（voice_clone / 角色声线），而不是拿文字重合成。
          ② **/v 前缀触发的文字转语音**：没有现成音频时才用 TTS 合成。

        无论哪条，超过 MAX_VOICE_CHARS 都不念：语音是给人听的结论，
        交接单/长报告念出来是噪音。
        """
        import base64
        from agent import run_log
        key = self.norm_key(key)
        ctx = self._ctx_for(key)
        api = ctx.get("api")
        if api is None or not str(text or "").strip():
            return False

        # ① 优先用agent 已生成的音频（保留它选的音色）
        existing = _first_existing_audio(str(text))
        if existing:
            path, raw = existing
            run_log.log(f"QQ 语音：直接发送 agent 生成的音频 {os.path.basename(path)}")
        else:
            # ② 回退：拿文字合成。超长不念。
            body = str(text).strip()
            if len(body) > MAX_VOICE_CHARS:
                run_log.log(f"QQ 语音跳过（文本 {len(body)} 字 > {MAX_VOICE_CHARS} 上限，"
                            f"语音只念给人听的结论）", "warn")
                return False

            def _synth() -> str:
                try:
                    from models.tts import TTSModel
                    r = TTSModel().synthesize(body[:MAX_VOICE_CHARS])
                    return str((r or {}).get("path") or "")
                except Exception as e:          # noqa: BLE001
                    run_log.log(f"QQ 语音合成失败：{type(e).__name__}: {str(e)[:120]}", "warn")
                    return ""

            path = await asyncio.to_thread(_synth)
            raw = ""
            if not path or not os.path.exists(path):
                return False
        try:
            if os.path.getsize(path) < 1024:      # TTS 偶尔产出 0 字节/极小文件
                run_log.log(f"QQ 语音跳过（音频过小 {os.path.getsize(path)} 字节）：{path}", "warn")
                return False
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
        except OSError as e:                    # noqa: BLE001
            run_log.log(f"QQ 语音读取失败：{type(e).__name__}", "warn")
            return False
        group_openid, user_openid = self.split_key(key)
        media = await self._upload_base64(api, user_openid, group_openid, data, file_type=3)
        if media is None:
            run_log.log("QQ 语音上传失败：无可用上传通道", "warn")
            return False
        # 带 msg_id 走被动回复（群聊窗口只有 5 分钟，且主动消息要占每日配额）
        msg_id, _fresh = self._reply_plan(key, ctx)
        seq = self._next_seq(key, msg_id)
        try:
            if group_openid:
                await api.post_group_message(group_openid=group_openid, msg_type=7,
                                             media=media, msg_id=msg_id, msg_seq=seq)
            else:
                await api.post_c2c_message(openid=user_openid, msg_type=7, media=media,
                                           msg_id=msg_id, msg_seq=seq)
            return True
        except Exception as e:                  # noqa: BLE001
            run_log.log(f"QQ 语音发送失败：{type(e).__name__}: {str(e)[:120]}", "warn")
            return False

    async def _upload_base64(self, api, user_openid: str, group_openid: str, data: str,
                             file_type: int = 1, srv_send_msg: bool = False):
        """把 base64 媒体直传给平台，换取 file_info。

        参数依官方《富媒体消息概述》：
          file_type     1=图片 2=视频 3=语音 4=文件
          file_data     整文件base64（官方推荐大文件走分片上传）
          srv_send_msg  true=上传即发送、跳过发消息接口，但会**占用主动消息频次**
        失败返回 None，由调用方降级。
        """
        try:
            from botpy.http import Route
            route = (Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid)
                     if group_openid else
                     Route("POST", "/v2/users/{openid}/files", openid=user_openid))
            resp = await api._http.request(route, json={"file_type": file_type,
                                                        "file_data": data,
                                                        "srv_send_msg": srv_send_msg})
        except Exception as e:                # noqa: BLE001
            from agent import run_log
            run_log.log(f"QQ 富媒体上传异常（file_type={file_type}）："
                        f"{type(e).__name__}: {str(e)[:140]}", "warn")
            return None
        return self._media_of(resp)

    async def _upload_url(self, api, user_openid: str, group_openid: str, url: str,
                          file_type: int = 1):
        """备用通道：让平台自己去抓一个公网可达的地址（官方 URL 上传，适合已在外网的产物）。"""
        try:
            from botpy.http import Route
            route = (Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid)
                     if group_openid else
                     Route("POST", "/v2/users/{openid}/files", openid=user_openid))
            resp = await api._http.request(route, json={"file_type": file_type, "url": url,
                                                        "srv_send_msg": False})
        except Exception as e:                # noqa: BLE001
            from agent import run_log
            run_log.log(f"QQ 富媒体 URL 上传异常：{type(e).__name__}: {str(e)[:140]}", "warn")
            return None
        return self._media_of(resp)

    async def push_media(self, openid: str, path: str, file_type: int = 1,
                         group_openid: str = "") -> bool:
        """**主动**把本地媒体推给用户（不经用户触发）。

        官方频控（2026-07 文档）：单聊/群聊每日上限 1000 条/用户|群，
        单关系维度 20qpm、Bot 维度群 60qpm（未认证 30）—— 远比"每月 4 条"宽裕。
        真正的前置条件：**用户没在 QQ 客户端关掉"允许主动发送"**，关了会一律失败。

        主动消息**不带 msg_id**（那是被动回复专用字段），所以它不吃"每条消息回 4 次"的额度，
        只吃每日/每分钟的主动频控。
        """
        import base64 as _b64
        from agent import run_log
        if not openid or not path or not os.path.isfile(path):
            run_log.log(f"QQ 主动推媒体跳过：{path}", "warn")
            return False
        try:
            with open(path, "rb") as f:
                data = _b64.b64encode(f.read()).decode("ascii")
        except OSError as e:                   # noqa: BLE001
            run_log.log(f"QQ 主动推媒体读取失败：{type(e).__name__}", "warn")
            return False
        ctx = self._ctx_for(self.scope_key(openid, group_openid))
        api = ctx.get("api")
        if api is None:
            run_log.log("QQ 主动推媒体失败：会话里没有 api（桥未连上？）", "warn")
            return False
        # 主动消息**不带 msg_id**——那是被动回复专用字段
        media = await self._upload_base64(api, openid, group_openid, data, file_type=file_type)
        if media is None:
            run_log.log("QQ 主动推媒体失败：上传通道无返回", "warn")
            return False
        try:
            if group_openid:
                await api.post_group_message(group_openid=group_openid, msg_type=7, media=media)
            else:
                await api.post_c2c_message(openid=openid, msg_type=7, media=media)
            run_log.log(f"QQ 主动推送{'群' if group_openid else '私聊'}媒体成功：{path}")
            return True
        except Exception as e:                  # noqa: BLE001
            run_log.log(f"QQ 主动推媒体失败：{type(e).__name__}: {str(e)[:160]}", "warn")
            return False

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
        if self.config.ack_enabled and self.plan(key, text) == "task":
            await self._send(key, self.ack_text())
        reply = await asyncio.to_thread(self.handle_text, openid, text)
        if reply:
            #剥掉"要语音"的哨兵，取出真正要念的文本
            want_voice = reply.startswith(_VOICE_FLAG)
            if want_voice:
                reply = reply[len(_VOICE_FLAG):]
            # /v 重播：handle_text已把上一条回复排进队列
            queued = self._pending_voice.pop(key, "")
            sent = await self.send_local_images(key, reply)
            for p in sent:                      # 已发出的媒体路径不再留在文字里
                reply = reply.replace(p, _sent_placeholder(p))
            # 额度由 _send 内部按"这条来信还剩几次"截断：回执 / 补发的图 / 语音 / 正文
            # 共用同一份，超了整条发送失败（此前那个 remaining 守卫恒不成立，等于没管）。
            await self._send(key, reply)
            # 语音默认**不发**（QQBOT_VOICE_REPLY=false）。三种情况才发：
            #   ① /v 前缀 → want_voice；② /v 重播 → queued；③ 全局开关打开
            # 语音也占「每条消息最多回 4 次」的额度，图片发多了让位给文字。
            voice_text = queued or (reply if want_voice else "")
            if voice_text and self._remaining_replies(key) > 0:
                await self.send_voice(key, voice_text)
            elif self.config.voice_reply and self._remaining_replies(key) > 0:
                await self.send_voice(key, reply)

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
        if self.config.ack_enabled and self.plan(key, text) == "task":
            await self._send(key, self.ack_text())
        reply = await asyncio.to_thread(self.handle_text, member, text, group_openid)
        if reply:
            # 剥掉"要语音"的哨兵，取出真正要念的文本
            want_voice = reply.startswith(_VOICE_FLAG)
            if want_voice:
                reply = reply[len(_VOICE_FLAG):]
            # /v 重播：handle_text 已把上一条回复排进队列
            queued = self._pending_voice.pop(key, "")
            sent = await self.send_local_images(key, reply)
            for p in sent:                      # 已发出的媒体路径不再留在文字里
                reply = reply.replace(p, _sent_placeholder(p))
            # 额度由 _send 内部按"这条来信还剩几次"截断（群的富媒体也算一条）。
            await self._send(key, reply)
            # 语音默认不发；/v 前缀、/v 重播、或全局开关打开时才发。
            voice_text = queued or (reply if want_voice else "")
            if voice_text and self._remaining_replies(key) > 0:
                await self.send_voice(key, voice_text)
            elif self.config.voice_reply and self._remaining_replies(key) > 0:
                await self.send_voice(key, reply)

    async def _send(self, key: str, payload: str) -> None:
        """按 scope key 发消息（群/私聊自动分流）；单条失败只记日志，不炸掉事件处理。"""
        key = self.norm_key(key)
        ctx = self._ctx_for(key)
        api = ctx.get("api")
        if api is None:
            return
        msg_id, fresh = self._reply_plan(key, ctx)
        group_openid, user_openid = self.split_key(key)
        # 额度：回执 / 补发的图 / 语音与正文共用同一份（同一条消息最多回 4 次，超了整条失败）
        budget = self._remaining_replies(key)
        if budget <= 0:
            from agent import run_log
            run_log.log("QQ 回复额度已用尽（同一条消息最多回 "
                        f"{MAX_REPLIES_PER_MESSAGE} 次），本条正文跳过", "warn")
            return
        pieces = (_chunks_markdown(payload, max_pieces=budget) if USE_MARKDOWN
                  else _chunks_capped(_md_to_text(payload), budget))
        for piece in pieces:
            seq = self._next_seq(key, msg_id)
            try:
                if USE_MARKDOWN:
                    md = {"content": piece}
                    if group_openid:
                        await api.post_group_message(group_openid=group_openid, msg_type=2,
                                                     msg_id=msg_id, msg_seq=seq, markdown=md)
                    else:
                        await api.post_c2c_message(openid=user_openid, msg_type=2, msg_id=msg_id,
                                                   msg_seq=seq, markdown=md)
                elif group_openid:
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
        from models.prompts import QQ_CHANNEL_HINT
        if not openid:
            return ""
        key = self.scope_key(openid, group_openid)
        if not self.allowed(openid, group_openid):
            run_log.log(f"QQ 消息被拒（不在白名单）: 用户尾号 {openid[-6:]}"
                        + (f"，群尾号 {group_openid[-6:]}" if group_openid else ""), "warn")
            return self.reject_hint(openid, group_openid)
        self._touch(key)          # 记活动时间，顺带按 TTL 回收空闲会话
        if self.resolve_approval(key, text):
            return "收到，已按你的答复处理。"
        # /stop 必须在这一层处理（和审批同级）——**不能走到下面的会话锁**：
        # 正在跑的任务占着 session.lock，任何走常规路径的消息都会阻塞排队，
        # 那样 /stop 永远轮不到执行、也就永远停不掉正在跑的任务。
        stop_reply = self._handle_stop(key, text)
        if stop_reply is not None:
            return stop_reply
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

        # ---- 按需语音：/v 前缀（或 /say）—— 必须排在所有斜杠分支**之前**，
        # 否则 "/v 帮我查下天气" 会被 /chat//do/startswith("/") 依次吃掉、内容丢失。----
        want_voice = False
        for prefix in ("/v ", "/v　", "/say ", "/say　"):
            if goal.lower().startswith(prefix):
                goal, want_voice = goal[len(prefix):].strip(), True
                break
        if goal.lower() in ("/v", "/say"):
            # 「/v」单独发 = 把上一条回复重播成语音
            return self._replay_voice(key)
        lowered = goal.lower()

        # ---- 对话 / 任务的显式切换 ----
        if lowered.startswith("/chat"):
            rest = goal[len("/chat"):].strip()
            if rest:
                out = self._chat_once(session, key, rest)
                return f"{_VOICE_FLAG}{out}" if want_voice and out else out
            session.mode = "chat"
            return "已切到对话模式：直接说话就是聊天，要干活用 /do <任务>。"
        if lowered.startswith("/do") or lowered.startswith("/task"):
            cut = 3 if lowered.startswith("/do") else 5
            rest = goal[cut:].strip()
            if rest:
                out = self._execute_task(session, key, rest)
                return f"{_VOICE_FLAG}{out}" if want_voice and out else out
            session.mode = "task"
            return "已切到任务模式：发什么都当成任务执行（寒暄仍会当聊天回你）。"

        if goal.startswith("/"):
            return self._slash(key, goal)
        if not goal:
            return "发一句话告诉我要做什么。"

        # ---- 默认路由：寒暄一律走对话；否则按会话模式 ----
        if looks_like_chitchat(goal) or session.mode == "chat":
            out = self._chat_once(session, key, goal)
        else:
            out = self._execute_task(session, key, f"{goal}\n\n{QQ_CHANNEL_HINT}")
        # 用哨兵标记把"要语音"的意图传给调用方（它负责真正发 send_voice）
        return f"{_VOICE_FLAG}{out}" if want_voice and out else out

    def _session_for(self, key: str) -> "UserSession":
        return self.sessions.setdefault(
            key, UserSession(key, self.config.preset, approver=self.approver_for(key),
                             task_timeout=self.config.task_timeout,
                             mode=self.config.default_mode,
                             chat_turns=self.config.chat_turns,
                             chat_max_tokens=self.config.chat_max_tokens))

    def _touch(self, key: str) -> None:
        """记一次活动时间；顺带按 TTL 回收空闲会话（限频扫描，不必每条消息都全扫）。"""
        self._last_active[self.norm_key(key)] = time.time()
        self._prune_sessions()

    def _prune_sessions(self, force: bool = False) -> int:
        """回收空闲会话：该用户的常驻 agent 与各种按会话存的状态一并丢弃。

        正在跑任务或挂着待审批的会话不动——那会中断用户正等着的结果。
        对话本身已落盘，用户回来时新建 agent 仍能续上。
        此前会话只增不减，常驻服务的内存会一直爬。
        """
        ttl = int(self.config.session_ttl or 0)
        if ttl <= 0:
            return 0
        now = time.time()
        if not force and now - self._last_prune < 60:
            return 0
        self._last_prune = now
        dropped = 0
        for key in list(self.sessions):
            if key in self._running or key in self._pending:
                continue
            if now - self._last_active.get(key, now) < ttl:
                continue
            self.sessions.pop(key, None)
            for store in (self._ctx, self._seq, self._recent,
                          self._pending_voice, self._always, self._last_active):
                store.pop(key, None)
            dropped += 1
        return dropped

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
                out = session.chat(text) or "（没有回复）"
                # 记进 last_output：`/v` 重播要念"上一条回复"，闲聊也得能念
                session.last_output = out
                return out
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

        kind="chat" 时是闲聊：也推帧（agent 可能在聊天中开浏览器）。
        """
        # 并发闸门：小机器上同时跑多个 agent 循环会互相抢核、内存峰值叠加。
        # 等不到空位就明确回绝，别让无限排队把线程也占满。
        if not self._slots.acquire(timeout=max(0, int(self.config.task_queue_timeout))):
            return (f"现在同时在跑的任务已达上限（{self.config.max_concurrent_tasks} 个），"
                    "请稍后再发，或等前面那条跑完。")
        self._running.add(key)
        try:
            return self._execute_task_locked(session, key, goal, kind)
        finally:
            self._running.discard(key)
            self._slots.release()

    def _execute_task_locked(self, session: "UserSession", key: str, goal: str,
                             kind: str = "task") -> str:
        from agent import run_log
        run_log.log(f"QQ {'任务' if kind == 'task' else '聊天'}：会话 {session.session_name()}，"
                    f"{len(goal)} 字，档位 {session.preset}")
        runner = self._run_task or session.run
        pusher = self._start_frame_pusher(key)
        # 长任务提示：超过 20 秒还没出结果就先说一声，避免用户以为卡死
        hint = threading.Timer(
            20.0, lambda: self._send_sync(
                key, "⏳ 还在处理中（遇到上游限流会自动重试），请稍候，不用重发。"))
        hint.daemon = True
        hint.start()
        try:
            output = runner(goal)
        finally:
            hint.cancel()
            if pusher is not None:
                # 保留 5 分钟让用户看完最后画面再停止推帧
                pusher.wait(timeout=300.0)
                pusher.set()
        if kind == "task" and self.config.send_frame and self._run_task is None:
            self.schedule_frame(key, only_live=True)   # B：任务收尾把画面发到 QQ（没开浏览器就跳过）
        return output or "（没有输出）"

    def _replay_voice(self, key: str) -> str:
        """`/v` 单独发 = 把上一条回复重播成语音（不用重新跑一遍 agent）。

        语音是异步发送的，而 handle_text 是同步的，所以这里把要念的文本
        放进 _pending_voice，由调用方（handle_c2c / handle_group_message）
        发完文字后再消费。
        """
        from agent import run_log
        session = self.sessions.get(self.norm_key(key))
        last = str(getattr(session, "last_output", "") or "").strip()
        if not last:
            return "还没有可念的内容，先跟我说点什么。"
        self._pending_voice[self.norm_key(key)] = last
        run_log.log(f"QQ 语音重播已排队：{len(last)} 字")
        return f"{last}\n\n🔊 正在合成语音…"

    def _slash(self, key: str, goal: str) -> str:
        cmd = goal.split()[0].lower()
        session = self.sessions.get(key)
        if cmd in ("/help", "/?", "/h"):
            return ("可用：直接说话=聊天（寒暄），/do <任务> 执行一次任务，/chat 切对话模式，"
                    "/task 切任务模式，/status 看状态，/new 开新会话，/shot 发一张当前画面，"
                    "/v <内容> 让这条回语音，/say 把上次回复念一遍，"
                    "/stop 停本会话在跑的任务（/stop all 停全部）。"
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

        async def on_group_add_robot(self, event):
            """机器人被拉进群：把群 openid 打进日志。

            群白名单要的是 `group_openid`（腾讯发的不透明 ID），**不是**人看的那个群号 ——
            群号填进去永远匹配不上；而这个 ID 只有消息/事件送进来才拿得到。私聊那条
            "第一次握手"（发句话就回你 openid）以前在群里没有对应物，就是这个事件。
            """
            from agent import run_log
            group_openid = str(getattr(event, "group_openid", "") or "")
            operator = str(getattr(event, "op_member_openid", "") or "")
            run_log.log(f"QQ 机器人被拉进群：group_openid={group_openid}"
                        f"（操作人 {operator[-6:]}）。要放行请把它加进 .env 的 "
                        f"QQBOT_ALLOW_GROUPS 后重启桥。", "warn")

        async def on_group_del_robot(self, event):
            from agent import run_log
            run_log.log(f"QQ 机器人被移出群：group_openid="
                        f"{str(getattr(event, 'group_openid', '') or '')}", "warn")

        async def on_at_message_create(self, message):
            from agent import run_log
            run_log.log("收到频道(guild)消息：本桥不支持频道，已忽略", "warn")

    # public_messages 覆盖群/C2C 公域消息事件（botpy Intents 文档如此标注）
    intents = botpy.Intents(public_messages=True)
    client = _Client(intents=intents, is_sandbox=bool(bridge.config.sandbox))
    # botpy 缺 GROUP_MESSAGE_CREATE 的 parser 注册 —— 见下面补丁的注释。
    #（这个事件本桥不处理：只响应 @机器人；但必须让它"安全落地"，否则会中断整个分发。）
    try:
        import botpy.gateway as _gw
        _WS = _gw.BotWebSocket
        # 幂等：重复调用 _build_client 时别把已打补丁的 __init__ 再包一层（会无限套娃）
        if not getattr(_WS, "_myagent_group_msg_patch", False):
            _orig_init = _WS.__init__

            def _patched_ws_init(self, *a, **kw):
                _orig_init(self, *a, **kw)
                parsers = getattr(self, "_parser", None)
                if isinstance(parsers, dict) and "group_message_create" not in parsers:
                    parsers["group_message_create"] = lambda payload: None

            _WS.__init__ = _patched_ws_init
            _WS._myagent_group_msg_patch = True
    except Exception as e:                              # noqa: BLE001
        from agent import run_log
        run_log.log(f"QQ 全量消息事件兜底注册失败（不影响 @ 消息）："
                    f"{type(e).__name__}: {str(e)[:120]}", "warn")
    return client


def _write_pid_file() -> str:
    """写 memory/qqbot.pid（`main.py --ps` 靠它判断桥在不在跑）；退出时删。"""
    import json as _json
    from datetime import datetime as _dt
    from config import PROJECT_ROOT
    path = os.path.join(PROJECT_ROOT, "memory", "qqbot.pid")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump({"pid": os.getpid(), "started_at": _dt.now().isoformat(timespec="seconds")},
                       fh, ensure_ascii=False, indent=2)
    except Exception:                           # noqa: BLE001
        pass
    return path


def _clear_pid_file(path: str) -> None:
    try:
        os.remove(path)
    except Exception:                           # noqa: BLE001
        pass


def main(argv: Optional[list] = None) -> int:
    """`python -m agent.qqbot`：连上 QQ，把私聊消息转给 agent。"""
    import argparse
    from agent import run_log
    parser = argparse.ArgumentParser(prog="python -m agent.qqbot",
                                     description="QQ 机器人桥：用 QQ 私聊驱动 agent")
    parser.add_argument("--probe-media", action="store_true", dest="probe_media",
                        help="查看截图上传通道现状（真正验证在会话内用 /shot）")
    parser.add_argument("--bind", action="store_true",
                        help="扫码绑定机器人并写进 .env（细节参数见 python -m agent.qqbot_bind --help）")
    # 只在显式传参（或 __main__ 传 sys.argv[1:]）时解析：main() 也会被测试/宿主直接调用，
    # 那时去解析 pytest 的 argv 会直接报"无法识别的参数"。
    raw = list(argv) if argv is not None else []
    if "--bind" in raw:
        # 绑定链路的参数（--timeout/--png/--no-write-env…）交给 qqbot_bind 自己解析
        from agent.qqbot_bind import main as bind_main
        return bind_main([a for a in raw if a != "--bind"])
    args = parser.parse_args(raw)
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
    _pid_file = _write_pid_file()
    try:
        run_log.start(tag="qqbot", extra={"qqbot": repr(config)})
        bridge = QQBotBridge(config)
        client = _build_client(bridge)
        run_log.log(f"QQ 桥启动（{where}环境），等待消息")
        try:
            client.run(appid=config.app_id, secret=config.secret)
        except RuntimeError as exc:      # 腾讯网关拒绝凭据时给一句人话，别甩 traceback
            print(f"QQ 桥启动失败：腾讯拒绝了这对凭据（{exc}）。"
                  "去 QQ 开放平台核对机器人的 AppID/AppSecret，"
                  "或重跑 `python -m agent.qqbot --bind` 重新扫码绑定。")
            return 3
    finally:
        _clear_pid_file(_pid_file)
    return 0


if __name__ == "__main__":      # pragma: no cover - 长驻入口
    import sys as _sys
    raise SystemExit(main(_sys.argv[1:]))
