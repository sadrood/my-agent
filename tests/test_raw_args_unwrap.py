"""工具参数 _raw 解包测试：模型/供应商把参数再包一层 JSON 字符串时，
必须解出真正的命名参数（否则工具收到 {"_raw": ...} 拿不到参数）。"""
import json

from models.llm import unwrap_raw_arguments


class TestUnwrapRaw:
    def test_single_nested(self):
        assert unwrap_raw_arguments({"_raw": '{"code": "print(1)"}'}) == {"code": "print(1)"}

    def test_double_nested(self):
        inner = json.dumps({"code": "print(1)"})
        once = json.dumps({"_raw": inner})
        assert unwrap_raw_arguments({"_raw": once}) == {"code": "print(1)"}

    def test_deep_nested(self):
        cur = {"code": "print(1)"}
        for _ in range(4):
            cur = {"_raw": json.dumps(cur)}
        assert unwrap_raw_arguments(cur) == {"code": "print(1)"}

    def test_depth_bounded(self):
        """超过 max_depth 不再硬解（防死循环）。"""
        cur = {"code": "x"}
        for _ in range(8):
            cur = {"_raw": json.dumps(cur)}
        result = unwrap_raw_arguments(cur)
        assert "_raw" in result

    def test_non_dict_value_wrapped_as_input(self):
        assert unwrap_raw_arguments({"_raw": "123"}) == {"input": 123}
        assert unwrap_raw_arguments({"_raw": '"hi"'}) == {"input": "hi"}

    def test_null_inner_becomes_empty(self):
        assert unwrap_raw_arguments({"_raw": "null"}) == {}

    def test_unparseable_stays_unchanged(self):
        """坏 JSON/截断解不开：原样返回，交给"参数解析失败"拦截。"""
        args = {"_raw": '{"code": "print(1"'}
        assert unwrap_raw_arguments(args) is args

    def test_empty_raw_stays_unchanged(self):
        args = {"_raw": ""}
        assert unwrap_raw_arguments(args) is args

    def test_no_raw_key_untouched(self):
        args = {"code": "x"}
        assert unwrap_raw_arguments(args) is args
