"""
视觉分析模块。
调用多模态 LLM（GPT-4V / Qwen-VL 等）分析截图：
- 页面内容描述
- 元素定位（返回坐标）
- OCR 文字提取
- 页面结构分析
"""
import base64
import json
import os
import re

from openai import OpenAI
from config import LLM_CONFIG, VISION_CONFIG


class VisionModel:
    """
    视觉分析模型。
    接收截图 + 问题，调用多模态 LLM 返回分析结果。

    支持的模型：GPT-4o、GPT-4V、Qwen-VL-Max 等多模态视觉模型。
    普通的 text-only 模型（如 gpt-4o-mini 无视觉能力）不可用于此模块。
    """

    # 可选择的视觉模型优先级列表
    RECOMMENDED_MODELS = [
        "gpt-4o",
        "gpt-4o-mini",
        "ox-alpha",          # OpenRouter stealth 推理模型（图片支持视上游而定）
        "gpt-4-vision-preview",
        "gpt-4-turbo",
        "qwen-vl-max",
        "qwen-vl-plus",
        "gemini-2.0-flash",
    ]

    def __init__(
        self,
        vision_model: str = None,
        base_url: str = None,
        api_key: str = None,
    ):
        """
        Args:
            vision_model: 视觉模型名称，默认自动检测。
            base_url / api_key: 覆盖端点与密钥（桌面端设置下发）；
                留空回退 VISION_* 环境变量，再回退主 LLM 配置。
        """
        # 视觉模型可走独立端点（VISION_API_KEY / VISION_BASE_URL），
        # 留空时回退到主 LLM 端点（如主模型用 OpenRouter、视觉用商汤）
        #
        # timeout/max_retries 必须显式给：OpenAI SDK 默认读超时 600s、自动重试
        # 2 次，一次挂死的视觉调用能拖到 ~1800s；而调用方预算只有 300s，
        # browser 的 visionclick 更是 60s。超时后执行器早已放弃，孤儿线程还在
        # 重试，白烧 3 次付费调用。重试交给上层（执行器/工具）统一管理。
        self.client = OpenAI(
            api_key=api_key or VISION_CONFIG.get("api_key") or LLM_CONFIG["api_key"],
            base_url=base_url or VISION_CONFIG.get("base_url") or LLM_CONFIG["base_url"],
            timeout=float(VISION_CONFIG.get("timeout", 30)),
            max_retries=0,
        )
        self.vision_model = vision_model or VISION_CONFIG.get("vision_model") or self._auto_detect_model()
        self.screenshot_dir = VISION_CONFIG.get("screenshot_path", "./screenshots")
        #: 上一次成功应答用的模型（备用顶上时，调用方/日志要知道是谁答的）
        self.last_model = self.vision_model
        self.last_detail = ""
        self.last_fallback_reason = ""
        self.base_url = base_url or VISION_CONFIG.get("base_url") or LLM_CONFIG["base_url"]

    # ------------------------------------------------------------
    # 备用端点（主模型超时/报错时接着试）
    # ------------------------------------------------------------

    def _fallback_clients(self):
        """按配置构建备用视觉端点 [(模型名, client), ...]（与主端点相同的跳过）。

        结果**按配置缓存**：旧实现每次 `analyze()` 都新建一批 `OpenAI` 客户端，
        每个自带一个 httpx 连接池，而全流程没有 `close()` 也不缓存 —— 一轮浏览器
        任务连续分析几十张截图就会创建几十个连接池，只能等 GC 回收
        （2026-09-22 审计）。
        """
        models = VISION_CONFIG.get("fallback_models") or []
        base = VISION_CONFIG.get("fallback_base_url") or VISION_CONFIG.get("base_url")
        key = VISION_CONFIG.get("fallback_api_key") or VISION_CONFIG.get("api_key")
        timeout = float(VISION_CONFIG.get("fallback_timeout", 60))
        cache_key = (tuple(models), str(base), str(key),
                     str(self.vision_model), str(self.base_url), timeout)
        if getattr(self, "_fallback_cache_key", None) == cache_key:
            return self._fallback_cache

        out = []
        if models:
            for model in models:
                if not model:
                    continue
                # 与主端点+主模型完全相同的条目没有意义（重试同一个东西）
                if model == self.vision_model and str(base) == str(self.base_url):
                    continue
                try:
                    client = OpenAI(api_key=key, base_url=base, timeout=timeout, max_retries=0)
                except Exception:                   # noqa: BLE001
                    continue
                out.append((model, client))
        self._fallback_cache_key = cache_key
        self._fallback_cache = out
        return out

    def fallback_note(self) -> str:
        """备用模型顶上时的说明（供工具输出给用户/模型看，避免误以为主模型正常）。"""
        if str(self.last_detail).startswith("备用"):
            why = (self.last_fallback_reason or "").strip()
            return (f"\n（本次由**备用模型** {self.last_model} 应答；主模型失败："
                    f"{why[:120] or '未知原因'}）")
        return ""

    def _analyze_once(self, client, model: str, messages: list, max_tokens: int,
                      reasoning_effort: str) -> str:
        """单次调用 + 响应加固（空 choices / 空内容都转成可读错误）。"""
        kwargs = dict(model=model, messages=messages, max_tokens=max_tokens)
        if reasoning_effort:
            # 网关把 reasoning_effort 作为顶层参数（OpenAI SDK 需 extra_body 透传）
            kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as e:
            raise RuntimeError(f"视觉模型调用失败: {str(e)}")

        # 纯文本模型收到图片、或被安全拦截时，常见返回空 choices/空 content，
        # 直接下标访问会抛晦涩的 NoneType 错误——这里转成可读的明确提示
        choices = getattr(response, "choices", None)
        if not choices:
            raise RuntimeError(
                "视觉模型返回空 choices：该模型可能不支持图片输入，或请求被服务端拒绝。")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if not content:
            reason = getattr(choices[0], "finish_reason", "") or ""
            raise RuntimeError(
                f"视觉模型返回空内容（finish_reason={reason or '未知'}）："
                "该模型可能不支持图片输入，或内容被安全策略拦截。")
        return content

    def _auto_detect_model(self) -> str:
        """自动选择可用的视觉模型。"""
        current = LLM_CONFIG.get("default_model", "gpt-4o-mini")
        # 如果当前默认模型本身支持视觉，直接使用
        for rec in self.RECOMMENDED_MODELS:
            if rec in current.lower():
                return current
        # 否则使用第一个推荐的（通常是 gpt-4o）
        return self.RECOMMENDED_MODELS[0]

    def analyze(
        self,
        image_data,
        question: str,
        image_type: str = "image/png",
        detail: str = "auto",
        max_tokens: int = 2000,
        reasoning_effort: str = "none",
        system_prompt: str = "你是一个说话客观公正的小助手。",
    ) -> str:
        """
        分析截图，回答关于页面内容的问题。

        Args:
            image_data: 单张图片的 base64（不含 data URI 前缀），
                或**多张图片的列表**（对比场景必须传全部图片——见下）。
            question: 要问的问题（如"页面上有哪些可点击的按钮？"）。
            image_type: 图片 MIME 类型。
            detail: 图片分析精度（'auto' / 'low' / 'high'）。
            max_tokens: 最大输出 token。
            reasoning_effort: 推理强度（官方推荐 "none"：视觉+推理组合在
                部分网关不稳定/易 500/拖慢；支持 "none"/"low"/"medium"/"high"）。
            system_prompt: 可选的 system 消息（对齐官方示例）。

        Returns:
            模型的分析结果文本。
        """
        # 支持多图：此前只能传一张，导致"对比前后两张截图"的调用方把 after 图
        # 悄悄丢掉，却仍然让模型去对比——模型只能凭空编造变化（实测审计发现）。
        images = image_data if isinstance(image_data, (list, tuple)) else [image_data]
        images = [i for i in images if i]
        if not images:
            return "未提供图片，无法分析。"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        content_parts = [{"type": "text", "text": question}]
        for img in images:
            content_parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_type};base64,{img}",
                    "detail": detail,
                },
            })
        messages.append({"role": "user", "content": content_parts})

        # 主模型 → 备用模型依次尝试：视觉上游抖动/超时/不支持图片时，
        # 换一家继续（用户要求："当模型超时就换这些"）。全失败才抛错，
        # 错误里带上每一次的失败原因，便于判断是"都不支持"还是"网络问题"。
        attempts = [(self.vision_model, self.client)] + self._fallback_clients()
        errors = []
        for idx, (model, client) in enumerate(attempts):
            try:
                content = self._analyze_once(client, model, messages, max_tokens,
                                             reasoning_effort)
                self.last_model = model
                self.last_detail = "主模型" if idx == 0 else f"备用模型（第 {idx} 个）"
                if idx > 0:
                    # 让上层知道这次是备用顶上的（工具输出里会显示，避免误以为主模型正常）
                    self.last_fallback_reason = errors[-1] if errors else ""
                return content
            except Exception as e:              # noqa: BLE001
                errors.append(f"{model}: {str(e)[:160]}")
                continue
        raise RuntimeError("视觉调用全部失败（主模型 + "
                           f"{len(attempts) - 1} 个备用）：" + "；".join(errors))

    # ================================================================
    # 高级封装：常用分析场景
    # ================================================================

    #: 一条视频消息里最多放几帧（帧数×单帧 token 决定请求大小）
    MAX_VIDEO_FRAMES = 16

    def analyze_video(self, video_path: str, question: str = "",
                      frames: int = 6, max_tokens: int = 1500,
                      detail: str = "auto") -> str:
        """分析本地**视频文件**：等间隔抽帧 → 一次请求发出全部帧，按时间顺序理解。

        为什么是抽帧：`GET /models` 显示商汤端点所有模型的 `input_modalities`
        只有 `text` 与 `text,image`，**没有任何模型声明 video**（2026-09-23 实测），
        所以视频没法直接喂给模型。帧必须**放在同一个请求**里 —— 分多次问
        "第 N 帧是什么"只能拿到各帧的孤立描述，答不了"视频里发生了什么变化"。

        Args:
            video_path: 视频文件路径。
            question: 要问的问题；留空走 VISION_VIDEO_ANALYSIS_QUESTION。
            frames: 抽几帧（1-16）：越长/变化越快的片子要越多，也越费 token。
            max_tokens / detail: 同 analyze()。

        Returns:
            模型的分析文本。

        Raises:
            RuntimeError: 视频不存在、缺 ffmpeg、或视觉调用全部失败。
        """
        from models.prompts import (VISION_VIDEO_ANALYSIS_QUESTION,
                                    VISION_VIDEO_FRAME_PREAMBLE)
        from models.video_edit import VideoEditError, VideoEditor, ffmpeg_path

        if not video_path:
            raise RuntimeError("未提供视频路径，无法分析。")
        if not os.path.exists(video_path):
            raise RuntimeError(f"视频不存在: {video_path}")
        if not ffmpeg_path():
            raise RuntimeError(
                "分析视频需要 ffmpeg 抽帧，当前未找到。请安装"
                "（winget install Gyan.FFmpeg）或在 .env 设置 FFMPEG_PATH。")

        try:
            frame_paths = VideoEditor().extract_frames(video_path, count=frames)
        except VideoEditError as e:
            raise RuntimeError(f"视频抽帧失败: {e}")

        try:
            images = []
            for path in frame_paths:
                with open(path, "rb") as fh:
                    images.append(base64.b64encode(fh.read()).decode())
            n = len(images)
            full_question = VISION_VIDEO_FRAME_PREAMBLE.format(
                n=n,
                question=(question or "").strip() or VISION_VIDEO_ANALYSIS_QUESTION)
            return self.analyze(images, full_question, image_type="image/jpeg",
                                detail=detail, max_tokens=max_tokens)
        finally:
            # 抽出来的帧是中间产物，成败都要清掉（AGENTS.md 规则 9）
            self._cleanup_frames(frame_paths)

    @staticmethod
    def _cleanup_frames(paths) -> None:
        """删除临时帧与（空）目录；目录非空就不动，避免误删用户指定的目录。"""
        parent = ""
        for path in paths or []:
            try:
                parent = parent or os.path.dirname(path)
                os.remove(path)
            except OSError:
                pass
        if parent:
            try:
                os.rmdir(parent)
            except OSError:
                pass

    def describe_page(self, screenshot_base64: str) -> str:
        """
        描述页面的整体内容和布局。

        Returns:
            页面内容描述。
        """
        question = (
            "请详细描述这个网页的内容和布局。包括：\n"
            "1. 页面的主要标题和主题\n"
            "2. 可见的主要内容区域（文章、列表、表单等）\n"
            "3. 所有可见的按钮、链接和交互元素\n"
            "4. 所有可见的输入框和表单元素\n"
            "5. 页面顶部导航栏的内容\n"
            "用中文回答，尽量详细但不啰嗦。"
        )
        return self.analyze(screenshot_base64, question, max_tokens=1500)

    def locate_element(self, screenshot_base64: str, description: str) -> dict:
        """
        在截图中定位指定元素，返回其大致坐标。

        Args:
            screenshot_base64: 截图 base64 数据。
            description: 元素描述（如"登录按钮"、"搜索输入框"等）。

        Returns:
            包含 x, y, width, height, found 的字典。
        """
        question = (
            f"请在截图中找到以下元素：{description}\n\n"
            "你需要返回一个 JSON 对象，格式如下：\n"
            '{{"found": true/false, "x": 中心x坐标, "y": 中心y坐标, '
            '"width": 元素大致宽度, "height": 元素大致高度, '
            '"selector_hint": "可能的CSS选择器或元素描述"}}\n\n'
            "坐标是相对于截图左上角的像素位置。\n"
            "如果找不到该元素，found 设为 false。\n"
            "只输出 JSON，不要其他文字。"
        )
        result = self.analyze(screenshot_base64, question, max_tokens=500)

        # 解析 JSON
        json_match = re.search(r'\{[\s\S]*\}', result)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        return {"found": False, "error": f"无法解析模型输出: {result[:200]}"}

    def extract_text(self, screenshot_base64: str) -> str:
        """
        从截图中提取所有可见文字（视觉 OCR）。

        Returns:
            提取的文字内容。
        """
        question = (
            "请提取截图中所有可见的文字内容。\n"
            "保持文字的顺序和层级关系。\n"
            "用 [标题]、[按钮]、[链接]、[正文] 等标签标注文字类型。\n"
            "不要添加额外的解释。"
        )
        return self.analyze(screenshot_base64, question, max_tokens=3000)

    def find_interactive_elements(self, screenshot_base64: str) -> str:
        """
        找出页面上所有可交互的元素。

        Returns:
            交互元素列表。
        """
        question = (
            "请列出截图中所有可点击、可交互的元素及其大致位置。\n"
            "以列表形式输出，每行格式：\n"
            "- 元素类型（按钮/链接/输入框/下拉菜单/复选框等）: 文字内容或描述 (大致位置：左上/中间/右上/左侧/右侧/底部)\n"
            "用中文回答。"
        )
        return self.analyze(screenshot_base64, question, max_tokens=1500)
