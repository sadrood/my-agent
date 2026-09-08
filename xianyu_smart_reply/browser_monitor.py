"""
闲鱼智能回复系统 - 浏览器消息监听器
通过浏览器自动化监听闲鱼网页版消息
"""
import json
import logging
import time
from datetime import datetime
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class Message:
    """消息数据结构"""
    def __init__(self, message_id: str, sender: str, content: str,
                 timestamp: str, item_name: str = "",
                 conversation_id: str = ""):
        self.id = message_id
        self.sender = sender
        self.content = content
        self.timestamp = timestamp
        self.item_name = item_name
        self.conversation_id = conversation_id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sender": self.sender,
            "content": self.content,
            "timestamp": self.timestamp,
            "item_name": self.item_name,
            "conversation_id": self.conversation_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        return cls(**data)

    def __repr__(self):
        return f"Message(id={self.id}, sender={self.sender}, content={self.content[:30]}...)"


class BrowserMonitor:
    """
    浏览器消息监听器

    使用 Playwright 控制浏览器，定期轮询闲鱼消息页面
    检测新消息并触发回调
    """

    def __init__(self, config):
        self.config = config
        self.playwright = None
        self.browser = None
        self.page = None
        self.processed_ids: set = set()
        self.on_new_message: Optional[Callable] = None
        self._running = False

    def start(self) -> None:
        """启动浏览器"""
        from playwright.sync_api import sync_playwright

        self.playwright = sync_playwright().start()

        launch_args = {
            "headless": self.config.browser.headless,
            "args": [
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
            ],
        }

        if self.config.browser.user_data_dir:
            launch_args["executable_path"] = None
            launch_args["channel"] = "chrome"
            # 使用用户数据目录保留登录态
            launch_args["args"].append(
                f"--user-data-dir={self.config.browser.user_data_dir}"
            )

        self.browser = self.playwright.chromium.launch(**launch_args)
        self.page = self.browser.new_page(
            viewport={
                "width": self.config.browser.viewport_width,
                "height": self.config.browser.viewport_height,
            }
        )

        logger.info("浏览器已启动")

    def login(self) -> None:
        """
        登录闲鱼
        注意：闲鱼网页版需要扫码登录，这里只导航到登录页
        实际使用时需要人工扫码或已有登录态
        """
        logger.info("正在导航到闲鱼...")
        self.page.goto(self.config.monitor.gofish_url,
                       wait_until="domcontentloaded",
                       timeout=self.config.browser.page_load_timeout)
        time.sleep(3)

        # 检查是否已登录
        if self._is_logged_in():
            logger.info("已检测到登录态")
        else:
            logger.warning("未检测到登录态，请在浏览器中手动扫码登录")
            logger.warning("登录后按 Enter 继续...")
            input()

    def _is_logged_in(self) -> bool:
        """检查是否已登录"""
        try:
            # 尝试查找用户头像或昵称元素
            user_elem = self.page.query_selector(
                "[class*='user'], [class*='avatar'], [class*='nickname']"
            )
            return user_elem is not None
        except Exception:
            return False

    def navigate_to_messages(self) -> None:
        """导航到消息页面"""
        logger.info("导航到消息页面...")
        self.page.goto(self.config.monitor.message_url,
                       wait_until="domcontentloaded",
                       timeout=self.config.browser.page_load_timeout)
        time.sleep(2)

    def poll_messages(self) -> list:
        """
        轮询获取新消息
        返回新消息列表
        """
        try:
            messages = self._extract_messages()
            new_messages = []

            for msg in messages:
                if msg.id not in self.processed_ids:
                    self.processed_ids.add(msg.id)
                    new_messages.append(msg)

            if new_messages:
                logger.info(f"发现 {len(new_messages)} 条新消息")
                for msg in new_messages:
                    logger.info(f"  新消息: {msg}")

            return new_messages

        except Exception as e:
            logger.error(f"轮询消息失败: {e}")
            return []

    def _extract_messages(self) -> list:
        """
        从页面提取消息列表
        使用 JavaScript 注入提取消息数据
        """
        try:
            # 尝试从页面 DOM 中提取消息
            # 闲鱼消息页面的 DOM 结构可能变化，这里提供通用提取逻辑
            result = self.page.evaluate("""() => {
                const messages = [];

                // 尝试多种选择器提取消息
                const selectors = [
                    '[class*="message"]',
                    '[class*="msg"]',
                    '[class*="chat-item"]',
                    '[class*="conversation"]',
                ];

                for (const selector of selectors) {
                    const elements = document.querySelectorAll(selector);
                    if (elements.length > 0) {
                        elements.forEach((el, idx) => {
                            const text = el.innerText || el.textContent || '';
                            if (text && text.length > 5) {
                                messages.push({
                                    id: `msg_${Date.now()}_${idx}`,
                                    sender: el.querySelector('[class*="sender"], [class*="name"]')?.innerText || '买家',
                                    content: text.trim().substring(0, 200),
                                    timestamp: new Date().toISOString(),
                                });
                            }
                        });
                        break;
                    }
                }

                return messages;
            }""")

            messages = []
            for item in result:
                messages.append(Message(
                    message_id=item.get("id", ""),
                    sender=item.get("sender", "买家"),
                    content=item.get("content", ""),
                    timestamp=item.get("timestamp", ""),
                ))
            return messages

        except Exception as e:
            logger.debug(f"提取消息失败: {e}")
            return []

    def send_reply(self, conversation_id: str, reply_text: str) -> bool:
        """
        发送回复消息

        Args:
            conversation_id: 对话ID
            reply_text: 回复内容

        Returns:
            是否发送成功
        """
        try:
            # 点击输入框
            input_selector = (
                "[class*='input'], [class*='textarea'], "
                "[class*='send-input'], textarea, input[type='text']"
            )
            input_elem = self.page.query_selector(input_selector)
            if not input_elem:
                logger.error("未找到输入框")
                return False

            input_elem.click()
            time.sleep(self.config.browser.action_delay)

            # 输入回复内容
            input_elem.fill(reply_text)
            time.sleep(0.5)

            # 点击发送按钮
            send_selector = (
                "[class*='send-btn'], [class*='send-button'], "
                "button[class*='send'], [class*='submit']"
            )
            send_elem = self.page.query_selector(send_selector)
            if send_elem:
                send_elem.click()
            else:
                # 尝试按 Enter 发送
                self.page.keyboard.press("Enter")

            time.sleep(self.config.browser.action_delay)
            logger.info(f"回复已发送: {reply_text[:50]}...")
            return True

        except Exception as e:
            logger.error(f"发送回复失败: {e}")
            return False

    def stop(self) -> None:
        """关闭浏览器"""
        self._running = False
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
        logger.info("浏览器已关闭")

    def run_loop(self, on_message: Callable) -> None:
        """
        运行监听主循环

        Args:
            on_message: 收到新消息时的回调函数
                        签名: on_message(message: Message) -> str  (返回回复内容)
        """
        self.on_new_message = on_message
        self._running = True

        self.start()
        self.login()
        self.navigate_to_messages()

        cycle = 0
        try:
            while self._running:
                cycle += 1
                logger.debug(f"轮询第 {cycle} 次...")

                new_messages = self.poll_messages()

                for msg in new_messages:
                    if self.on_new_message:
                        try:
                            reply = self.on_new_message(msg)
                            if reply:
                                self.send_reply(msg.conversation_id, reply)
                        except Exception as e:
                            logger.error(f"处理消息失败: {e}")

                # 检查是否达到最大轮询次数
                if (self.config.monitor.max_poll_cycles > 0
                        and cycle >= self.config.monitor.max_poll_cycles):
                    logger.info("达到最大轮询次数，退出")
                    break

                time.sleep(self.config.monitor.poll_interval)

        except KeyboardInterrupt:
            logger.info("收到中断信号，正在退出...")
        finally:
            self.stop()