# 闲鱼智能回复系统

> 大模型加持的闲鱼自动回复工具，让卖家的"秒回"更智能、更自然。

## 核心功能

| 功能 | 说明 |
|------|------|
| 🧠 智能意图识别 | 自动识别买家意图：问价/砍价/库存/物流/商品咨询 |
| 🎭 场景化回复 | 针对不同场景使用不同的提示词策略 |
| 💬 对话历史 | 记住上下文，回复更连贯 |
| 🎨 人设定制 | 可配置卖家的回复风格、签名、营业时间 |
| 🔄 浏览器监听 | 通过浏览器自动化监听闲鱼消息并自动回复 |

## 系统架构

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│ 浏览器监听器 │────▶│ 智能回复引擎 │────▶│   大模型 API  │
│ (Playwright) │     │              │     │  (OpenAI/DS) │
└─────────────┘     └──────┬───────┘     └──────────────┘
       │                   │
       ▼                   ▼
┌─────────────┐     ┌──────────────┐
│  消息发送器  │     │  对话历史管理  │
└─────────────┘     └──────────────┘
```

## 快速开始

### 1. 安装依赖

```bash
pip install httpx playwright
playwright install chromium
```

### 2. 演示模式（测试回复引擎）

```bash
# 需要设置 API Key
export XIANYU_LLM_API_KEY="your-api-key"
python -m xianyu_smart_reply --demo

# 或使用命令行参数
python -m xianyu_smart_reply --demo --model gpt-4o-mini --api-key sk-xxx
```

### 3. 监听模式（自动回复）

```bash
# 配置环境变量
export XIANYU_LLM_API_KEY="your-api-key"
export XIANYU_BROWSER_USER_DIR="C:/path/to/chrome/profile"

# 启动监听
python -m xianyu_smart_reply --monitor
```

## 配置说明

### 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `XIANYU_LLM_API_KEY` | 大模型 API Key | - |
| `XIANYU_LLM_API_BASE` | 自定义 API 地址 | `https://api.openai.com/v1` |
| `XIANYU_LLM_MODEL` | 模型名称 | `gpt-4o-mini` |
| `XIANYU_LLM_PROVIDER` | 模型提供商 | `openai` |
| `XIANYU_BROWSER_USER_DIR` | Chrome 用户数据目录 | - |
| `XIANYU_HEADLESS` | 无头模式 | `false` |
| `XIANYU_POLL_INTERVAL` | 轮询间隔(秒) | `5.0` |

### 卖家人设配置

在代码中修改 `SellerProfile`：

```python
config.seller.name = "数码小店"
config.seller.tone = "casual"          # friendly / professional / casual
config.seller.signature = "亲，在的哦~"
config.seller.business_hours = "9:00-22:00"
```

## 支持的模型

- OpenAI: `gpt-4o-mini`, `gpt-4o`, `gpt-3.5-turbo`
- DeepSeek: `deepseek-v3`, `deepseek-chat`
- 任何兼容 OpenAI API 的模型

## 注意事项

1. **风险提示**：本工具通过浏览器自动化操作闲鱼，可能违反闲鱼用户协议，请谨慎使用
2. **登录态**：需要保持浏览器登录态，建议使用 Chrome 用户数据目录
3. **风控**：操作间隔、回复频率等参数请合理设置，避免被平台风控
4. **成本**：每次回复都会调用大模型 API，请控制调用频率以降低成本