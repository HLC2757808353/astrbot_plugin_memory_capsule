"""自动采集热榜梗/热词，用 LLM 提炼后写入梗库。

数据源（公开热榜，无需登录）：
- B站热门（视频标题）
- B站搜索热词（更偏热词/梗）
- 百度热搜
- 头条热榜

采集策略（质量优先，宁缺毋滥）：
- 低频运行（默认 24h 一次），避免 API 费用上升
- Stage1：从标题中挑出可能是梗的词/短语（排除新闻事件、视频标题本身）
- Stage2：LLM 独立核验，高置信(≥0.8)且释义充分才直接入库
- 联网查证：LLM 没把握的低置信词，自动去搜索引擎抓真实语料，
  由 LLM 依据语料二次判断——确认为梗才入库，避免"超新梗因模型不知道而漏掉"
- 入库前自动去重（归一化 + 相似度）
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


def _strip_html(text: str) -> str:
    """去除简单 HTML 标签并反转义。"""
    import html as _html
    text = re.sub(r"<[^>]+>", "", text or "")
    return _html.unescape(text).strip()


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

    async def _llm_chat_json(self, llm, user_prompt: str, system_prompt: str) -> list:
        """调用 LLM 并要求只返回 JSON 数组；解析失败返回 []。"""
        try:
            resp = await llm.text_chat(prompt=user_prompt, system_prompt=system_prompt)
            text = (resp.completion_text if resp else "").strip()
            # 提取 JSON 数组（兼容 markdown 代码块等包裹）
            m = re.search(r"\[.*\]", text, re.S)
            if not m:
                return []
            parsed = json.loads(m.group(0))
            return parsed if isinstance(parsed, list) else []
        except Exception as e:
            logger.warning(f"LLM 响应解析失败: {e}")
            return []

    async def _llm_extract(self, trends: list[dict]) -> tuple[list, list]:
        """两阶段提炼：先筛候选梗词，再独立核验。

        Stage 1 从热榜标题里挑出【可能是梗的词汇/短语】（排除纯新闻事件、视频标题本身）。
        Stage 2 拿着候选词列表重新核验。核验通过的返回释义并直接可用；
                核验没把握（confidence < 0.8）的词放进 unverified 列表，
                交给后续"联网查证"环节查真实语料，而不是直接丢弃。
        返回 (items, unverified_terms)。
        """
        llm = await self._get_llm()
        if not llm:
            logger.warning("无可用 LLM，跳过提炼（可手动导入）")
            return [], []

        # 去重并控制输入量（混合源标题约 110 条，样本取 90 内）
        seen_titles = set()
        titles = []
        for t in [x["title"] for x in trends]:
            tt = str(t).strip()
            if tt and tt not in seen_titles:
                seen_titles.add(tt)
                titles.append(tt)
        sample = titles[:90]
        if not sample:
            return [], []

        # ---------- Stage 1：从标题中挑候选梗词 ----------
        stage1_prompt = (
            "以下是一批热搜/热榜标题。请找出其中【可能是网络梗、黑话、缩写、流行语】的词或短语。\n\n"
            "符合要求的词通常有这些特征：\n"
            "- 有独立含义，被网友反复用来刷屏/玩梗/评论\n"
            "- 单独拿出来网友能心领神会，常见于弹幕、评论区\n"
            "- 可能出自某段视频、事件、台词、歌曲、社区\n\n"
            "严格排除（这些不是梗）：\n"
            "- 纯新闻事件叙述，如「某地发生……」这种整句标题\n"
            "- 人名/地名/机构名、某个作品或视频的标题本身\n"
            "- 只是普通热门话题，没有玩梗性质的词语\n\n"
            "只返回 JSON 数组，格式：[{\"term\": \"候选词\", \"reason\": \"为什么可能是梗，一句话\"}]\n"
            "最多返回 15 个。宁可少报，不可错报。\n\n标题列表：\n"
            + "\n".join(f"- {t}" for t in sample)
        )
        candidates = await self._llm_chat_json(
            llm, stage1_prompt,
            system_prompt="你是网络流行语研究专家。只输出 JSON 数组，不输出任何其他文字。"
        )
        cand_terms = []
        for c in candidates:
            if not isinstance(c, dict):
                continue
            term = str(c.get("term", "")).strip()
            # 过滤过长短语（整句标题）与过短无意义词
            if not term or len(term) > 20 or len(term) < 2:
                continue
            cand_terms.append(term)
        # 去重保序
        cand_terms = list(dict.fromkeys(cand_terms))
        if not cand_terms:
            logger.info("Stage1 未筛出候选梗词")
            return [], []
        logger.info(f"Stage1 筛出 {len(cand_terms)} 个候选梗词")

        # ---------- Stage 2：核验（候选词全部打分，低置信的进待查证列表） ----------
        stage2_prompt = (
            "请对以下候选词列表中的【每一个词】都逐条核验并返回结果，判断它们是否是真·网络梗。\n\n"
            "判断时注意：\n"
            "- 它是不是一个被大众传播、有稳定含义的网络梗/黑话/流行语？\n"
            "- 它的含义与出处你是否真的了解？\n\n"
            "含义/出处拿不准、只是猜测的词，confidence 打低分（0.2-0.6 之间），不要硬写释义。\n"
            "确认是真梗的词，释义要准确，禁止望文生义，需包含：含义（表达什么、什么场景用）、"
            "出处（哪个事件/视频/台词/社区）、演变（怎么火的），合写 60-150 字。\n\n"
            "只返回 JSON 数组，格式：[{\"term\": \"词\", \"meaning\": \"释义或留空\", "
            "\"category\": \"谐音梗|行为梗|抽象梗|表情包梗|其他梗\", \"confidence\": 0到1的小数}]\n"
            "候选词有多少个就返回多少条。\n\n候选词列表：\n"
            + "\n".join(f"- {t}" for t in cand_terms)
        )
        verified = await self._llm_chat_json(
            llm, stage2_prompt,
            system_prompt="你是严格的网络梗考据专家，只输出 JSON 数组，不输出任何其他文字。"
        )

        items = []
        unverified = []
        parsed_terms = set()
        for p in verified:
            if not isinstance(p, dict):
                continue
            term = str(p.get("term", "")).strip()
            if not term or len(term) > 20 or len(term) < 2:
                continue
            if term not in cand_terms:
                continue
            parsed_terms.add(term)
            try:
                conf = float(p.get("confidence", 0))
            except (TypeError, ValueError):
                conf = 0
            meaning = str(p.get("meaning", "")).strip()
            if conf >= 0.8 and len(meaning) >= 15:
                category = str(p.get("category", "其他梗")).strip()
                if category not in ("谐音梗", "行为梗", "抽象梗", "表情包梗", "其他梗"):
                    category = "其他梗"
                items.append({
                    "term": term,
                    "meaning": meaning[:300],
                    "category": category,
                    "source": "自动采集"
                })
            else:
                unverified.append(term)
        # LLM 漏返回的候选词同样视为低置信，送查证
        for t in cand_terms:
            if t not in parsed_terms:
                unverified.append(t)
        unverified = list(dict.fromkeys(unverified))
        logger.info(f"Stage2 直接通过 {len(items)} 条，低置信待联网查证 {len(unverified)} 条")
        return items, unverified

    async def _search_snippets(self, term: str, limit: int = 6) -> list[str]:
        """用必应搜索该词，返回真实网页的标题+摘要片段，用于查证。"""
        if not _HAS_HTTPX:
            return []
        try:
            from urllib.parse import quote
        except ImportError:
            return []
        q = quote(f'"{term}" 网络梗')
        url = f"https://cn.bing.com/search?q={q}"
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return []
                html = resp.text
        except Exception as e:
            logger.warning(f"搜索查证失败 [{term}]: {e}")
            return []
        snippets = []
        for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
            tm = re.search(r'<h2[^>]*>\s*<a[^>]*>(.*?)</a>', block, re.S)
            title = _strip_html(tm.group(1)) if tm else ""
            pm = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
            capt = _strip_html(pm.group(1)) if pm else ""
            text = f"{title}。{capt}".strip(" 。")
            if len(text) > 15:
                snippets.append(text[:260])
            if len(snippets) >= limit:
                break
        return snippets

    async def _verify_via_search(self, llm, term: str) -> dict | None:
        """联网查证一个低置信候选词：抓真实语料 → LLM 依据语料判断并释义。"""
        snippets = await self._search_snippets(term)
        if not snippets:
            logger.info(f"查证 [{term}]：无搜索结果，跳过")
            return None
        prompt = (
            f"需要查证的词：{term}\n\n"
            "以下是搜索引擎返回的关于该词的真实网页标题与摘要（真实语料）：\n"
            + "\n".join(f"- {s}" for s in snippets)
            + "\n\n请仅依据上述语料判断这个词是否是真实的网络梗/流行语，并给出有据可依的释义。\n"
            "- 语料显示它在玩梗/社区语境中被广泛使用 → is_meme: true，并按语料写出释义（需体现它的来源线索）\n"
            "- 语料只是无关词条、无玩梗语境、或证据不足无法判断 → is_meme: false\n"
            "禁止使用语料之外的猜测，宁可 false 也不编造。\n"
            "只返回 JSON 数组：[{\"term\": \"词\", \"is_meme\": true或false, \"meaning\": \"释义30-150字\", "
            "\"category\": \"谐音梗|行为梗|抽象梗|表情包梗|其他梗\", \"confidence\": 0到1}]"
        )
        parsed = await self._llm_chat_json(
            llm, prompt,
            system_prompt="你是网络梗考据专家，严格依据给定语料判断，只输出 JSON 数组，不输出其他文字。"
        )
        for p in parsed:
            if not isinstance(p, dict):
                continue
            if str(p.get("term", "")).strip() != term:
                continue
            if p.get("is_meme") is not True:
                logger.info(f"查证 [{term}]：判定非梗，剔除")
                return None
            try:
                conf = float(p.get("confidence", 0))
            except (TypeError, ValueError):
                conf = 0
            meaning = str(p.get("meaning", "")).strip()
            if conf < 0.7 or len(meaning) < 30:
                logger.info(f"查证 [{term}]：置信度/释义不足，剔除")
                return None
            category = str(p.get("category", "其他梗")).strip()
            if category not in ("谐音梗", "行为梗", "抽象梗", "表情包梗", "其他梗"):
                category = "其他梗"
            logger.info(f"查证 [{term}]：确认为梗并入库")
            return {
                "term": term,
                "meaning": meaning[:300],
                "category": category,
                "source": "自动采集"
            }
        return None

    async def run_once(self) -> dict:
        """执行一次完整采集：抓取 → 两阶段提炼 → 低置信词联网查证 → 入库。"""
        if not self.config.get("glossary_collect_enabled", True):
            return {"status": "disabled"}
        trends = await self.fetch_trends()
        if not trends:
            return {"status": "no_data", "count": 0}
        items, unverified = await self._llm_extract(trends)

        # 对 LLM 没把握的词联网查证：抓真实语料 → 判断 → 能确认则补入
        verified_count = 0
        if unverified:
            llm = await self._get_llm()
            if llm:
                probe_terms = unverified[:8]  # 限制单次查证数量，避免请求过多
                logger.info(f"开始联网查证 {len(probe_terms)} 个词: {probe_terms}")
                sem = asyncio.Semaphore(3)   # 并发限 3，避免被搜索站点封
                async def guarded(term):
                    async with sem:
                        return await self._verify_via_search(llm, term)
                results = await asyncio.gather(
                    *(guarded(t) for t in probe_terms), return_exceptions=True)
                for r in results:
                    if isinstance(r, dict):
                        items.append(r)
                        verified_count += 1
                    elif isinstance(r, BaseException):
                        logger.warning(f"联网查证异常: {r}")

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
                    "",
                    True   # fuzzy_dedup：与已有梗相似度高的也视为重复
                )
                if result == "already_exists":
                    skipped += 1
                else:
                    imported += 1
            except Exception as e:
                logger.warning(f"入库失败 {it['term']}: {e}")
                skipped += 1
        logger.info(f"热榜采集完成: 新增 {imported}（其中联网查证 {verified_count}）, 跳过重复 {skipped}")
        return {"status": "ok", "imported": imported, "skipped": skipped,
                "verified": verified_count}


async def run_collect_once(db_manager, context=None, config=None):
    """供外部调用的采集入口。"""
    collector = HotTrendCollector(db_manager, context, config)
    return await collector.run_once()
