# astrbot_plugin_Tavily Hub

给 AstrBot 接入 **Tavily Hub** 联网搜索的插件。


两者分工：
| 需求 | 用什么 |
| --- | --- |
| 国内直连、低延迟、免费 3600 次/月、**自带 crawl / map** | 本插件（Tavily Hub） |
| Tavily 官方接口 | AstrBot 自带联网搜索 |
| 兜底 | 本插件额度耗尽 → 自动降级到自带搜索 |

两者 Key 不通用：官方的 `tvly-` Key 拿去打 Hub 会返回 `40301 无效的 API Key`，必须用 Hub 控制台生成的 `thb-` Key。

## 拿 Key

1. 注册 https://tavily.sharyuke.com（国内邮箱，无需信用卡）
2. 控制台创建 API Key（形如 `thb-xxxxxxxx`）
3. 填进插件配置的 `api_key`

## 安装

1. 把 `astrbot_plugin_tavily` 整个目录复制到 AstrBot 插件目录：

   ```
   <AstrBot 根目录>/data/plugins/astrbot_plugin_tavily/
   ```

2. 重启 AstrBot（或在管理面板重载插件）。
3. 在 **管理面板 → 插件 → Tavily 联网搜索 → 配置** 里填 `api_key`。

Hub 是国内机房，直连即可，**不要开代理**，否则绕去国外反而慢。

## 功能

| 能力 | 触发方式 |
| --- | --- |
| 通用联网搜索 | `/tavily 关键词`（别名 `/tvly` `/search` `/搜索`） |
| 新闻检索 | `/news 关键词`（`/新闻`，自动 `topic=news` + 天数回溯） |
| 网页正文抽取 | `/extract 网址`（`/抽取`） |
| 整站爬取 | `/crawl 网址`（`/爬取`） |
| 站点地图 | `/map 网址`（`/地图`） |
| 帮助 | `/tavilyhelp` |
| LLM 自动搜索 | 工具 `tavily_web_search` |
| LLM 自动读网页 | 工具 `tavily_extract_url` |
| LLM 摸站点结构 | 工具 `tavily_crawl_site` |

## Hub 接口的坑（插件已自动处理）

1. **HTTP 状态码恒为 200**。错误藏在响应体的 `code` 字段里，`code == 0` 才是成功。只看 HTTP status 判断成败会把错误当成功。
2. **响应包两层**。真正的结果在 `data.data`，不是顶层：
   ```json
   {"code":0,"message":"ok","data":{"ok":true,"data":{"results":[...]},"credits":1}}
   ```
3. **搜索深度只有 `basic` / `advanced`**。
4. **extract 只接受 `urls`**，`extract_depth` / `format` 是官方专有参数，Hub 不支持。

## 配置项

关键几项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `api_key` | 空 | 必填，`thb-` 开头 |
| `api_base` | 空 | 自建中转可填，留空用 Hub |
| `search_depth` | `basic` | `advanced` 更全但更贵 |
| `max_results` | `5` | 返回条数 |
| `include_answer` | `true` | 是否要 AI 摘要 |
| `include_raw_content` | `false` | 网页全文，很吃 token |
| `time_range` | 空 | day / week / month / year |
| `news_days` | `3` | 新闻回溯天数 |
| `enable_llm_tool` | `true` | 允许模型自动调用 |
| `proxy` / `trust_env` | 空 / `false` | 直连即可，一般不用动 |

其余是展示长度与 crawl / map 参数。

## 与 AstrBot 自带联网搜索协作（优先本插件，失败自动降级）

AstrBot 自带联网搜索注册的内置工具叫 **`web_search`** 和 **`fetch_url`**（内置包 `web_searcher`），开关在
**配置 → AI → 能力 → Web Search**（`provider_settings.web_search`，默认是关的），可选 Tavily / BoCha / Brave / Exa 等 provider。

本插件用三层机制做到「优先用插件，没额度了自动换自带的」：

| 层 | 机制 | 说明 |
| --- | --- | --- |
| 1 | **提示词优先级** | 插件通过 `@filter.on_llm_request` 往系统提示词追加一段**固定**规则：优先 `tavily_web_search`，只有它明确不可用时才用 `web_search` / `fetch_url`。开关 `inject_priority_prompt` |
| 2 | **错误即降级信号** | 额度耗尽/限流时，工具不返回空结果，而是返回一段明确的指令文本：「tavily_web_search 暂时不可用，请立即改用 web_search」。模型看到就会换工具，不靠猜 |
| 3 | **熔断** | 触发额度类错误后进入冷却（默认 1 小时），冷却期内插件直接返回降级指令、不再请求 Tavily，省时间也避免反复报错。冷却结束自动重试一次 |

要这套机制生效，**必须先在 AstrBot 里开启自带联网搜索并配好它的 Key**，否则没有可降级的工具。

相关配置：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `inject_priority_prompt` | `true` | 是否注入优先级规则 |
| `priority_prompt` | 内置规则 | 自定义规则措辞，留空用默认 |
| `fallback_tool_name` | `web_search` | 降级时让模型改用的工具名 |
| `quota_cooldown` | `3600` | 额度耗尽后的冷却秒数 |

识别为「额度类错误」的条件：Hub 业务码前缀 `429` / `402`，或错误信息里含「额度 / 配额 / 余额 / quota / insufficient / credits / exceed」。

> 提示词只追加**固定不变**的规则文本。额度状态这类每轮变化的信息通过工具返回值传达，不写进 system_prompt —— 否则会破坏模型服务端的提示词缓存，显著增加成本和首 token 延迟。

## 关于 LLM 工具

工具参数 schema 由 AstrBot 解析函数 docstring 生成，**必须**保持这个格式：

```python
Args:
    query(string): 搜索关键词
```

少写 `Args:` 段或括号写错，模型传进来的参数会被静默丢弃，函数报缺少参数。改代码时别破坏 docstring。

## 额度

Hub 免费档 3600 次/月，不限制并发。`include_raw_content` + `advanced` 深度 + `crawl` 会明显加快消耗。

## License

MIT
