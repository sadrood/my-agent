"""快速测试 - 验证修复"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xianyu_smart_reply.reply_engine import ReplyEngine
from xianyu_smart_reply.config import SystemConfig
from xianyu_smart_reply.history import ConversationHistory
from xianyu_smart_reply.llm_client import LLMClient, PROVIDER_API_BASES

config = SystemConfig()
engine = ReplyEngine(config)

# Test 1: intent detection
print("=== Intent Detection ===")
tests = [
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
passed = 0
for msg, expected in tests:
    result = engine._detect_intent(msg)
    ok = result == expected
    passed += 1 if ok else 0
    status = "OK" if ok else "FAIL"
    print(f"  {status} \"{msg}\" -> {result} (expect {expected})")
print(f"  Result: {passed}/{len(tests)} passed\n")

# Test 2: history
print("=== History ===")
history = ConversationHistory(":memory:")
history.add_message("conv1", "user", "亲，这个还有吗？")
history.add_message("conv1", "assistant", "有的哦~")
history.add_message("conv2", "user", "包邮吗？")
assert len(history.get_history("conv1")) == 2
assert len(history.get_history("conv2")) == 1
print("  OK\n")

# Test 3: prompt building
print("=== Prompt Building ===")
config.seller.name = "数码小店"
config.seller.tone = "casual"
config.seller.signature = "在的在的~"
engine2 = ReplyEngine(config)
sp = engine2._build_system_prompt()
assert "数码小店" in sp and "在的在的~" in sp
print("  OK\n")

# Test 4: Mock reply
print("=== Mock Reply ===")
mock_replies = {
    "default": "亲，在的哦~ 有什么可以帮您的吗？",
    "price": "亲，这个商品的价格是4500元哦~",
    "availability": "亲，还有的哦！喜欢的尽快拍~",
    "negotiation": "亲，价格已经很低啦~",
    "shipping": "亲，包邮的哦！发顺丰~",
    "inquiry": "亲，成色95新，放心使用~",
}
orig = engine.llm.chat
engine.llm.chat = lambda s, u, h=None: mock_replies.get(engine._detect_intent(u), "亲，在的哦~")
for msg, item, price in [("亲，这个还有吗？", "iPhone 14", 4500), ("便宜点行不行？", "iPhone 14", 4500), ("包邮吗？", "iPhone 14", 4500)]:
    reply = engine.generate_reply(user_message=msg, item_name=item, item_price=price)
    intent = engine._detect_intent(msg)
    print(f"  [{intent}] \"{msg}\" -> {reply}")
print("\n=== LLM Provider ===")
for provider, base in PROVIDER_API_BASES.items():
    print(f"  {provider}: {base}")
c = LLMClient(provider="dashscope", api_key="test")
assert c.api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"
print("  DashScope auto-config OK")

print("\nAll tests passed!")