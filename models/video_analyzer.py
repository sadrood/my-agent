"""
视频帧分析模块。对截图帧序列进行时序分析：页面变化检测、异常检测、操作序列验证。
"""
from typing import Optional, List, Dict, Any
from models.vision import VisionModel


class VideoFrameAnalyzer:
    """视频帧序列分析器。支持帧对比、序列验证、异常检测。"""

    def __init__(self, vision_model: VisionModel = None):
        self.vision_model = vision_model
        if self.vision_model is None:
            try:
                self.vision_model = VisionModel()
            except Exception:
                self.vision_model = None

    def compare_frames(self, before_base64: str, after_base64: str,
                       expected_action: str = "") -> str:
        """对比操作前后两张截图的变化。

        修过的坑：此前只把 before 图发给模型，却让模型"对比这两张截图"——
        after 被静默丢弃，模型只能凭空编造变化（审计发现）。现在两张都发。
        """
        if self.vision_model is None:
            return "视觉模型不可用。"
        images = [i for i in (before_base64, after_base64) if i]
        if len(images) < 2:
            # 缺图就别装作能对比：只描述现有这一张，并如实说明缺了什么
            missing = "操作前" if not before_base64 else "操作后"
            question = (f"缺少{missing}截图，无法做前后对比。"
                        f"请只描述当前这张图的内容。预期操作: {expected_action or '未知'}")
            try:
                return self.vision_model.analyze(images[0] if images else "",
                                                 question, max_tokens=600)
            except Exception as e:
                return f"帧对比失败: {str(e)}"
        question = (
            f"请对比这两张网页截图（第 1 张=操作前，第 2 张=操作后）。"
            f"预期操作: {expected_action or '未知'}\n"
            "分析：1.页面是否变化？ 2.变化是否符合预期？ 3.是否有异常？ 4.操作是否成功？"
        )
        try:
            return self.vision_model.analyze(images, question, max_tokens=1000)
        except Exception as e:
            return f"帧对比失败: {str(e)}"

    def detect_anomaly(self, screenshot_base64: str, context: str = "") -> dict:
        """检测页面异常状态（弹窗、错误、加载失败等）。"""
        if self.vision_model is None:
            # 检测**失败**必须能和"检测过、确实没问题"区分开：两者 has_anomaly 都是
            # False，而调用方只看这个字段 —— 于是视觉模型挂掉时，agent 会在完全没做过
            # 页面检查的情况下继续操作，用户和 rollout 里都看不到任何痕迹
            # （2026-09-22 审计）。加一个显式标记让上层能说出来。
            return {"has_anomaly": False, "type": "detect_failed",
                    "description": "视觉模型不可用", "detect_failed": True}
        question = (
            "请检查这个网页截图是否存在异常：\n"
            "1. 是否有弹窗（alert/confirm/prompt/登录弹窗/广告弹窗）？\n"
            "2. 是否有错误信息（404/500/连接失败等）？\n"
            "3. 页面是否加载完成？\n"
            "4. 是否有验证码/CAPTCHA？\n"
            f"当前上下文: {context or '无'}\n"
            "用 JSON 回答: {\"has_anomaly\": true/false, \"type\": \"弹窗类型\", \"description\": \"描述\"}"
        )
        try:
            result = self.vision_model.analyze(screenshot_base64, question, max_tokens=600)
            import re, json
            m = re.search(r'\{[\s\S]*\}', result)
            if m:
                return json.loads(m.group())
            return {"has_anomaly": "正常" not in result, "type": "", "description": result[:200]}
        except Exception as e:
            return {"has_anomaly": False, "type": "detect_failed",
                    "description": str(e), "detect_failed": True}

    def validate_progress(self, screenshot_base64: str, expected_state: str) -> str:
        """验证当前页面是否符合预期进度。"""
        if self.vision_model is None:
            return "视觉模型不可用。"
        question = f"当前步骤预期状态: {expected_state}\n请判断当前页面是否符合预期，并说明下一步应该做什么。"
        try:
            return self.vision_model.analyze(screenshot_base64, question, max_tokens=800)
        except Exception as e:
            return f"进度验证失败: {str(e)}"
