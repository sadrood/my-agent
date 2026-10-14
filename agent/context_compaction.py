"""上下文压缩的公共纯逻辑：开头的 system 保留、保留区工具闭环修复。

executor（单循环）与 rollout（计划模式轨迹）各有一套压缩，细节曾在两处漂移；
这里只抽出**两边完全同义**的两段，总结/指针策略仍各自保留。
"""


def leading_system_messages(messages: list) -> list:
    """开头的 system 消息（连续的一串）：压缩时必须原样保留，不能被压进摘要。"""
    head = []
    for m in messages:
        if m.get("role") == "system":
            head.append(m)
        else:
            break
    return head


def repair_tool_boundary(messages: list, keep: int) -> list:
    """从尾部取 keep 条作为保留区，并修掉"孤立的 tool 消息"。

    单循环里每轮追加 assistant(tool_calls) + N 条 tool；直接切 messages[-keep:] 有
    N/(N+2) 的概率正好切在中间——孤立的 tool 消息会让下一轮请求 400。
    修复：以 tool 开头时，能向前补回配对的 assistant(tool_calls) 就补（保留区变长一条），
    补不回（前面不是配对消息）就裁掉这条孤立 tool。返回修好的保留区。
    """
    recent = list(messages[-keep:]) if keep > 0 else []
    while recent and recent[0].get("role") == "tool":
        start = len(messages) - len(recent) - 1
        if start < 0:
            break
        prev = messages[start]
        if prev.get("role") == "assistant" and prev.get("tool_calls"):
            recent = [prev] + recent        # 连同它的 assistant(tool_calls) 一起留下，保持对仗
            break
        recent = recent[1:]                 # 前面不是配对消息：这条孤立 tool 只能裁掉
    return recent
