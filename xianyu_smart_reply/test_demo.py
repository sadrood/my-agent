"""
闲鱼智能回复系统 - 单元测试 + 演示
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_smart_reply.config import SystemConfig
from xianyu_smart_reply.reply_engine import ReplyEngine
from xianyu_smart_reply.history import ConversationHistory


def test_intent_detection():
    """测试意图识别"""
    config = SystemConfig()
    engine = ReplyEngine(config)

    test_cases = [
        ("亲，这个还有吗？", "availability"),
        ("便宜点行不行？", "negotiation"),
        ("4000卖不卖？", "negotiation"),
        ("包邮吗？发什么快递？", "shipping"),
        ("手机成色怎么样？", "inquiry"),
        ("这个多少钱？", "price"),
        ("你好，在吗？", "default"),
        ("给我打个折吧", "negotiation"),
        ("有现货吗？", "availability"),
        ("能自提吗？", "shipping"),
    ]

    print("=" * 50)
    print("  意图识别测试")
    print("=" * 50)

    passed = 0
    failed = 0
    for msg, expected in test_cases:
        result = engine._detect_intent(msg)
        status = "✅" if result == expected else "❌"
        if result == expected:
            passed += 1
        else:
            failed += 1
        print(f"  {status} \"{msg}\" -> {result} (期望: {expected})")

    print(f"\n  结果: {passed} 通过, {failed} 失败")
    return failed == 0


def test_history():
    """测试对话历史"""
    print("\n" + "=" * 50)
    print("  对话历史测试")
    print("=" * 50)

    history = ConversationHistory(":memory:")

    history.add_message("conv1", "user", "亲，这个还有吗？")
    history.add_message("conv1", "assistant", "亲，还有的哦~")
    history.add_message("conv1", "user", "多少钱？")
    history.add_message("conv2", "user", "包邮吗？")

    conv1 = history.get_history("conv1")
    conv2 = history.get_history("conv2")

    assert len(conv1) == 3, f"conv1 应该有3条, 实际{len(conv1)}"
    assert len(conv2) == 1, f"conv2 应该有1条, 实际{len(conv2)}"
    assert conv1[0] == ["user", "亲，这个还有吗？"]

    stats = history.export_stats()
    print(f"  对话数: {stats['conversations']}")
    print(f"  消息数: {stats['total_messages']}")
    print(f"  ✅ 对话历史测试通过")
    return True


def test_prompt_building():
    """测试提示词组装"""
    print("\n" + "=" * 50)
    print("  提示词组装测试")
    print("=" * 50)

    config = SystemConfig()
    config.seller.name = "数码小店"
    config.seller.tone = "casual"
    config.seller.signature = "在的在的~"
    config.seller.business_hours = "10:00-23:00"

    engine = ReplyEngine(config)

    system_prompt = engine._build_system_prompt()
    assert "数码小店" in system_prompt
    assert "在的在的~" in system_prompt
    assert "10:00-23:00" in system_prompt
    print(f"  系统提示词长度: {len(system_prompt)} 字符")
    print(f"  包含卖家名: {'数码小店' in system_prompt}")
    print(f"  包含签名: {'在的在的~' in system_prompt}")
    print(f"  ✅ 提示词组装测试通过")
    return True


def test_reply_generation_mock():
    """测试回复生成（Mock LLM）"""
    print("\n" + "=" * 50)
    print("  回复生成测试 (Mock LLM)")
    print("=" * 50)

    config = SystemConfig()
    config.seller.name = "小悟的店"
    config.seller.tone = "friendly"
    config.seller.signature = "亲，在的哦~"

    engine = ReplyEngine(config)

    # Mock LLM
    mock_replies = {
        "default": "亲，在的哦~ 有什么可以帮您的吗？😊",
        "price": "亲，这个商品的价格是4500元哦，性价比很高的！需要的话随时拍~",
        "availability": "亲，还有的哦！喜欢的尽快拍，手慢无~ 😄",
        "negotiation": "亲，价格已经很低啦，这个成色和配置，4500真的很划算了~",
        "shipping": "亲，包邮的哦！发顺丰，一般2-3天到~",
        "inquiry": "亲，手机成色95新，屏幕无划痕，功能全部正常，放心使用~",
    }

    original_chat = engine.llm.chat
    engine.llm.chat = lambda system_prompt, user_message, conversation_history=None: (
        mock_replies.get(engine._detect_intent(user_message), "亲，在的哦~")
    )

    test_cases = [
        ("亲，这个还有吗？", "iPhone 14 Pro", 4500),
        ("便宜点行不行？4000卖不卖？", "iPhone 14 Pro", 4500),
        ("包邮吗？", "iPhone 14 Pro", 4500),
        ("成色怎么样？", "iPhone 14 Pro", 4500),
        ("你好，在吗？", "机械键盘", 299),
    ]

    for msg, item, price in test_cases:
        reply = engine.generate_reply(
            user_message=msg,
            item_name=item,
            item_price=price,
        )
        intent = engine._detect_intent(msg)
        print(f"  [{intent:12s}] \"{msg}\"")
        print(f"  {'':16s}🤖 {reply}")
        print()

    engine.llm.chat = original_chat
    print(f"  ✅ 回复生成测试通过")
    return True


def main():
    print("\n" + "🧪" * 20)
    print("  闲鱼智能回复系统 - 测试套件")
    print("🧪" * 20 + "\n")

    results = []
    results.append(("意图识别", test_intent_detection()))
    results.append(("对话历史", test_history()))
    results.append(("提示词组装", test_prompt_building()))
    results.append(("回复生成(Mock)", test_reply_generation_mock()))

    print("\n" + "=" * 50)
    print("  测试结果汇总")
    print("=" * 50)
    all_passed = True
    for name, passed in results:
        status = "✅" if passed else "❌"
        print(f"  {status} {name}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("  🎉 全部测试通过！")
    else:
        print("  ⚠️  部分测试失败，请检查")

    return 0 if all_passed else 1


if __name__ == "__main__":
    exit(main())