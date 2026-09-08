"""
闲鱼智能回复系统 - 配置模块
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BrowserConfig:
    """浏览器配置"""
    headless: bool = False                    # 是否无头模式（调试建议 False）
    user_data_dir: str = ""                   # Chrome 用户数据目录（保留登录态）
    viewport_width: int = 1280
    viewport_height: int = 800
    page_load_timeout: int = 30000            # 页面加载超时(ms)
    action_delay: float = 1.5                 # 操作间隔(秒)，防风控


@dataclass
class MonitorConfig:
    """消息监听配置"""
    gofish_url: str = "https://www.goofish.com"
    message_url: str = "https://www.goofish.com/msg"
    poll_interval: float = 5.0                # 轮询间隔(秒)
    max_poll_cycles: int = 0                  # 最大轮询次数，0=无限
    last_message_id: str = ""                 # 上次已处理的消息ID


@dataclass
class LLMConfig:
    """大模型配置"""
    provider: str = "openai"                  # openai / deepseek / custom
    api_base: str = ""                        # 自定义 API 地址
    api_key: str = ""                         # API Key
    model: str = "gpt-4o-mini"                # 推荐：gpt-4o-mini（便宜快）或 deepseek-v3
    max_tokens: int = 500
    temperature: float = 0.7
    timeout: int = 30


@dataclass
class SellerProfile:
    """卖家人设配置 - 决定回复风格"""
    name: str = "店主"
    tone: str = "friendly"                    # friendly / professional / casual
    signature: str = "亲，在的哦~"            # 常用签名/结尾
    business_hours: str = "9:00-22:00"        # 营业时间
    auto_reply_enabled: bool = True
    keywords: dict = field(default_factory=lambda: {
        "price": ["价格", "多少钱", "什么价", "便宜", "砍价", "最低", "底价"],
        "availability": ["还有吗", "有货吗", "库存", "现货", "还有货", "没卖"],
        "shipping": ["发货", "快递", "包邮", "运费", "物流", "寄"],
        "negotiation": ["便宜点", "打折", "优惠", "让让", "少点"],
        "inquiry": ["怎么样", "质量", "新旧", "成色", "瑕疵", "实拍"],
    })


@dataclass
class SystemConfig:
    """系统总配置"""
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    seller: SellerProfile = field(default_factory=SellerProfile)
    log_file: str = "smart_reply.log"
    reply_history_file: str = "reply_history.json"


def load_config_from_env() -> SystemConfig:
    """从环境变量加载配置（覆盖默认值）"""
    config = SystemConfig()

    # LLM 配置
    if api_key := os.getenv("XIANYU_LLM_API_KEY"):
        config.llm.api_key = api_key
    if api_base := os.getenv("XIANYU_LLM_API_BASE"):
        config.llm.api_base = api_base
    if model := os.getenv("XIANYU_LLM_MODEL"):
        config.llm.model = model
    if provider := os.getenv("XIANYU_LLM_PROVIDER"):
        config.llm.provider = provider

    # 浏览器配置
    if headless := os.getenv("XIANYU_HEADLESS", "false").lower() == "true":
        config.browser.headless = headless
    if user_dir := os.getenv("XIANYU_BROWSER_USER_DIR"):
        config.browser.user_data_dir = user_dir

    # 监听配置
    if interval := os.getenv("XIANYU_POLL_INTERVAL"):
        config.monitor.poll_interval = float(interval)

    return config