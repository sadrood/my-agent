"""
运行时统计（状态行）测试。
"""
from agent.metrics import RunMetrics, humanize_seconds, humanize_tokens


class TestHumanize:
    def test_seconds(self):
        assert humanize_seconds(0) == "0s"
        assert humanize_seconds(42.3) == "42.3s"
        assert humanize_seconds(83) == "1m23s"
        assert humanize_seconds(3900) == "1h05m"

    def test_tokens(self):
        assert humanize_tokens(0) == "0"
        assert humanize_tokens(312) == "312"
        assert humanize_tokens(1200) == "1.2K"
        assert humanize_tokens(64_500_000) == "64.5M"


class TestRunMetrics:
    def test_accumulation(self):
        m = RunMetrics()
        m.turns = 2
        m.steps = 3
        m.tool_seconds = 1.5
        m.add_llm_call(elapsed=10.0, first_token=2.0,
                       input_tokens=1000, output_tokens=200, cached_tokens=600)
        m.add_llm_call(elapsed=5.0, first_token=4.0,
                       input_tokens=500, output_tokens=100, cached_tokens=400)

        assert m.llm_seconds == 15.0
        assert m.input_tokens == 1500
        assert m.output_tokens == 300
        assert m.cached_tokens == 1000
        assert m.first_token_avg == 3.0
        assert m.tokens_per_sec == 300 / 15.0
        assert m.cache_hit_rate == 1000 / 1500
        assert m.has_data() is True

    def test_has_data_empty(self):
        assert RunMetrics().has_data() is False

    def test_render_line(self):
        m = RunMetrics()
        m.turns = 4
        m.steps = 6
        m.tool_seconds = 0.8
        m.add_llm_call(elapsed=42.3, first_token=2.1,
                       input_tokens=1200, output_tokens=312, cached_tokens=756)
        line = m.render_line()
        assert "4 轮" in line
        assert "6 步" in line
        assert "LLM 42.3s" in line
        assert "工具 0.8s" in line
        assert "首 token 平均 2.1s" in line
        assert "缓存命中 63%" in line
        assert "输入 1.2K" in line
        assert "输出 312" in line

    def test_render_line_no_tokens(self):
        m = RunMetrics()
        m.turns = 1
        m.add_llm_call(elapsed=1.0)
        line = m.render_line()
        assert "1 轮" in line
        assert "首 token" not in line      # 无首 token 数据不显示

    def test_render_line_with_modified_files(self):
        m = RunMetrics()
        m.turns = 1
        m.add_file_change("tools/file.py")
        m.add_file_change("config.py")
        m.add_file_change("config.py")
        line = m.render_line()
        assert "修改 " in line
        assert "tools/file.py" in line
        assert "config.py ×2" in line   # 排序后 config.py 在前
