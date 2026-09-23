"""
运行时统计（状态行）测试。
"""
import pytest

from agent.metrics import (RunMetrics, humanize_duration_cn,
                           humanize_seconds, humanize_tokens)


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
        # 速率分母是**纯解码时长**（LLM 总耗时 − 首 token 等待），
        # 旧实现用 llm_seconds 当分母，会把首 token 延迟也算成生成时间，
        # 系统性低估速率（此处 15s 里有 6s 是等首 token）
        assert m.decode_seconds == 15.0 - 6.0
        assert m.tokens_per_sec == 300 / 9.0
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


class TestWallClockElapsed:
    """「这次一共跑了多久」——用户感知的是墙钟，不是分项之和。

    背景（2026-09-23）：统计行原先只有 `LLM 42.3s · 工具 0.8s` 这类**分项之和**，
    没有总时间。而分项与墙钟的**差额**才是信息量所在：那部分是"看不见的等待"
    （限流退避 / 等人工审批 / 快照 git 操作 / 压缩 / 浏览器启动 / MCP 连接）。
    实测一次简单任务：工具 2.0s，但墙钟 5s。
    """

    def test_elapsed_is_wall_clock(self):
        m = RunMetrics()
        m.started -= 183
        assert 182 <= m.elapsed <= 185, f"elapsed={m.elapsed}"

    def test_elapsed_zero_when_not_started(self):
        """没记录起点时给 0，不能算成"从 epoch 到现在"。"""
        m = RunMetrics()
        m.started = 0
        assert m.elapsed == 0.0

    def test_elapsed_covers_more_than_parts(self):
        """墙钟 >= LLM+工具之和（分项有并行时会互相重叠，所以只断言不矛盾）。"""
        m = RunMetrics()
        m.started -= 10
        m.add_llm_call(elapsed=3.0)
        m.tool_seconds = 2.0
        assert m.elapsed >= 9


class TestHumanizeDurationCn:
    """中文时长格式：3分03秒（秒补零），不是 3m3s。"""

    @pytest.mark.parametrize("seconds,expected", [
        (0, "0秒"), (3, "3秒"), (12, "12秒"), (59, "59秒"),
        (60, "1分00秒"), (63, "1分03秒"), (183, "3分03秒"), (599, "9分59秒"),
        (3600, "1时00分00秒"), (3661, "1时01分01秒"),
        (-5, "0秒"), (None, "0秒"),          # 异常输入不炸
    ])
    def test_format(self, seconds, expected):
        assert humanize_duration_cn(seconds) == expected

    def test_truncates_instead_of_rounding_up(self):
        """3分59.8秒 不该显示成 4分00秒（会让人以为多跑了一分钟）。"""
        assert humanize_duration_cn(239.8) == "3分59秒"
