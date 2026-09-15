"""
图像生成模块（OpenAI 兼容 /images/generations 文生图）。

支持任意 OpenAI 兼容提供方，实测：
    POST {base_url}/images/generations
    {"model": "...", "prompt": "...", "n": 1,
     "size": "1024x1024", "response_format": "b64_json"}
响应:
    {"created": ..., "data": [{"b64_json": "..."} | {"url": "..."}]}
    - b64_json（商汤 sensenova-u1.5-lite）→ 解码保存为本地 PNG/JPEG
    - url（Agnes agnes-image-2.5-flash）→ **自动下载**后保存（失败回退 URL）

配置：config.IMAGE_GEN_CONFIG（环境变量 IMAGE_GEN_*）。

直接用 httpx 而不依赖 openai SDK 的 images 接口：契约简单、对
OpenAI 兼容提供方的兼容性最好。
"""
import base64
import os
from datetime import datetime
from typing import List, Optional

import httpx

from config import IMAGE_GEN_CONFIG

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

# 提供方专有 / 可能不被支持的可选字段：报 400 提到该字段名时自动剔除重试
# （商汤认 watermark；Agnes 不认，会回 "watermark 不是文生图队列支持的字段"）
_OPTIONAL_FIELDS = ("watermark", "response_format")


class ImageGenModel:
    """文生图客户端（OpenAI 兼容 /images/generations）。"""

    def __init__(
        self,
        api_key: str = None,
        base_url: str = None,
        model: str = None,
        size: str = None,
        save_dir: str = None,
        timeout: float = None,
        watermark: bool = None,
    ):
        cfg = IMAGE_GEN_CONFIG
        self.api_key = api_key or cfg["api_key"]
        self.base_url = (base_url or cfg["base_url"]).rstrip("/")
        self.model = model or cfg["model"]
        self.default_size = size or cfg["size"]
        self.save_dir = save_dir or cfg["save_dir"]
        self.timeout = timeout or cfg["timeout"]
        # 官方公测期免费开放去水印：false = 不带水印
        self.watermark = cfg["watermark"] if watermark is None else bool(watermark)

    # ------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------

    def _post(self, payload: dict) -> dict:
        """发送生成请求；提供方不认的可选字段自动剔除后重试一次。

        各家专有字段不同：商汤认 `watermark`，Agnes 会以
        "watermark 不是文生图队列支持的字段" 报 400。这里按错误文本识别
        并剔除该字段重试（与 models/llm.py 的 400 参数降级同一思路），
        避免换提供方就要改代码。
        """
        try:
            return self._post_raw(payload)
        except RuntimeError as e:
            msg = str(e)
            for field in _OPTIONAL_FIELDS:
                if field in payload and field in msg:
                    retry = {k: v for k, v in payload.items() if k != field}
                    return self._post_raw(retry)
            raise

    def _post_raw(self, payload: dict) -> dict:
        url = f"{self.base_url}/images/generations"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = httpx.post(url, json=payload, headers=headers,
                              timeout=self.timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(f"图像生成请求失败: {str(e)[:200]}") from e
        if resp.status_code >= 400:
            raise RuntimeError(
                f"图像生成失败 HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as e:
            raise RuntimeError(f"图像生成响应不是 JSON: {resp.text[:200]}") from e

    # ------------------------------------------------------------
    # 生成接口
    # ------------------------------------------------------------

    def generate_b64(self, prompt: str, size: str = None, n: int = 1,
                     model: str = None) -> List[str]:
        """生成图片，返回 b64_json 列表（若提供方返回 url 则原样返回 url）。"""
        payload = {
            "model": model or self.model,
            "prompt": prompt,
            "n": max(1, min(int(n), 4)),
            "size": size or self.default_size,
            "response_format": "b64_json",
            # 官方公测期免费开放去水印（watermark=false）
            "watermark": self.watermark,
        }
        data = self._post(payload)
        items = data.get("data") or []
        out = []
        for item in items:
            if item.get("b64_json"):
                out.append(item["b64_json"])
            elif item.get("url"):
                out.append(item["url"])
        if not out:
            raise RuntimeError(f"图像生成响应异常: {str(data)[:300]}")
        return out

    def generate(self, prompt: str, size: str = None, n: int = 1,
                 save: bool = True, save_dir: str = None,
                 model: str = None) -> dict:
        """
        生成图片并保存到本地。

        提供方有两大类返回：
        - b64_json（如商汤）→ 解码落盘
        - url（如 Agnes）→ **自动下载**后落盘（下载失败则回退返回 URL，
          不因网络问题丢掉整次生成结果）

        Returns:
            {"images": [本地路径（优先）或 url], "model": ..., "size": ...,
             "saved": bool, "prompt": prompt}
        """
        items = self.generate_b64(prompt, size=size, n=n, model=model)
        images = []
        for i, item in enumerate(items):
            if item.startswith("http"):
                if save:
                    try:
                        images.append(self._download_image(item, i, save_dir))
                    except Exception:
                        images.append(item)   # 下载失败：回退给出 URL
                else:
                    images.append(item)
            elif save:
                images.append(self._save_image(item, i, save_dir))
        return {
            "images": images,
            "model": model or self.model,
            "size": size or self.default_size,
            "saved": save,
            "prompt": prompt,
        }

    def _download_image(self, url: str, index: int,
                        save_dir: str = None) -> str:
        """下载 URL 形式的图片，按魔数保存为 .png/.jpg，返回本地路径。

        为什么需要：不同提供方返回形式不同（商汤给 b64、Agnes 给 url）。
        若 url 形式不落盘，用户/agent 只能拿到一个链接，体验与 b64 不一致
        （生成的图不在本地、无法直接当文件用）。
        """
        resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
        if resp.status_code >= 400:
            raise RuntimeError(f"图片下载失败 HTTP {resp.status_code}")
        raw = resp.content
        if raw.startswith(PNG_MAGIC):
            ext = ".png"
        elif raw.startswith(JPEG_MAGIC):
            ext = ".jpg"
        else:
            raise RuntimeError("下载内容不是 PNG/JPEG（魔数不匹配）")

        directory = save_dir or self.save_dir
        os.makedirs(directory, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:17]
        path = os.path.join(directory, f"img-{ts}-{index}{ext}")
        with open(path, "wb") as f:
            f.write(raw)
        return path

    def _save_image(self, b64: str, index: int, save_dir: str = None) -> str:
        """解码 b64 并按魔数保存为 .png / .jpg 文件，返回路径。"""
        raw = base64.b64decode(b64)
        if raw.startswith(PNG_MAGIC):
            ext = ".png"
        elif raw.startswith(JPEG_MAGIC):
            ext = ".jpg"
        else:
            raise RuntimeError("返回的图片数据不是 PNG/JPEG（魔数不匹配）")

        directory = save_dir or self.save_dir
        os.makedirs(directory, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:17]
        path = os.path.join(directory, f"img-{ts}-{index}{ext}")
        with open(path, "wb") as f:
            f.write(raw)
        return path


def is_configured() -> bool:
    """图像生成是否已配置可用（enabled 且有 key）。"""
    cfg = IMAGE_GEN_CONFIG
    return bool(cfg.get("enabled") and cfg.get("api_key"))
