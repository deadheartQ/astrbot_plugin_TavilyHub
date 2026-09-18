"""
astrbot_plugin_tavily
=====================

为 AstrBot 接入 Tavily Hub（https://tavily.sharyuke.com）联网搜索能力。

只走 Hub 中转：国内机房直连、无需代理、Key 前缀 thb-。
Tavily 官方接口请直接用 AstrBot 自带的联网搜索
（配置 → AI → 能力 → Web Search，provider 选 tavily）；
本插件额度耗尽时会自动降级到它。

Hub 与 Tavily 官方的两处关键差异（已在本插件内处理）：
1. Hub 的 HTTP 状态码恒为 200，真正的错误码在响应体 code 字段（0 才是成功）。
2. Hub 的结果被包了两层：data.data 才是 Tavily 原生结构。

命令：
    /tavily 关键词       通用搜索（/tvly /search /搜索）
    /news 关键词         新闻检索（/新闻）
    /extract 网址        网页正文抽取（/抽取）
    /crawl 网址          整站爬取（/爬取）
    /map 网址            站点地图（/地图）
    /tavilyhelp          帮助

LLM 工具（模型自动调用）：
    tavily_web_search / tavily_extract_url / tavily_crawl_site
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

HUB_BASE = "https://tavily.sharyuke.com"

# Hub 的业务错误码语义（HTTP 仍是 200）
HUB_ERR_HINT = {
    "400": "请求参数错误，检查请求体字段",
    "401": "API Key 无效，检查 Authorization 头",
    "429": "超过速率限制，降低请求频率",
    "500": "服务端内部错误，稍后重试",
    "503": "上游服务不可用，稍后重试",
}

SEARCH_CMD_NAMES = ("tavily", "tvly", "search", "sou", "搜索", "搜一下")
NEWS_CMD_NAMES = ("news", "tavilynews", "新闻", "资讯")
EXTRACT_CMD_NAMES = ("extract", "tavilyextract", "抽取", "网页")
CRAWL_CMD_NAMES = ("crawl", "tavilycrawl", "爬取", "爬站")
MAP_CMD_NAMES = ("map", "tavilymap", "地图", "站点地图")


class TavilyError(Exception):
    """Tavily / Tavily Hub 调用失败。"""


class TavilyQuotaError(TavilyError):
    """额度耗尽 / 限流类错误。调用方应降级到 AstrBot 自带搜索，而不是重试。"""


# 判断为"额度类错误"的关键词（同时匹配中文与英文错误信息）
QUOTA_KEYWORDS = ("额度", "配额", "余额", "quota", "insufficient", "credits", "exceed")

# 注入给模型的工具优先级规则（内容固定不变，避免破坏 provider 侧 prompt 缓存）
DEFAULT_PRIORITY_PROMPT = (
    "## 联网搜索工具使用优先级\n"
    "需要检索实时信息（新闻、股价、天气、最新资料等）时，严格按以下顺序选择工具：\n"
    "1. 首选 tavily_web_search：本插件提供的搜索，速度快且结果带来源链接。\n"
    "2. 只有当 tavily_web_search 明确返回“不可用 / 额度耗尽”时，才改用 web_search 或 fetch_url。\n"
    "3. 若上述工具都不可用，直接告诉用户当前无法联网检索，绝对不要编造答案。\n"
    "不要为同一个问题并行或重复调用多个搜索工具。"
)


def _is_quota_msg(msg: str) -> bool:
    low = (msg or "").lower()
    return any(k in low for k in QUOTA_KEYWORDS)


@register(
    "astrbot_plugin_tavily",
    "Buddy",
    "接入 Tavily Hub（或官方）搜索 API：联网搜索 / 新闻 / 网页抽取 / 整站爬取 / 站点地图，并可作为 LLM 工具被模型自动调用。",
    "1.1.0",
    "https://github.com/AstrBotDevs/AstrBot",
)
class TavilyPlugin(Star):
    def __init__(self, context: Context, config: Optional[Any] = None):
        super().__init__(context)
        self.config: Dict[str, Any] = dict(config or {})
        self._session: Optional[aiohttp.ClientSession] = None
        # 额度熔断：触发额度类错误后，在冷却期内不再请求 Tavily，直接让模型降级
        self._quota_dead_until = 0.0
        logger.info("[Tavily] 插件已加载，后端：Tavily Hub")

    async def initialize(self):
        """异步初始化（AstrBot 在实例化插件类后自动调用）。"""
        if not self.api_key:
            logger.warning(
                "[Tavily] 尚未配置 API Key，所有命令与工具将不可用，请在插件配置面板填写。"
            )
        else:
            logger.info("[Tavily] 初始化完成，搜索接口：%s", self._url("search"))

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def cfg(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    @property
    def api_key(self) -> str:
        key = str(self.cfg("api_key", "") or "").strip()
        if not key:
            key = os.environ.get("TAVILY_API_KEY", "").strip()
        return key

    def _url(self, op: str) -> str:
        base = str(self.cfg("api_base", "") or "").strip().rstrip("/")
        if not base:
            base = HUB_BASE
        return base + "/api/proxy/" + op

    def _depth(self) -> str:
        depth = str(self.cfg("search_depth", "basic") or "basic").strip()
        if depth not in ("basic", "advanced"):
            # Hub 只支持 basic / advanced
            logger.warning("[Tavily] Hub 不支持 %s 深度，已回落到 basic", depth)
            return "basic"
        return depth

    # ------------------------------------------------------------------ #
    # 额度熔断与降级
    # ------------------------------------------------------------------ #
    def _quota_blocked(self) -> bool:
        """额度是否处于冷却期（冷却期内不再请求 Tavily）。"""
        return time.time() < self._quota_dead_until

    def _mark_quota_dead(self) -> None:
        """检测到额度类错误，进入冷却。"""
        cooldown = max(60, int(self.cfg("quota_cooldown", 3600) or 3600))
        self._quota_dead_until = time.time() + cooldown
        logger.warning(
            "[Tavily] 检测到额度类错误，进入冷却 %d 秒，期间请求将直接降级到自带搜索",
            cooldown,
        )

    def _fallback_msg(self, reason: str) -> str:
        """返回给 LLM 的降级指令：明确告诉它改用哪个工具。"""
        alt = str(self.cfg("fallback_tool_name", "web_search") or "web_search").strip()
        return (
            "[tavily_web_search 暂时不可用] 原因：%s。\n"
            "请立即改用 %s 完成本次搜索。若 %s 同样不可用，"
            "直接告诉用户当前无法联网检索，不要编造答案，也不要重试本工具。"
            % (reason, alt, alt)
        )

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=int(self.cfg("timeout", 30)))
            trust_env = bool(self.cfg("trust_env", False))
            self._session = aiohttp.ClientSession(timeout=timeout, trust_env=trust_env)
        return self._session

    def _unwrap(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
        """把 Hub / 官方的响应统一成 Tavily 原生结构。"""
        # ---------- Hub ----------
        code = payload.get("code")
        if code not in (0, "0", None):
            msg = str(payload.get("message") or "未知错误")
            # 业务码形如 40301，前三位对应 HTTP 语义
            prefix = str(code)[:3]
            hint = HUB_ERR_HINT.get(prefix, "")
            text = "Tavily Hub 错误 %s：%s%s" % (
                code,
                msg,
                ("（" + hint + "）") if hint else "",
            )
            if prefix in ("429", "402") or _is_quota_msg(msg):
                raise TavilyQuotaError(text)
            raise TavilyError(text)

        data = payload.get("data")
        if data is None:
            raise TavilyError("Tavily Hub 未返回数据：%s" % (payload.get("message") or ""))
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict):
                # {"code":0,"message":"ok","data":{"ok":true,"data":{...},"credits":1}}
                return inner, data.get("credits")
            # 也可能是 {"code":0,"message":"ok","data":{...tavily原生...}}
            return data, data.get("credits")
        raise TavilyError("Tavily Hub 返回结构异常：%s" % str(data)[:200])

    async def _request(self, op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        key = self.api_key
        if not key:
            raise TavilyError(
                "未配置 API Key，请在插件配置面板填写（Tavily Hub 控制台获取）。"
            )

        body = dict(payload)

        headers = {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        }
        proxy = str(self.cfg("proxy", "") or "").strip() or None
        url = self._url(op)

        session = await self._get_session()
        async with session.post(url, json=body, headers=headers, proxy=proxy) as resp:
            status = resp.status
            raw = await resp.text()

        if status >= 400:
            try:
                err = json.loads(raw)
                msg = err.get("message") or err.get("error") or raw[:200]
            except Exception:
                msg = raw[:200]
            raise TavilyError("HTTP %s：%s" % (status, msg))

        try:
            parsed = json.loads(raw)
        except Exception:
            raise TavilyError("返回内容不是合法 JSON：%s" % raw[:200])
        if not isinstance(parsed, dict):
            raise TavilyError("返回结构异常：%s" % raw[:200])

        data, credits = self._unwrap(parsed)
        if credits is not None:
            logger.debug("[Tavily] 本次消耗 credits：%s", credits)
        return data

    # ------------------------------------------------------------------ #
    # API 封装
    # ------------------------------------------------------------------ #
    async def search(
        self,
        query: str,
        topic: Optional[str] = None,
        max_results: Optional[int] = None,
        include_raw_content: Optional[bool] = None,
    ) -> Dict[str, Any]:
        topic = (topic or self.cfg("topic", "general") or "general").strip()
        payload: Dict[str, Any] = {
            "query": query,
            "topic": topic,
            "search_depth": self._depth(),
            "max_results": int(max_results or self.cfg("max_results", 5) or 5),
            "include_answer": bool(self.cfg("include_answer", True)),
            "include_images": bool(self.cfg("include_images", False)),
        }

        raw_flag = self.cfg("include_raw_content", False)
        if include_raw_content is not None:
            raw_flag = include_raw_content
        if raw_flag:
            payload["include_raw_content"] = True
            chunks = int(self.cfg("chunks_per_source", 0) or 0)
            if chunks > 0:
                payload["chunks_per_source"] = chunks

        time_range = str(self.cfg("time_range", "") or "").strip()
        if time_range:
            payload["time_range"] = time_range

        if topic == "news":
            days = int(self.cfg("news_days", 3) or 0)
            if days > 0:
                payload["days"] = days

        include_domains = self.cfg("include_domains", []) or []
        if include_domains:
            payload["include_domains"] = [str(x).strip() for x in include_domains]
        exclude_domains = self.cfg("exclude_domains", []) or []
        if exclude_domains:
            payload["exclude_domains"] = [str(x).strip() for x in exclude_domains]

        country = str(self.cfg("country", "") or "").strip()
        if country:
            payload["country"] = country

        return await self._request("search", payload)

    async def extract(self, urls: List[str]) -> Dict[str, Any]:
        # Hub 的 extract 只接受 urls
        payload: Dict[str, Any] = {"urls": urls}
        return await self._request("extract", payload)

    async def crawl(self, url: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "url": url,
            "max_depth": int(self.cfg("crawl_max_depth", 2) or 2),
            "limit": int(self.cfg("crawl_limit", 10) or 10),
            "extract_depth": self.cfg("crawl_extract_depth", "basic") or "basic",
        }
        instructions = str(self.cfg("crawl_instructions", "") or "").strip()
        if instructions:
            payload["instructions"] = instructions
        return await self._request("crawl", payload)

    async def map_site(self, url: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "url": url,
            "max_depth": int(self.cfg("map_max_depth", 1) or 1),
            "limit": int(self.cfg("map_limit", 50) or 50),
        }
        instructions = str(self.cfg("map_instructions", "") or "").strip()
        if instructions:
            payload["instructions"] = instructions
        return await self._request("map", payload)

    # ------------------------------------------------------------------ #
    # 格式化
    # ------------------------------------------------------------------ #
    def _clip(self, text: Any, limit: Any) -> str:
        text = (str(text) if text is not None else "").strip()
        limit = int(limit or 0)
        if limit > 0 and len(text) > limit:
            return text[:limit] + " ...(已截断)"
        return text

    def format_search(self, data: Dict[str, Any], for_llm: bool = False) -> str:
        lines: List[str] = []

        answer = data.get("answer")
        if answer:
            lines.append("【AI 摘要】")
            lines.append(self._clip(answer, self.cfg("answer_max_chars", 800)))
            lines.append("")

        results = data.get("results") or []
        if not results:
            lines.append("没有搜到相关结果。")
        else:
            for idx, item in enumerate(results, 1):
                title = str(item.get("title") or "无标题").strip()
                url = str(item.get("url") or "").strip()
                lines.append("%d. %s" % (idx, title))
                if item.get("published_date"):
                    lines.append("   时间：%s" % item.get("published_date"))
                if item.get("content"):
                    lines.append(
                        "   %s"
                        % self._clip(item.get("content"), self.cfg("content_max_chars", 400))
                    )
                if item.get("raw_content"):
                    lines.append(
                        "   正文：%s"
                        % self._clip(item.get("raw_content"), self.cfg("raw_max_chars", 1500))
                    )
                if url:
                    lines.append("   %s" % url)
                lines.append("")

        images = data.get("images") or []
        if images and not for_llm:
            shown = images[: int(self.cfg("max_images", 3) or 3)]
            lines.append("【相关图片】")
            for img in shown:
                if isinstance(img, dict):
                    lines.append("%s %s" % (img.get("url") or "", img.get("description") or ""))
                else:
                    lines.append(str(img))

        text = "\n".join(x for x in lines if x is not None).strip()
        if for_llm:
            limit = int(self.cfg("llm_result_max_chars", 4000) or 0)
            if limit > 0 and len(text) > limit:
                text = text[:limit] + "\n...(结果过长已截断)"
        return text

    def format_extract(self, data: Dict[str, Any]) -> str:
        lines: List[str] = []
        for item in data.get("results") or []:
            url = str(item.get("url") or "").strip()
            if url:
                lines.append("来源：%s" % url)
            lines.append(self._clip(item.get("raw_content"), self.cfg("raw_max_chars", 1500)))
            lines.append("")

        failed = data.get("failed_results") or []
        if failed:
            lines.append("以下链接抽取失败：")
            for item in failed:
                lines.append("- %s" % (item.get("url") if isinstance(item, dict) else item))

        if not lines:
            return "没有抽取到任何内容。"
        return "\n".join(lines).strip()

    def format_crawl(self, data: Dict[str, Any]) -> str:
        lines: List[str] = []
        base = data.get("base_url")
        if base:
            lines.append("起始站点：%s" % base)
        results = data.get("results") or []
        if not results:
            lines.append("没有爬取到任何页面。")
        for idx, item in enumerate(results, 1):
            url = str(item.get("url") or "").strip()
            lines.append("%d. %s" % (idx, url))
            raw = item.get("raw_content")
            if raw:
                lines.append("   %s" % self._clip(raw, self.cfg("crawl_content_max_chars", 300)))
        failed = data.get("failed_results") or []
        if failed:
            lines.append("失败的页面：%d 个" % len(failed))
        return "\n".join(lines).strip()

    def format_map(self, data: Dict[str, Any]) -> str:
        lines: List[str] = []
        base = data.get("base_url")
        if base:
            lines.append("站点：%s" % base)
        results = data.get("results") or []
        show = int(self.cfg("map_show_limit", 50) or 50)
        if not results:
            lines.append("没有发现任何链接。")
        for idx, item in enumerate(results[:show], 1):
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            lines.append("%d. %s%s" % (idx, url, ("  (" + title + ")") if title else ""))
        if len(results) > show:
            lines.append("...共 %d 条，已省略部分" % len(results))
        return "\n".join(lines).strip()

    # ------------------------------------------------------------------ #
    # 命令解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def strip_command(text: str, names) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        parts = text.split(None, 1)
        head = parts[0]
        raw_head = head[1:] if head.startswith("/") else head
        low = raw_head.lower()
        for name in names:
            if low == name:
                return parts[1].strip() if len(parts) > 1 else ""
            if low.startswith(name):
                rest = raw_head[len(name):]
                tail = (" " + parts[1]) if len(parts) > 1 else ""
                return (rest + tail).strip()
        return text

    def _first_url(self, text: str) -> str:
        for token in (text or "").split():
            token = token.strip()
            if token.startswith("http://") or token.startswith("https://"):
                return token
        return ""

    # ------------------------------------------------------------------ #
    # 命令
    # ------------------------------------------------------------------ #
    async def _run(self, coro, what: str) -> Any:
        """执行协程，出错时返回可直接发送的错误字符串。"""
        try:
            return await coro
        except TavilyQuotaError as e:
            self._mark_quota_dead()
            logger.error("[Tavily] %s失败（额度问题）：%s", what, e)
            return "%s失败：%s\n已自动切换冷却期，期间建议改用 AstrBot 自带联网搜索。" % (what, e)
        except TavilyError as e:
            logger.error("[Tavily] %s失败：%s", what, e)
            return "%s失败：%s" % (what, e)
        except Exception as e:
            logger.error("[Tavily] %s异常：%s", what, e)
            return "%s异常：%s" % (what, e)

    async def _search_text(self, query: str, topic: str) -> Any:
        data = await self.search(query, topic=topic)
        if isinstance(data, dict):
            return self.format_search(data)
        return data

    @filter.command("tavily")
    async def cmd_tavily(self, event: AstrMessageEvent):
        """Tavily 联网搜索，用法：/tavily 搜索内容"""
        query = self.strip_command(event.message_str, SEARCH_CMD_NAMES)
        if not query:
            yield event.plain_result("用法：/tavily 你要搜索的内容")
            return
        yield event.plain_result(await self._run(self._search_text(query, "general"), "搜索"))

    @filter.command("tvly")
    async def cmd_tvly(self, event: AstrMessageEvent):
        """/tavily 的简写，用法：/tvly 搜索内容"""
        query = self.strip_command(event.message_str, SEARCH_CMD_NAMES)
        if not query:
            yield event.plain_result("用法：/tvly 你要搜索的内容")
            return
        yield event.plain_result(await self._run(self._search_text(query, "general"), "搜索"))

    @filter.command("search")
    async def cmd_search(self, event: AstrMessageEvent):
        """联网搜索，用法：/search 搜索内容"""
        query = self.strip_command(event.message_str, SEARCH_CMD_NAMES)
        if not query:
            yield event.plain_result("用法：/search 你要搜索的内容")
            return
        yield event.plain_result(await self._run(self._search_text(query, "general"), "搜索"))

    @filter.command("搜索")
    async def cmd_search_zh(self, event: AstrMessageEvent):
        """联网搜索，用法：/搜索 搜索内容"""
        query = self.strip_command(event.message_str, SEARCH_CMD_NAMES)
        if not query:
            yield event.plain_result("用法：/搜索 你要搜索的内容")
            return
        yield event.plain_result(await self._run(self._search_text(query, "general"), "搜索"))

    @filter.command("news")
    async def cmd_news(self, event: AstrMessageEvent):
        """新闻检索，用法：/news 关键词"""
        query = self.strip_command(event.message_str, NEWS_CMD_NAMES)
        if not query:
            yield event.plain_result("用法：/news 关键词")
            return
        yield event.plain_result(await self._run(self._search_text(query, "news"), "新闻搜索"))

    @filter.command("新闻")
    async def cmd_news_zh(self, event: AstrMessageEvent):
        """新闻检索，用法：/新闻 关键词"""
        query = self.strip_command(event.message_str, NEWS_CMD_NAMES)
        if not query:
            yield event.plain_result("用法：/新闻 关键词")
            return
        yield event.plain_result(await self._run(self._search_text(query, "news"), "新闻搜索"))

    @filter.command("extract")
    async def cmd_extract(self, event: AstrMessageEvent):
        """抽取网页正文，用法：/extract 网址"""
        url = self._first_url(self.strip_command(event.message_str, EXTRACT_CMD_NAMES))
        if not url:
            yield event.plain_result("用法：/extract 网址")
            return
        data = await self._run(self.extract([url]), "抽取")
        yield event.plain_result(
            data if isinstance(data, str) else self.format_extract(data)
        )

    @filter.command("抽取")
    async def cmd_extract_zh(self, event: AstrMessageEvent):
        """抽取网页正文，用法：/抽取 网址"""
        url = self._first_url(self.strip_command(event.message_str, EXTRACT_CMD_NAMES))
        if not url:
            yield event.plain_result("用法：/抽取 网址")
            return
        data = await self._run(self.extract([url]), "抽取")
        yield event.plain_result(
            data if isinstance(data, str) else self.format_extract(data)
        )

    @filter.command("crawl")
    async def cmd_crawl(self, event: AstrMessageEvent):
        """整站爬取，用法：/crawl 网址"""
        url = self._first_url(self.strip_command(event.message_str, CRAWL_CMD_NAMES))
        if not url:
            yield event.plain_result("用法：/crawl 网址")
            return
        data = await self._run(self.crawl(url), "爬取")
        yield event.plain_result(
            data if isinstance(data, str) else self.format_crawl(data)
        )

    @filter.command("爬取")
    async def cmd_crawl_zh(self, event: AstrMessageEvent):
        """整站爬取，用法：/爬取 网址"""
        url = self._first_url(self.strip_command(event.message_str, CRAWL_CMD_NAMES))
        if not url:
            yield event.plain_result("用法：/爬取 网址")
            return
        data = await self._run(self.crawl(url), "爬取")
        yield event.plain_result(
            data if isinstance(data, str) else self.format_crawl(data)
        )

    @filter.command("map")
    async def cmd_map(self, event: AstrMessageEvent):
        """站点地图，用法：/map 网址"""
        url = self._first_url(self.strip_command(event.message_str, MAP_CMD_NAMES))
        if not url:
            yield event.plain_result("用法：/map 网址")
            return
        data = await self._run(self.map_site(url), "站点地图")
        yield event.plain_result(data if isinstance(data, str) else self.format_map(data))

    @filter.command("地图")
    async def cmd_map_zh(self, event: AstrMessageEvent):
        """站点地图，用法：/地图 网址"""
        url = self._first_url(self.strip_command(event.message_str, MAP_CMD_NAMES))
        if not url:
            yield event.plain_result("用法：/地图 网址")
            return
        data = await self._run(self.map_site(url), "站点地图")
        yield event.plain_result(data if isinstance(data, str) else self.format_map(data))

    @filter.command("tavilyhelp")
    async def cmd_help(self, event: AstrMessageEvent):
        """Tavily 搜索插件帮助"""
        yield event.plain_result(
            "Tavily Hub 搜索插件\n"
            "/tavily 关键词   联网搜索（/tvly /search /搜索）\n"
            "/news 关键词     新闻检索（/新闻）\n"
            "/extract 网址    网页正文抽取（/抽取）\n"
            "/crawl 网址      整站爬取（/爬取）\n"
            "/map 网址        站点地图（/地图）\n"
            "/tavilyhelp      本帮助\n"
            "开启 LLM 工具后，模型会在需要实时信息时自动调用搜索。"
        )

    # ------------------------------------------------------------------ #
    # LLM Function Calling 工具
    # 注意：@filter.llm_tool 通过解析 docstring 的 Args: 段生成参数 schema，
    #      格式必须是「参数名(类型): 描述」，写错会导致 LLM 传参被静默丢弃。
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="tavily_web_search")
    async def llm_web_search(self, event: AstrMessageEvent, query: str):
        """联网搜索，获取实时信息。

        当用户询问近期新闻、时事、最新资料、股价财报、天气、赛事结果，
        或者任何你的训练数据之外、需要引用来源的问题时，必须调用本工具。
        不要凭记忆编造答案。

        Args:
            query(string): 搜索关键词，用简洁且搜索引擎友好的语句
        """
        if not self.cfg("enable_llm_tool", True):
            return "搜索工具已被管理员禁用。"
        query = (query or "").strip()
        if not query:
            return "搜索失败：query 不能为空。"
        if self._quota_blocked():
            return self._fallback_msg("额度已用尽，当前处于冷却期")
        try:
            data = await self.search(query)
        except TavilyQuotaError as e:
            self._mark_quota_dead()
            logger.error("[Tavily] LLM 工具额度不足：%s", e)
            return self._fallback_msg(str(e))
        except Exception as e:
            logger.error("[Tavily] LLM 工具搜索失败：%s", e)
            return "搜索失败：%s" % e
        return self.format_search(data, for_llm=True)

    @filter.llm_tool(name="tavily_extract_url")
    async def llm_extract(self, event: AstrMessageEvent, url: str):
        """抓取指定网页的正文内容。

        当你已经拿到一个具体网址，需要读取其中详细内容时调用本工具。

        Args:
            url(string): 要读取的完整网页地址，需以 http:// 或 https:// 开头
        """
        if not self.cfg("enable_llm_tool", True):
            return "网页读取工具已被管理员禁用。"
        url = (url or "").strip()
        if not url.startswith("http"):
            return "读取失败：url 必须以 http:// 或 https:// 开头。"
        if self._quota_blocked():
            return self._fallback_msg("额度已用尽，当前处于冷却期")
        try:
            data = await self.extract([url])
        except TavilyQuotaError as e:
            self._mark_quota_dead()
            logger.error("[Tavily] LLM 工具额度不足：%s", e)
            return self._fallback_msg(str(e))
        except Exception as e:
            logger.error("[Tavily] LLM 工具抽取失败：%s", e)
            return "读取失败：%s" % e
        return self.format_extract(data)

    @filter.llm_tool(name="tavily_crawl_site")
    async def llm_crawl(self, event: AstrMessageEvent, url: str):
        """遍历一个网站，列出其下的页面地址。

        当你需要摸清某个站点有哪些页面、查找文档目录结构时调用本工具。
        本工具只返回页面地址与标题摘要，不返回整站全文。

        Args:
            url(string): 站点入口地址，需以 http:// 或 https:// 开头
        """
        if not self.cfg("enable_llm_tool", True):
            return "站点爬取工具已被管理员禁用。"
        url = (url or "").strip()
        if not url.startswith("http"):
            return "爬取失败：url 必须以 http:// 或 https:// 开头。"
        if self._quota_blocked():
            return self._fallback_msg("额度已用尽，当前处于冷却期")
        try:
            data = await self.crawl(url)
        except TavilyQuotaError as e:
            self._mark_quota_dead()
            logger.error("[Tavily] LLM 工具额度不足：%s", e)
            return self._fallback_msg(str(e))
        except Exception as e:
            logger.error("[Tavily] LLM 工具爬取失败：%s", e)
            return "爬取失败：%s" % e
        return self.format_map(data)

    # ------------------------------------------------------------------ #
    # LLM 请求钩子：注入工具优先级
    # 注意：这里只追加「固定不变」的规则文本。
    # 每轮变化的内容（如额度状态）不要放进 system_prompt，否则会破坏
    # provider 侧 prompt 缓存并显著增加成本；动态状态通过工具返回值传达。
    # ------------------------------------------------------------------ #
    @filter.on_llm_request()
    async def inject_search_priority(self, event: AstrMessageEvent, req: ProviderRequest):
        """告诉模型：优先用本插件搜索，失败再用 AstrBot 自带联网搜索。"""
        if not self.cfg("enable_llm_tool", True):
            return
        if not self.cfg("inject_priority_prompt", True):
            return
        prompt = str(self.cfg("priority_prompt", "") or "").strip()
        if not prompt:
            prompt = DEFAULT_PRIORITY_PROMPT
        if prompt in (req.system_prompt or ""):
            return  # 已注入（例如热重载后重复触发），避免无限追加
        req.system_prompt = (req.system_prompt or "") + "\n\n" + prompt

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def terminate(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        logger.info("[Tavily] 插件已卸载")
