"""
闲鱼智能回复系统 - 主入口
"""
import argparse
import json
import logging
import sys
from datetime import datetime

from .config import SystemConfig, load_config_from_env
from .reply_engine import ReplyEngine
from .history import ConversationHistory
from .browser_monitor import BrowserMonitor, Message


def setup_logging(log_file: str = "smart_reply.log"):
    """配置日志"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def create_demo_config() -> SystemConfig:
    """创建演示用配置"""
    config = SystemConfig()
    config.seller.name = "小悟的店"
    config.seller.tone = "friendly"
    config.seller.signature = "亲，在的哦~"
    config.seller.business_hours = "9:00-22:00"
    config.llm.model = "gpt-4o-mini"
    config.browser.headless = False
    config.monitor.poll_interval = 5.0
    return config


def run_demo(config: SystemConfig):
    """
    演示模式：不调用真实浏览器，直接测试回复引擎
    """
    setup_logging()
    logger = logging.getLogger("demo")

    engine = ReplyEngine(config)
    history = ConversationHistory(":memory:")

    test_cases = [
        {"msg": "亲，这个还有吗？", "item": "iPhone 14 Pro 256G", "price": 4500},
        {"msg": "便宜点行不行？4000卖不卖？", "item": "iPhone 14 Pro 256G", "price": 4500, "offer": 4000},
        {"msg": "包邮吗？发什么快递？", "item": "iPhone 14 Pro 256G", "price": 4500},
        {"msg": "手机成色怎么样？有划痕吗？", "item": "iPhone 14 Pro 256G", "price": 4500},
        {"msg": "你好，在吗？", "item": "机械键盘 Cherry轴", "price": 299},
    ]

    logger.info("=" * 60)
    logger.info("  闲鱼智能回复系统 - 演示模式")
    logger.info("=" * 60)
    logger.info(f"  模型: {config.llm.model}")
    logger.info(f"  人设: {config.seller.name} ({config.seller.tone})")
    logger.info(f"  签名: {config.seller.signature}")
    logger.info("=" * 60)

    for i, case in enumerate(test_cases, 1):
        logger.info(f"\n{'─' * 40}")
        logger.info(f"场景 {i}: {case['msg']}")
        logger.info(f"商品: {case['item']} ({case['price']}元)")

        try:
            reply = engine.generate_reply(
                user_message=case["msg"],
                item_name=case["item"],
                item_price=case["price"],
                buyer_offer=case.get("offer"),
            )
            logger.info(f"🤖 智能回复: {reply}")
        except Exception as e:
            logger.error(f"❌ 生成失败: {e}")

    logger.info(f"\n{'=' * 60}")
    logger.info("  演示完成！")
    logger.info("=" * 60)


def run_monitor(config: SystemConfig):
    """
    监听模式：启动浏览器监听闲鱼消息
    """
    setup_logging(config.log_file)
    logger = logging.getLogger("monitor")

    engine = ReplyEngine(config)
    history = ConversationHistory(config.reply_history_file)

    def on_new_message(msg: Message) -> str:
        """收到新消息时的回调"""
        logger.info(f"📨 收到消息: {msg.sender}: {msg.content}")

        # 记录买家消息
        history.add_message(msg.conversation_id or msg.id, "user", msg.content)

        # 获取对话历史
        conv_history = history.get_history(msg.conversation_id or msg.id)

        # 生成回复
        reply = engine.generate_reply(
            user_message=msg.content,
            item_name=msg.item_name,
            conversation_history=conv_history,
        )

        # 记录回复
        history.add_message(msg.conversation_id or msg.id, "assistant", reply)

        return reply

    monitor = BrowserMonitor(config)
    logger.info("🚀 闲鱼智能回复系统启动")
    logger.info(f"   轮询间隔: {config.monitor.poll_interval}秒")
    logger.info(f"   模型: {config.llm.model}")
    logger.info("   按 Ctrl+C 退出")

    monitor.run_loop(on_message)


def main():
    parser = argparse.ArgumentParser(
        description="闲鱼智能回复系统 - 大模型加持的自动回复工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 演示模式（测试回复引擎，不需要登录闲鱼）
  python -m xianyu_smart_reply --demo

  # 监听模式（需要配置 API Key 和浏览器登录态）
  python -m xianyu_smart_reply --monitor

  # 指定模型
  python -m xianyu_smart_reply --demo --model deepseek-v3

  # 自定义卖家人设
  python -m xianyu_smart_reply --demo --seller-name "数码小店" --tone casual
        """,
    )

    parser.add_argument("--demo", action="store_true", help="演示模式")
    parser.add_argument("--monitor", action="store_true", help="监听模式")
    parser.add_argument("--model", type=str, help="指定 LLM 模型")
    parser.add_argument("--api-key", type=str, help="API Key")
    parser.add_argument("--api-base", type=str, help="API Base URL")
    parser.add_argument("--seller-name", type=str, help="卖家名称")
    parser.add_argument("--tone", type=str, choices=["friendly", "professional", "casual"], help="回复风格")
    parser.add_argument("--signature", type=str, help="签名")
    parser.add_argument("--interval", type=float, help="轮询间隔(秒)")

    args = parser.parse_args()

    config = load_config_from_env()

    # CLI 参数覆盖
    if args.model:
        config.llm.model = args.model
    if args.api_key:
        config.llm.api_key = args.api_key
    if args.api_base:
        config.llm.api_base = args.api_base
    if args.seller_name:
        config.seller.name = args.seller_name
    if args.tone:
        config.seller.tone = args.tone
    if args.signature:
        config.seller.signature = args.signature
    if args.interval:
        config.monitor.poll_interval = args.interval

    if args.demo:
        run_demo(config)
    elif args.monitor:
        run_monitor(config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()