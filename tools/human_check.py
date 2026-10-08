"""人机验证（CAPTCHA / Cloudflare 挑战 / 滑块）信号识别。

命中就该停下来交给人：agent 过不了验证码，硬试只会空转或触发风控。
"""
import re

URL_SIGNS = ("captcha", "challenge", "turnstile", "recaptcha", "hcaptcha",
             "checkpoint", "verify", "challenge_platform")
DOM_SELECTORS = ("iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
                 ".cf-turnstile", "#cf-challenge-running", "#challenge-form",
                 "[class*='captcha' i]", "[id*='captcha' i]",
                 "[class*='turnstile' i]", "iframe[title*='challenge' i]")
TEXT_SIGNS = ("验证码", "人机验证", "安全验证", "滑动验证", "请完成验证",
              "verify you are human", "are you a human", "confirm you are human",
              "i'm not a robot", "unusual traffic")

HUMAN_HINT = ("【需要人工完成人机验证】agent 无法代替你通过验证码/安全挑战。"
              "请在浏览器窗口（桌面端内嵌面板或可见浏览器）里完成验证，"
              "然后让 agent 继续（browser 的 humancheck 可复查是否已通过）。")


def hint_for_url(url: str) -> str:
    """URL 层面就能看出是验证页时给提示（便宜，不需要浏览器往返）。"""
    text = str(url or "").lower()
    return HUMAN_HINT if any(sign in text for sign in URL_SIGNS) else ""


def detect(url: str = "", dom_hits=None, text: str = "") -> list:
    """返回命中的信号描述列表（空列表 = 没发现人机验证）。"""
    found = []
    reason = hint_for_url(url)
    if reason:
        found.append(f"URL 含验证页特征: {url}")
    for selector in dom_hits or []:
        found.append(f"页面存在验证控件: {selector}")
    lowered = str(text or "").lower()
    for sign in TEXT_SIGNS:
        if sign in lowered:
            found.append(f"页面文字提示: {sign}")
            break
    return found


def verdict(url: str = "", dom_hits=None, text: str = "") -> dict:
    """{"needs_human": bool, "reasons": [...], "hint": str}"""
    reasons = detect(url, dom_hits, text)
    return {"needs_human": bool(reasons), "reasons": reasons,
            "hint": HUMAN_HINT if reasons else ""}


def url_from_status(text: str) -> str:
    """从桥返回的状态文本里抠出 URL（内嵌桥只回文本，没有结构化字段）。"""
    match = re.search(r"URL[:：]\s*(\S+)", str(text or ""))
    return match.group(1) if match else ""
