"""dashboard 前端：内联脚本必须能通过语法检查（一次单引号嵌套曾让整页脚本全废）。"""
import re
import shutil
import subprocess

import pytest

INDEX = "dashboard/static/index.html"


def _inline_script() -> str:
    html = open(INDEX, encoding="utf-8").read()
    blocks = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert blocks, "index.html 里没有内联脚本"
    return "\n".join(blocks)


class TestInlineScript:
    @pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 做语法检查")
    def test_script_parses(self, tmp_path):
        path = tmp_path / "inline.js"
        path.write_text(_inline_script(), encoding="utf-8")
        proc = subprocess.run([shutil.which("node"), "--check", str(path)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"内联脚本语法错误:\n{proc.stderr[:400]}"

    def test_no_single_quote_nesting_in_generated_html(self):
        """生成 HTML 的 JS 字符串里不能再出现裸的单引号嵌套（会截断字符串）。"""
        bad = re.findall(r"\+ '.*?classList\.toggle\('([a-z]+)'\)", _inline_script())
        assert bad == [], f"这些 onclick 的引号没转义: {bad}"


class TestLivePanelMarkup:
    def test_panel_and_button_present(self):
        html = open(INDEX, encoding="utf-8").read()
        for token in ('id="liveToggle"', 'id="livePanel"', 'id="liveImg"',
                      "/api/browser-live"):
            assert token in html, f"缺少 {token}"
