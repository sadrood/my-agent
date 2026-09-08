"""
意图检测模块。
在指令进入规划/执行之前，识别中文网站名/应用名并翻译为 URL，
避免 LLM 将"打开快手"误解为浏览器"返回"操作。
"""
import re
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# 中文网站名 / 应用名 → URL 映射表
# ============================================================
# 注意：key 必须是全小写中文，匹配时输入也会转小写。
SITE_MAP: dict[str, str] = {
    # --- 短视频 / 直播 ---
    "快手":        "www.kuaishou.com",
    "抖音":        "www.douyin.com",
    "b站":         "www.bilibili.com",
    "bilibili":   "www.bilibili.com",
    "哔哩哔哩":     "www.bilibili.com",
    "虎牙":        "www.huya.com",
    "斗鱼":        "www.douyu.com",
    "yy直播":      "www.yy.com",
    "yy":          "www.yy.com",
    "小红书":       "www.xiaohongshu.com",

    # --- 搜索 / 门户 ---
    "百度":        "www.baidu.com",
    "搜狗":        "www.sogou.com",
    "360搜索":     "www.so.com",
    "360":         "www.so.com",
    "必应":        "www.bing.com",
    "bing":       "www.bing.com",
    "谷歌":        "www.google.com",
    "google":     "www.google.com",
    "新浪":        "www.sina.com.cn",
    "网易":        "www.163.com",
    "搜狐":        "www.sohu.com",
    "凤凰网":       "www.ifeng.com",
    "腾讯":        "www.qq.com",
    "腾讯网":       "www.qq.com",
    "qq":          "www.qq.com",
    "hao123":     "www.hao123.com",

    # --- 电商 ---
    "淘宝":        "www.taobao.com",
    "天猫":        "www.tmall.com",
    "京东":        "www.jd.com",
    "拼多多":       "www.pinduoduo.com",
    "苏宁":        "www.suning.com",
    "唯品会":       "www.vip.com",
    "闲鱼":        "www.goofish.com",
    "当当":        "www.dangdang.com",

    # --- 社交 / 社区 ---
    "微博":        "www.weibo.com",
    "知乎":        "www.zhihu.com",
    "豆瓣":        "www.douban.com",
    "贴吧":        "tieba.baidu.com",
    "百度贴吧":     "tieba.baidu.com",
    "天涯":        "bbs.tianya.cn",
    "虎扑":        "www.hupu.com",
    "csdn":        "www.csdn.net",

    # --- 视频 / 影视 ---
    "优酷":        "www.youku.com",
    "爱奇艺":       "www.iqiyi.com",
    "腾讯视频":     "v.qq.com",
    "芒果tv":      "www.mgtv.com",
    "搜狐视频":     "tv.sohu.com",

    # --- 音乐 ---
    "网易云音乐":   "music.163.com",
    "网易云":       "music.163.com",
    "qq音乐":      "y.qq.com",
    "酷狗":        "www.kugou.com",
    "酷我":        "www.kuwo.cn",

    # --- 技术 / 开发 ---
    "github":     "github.com",
    "gitee":      "gitee.com",
    "码云":        "gitee.com",
    "stackoverflow": "stackoverflow.com",
    "掘金":        "juejin.cn",
    "segmentfault": "segmentfault.com",
    "思否":        "segmentfault.com",
    "npm":         "www.npmjs.com",
    "pypi":        "pypi.org",

    # --- 资讯 ---
    "今日头条":     "www.toutiao.com",
    "头条":        "www.toutiao.com",
    "36氪":        "36kr.com",
    "虎嗅":        "www.huxiu.com",

    # --- 工具 ---
    "百度翻译":     "fanyi.baidu.com",
    "谷歌翻译":     "translate.google.com",
    "有道翻译":     "fanyi.youdao.com",
    "百度地图":     "map.baidu.com",
    "高德地图":     "www.amap.com",

    # --- AI ---
    "文心一言":     "yiyan.baidu.com",
    "kimi":        "kimi.moonshot.cn",
    "通义千问":     "tongyi.aliyun.com",
    "deepseek":    "chat.deepseek.com",
    "豆包":        "www.doubao.com",
    "chatgpt":     "chat.openai.com",

    # --- 办公 ---
    "飞书":        "www.feishu.cn",
    "钉钉":        "www.dingtalk.com",
    "企业微信":     "work.weixin.qq.com",
    "腾讯文档":     "docs.qq.com",
    "石墨文档":     "shimo.im",
}


# ============================================================
# 浏览器导航命令关键词（需要与"打开网站"区分）
# ============================================================
# 这些词当出现在用户指令中时，表示的是浏览器导航操作而非网站名
NAVIGATION_COMMANDS: set[str] = {
    "后退", "返回", "back", "前进", "forward",
    "刷新", "refresh", "reload", "关闭页面", "close",
    "新标签页", "new tab", "切换标签", "switch tab",
}


@dataclass
class IntentResult:
    """意图检测结果。"""
    original: str                          # 原始输入
    detected_sites: list[dict] = field(default_factory=list)  # [{name, url, position}]
    has_navigation_command: bool = False   # 是否包含导航命令
    enriched_goal: str = ""               # 注入 URL 信息后的增强目标


class IntentDetector:
    """用户意图检测器：将中文网站名翻译为 URL，并区分导航命令。"""

    def __init__(self, site_map: dict[str, str] | None = None):
        self.site_map = site_map or SITE_MAP
        # 按名称长度降序排列，优先匹配长名称（如"百度贴吧"优先于"百度"）
        self._sorted_keys = sorted(self.site_map.keys(), key=len, reverse=True)

    def detect(self, user_input: str) -> IntentResult:
        """
        检测用户输入中的网站意图。

        Args:
            user_input: 用户原始输入文本。

        Returns:
            IntentResult 包含检测到的网站列表和增强后的目标描述。
        """
        result = IntentResult(original=user_input)

        # 1. 检测导航命令
        result.has_navigation_command = self._has_nav_command(user_input)

        # 2. 检测已知网站名
        lowered = user_input.lower()
        matched_names = []  # 记录已经匹配的位置，避免重叠匹配

        for name in self._sorted_keys:
            name_lower = name.lower()
            idx = lowered.find(name_lower)
            if idx == -1:
                continue

            # 检查这个位置是否已经被一个更长的匹配覆盖
            overlap = False
            for prev_start, prev_end in matched_names:
                if idx < prev_end and idx + len(name_lower) > prev_start:
                    overlap = True
                    break
            if overlap:
                continue

            matched_names.append((idx, idx + len(name_lower)))
            result.detected_sites.append({
                "name": name,
                "url": self.site_map[name],
                "position": idx,
            })

        # 3. 生成增强目标
        result.enriched_goal = self._enrich(user_input, result)

        return result

    def _has_nav_command(self, text: str) -> bool:
        """检测文本是否包含浏览器导航命令。"""
        lowered = text.lower()
        return any(cmd.lower() in lowered for cmd in NAVIGATION_COMMANDS)

    def _enrich(self, user_input: str, result: IntentResult) -> str:
        """
        将检测到的网站 URL 注入到目标描述中，让 Planner/Executor 的 LLM 知道精确 URL。

        例如：
            "打开快手" → "打开快手（网址: www.kuaishou.com）。注意：需要使用浏览器 goto 命令导航到该网址。"
        """
        if not result.detected_sites:
            return user_input

        # 构建 URL 提示
        site_hints = []
        for s in result.detected_sites:
            site_hints.append(f"{s['name']} → {s['url']}")

        # 判断指令类型
        is_open_command = any(
            kw in user_input for kw in ("打开", "访问", "浏览", "去", "看", "逛")
        )

        if is_open_command and not result.has_navigation_command:
            # 明确是打开网站
            hint = (
                f"\n\n[系统提示] 以下中文名称对应的网址: {', '.join(site_hints)}。"
                f"请使用浏览器 goto 命令导航到对应网址，不要执行浏览器后退/返回等操作。"
            )
        elif result.has_navigation_command:
            # 同时包含网站名和导航命令，需要更精确的提示
            hint = (
                f"\n\n[系统提示] 以下中文名称对应的网址: {', '.join(site_hints)}。"
                f"当前用户指令同时包含导航操作词，请根据上下文判断是打开网站还是执行导航。"
            )
        else:
            # 只是提到了网站名
            hint = f"\n\n[系统提示] 以下中文名称对应的网址: {', '.join(site_hints)}。"

        return user_input + hint

    def resolve_url(self, name_or_url: str) -> Optional[str]:
        """
        将中文名或部分 URL 解析为完整 URL。

        Args:
            name_or_url: 中文名（如"快手"）或不完整 URL（如"kuaishou.com"）。

        Returns:
            完整域名，若无法解析则返回 None。
        """
        # 先查映射表
        lowered = name_or_url.strip().lower()
        if lowered in self.site_map:
            return self.site_map[lowered]

        # 如果不包含中文且看起来像域名，直接返回
        if not self._contains_chinese(name_or_url):
            return name_or_url.strip()

        # 包含中文但不在映射表中，尝试模糊匹配
        for name in self._sorted_keys:
            if name.lower() in lowered or lowered in name.lower():
                return self.site_map[name]

        return None

    @staticmethod
    def _contains_chinese(text: str) -> bool:
        """检测文本是否包含中文字符。"""
        return bool(re.search(r'[\u4e00-\u9fff]', text))


# ============================================================
# 全局单例
# ============================================================
_intent_detector: IntentDetector | None = None


def get_intent_detector() -> IntentDetector:
    global _intent_detector
    if _intent_detector is None:
        _intent_detector = IntentDetector()
    return _intent_detector
