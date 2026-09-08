"""
图像生成模块（SenseNova Token Plan 文生图）。

调用商汤 SenseNova 的 OpenAI 兼容文生图端点（实测契约）：
    POST {base_url}/images/generations
    {"model": "sensenova-u1.5-lite", "prompt": "...", "n": 1,
     "size": "1024x1024", "response_format": "b64_json"}
响应:
    {"created": ..., "data": [{"b64_json": "..."} | {"url": "..."}]}

本模块负责请求、解码 b64 并保存为本地 PNG（按魔数识别 PNG/JPEG）。
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

        Returns:
            {"images": [路径或 url], "model": ..., "size": ...,
             "saved": bool, "prompt": prompt}
        """
        items = self.generate_b64(prompt, size=size, n=n, model=model)
        images = []
        for i, item in enumerate(items):
            if item.startswith("http"):
                images.append(item)   # 提供方直接给 url：原样返回
            elif save:
                images.append(self._save_image(item, i, save_dir))
        return {
            "images": images,
            "model": model or self.model,
            "size": size or self.default_size,
            "saved": save,
            "prompt": prompt,
        }

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
