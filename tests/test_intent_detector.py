"""意图识别回归（此前**没有任何测试文件**）。

实测故障（2026-09-22 审计）：
- `detect` 对站点名做裸子串匹配，`"帮我算一下 3600 元是多少美金"` 因为 "3600" 里
  含 "360" 被判成命中 360、`"分析一下 ayy 这个缩写"` 命中 yy；
- `_has_nav_command` 同样裸子串，`back` 命中 `backup`、`close` 命中 `closed`，
  于是"打开淘宝，顺便看下 backup 目录"被判成含导航命令；
- `resolve_url` 允许反向包含（查询是站名的一部分），`goto 微信` 解析成
  work.weixin.qq.com（因为有个键叫"企业微信"）、`goto 书` 解析成小红书
  —— 这些结果会**直接进 browser goto**，用户被带到完全不相干的站点。
"""
import pytest

from tools.intent_detector import get_intent_detector


@pytest.fixture(scope="module")
def detector():
    return get_intent_detector()


class TestShortSiteKeyBoundary:
    """短的纯 ASCII 站名必须词边界匹配，不能裸子串。"""

    @pytest.mark.parametrize("text", [
        "帮我算一下 3600 元是多少美金",
        "分析一下 ayy 这个缩写",
        "把 360000 转成十六进制",
    ])
    def test_numbers_and_suffixes_do_not_look_like_sites(self, detector, text):
        names = [s["name"] for s in detector.detect(text).detected_sites]
        assert "360" not in names and "yy" not in names, f"{text} 误判成 {names}"

    @pytest.mark.parametrize("text,expected", [
        ("上 360 查一下", "360"),
        ("去 b站 看视频", "b站"),
    ])
    def test_standalone_short_key_still_matches(self, detector, text, expected):
        names = [s["name"] for s in detector.detect(text).detected_sites]
        assert expected in names, f"{text} 应该命中 {expected}，实际 {names}"


class TestNavCommandBoundary:
    """导航命令要词边界匹配，`backup` 不该命中 `back`。"""

    @pytest.mark.parametrize("text", [
        "打开淘宝，顺便看下 backup 目录",
        "检查一下 feedback 目录",
        "把 background 设成白色",
    ])
    def test_words_containing_nav_commands(self, detector, text):
        assert detector._has_nav_command(text) is False, f"{text} 被误判成含导航命令"

    @pytest.mark.parametrize("text", ["后退一下", "刷新页面", "switch tab"])
    def test_real_nav_command_still_detected(self, detector, text):
        assert detector._has_nav_command(text) is True


class TestResolveUrlFuzzyMatch:
    """模糊匹配只能"站名出现在查询里"，不能反过来。"""

    @pytest.mark.parametrize("query", ["微信", "云盘", "书"])
    def test_partial_name_not_guessed(self, detector, query):
        """宁可返回 None（上层报"未识别"），也不要带到不相干的站点。"""
        assert detector.resolve_url(query) is None, f"{query} 被猜成了别的站"

    @pytest.mark.parametrize("query,expected", [
        ("淘宝", "www.taobao.com"),
        ("b站", "www.bilibili.com"),
    ])
    def test_exact_keys_still_resolve(self, detector, query, expected):
        assert detector.resolve_url(query) == expected

    def test_real_domain_passes_through(self, detector):
        assert detector.resolve_url("example.com") == "example.com"
