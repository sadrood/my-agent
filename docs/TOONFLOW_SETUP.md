# 短剧工厂服务接入与出片流程（实测记录，2026-09-17）

> 目标：让 **my_agent 通过 REST 驱动本机短剧工厂服务**（`127.0.0.1:10588`）走完
> 「原文 → 事件图谱 → 剧本 → 分镜 → 出图 → 出片」，产出**真动态**短视频。
> 本文记录实测通过的配置与调用序列，避免重复踩坑。

## 一、供应商与密钥（"改成我的 API"）

- 短剧工厂服务的密钥存在它自己的库里（`data/db2.sqlite` → `o_vendorConfig`），
  实例：`id=agnes`，`inputValues.apiKey`、`baseUrl=https://<备用端点>/v1`，`enable=1`。
- 与 my_agent 的 `IMAGE_GEN_API_KEY` / `VIDEO_GEN_API_KEY` 是**同一把备用供应商 key**（已逐字符比对一致）。
- 换 key 的方式（用官方 API，不要直接改库）：
  `POST /api/setting/vendorConfig/updateVendorInputs` → `{id:"agnes", inputValues:{apiKey, baseUrl}}`。

## 二、⚠️ 必配：Agent 部署表的模型串（本次最大的坑）

短剧工厂服务的每个 Agent 槽位在 `o_agentDeploy` 里，`resolveModelName()` 取的是
**`modelName` 字段**，并且要求它是 **`供应商:模型` 的完整串**：

```
key=universalAi      model=<备用文本模型 id>   modelName=备用供应商:<备用文本模型 id>   vendorId=备用供应商
key=scriptAgent      model=<备用文本模型 id>   modelName=备用供应商:<备用文本模型 id>   vendorId=备用供应商
key=productionAgent  model=<备用文本模型 id>   modelName=备用供应商:<备用文本模型 id>   vendorId=备用供应商
```

**踩坑现象**（两个字段填反时）：

| 现象 | 真实原因 |
|---|---|
| 提示词生成失败：`简易配置模式下，未找到部署配置 universalAi` | 槽位 `key=universalAi` 那行没配（`modelName` 为空） |
| 提示词生成失败：`未找到供应商配置 id=<备用文本模型 id>` | `modelName` 少了供应商前缀 → 代码按 `:` 切分后把模型名当成了供应商 id |

修复（官方 API，逐行更新）：

```
POST /api/setting/agentDeploy/updateAgentModel
{"id":3,"name":"通用AI","model":"<备用文本模型 id>",
 "modelName":"备用供应商:<备用文本模型 id>","vendorId":"备用供应商","desc":"…","temperature":0.7}
```

三个主槽位（`universalAi` / `scriptAgent` / `productionAgent`）都要这样配；
`scriptAgent:xxx`、`productionAgent:xxx` 等子槽位会**自动回退**到主槽位，不必逐个配。

## 三、my_agent 侧的命令序列（已实测跑通）

`toonflow` 工具（`tools/toonflow.py`，客户端 `models/toonflow.py`）：

```
1. toonflow(command="projects")                     # 拿 project_id（或 create_project 新建）
2. toonflow(command="scripts",  project_id=…)       # 拿 script_id
3. toonflow(command="storyboard", script_id=…)      # 拿分镜 id（出图/出片都以分镜为单位）
4. toonflow(command="add_track", project_id=…, script_id=…)        → track_id（免费，仅写库）
5. toonflow(command="gen_prompts", project_id=…,                    # 生成 [Visual]+[Motion] 提示词
             track_data=[{trackId, info:[{id:<分镜id>, sources:"storyboard"}]}],
             model="备用供应商:<视频模型 id>", mode="text")
6. toonflow(command="generate_video", project_id=…, script_id=…,     # 提交出片（**消耗视频额度**）
             track_data=[{uploadData:[{id:<分镜id>, sources:"storyboard"}],
                          trackId, prompt:<第5步的 prompt>, duration:5}],
             model="备用供应商:<视频模型 id>", mode="text", resolution="720P")
7. toonflow(command="video_state", project_id=…, script_id=…, video_ids=[…])   # 权威状态
8. toonflow(command="file_url" / workbench)                          # 取成片地址/工作台数据
```

关键实测结论：

- **状态以 `checkVideoStateList` 为准**：`getGenerateData`（workbench）会显示过期的
  `未生成`，而 `checkVideoStateList` 返回 `生成成功` + `filePath`。别被前者误导。
- **时长**：`<视频模型 id>` 只支持 `mode=text`（**没有图生视频**），
  合法时长 4–12s；画面一致性靠提示词里的固定描述（角色/场景/风格），不是靠首帧图。
- **配额**：免费档限流明显（本次先遇到 `HTTP 429 rate_limit_exceeded`，
  稍后恢复；每次出片 5s/720P 约 1–3 分钟完成）。
- 出片产物是**真动态视频**（AI 生成运动画面），再交给 my_agent 的 `video_edit`
  做配音/BGM/字幕/拼接 → 完整成片。

## 四、端到端实测样例（2026-09-17）

```
项目: 第七次葬礼（备用供应商试跑）  projectId=1789640003887  scriptId=1
轨道: 1789651015974  提示词: [Visual]+[Motion]，1586 字符
出片: videoId=12 → 生成成功
产物: output/toonflow_shot1.mp4  720x1280 · 5.18s · 3.58MB（竖屏 9:16）
```

## 五、注意

- 短剧工厂服务的读接口**几乎全是 POST + JSON body**（1.1.8 版 169 条路由里 159 条 POST），
  用 GET 会 404；`toonflow(command="call", path=…)` 会按内置路由表自动选方法。
- 直接改 `data/db2.sqlite` 有缓存/并发风险，**优先走它的 HTTP API**。
- 服务日志与数据都在该工具的安装目录 `data/` 下。
