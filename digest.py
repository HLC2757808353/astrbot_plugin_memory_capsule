"""每日日报：抓公开热榜/RSS，交给模型整理成条目，入库并写进记忆。

数据源（全部公开、免登录）：
- RSS：量子位（AI）、游研社（游戏/二次元）、机核（游戏/亚文化）
- B站分区排行：动画、游戏
- 热榜：百度热搜、头条热榜

原则：
- 逐源独立失败：某个源挂了只跳过它，不影响其它，全挂就跳过当天（不写空记录）
- 只做"索引级"摘要：标题 + 一两句 + 关键词 + 链接，细节让她自己去搜
"""
from __future__ import annotations

import asyncio
import difflib
import json
import re
import xml.etree.ElementTree as ET

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

from .collector import _fetch_json


_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_BILI_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}

# (显示名, 地址, 分类)
_RSS_SOURCES = [
    ("量子位", "https://www.qbitai.com/feed", "AI"),
    ("游研社", "https://www.yystv.cn/rss/feed", "二次元"),
    ("机核", "https://www.gcores.com/rss", "二次元"),
]

# (显示名, 分区 rid, 分类)
_BILI_REGIONS = [
    ("B站动画", 1, "二次元"),
    ("B站游戏", 4, "二次元"),
]

_HOT_SOURCES = [
    ("百度热搜", "https://top.baidu.com/api/board?platform=wise&tab=realtime", "热点"),
    ("头条热榜", "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc", "热点"),
]


async def _fetch_text(url, headers=None, timeout=12.0):
    """抓纯文本（RSS/XML 用）。失败返回 None。"""
    if not _HAS_HTTPX:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers or _UA)
            if resp.status_code != 200:
                return None
            return resp.text
    except Exception as e:
        logger.warning(f"抓取失败 {url}: {e}")
        return None


async def _fetch_rss(source_name, url, category):
    """抓一个 RSS 源，返回 [{'title','url','category','source'}]。"""
    text = await _fetch_text(url)
    if not text or "<rss" not in text[:2000]:
        logger.warning(f"RSS 不可用或格式异常: {source_name}")
        return []
    items = []
    try:
        root = ET.fromstring(text)
        for node in root.iter("item"):
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            if not title:
                continue
            items.append({"title": title[:160], "url": link,
                          "category": category, "source": source_name})
            if len(items) >= 15:
                break
    except Exception as e:
        logger.warning(f"解析 RSS 失败 {source_name}: {e}")
        return []
    return items


async def _fetch_bili_region(name, rid, category):
    """抓 B站某个分区的三日排行。"""
    data = await _fetch_json(
        f"https://api.bilibili.com/x/web-interface/ranking/region?rid={rid}&day=3",
        _BILI_HEADERS)
    if not data or data.get("code") != 0:
        logger.warning(f"B站分区不可用: {name} (rid={rid})")
        return []
    items = []
    for v in (data.get("data") or [])[:15]:
        title = str(v.get("title") or "").strip()
        if not title:
            continue
        bvid = str(v.get("bvid") or "")
        items.append({"title": title[:160],
                      "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                      "category": category, "source": name})
    return items


async def _fetch_hot(name, url, category):
    """抓热榜标题（复用 collector 里已有的解析器，两个站结构不同）。"""
    from .collector import _extract_baidu, _extract_toutiao

    data = await _fetch_json(url, _UA)
    if not data:
        logger.warning(f"热榜不可用: {name}")
        return []
    titles = _extract_baidu(data) if name == "百度热搜" else _extract_toutiao(data)
    if not titles:
        logger.warning(f"热榜解析为空: {name}")
        return []
    return [{"title": t[:120], "url": "", "category": category, "source": name}
            for t in titles[:20]]


async def run_digest_once(db_manager, provider, config=None, reason="每日"):
    """抓一轮日报：多源 → 模型整理 → 入库 → 每一条写成记忆。

    Args:
        db_manager: 数据库管理器。
        provider: 用于整理摘要的 LLM provider；为 None 时跳过。
        config: 插件配置。
        reason: 触发原因，仅用于日志。

    Returns:
        dict: {'status': ..., 'added': n, 'sources': [成功源名]}
    """
    config = config or {}

    async def _record(status, added=0, sources=None, detail=""):
        """把本轮结果记进库，面板上就能看出"到底抓没抓到"。"""
        try:
            await asyncio.to_thread(
                db_manager.record_digest_run, status, added, sources or [], detail)
        except Exception as e:
            logger.warning(f"日报记录写入失败: {e}")
        return {"status": status, "added": added, "sources": sources or [], "detail": detail}

    if not provider:
        logger.warning("日报：没有可用模型，跳过")
        return await _record("no_provider", detail="没有可用的模型")
    if not _HAS_HTTPX:
        logger.warning("日报：缺少 httpx，跳过")
        return await _record("no_httpx", detail="缺少 httpx 依赖")

    # ---------- 抓取：逐源独立，失败只跳过自己 ----------
    tasks = [_fetch_rss(n, u, c) for n, u, c in _RSS_SOURCES]
    tasks += [_fetch_bili_region(n, r, c) for n, r, c in _BILI_REGIONS]
    tasks += [_fetch_hot(n, u, c) for n, u, c in _HOT_SOURCES]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    candidates = []
    ok_sources = []
    for r in results:
        if isinstance(r, BaseException):
            logger.warning(f"日报源异常: {r}")
            continue
        if r:
            candidates.extend(r)
            if r[0]["source"] not in ok_sources:
                ok_sources.append(r[0]["source"])
    if not candidates:
        logger.warning("日报：所有源都抓不到，跳过今天（不写空记录）")
        return await _record("no_data", detail="所有源都抓不到")

    # 同一标题只留一次
    seen = set()
    uniq = []
    for c in candidates:
        key = re.sub(r"\s+", "", c["title"])[:60]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)

    # ---------- 让模型挑并整理 ----------
    want = int(config.get("digest_items_per_day", 12) or 12)
    listing = "\n".join(f"{i+1}. [{c['source']}] {c['title']}" for i, c in enumerate(uniq[:90]))
    prompt = (
        f"下面是今天各平台的热榜和资讯标题（共 {min(len(uniq), 90)} 条）。\n"
        f"请挑出 {want} 条最值得知道的，整理成 JSON 数组。\n\n"
        f"{listing}\n\n"
        "要求：\n"
        f"- 覆盖三类，各约三分之一：AI/大模型/科技产品、二次元/游戏、大众热点\n"
        "- 跳过广告、抽奖、纯营销、明星八卦绯闻、负面社会新闻的细节（提到即可，不要渲染）\n"
        "- summary 用一两句话说清「发生了什么」，不要标题党，不要评价\n"
        "- keywords 给 2-4 个便于检索的词，用逗号分隔\n"
        "- chat_value 是 1-5 的整数：这个内容在群里聊天时好不好接话。硬核论文/财报给 2，"
        "有梗、有争议、跟大家生活相关的给 5\n"
        "- category 只能是 AI / 二次元 / 热点 三者之一\n"
        "- 只输出 JSON 数组，不要解释。格式：\n"
        '[{"title":"...","summary":"...","keywords":"a,b","category":"AI",'
        '"chat_value":4,"source":"量子位","url":""}]'
    )
    try:
        resp = await provider.text_chat(
            prompt=prompt,
            system_prompt="你是一个资讯编辑，只输出严格合法的 JSON 数组，不加任何多余文字。")
        text = (resp.completion_text if resp else "").strip()
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            logger.warning(f"日报：模型没给出 JSON，原文前 200 字: {text[:200]}")
            return await _record("no_json", sources=ok_sources, detail=text[:200])
        items = json.loads(m.group(0))
    except Exception as e:
        logger.error(f"日报：模型整理失败: {e}")
        return await _record("llm_error", sources=ok_sources, detail=str(e)[:200])

    cleaned = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        cleaned.append({
            "title": title[:120],
            "summary": str(it.get("summary") or "").strip()[:200],
            "keywords": str(it.get("keywords") or "").strip()[:120],
            "category": str(it.get("category") or "").strip()[:10] or "热点",
            "chat_value": max(1, min(5, int(it.get("chat_value") or 3))),
            "source": str(it.get("source") or "").strip()[:20],
            "url": str(it.get("url") or "").strip()[:300],
        })
    if not cleaned:
        return await _record("no_items", sources=ok_sources, detail="模型返回的条目为空")

    # ---------- 跨天去重：同一件事每天重复入库，她就会每天重复讲 ----------
    recent = await asyncio.to_thread(db_manager.find_recent_digest_titles, 14)
    def _norm(text):
        return re.sub(r"[\s\W_]+", "", str(text or ""))[:60]

    seen_norm = [x for x in (_norm(t) for t in recent) if x]
    deduped = []
    for it in cleaned:
        key = _norm(it["title"])
        if not key:
            continue
        duplicate = False
        for old in seen_norm:
            if key == old or key in old or old in key:
                duplicate = True
                break
            if difflib.SequenceMatcher(None, key, old).ratio() >= 0.82:
                duplicate = True
                break
        if duplicate:
            continue
        seen_norm.append(key)
        deduped.append(it)
    skipped_dup = len(cleaned) - len(deduped)
    cleaned = deduped
    if not cleaned:
        return await _record("all_duplicate", sources=ok_sources,
                             detail=f"{skipped_dup} 条都是最近两周讲过的")

    # ---------- 入库 ----------
    from datetime import date
    today = date.today().isoformat()
    added = await asyncio.to_thread(db_manager.add_digest_items, cleaned, today)

    # ---------- 每条写一条记忆：让她"知道自己看过什么" ----------
    # 内容不写日期（日期放标签）：同一件事再次被抓到时，内容哈希相同会被自动去重，
    # 否则每天存一份，她就会每天把同一件事再说一遍
    for it in cleaned:
        try:
            line = it['title']
            if it["summary"]:
                line += f"：{it['summary']}"
            await asyncio.to_thread(
                db_manager.write_memory,
                line[:118],
                category="每日日报",
                importance=3,
                tags=["新闻"] + [k for k in re.split(r"[,，]", it["keywords"]) if k.strip()][:4] + [today],
                source="daily_digest",
                visibility="public",
                owner_id="")
        except Exception as e:
            logger.warning(f"日报记忆写入失败: {e}")

    logger.info(f"日报完成({reason}): 抓 {len(candidates)} 条 → 入库 {added} 条"
                f"（跳重复 {skipped_dup} 条），源 {', '.join(ok_sources)}")
    return await _record("ok", added, ok_sources,
                         f"候选 {len(candidates)} 条，整理 {len(cleaned)} 条")
