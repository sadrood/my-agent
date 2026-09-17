"""
视频生成模块（OpenAI Videos 兼容 / 异步任务）。

实测契约（Agnes agnes-video-2.5-flash）：
    创建: POST {base_url}/videos
          {"model": "...", "prompt": "...", "seconds": "5",
           "mode": "text", "size": "720P", "aspect_ratio": "16:9"}
          → {"id": "task_xxx", "video_id": "task_xxx", "status": "queued", ...}
    查询: GET  {query_base}/agnesapi?video_id=<ID>&model_name=<模型>
          → {"status": "queued|in_progress|completed|failed",
             "progress": 0-100, "url": "<mp4 url | null>", "error": null}
          ⚠️ 查询端点在 **HOST 根路径**（不在 /v1 下），故单独配 query_base。

与图像生成的最大差异：**异步任务**——创建后要轮询；5 秒/720P 实测约 41 秒
完成，更长视频更久，因此等待有上限，超时后把 task_id 交回调用方稍后查询。

配置：config.VIDEO_GEN_CONFIG（环境变量 VIDEO_GEN_*）。
"""
import json
import os
from datetime import datetime

import httpx

from config import VIDEO_GEN_CONFIG

# 任务终态
_STATUS_DONE = ("completed", "succeeded", "success")
_STATUS_FAILED = ("failed", "error", "canceled", "cancelled")

# 轮询期可重试的上游错误（限流/网关抖动/网络闪断）。
# 这些**不代表任务失败**——任务还在服务端跑，重试即可，别让模型重做。
_TRANSIENT_MARKERS = (
    "429", "500", "502", "503", "504",
    "查询过于频繁", "too many requests", "rate limit",
    "timeout", "timed out", "连接", "connection",
)


def _is_transient_query_error(err: Exception) -> bool:
    """轮询查询失败是否属于"待会儿再问就好"的瞬时错误。"""
    msg = str(err).lower()
    return any(m.lower() in msg for m in _TRANSIENT_MARKERS)


def _query_base_from(base_url: str) -> str:
    """由 base_url 推导查询端点主机（去掉结尾的 /v1）。

    查询端点在 HOST 根路径：https://api.agnes-ai.cn/agnesapi?...
    而生成端点在 https://api.agnes-ai.cn/v1/videos
    """
    base = (base_url or "").rstrip("/")
    if base.endswith("/v1"):
        return base[:-3]
    return base


class VideoGenModel:
    """文生视频客户端（异步任务：创建 → 轮询 → 下载）。"""

    def __init__(
        self,
        api_key: str = None,
        base_url: str = None,
        model: str = None,
        save_dir: str = None,
        timeout: float = None,
    ):
        cfg = VIDEO_GEN_CONFIG
        self.api_key = api_key or cfg["api_key"]
        self.base_url = (base_url or cfg["base_url"]).rstrip("/")
        self.query_base = (cfg.get("query_base") or "").rstrip("/") \
            or _query_base_from(self.base_url)
        self.model = model or cfg["model"]
        self.save_dir = save_dir or cfg["save_dir"]
        self.timeout = timeout or cfg["timeout"]
        self.seconds = str(cfg.get("seconds", "5"))
        self.size = cfg.get("size", "720P")
        self.aspect_ratio = cfg.get("aspect_ratio", "16:9")
        self.max_wait = float(cfg.get("max_wait", 240))
        self.poll_interval = float(cfg.get("poll_interval", 6))

    # ------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def create(self, prompt: str, seconds: str = None, size: str = None,
               aspect_ratio: str = None, mode: str = "text",
               model: str = None) -> dict:
        """创建视频任务，返回创建响应（含 video_id / status）。"""
        payload = {
            "model": model or self.model,
            "prompt": prompt,
            "seconds": str(seconds or self.seconds),
            "mode": mode,
            "size": size or self.size,
            "aspect_ratio": aspect_ratio or self.aspect_ratio,
        }
        try:
            resp = httpx.post(f"{self.base_url}/videos", json=payload,
                              headers=self._headers(), timeout=self.timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(f"视频任务创建失败: {str(e)[:200]}") from e
        if resp.status_code >= 400:
            raise RuntimeError(
                f"视频任务创建失败 HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(f"视频任务创建响应不是 JSON: {resp.text[:200]}") from e
        # 兼容不同字段名（id / video_id / task_id）
        vid = data.get("video_id") or data.get("id") or data.get("task_id")
        if not vid:
            raise RuntimeError(f"视频任务创建响应缺少任务 ID: {json.dumps(data)[:300]}")
        data["video_id"] = vid
        return data

    def query(self, video_id: str, model: str = None) -> dict:
        """查询任务状态（返回 status / progress / url / error）。"""
        url = (f"{self.query_base}/agnesapi?video_id={video_id}"
               f"&model_name={model or self.model}")
        try:
            resp = httpx.get(url, headers={"Authorization": f"Bearer {self.api_key}"},
                             timeout=self.timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(f"视频任务查询失败: {str(e)[:200]}") from e
        if resp.status_code >= 400:
            raise RuntimeError(
                f"视频任务查询失败 HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as e:
            raise RuntimeError(f"视频任务查询响应不是 JSON: {resp.text[:200]}") from e

    # ------------------------------------------------------------
    # 等待 / 下载
    # ------------------------------------------------------------

    def wait(self, video_id: str, max_wait: float = None,
             on_progress=None) -> dict:
        """轮询直到完成/失败/超时。

        Returns:
            {"status": ..., "url": ..., "progress": ..., "timed_out": bool,
             "error": ...}
            超时时 status 为最后一次观测值、timed_out=True（任务仍在跑，
            调用方可用 video_id 稍后再查）。

        轮询期的**瞬时错误不计为任务失败**：实测 6 秒一次的查询会被上游
        拒绝（HTTP 429 "查询过于频繁"），旧实现直接抛错，模型只好重做一遍，
        白烧一次视频配额（Token Plan 仅 500 秒/天）外加两分钟等待。
        这类错误只降速重试，直到 deadline 或任务真正完成。
        """
        import time
        deadline = time.time() + (max_wait if max_wait is not None else self.max_wait)
        last: dict = {"status": "unknown", "progress": 0, "url": None, "error": None}
        interval = self.poll_interval
        transient_error = None
        while True:
            try:
                data = self.query(video_id)
            except RuntimeError as e:
                if not _is_transient_query_error(e):
                    raise
                # 任务还在服务端跑：退避后继续问，不要把它判死
                transient_error = str(e)[:200]
                interval = min(interval * 2, 30.0)
                if time.time() >= deadline:
                    return {**last, "timed_out": True,
                            "transient_error": transient_error}
                time.sleep(interval)
                continue

            interval = self.poll_interval          # 查询恢复即回到常规节奏
            transient_error = None
            last = {
                "status": data.get("status"),
                "progress": data.get("progress"),
                "url": data.get("url"),
                "error": data.get("error"),
            }
            if on_progress is not None:
                try:
                    on_progress(last)
                except Exception:
                    pass
            st = str(last["status"] or "").lower()
            if st in _STATUS_DONE and last["url"]:
                return {**last, "timed_out": False}
            if st in _STATUS_FAILED:
                return {**last, "timed_out": False}
            if time.time() >= deadline:
                return {**last, "timed_out": True}
            time.sleep(interval)

    def download(self, url: str, video_id: str = "") -> str:
        """下载 mp4 到 save_dir，返回本地路径。"""
        resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
        if resp.status_code >= 400:
            raise RuntimeError(f"视频下载失败 HTTP {resp.status_code}")
        directory = self.save_dir
        os.makedirs(directory, exist_ok=True)
        # 毫秒精度：旧实现用 [:17] 只保留微秒第 1 位，同秒内会生成同名文件
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:21]
        tag = (video_id or "video")[-8:]
        path = os.path.join(directory, f"vid-{ts}-{tag}.mp4")
        with open(path, "wb") as f:
            f.write(resp.content)
        return path

    # ------------------------------------------------------------
    # 组合流程
    # ------------------------------------------------------------

    def generate(self, prompt: str, seconds: str = None, size: str = None,
                 aspect_ratio: str = None, wait: bool = True,
                 model: str = None, save: bool = True,
                 max_wait: float = None, on_progress=None) -> dict:
        """创建任务并在需要时等待完成。

        Returns:
            {"video_id", "status", "url", "local_path", "timed_out",
             "model", "seconds", "size", "prompt"}
            - wait=False：只创建，立即返回 video_id（适合超长视频）
            - 超时：timed_out=True，video_id 可稍后查询，不丢任务
        """
        created = self.create(prompt, seconds=seconds, size=size,
                              aspect_ratio=aspect_ratio, model=model)
        vid = created["video_id"]
        result = {
            "video_id": vid,
            "status": created.get("status"),
            "url": None,
            "local_path": None,
            "timed_out": False,
            "model": model or self.model,
            "seconds": str(seconds or self.seconds),
            "size": size or self.size,
            "prompt": prompt,
        }
        if not wait:
            return result

        final = self.wait(vid, max_wait=max_wait, on_progress=on_progress)
        result.update({
            "status": final.get("status"),
            "url": final.get("url"),
            "timed_out": final.get("timed_out", False),
            "error": final.get("error"),
            "progress": final.get("progress"),
        })
        url = final.get("url")
        if save and url and not final.get("timed_out"):
            try:
                result["local_path"] = self.download(url, vid)
            except Exception as e:
                result["download_error"] = str(e)[:200]
        return result


def is_configured() -> bool:
    """视频生成是否已配置可用（enabled 且有 key）。"""
    cfg = VIDEO_GEN_CONFIG
    if not cfg.get("enabled"):
        return False
    return bool(cfg.get("api_key"))
