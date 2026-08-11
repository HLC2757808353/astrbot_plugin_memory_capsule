"""自动采集热榜梗/热词，用 LLM 提炼后写入梗库。

数据源（公开热榜，无需登录）：
- B站热门（视频标题）
- B站搜索热词（更偏热词/梗）
- 百度热搜
- 头条热榜

采集策略：
- 低频运行（默认 24h 一次），避免 API 费用上升
- 一次性把多个热榜标题喂给 LLM，让它提炼出"可能是梗/黑话/热词"的条目
- 自动去重（term 相同跳过）
"""
from __future__ import annotations

import asyncio
import json
import re

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False


# ==================== 热榜数据源 ====================

async def _fetch_json(url: str, headers: dict | None = None, timeout: float = 15) -> dict | None:
    if not _HAS_HTTPX:
        logger.warning("httpx 未安装，无法抓取热榜")
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers=headers or {"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception as e:
        logger.warning(f"热榜抓取失败 {url}: {e}")
        return None


def _extract_bilibili(data: dict | None) -> list[str]:
    if not data:
        return []
    items = []
    try:
        list_ = data.get("data", {}).get("list", [])
        for it in list_:
            title = it.get("title") or ""
            if title:
                items.append(str(title).strip())
    except Exception:
        pass
    return items[:50]


def _extract_bilibili_hotword(data: dict | None) -> list[str]:
    """B站搜索热词（更偏词/梗，而非视频标题）。"""
    if not data:
        return []
    items = []
    try:
        list_ = data.get("list", []) or data.get("data", {}).get("list", [])
        for it in list_:
            kw = it.get("keyword") or it.get("show_name") or ""
            if kw:
                items.append(str(kw).strip())
    except Exception:
        pass
    return items[:50]


def _extract_baidu(data: dict | None) -> list[str]:
    if not data:
        return []
    items = []
    try:
        cards = data.get("data", {}).get("cards", [])
        for card in cards:
            for content_group in card.get("content", []):
                for content in content_group.get("content", []):
                    word = content.get("word") or content.get("desc") or ""
                    if word:
                        items.append(str(word).strip())
    except Exception:
        pass
    return items[:50]


def _extract_toutiao(data: dict | None) -> list[str]:
    if not data:
        return []
    items = []
    try:
        list_ = data.get("data", [])
        for it in list_:
            title = it.get("Title") or ""
            if title:
                items.append(str(title).strip())
    except Exception:
        pass
    return items[:50]


# 数据源定义：(名称, URL, 解析函数, 请求头)
SOURCES = [
    ("B站热门", "https://api.bilibili.com/x/web-interface/ranking/v2",
     _extract_bilibili,
     {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}),
    ("B站热词", "https://s.search.bilibili.com/main/hotword",
     _extract_bilibili_hotword,
     {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}),
    ("百度热搜", "https://top.baidu.com/api/board?platform=wise&tab=realtime",
     _extract_baidu,
     {"User-Agent": "Mozilla/5.0", "Referer": "https://top.baidu.com/"}),
    ("头条热榜", "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
     _extract_toutiao,
     {"User-Agent": "Mozilla/5.0"}),
]


class HotTrendCollector:
    """热榜采集 + LLM 提炼 + 入库。"""

    def __init__(self, db_manager, context=None, config=None):
        self.db_manager = db_manager
        self.context = context
        self.config = config or {}

    async def _get_llm(self):
        """获取用于提炼的 LLM provider。"""
        try:
            model_id = self.config.get("glossary_collect_llm_model", "")
            if model_id and self.context:
                prov = self.context.get_provider_by_id(model_id)
                if prov:
                    return prov
            if self.context:
                provs = self.context.get_all_providers()
                if provs:
                    return provs[0]
        except Exception as e:
            logger.warning(f"获取 LLM 失败: {e}")
        return None

    async def fetch_trends(self) -> list[dict]:
        """抓取所有数据源，返回 [{source, title}] 列表。"""
        results = []
        for name, url, parser, headers in SOURCES:
            if not self.config.get("glossary_collect_enabled", True):
                break
            try:
                data = await _fetch_json(url, headers)
                titles = parser(data)
                for t in titles:
                    results.append({"source": name, "title": t})
                logger.info(f"热榜 [{name}] 抓取到 {len(titles)} 条")
            except Exception as e:
                logger.warning(f"热榜 [{name}] 解析失败: {e}")
        return results

    async def _llm_extract(self, trends: list[dict]) -> list[dict]:
        """把热榜标题一次性喂给 LLM，提炼出可能是梗/黑话/热词的条目。"""
        llm = await self._get_llm()
        if not llm:
            logger.warning("无可用 LLM，跳过提炼（可手动导入）")
            return []

        titles = [t["title"] for t in trends]
        # 控制输入量
        sample = titles[:80]
        prompt = (
            "以下是一批热搜/热榜标题。请从中找出【可能是网络流行梗、黑话、缩写、新热词】的条目，"
            "并为每条给出简短解释。\n"
            "只返回 JSON 数组，格式：[{\"term\": \"词\", \"meaning\": \"简短解释\", "
            "\"category\": \"谐音梗|行为梗|抽象梗|表情包梗|其他梗\"}]\n"
            "如果某条不是梗/热词，直接跳过。最多返回 10 条。\n\n"
            "标题列表：\n" + "\n".join(f"- {t}" for t in sample)
        )
        try:
            resp = await llm.text_chat(
                prompt=prompt,
                system_prompt="你是网络流行语专家，只输出 JSON。"
            )
            text = resp.completion_text if resp else ""
            text = text.strip()
            # 提取 JSON 数组
            m = re.search(r"\[.*\]", text, re.S)
            if not m:
                return []
            parsed = json.loads(m.group(0))
            items = []
            for p in parsed:
                if not isinstance(p, dict):
                    continue
                term = str(p.get("term", "")).strip()
                if not term or len(term) > 30:
                    continue
                meaning = str(p.get("meaning", "")).strip()[:200]
                category = str(p.get("category", "其他梗")).strip()
                if category not in ("谐音梗", "行为梗", "抽象梗", "表情包梗", "其他梗"):
                    category = "其他梗"
                items.append({
                    "term": term,
                    "meaning": meaning,
                    "category": category,
                    "source": "自动采集"
                })
            return items
        except Exception as e:
            logger.warning(f"LLM 提炼失败: {e}")
            return []

    async def run_once(self) -> dict:
        """执行一次完整采集：抓取 → 提炼 → 入库。"""
        if not self.config.get("glossary_collect_enabled", True):
            return {"status": "disabled"}
        trends = await self.fetch_trends()
        if not trends:
            return {"status": "no_data", "count": 0}
        items = await self._llm_extract(trends)
        if not items:
            return {"status": "no_extracted", "count": 0}

        imported = 0
        skipped = 0
        for it in items:
            try:
                result = await asyncio.to_thread(
                    self.db_manager.add_glossary,
                    it["term"],
                    it["category"],
                    it["meaning"],
                    it.get("source", "自动采集"),
                    ""
                )
                if result == "already_exists":
                    skipped += 1
                else:
                    imported += 1
            except Exception as e:
                logger.warning(f"入库失败 {it['term']}: {e}")
                skipped += 1
        logger.info(f"热榜采集完成: 新增 {imported}, 跳过重复 {skipped}")
        return {"status": "ok", "imported": imported, "skipped": skipped}


async def run_collect_once(db_manager, context=None, config=None):
    """供外部调用的采集入口。"""
    collector = HotTrendCollector(db_manager, context, config)
    return await collector.run_once()
