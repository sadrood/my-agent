"""
文生视频工具（VideoGenTool）。

视频生成是**异步任务**，与文生图（同步返回）不同：
    generate → POST /videos 创建任务 → 轮询等待 → 完成下载 mp4
    status   → 查询已有任务（等待超时/长任务稍后取结果）

为什么要 status 命令：5 秒 / 720P 实测约 41 秒完成，更长视频可能超过
工具内等待上限（VIDEO_GEN_MAX_WAIT，默认 240s，需小于工具级硬超时 300s）。
超时时不会丢任务——返回 task_id，模型可稍后用 status 取回。

依赖：models.video_gen.VideoGenModel。
配置：VIDEO_GEN_*（.env），默认 Agnes：https://api.agnes-ai.cn/v1。
"""
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

# 实测支持的档位（Flash 仅 720P；2.5 更多，配置可覆盖）
SIZE_CHOICES = ["720P", "1080P"]
ASPECT_CHOICES = ["16:9", "9:16", "1:1", "4:3", "3:4"]
SECOND_CHOICES = ["3", "5", "10"]


class VideoGenTool(BaseTool):
    """文生视频工具：异步创建任务、等待结果、下载 mp4 到本地。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"   # 需写入视频文件

    def __init__(self, video_model=None):
        """
        Args:
            video_model: VideoGenModel 实例（None 时延迟创建；测试注入用）
        """
        self._model = video_model

    @property
    def name(self) -> str:
        return "video_gen"

    @property
    def description(self) -> str:
        from config import VIDEO_GEN_CONFIG
        max_wait = int(float(VIDEO_GEN_CONFIG.get("max_wait", 240)))
        default_sec = VIDEO_GEN_CONFIG.get("seconds", "5")
        return (
            "文生视频工具（异步任务）。根据文字描述生成短视频（mp4），完成后"
            "自动下载到本地并返回文件路径。\n"
            f"用法：\n"
            f"  generate — 创建并**等待**任务（最长约 {max_wait} 秒）；"
            f"若超时会返回 task_id（任务仍在跑），稍后用 status 取结果\n"
            f"  status   — 查询任务状态/结果（用 generate 返回的 task_id）\n"
            f"参考生成耗时：5 秒 / 720P 约 40-60 秒，时长越长越久。\n"
            "提示词建议写清：主体动作、镜头运动（推拉摇移）、光线氛围、风格。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["generate", "status"],
                    "description": "generate=生成视频并等待；status=查询已有任务",
                },
                "prompt": {
                    "type": "string",
                    "description": "视频内容描述（中文）。建议含主体动作/镜头/光线/风格",
                },
                "task_id": {
                    "type": "string",
                    "description": "status 用：generate 返回的 task_id（video_id）",
                },
                "seconds": {
                    "type": "string",
                    "enum": SECOND_CHOICES,
                    "description": "视频时长（秒），默认配置值（通常 5）",
                },
                "size": {
                    "type": "string",
                    "enum": SIZE_CHOICES,
                    "description": "分辨率，默认配置值（Flash 仅支持 720P）",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ASPECT_CHOICES,
                    "description": "画幅比例，默认 16:9",
                },
                "wait": {
                    "type": "boolean",
                    "description": "generate 时是否等待完成，默认 true；"
                                   "超长视频可设 false 只创建任务",
                },
            },
            "required": ["command"],
        }

    # ------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------

    def _get_model(self):
        if self._model is not None:
            return self._model
        try:
            from models.video_gen import VideoGenModel
            self._model = VideoGenModel()
        except Exception:
            self._model = None
        return self._model

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        cmd = str(arguments.get("command") or "generate").strip().lower()
        if cmd == "status":
            return self._status(str(arguments.get("task_id") or "").strip())
        if cmd != "generate":
            return ToolResult(success=False, output="",
                              error=f"未知命令: {cmd}（可用: generate / status）")
        return self._generate(
            prompt=str(arguments.get("prompt") or "").strip(),
            seconds=arguments.get("seconds") or None,
            size=arguments.get("size") or None,
            aspect_ratio=arguments.get("aspect_ratio") or None,
            wait=arguments.get("wait", True) is not False,
        )

    def execute(self, input_str: str) -> ToolResult:
        """字符串入口：`generate <提示词>` / `status <task_id>`。"""
        text = (input_str or "").strip()
        if not text:
            return ToolResult(success=False, output="",
                              error="用法: generate <提示词> 或 status <task_id>")
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "status":
            return self._status(rest)
        # 默认按 generate 处理（允许直接给提示词）
        return self._generate(prompt=rest or text)

    # ------------------------------------------------------------
    # 具体命令
    # ------------------------------------------------------------

    def _generate(self, prompt: str, seconds=None, size=None,
                  aspect_ratio=None, wait: bool = True) -> ToolResult:
        if not prompt:
            return ToolResult(
                success=False, output="",
                error="请提供视频描述（prompt），例如：一只橘猫在窗台上打哈欠，"
                      "特写镜头，暖色夕阳，毛发细节清晰。")

        model = self._get_model()
        if model is None:
            return ToolResult(success=False, output="",
                              error="视频生成模块不可用（导入失败）。")
        if not getattr(model, "api_key", ""):
            return ToolResult(
                success=False, output="",
                error="未配置视频生成 API key：请在 .env 设置 VIDEO_GEN_API_KEY"
                      "（或复用主 LLM key）。")

        try:
            result = model.generate(prompt, seconds=seconds, size=size,
                                    aspect_ratio=aspect_ratio, wait=wait)
        except Exception as e:
            return ToolResult(success=False, output="",
                              error=f"视频生成失败: {str(e)[:300]}")

        vid = result.get("video_id", "")
        status = result.get("status")
        lines = [f"视频任务: {vid}（{result.get('model')} · "
                 f"{result.get('seconds')}s · {result.get('size')}）"]

        if result.get("timed_out"):
            lines.append(
                f"⏳ 仍在生成（进度 {result.get('progress')}%），已等待超时返回。"
                f"稍后用 video_gen(command=\"status\", task_id=\"{vid}\") 取结果。")
            return ToolResult(success=True, output="\n".join(lines),
                              metadata={"video_id": vid, "timed_out": True,
                                        "prompt": prompt, "pending": True})

        err = result.get("error")
        if err:
            lines.append(f"❌ 生成失败: {str(err)[:200]}")
            return ToolResult(success=False, output="\n".join(lines),
                              error=str(err)[:200], metadata={"video_id": vid})

        local = result.get("local_path")
        url = result.get("url")
        if local:
            lines.append(f"✅ 已生成并保存: {local}")
        elif url and result.get("download_error"):
            lines.append(f"✅ 已生成（下载失败: {result['download_error']}）\n{url}")
        elif url:
            lines.append(f"✅ 已生成: {url}")
        else:
            lines.append(f"状态: {status}（暂无结果）")

        return ToolResult(
            success=bool(local or url),
            output="\n".join(lines),
            metadata={"video_id": vid, "url": url, "local_path": local,
                      "prompt": prompt},
        )

    def _status(self, task_id: str) -> ToolResult:
        if not task_id:
            return ToolResult(
                success=False, output="",
                error="请提供 task_id（generate 返回的 task_id / video_id）。")
        model = self._get_model()
        if model is None:
            return ToolResult(success=False, output="",
                              error="视频生成模块不可用（导入失败）。")
        try:
            data = model.query(task_id)
        except Exception as e:
            return ToolResult(success=False, output="",
                              error=f"查询失败: {str(e)[:300]}")

        status = str(data.get("status") or "unknown").lower()
        progress = data.get("progress")
        url = data.get("url")
        err = data.get("error")
        lines = [f"任务 {task_id}: {status}"
                 + (f"（进度 {progress}%）" if progress is not None else "")]

        if err:
            lines.append(f"❌ 失败: {str(err)[:200]}")
            return ToolResult(success=False, output="\n".join(lines),
                              error=str(err)[:200])
        if url:
            local = None
            try:
                local = model.download(url, task_id)
                lines.append(f"✅ 已完成并保存: {local}")
            except Exception as e:
                lines.append(f"✅ 已完成: {url}\n（下载失败: {str(e)[:120]}）")
            return ToolResult(success=True, output="\n".join(lines),
                              metadata={"video_id": task_id, "url": url,
                                        "local_path": local})
        lines.append("⏳ 仍在生成，稍后再查。")
        return ToolResult(success=True, output="\n".join(lines),
                          metadata={"video_id": task_id, "pending": True})
