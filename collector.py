"""自动采集热榜梗/热词，用 LLM 提炼后写入梗库。

数据源（公开热榜，无需登录）：
- B站热门（视频标题）
- B站搜索热词（更偏热词/梗）
- 百度热搜
- 头条热榜

采集策略（质量优先，宁缺毋滥）：
- 低频运行（默认 24h 一次），单轮入库条数有上限，避免 API 费用和噪声
- 形态过滤：整句标题、带感叹号/引号、含新闻事件词、人名产品名一律不进候选
- Stage1：从标题中挑出可能是梗的词/短语，并把【已收录词表】一并给它，
  避免重复提出已有的词或它的同义变体
- Stage2：结构化核验，必须同时给出 usage（怎么用）/origin（出处）/example（例句），
  形似"字面解释"或出处不明的直接降级；判断不是梗的直接丢弃
- 联网查证：核验不过或缺出处的词，去搜索引擎抓真实语料补依据，确认才入库
- 入库前三重去重：词形归一化（含语气尾巴）/变体包含/释义近似
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


# ==================== 候选词形态过滤 ====================

# 标题句里常见、真·梗词里几乎不会出现的标点
_BAD_TERM_CHARS = re.compile(r"[，。！？、；：…“”‘’《》〈〉【】（）()!?,.:;\"'\[\]{}|\\/@#$%^&*+=~`<>]")

# 新闻/资讯类高频词：命中基本说明这是条新闻标题而不是黑话
_NEWS_WORDS = (
    "回应", "通报", "官方", "宣布", "披露", "曝光", "致歉", "道歉", "开庭", "起诉",
    "调查", "警方", "抓获", "事故", "车祸", "死亡", "受伤", "遇难", "确诊", "发布",
    "声明", "记者", "国务院", "法院", "审判", "判决", "查处", "景区", "医院", "学校",
    "发布会", "外交部", "证监会", "暴雨", "地震", "台风", "高考", "中考",
)

# 只用于判重的口语尾巴，不代表语义差异（整活 / 整活啊 / 整活啦）
_TERM_TAIL_PARTICLES = ("啊", "呀", "吧", "呢", "哦", "噢", "喔", "啦", "嘛", "哒", "咯", "嘞", "惹", "诶")


def _term_reject_reason(term: str) -> str | None:
    """判断候选词是否明显不像"梗/黑话"。返回剔除原因，通过则返回 None。"""
    t = (term or "").strip()
    if len(t) < 2:
        return "太短"
    if len(t) > 14:
        return "太长（像整句标题）"
    if _BAD_TERM_CHARS.search(t):
        return "含标题标点"
    if any(w in t for w in _NEWS_WORDS):
        return "含新闻事件词"
    if re.search(r"\d", t) and re.search(r"[年月日点元块岁%美元万亿]", t):
        return "含数字+单位（像资讯）"
    # 纯英文长短语（如 "iPhone 17 Pro Max"）通常是产品名
    if re.fullmatch(r"[\x00-\x7f ]+", t) and len(t) > 14:
        return "纯英文长短语"
    return None


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

    def _compose_item(self, payload: dict, min_conf: float = 0.8):
        """把 LLM 返回的一条结构化结果校验并组装成可入库条目。

        硬性要求（任一不满足就不收）：形态像词、不是重复、释义有场景+出处+例句。
        这正是"释义停留在字面、看着空虚"的治法——只有 usage/origin/example 三样
        都有实料，才算真的知道这个词怎么用。

        Args:
            payload: LLM 返回的单个对象。
            min_conf: 置信度门槛。

        Returns:
            (item, reason)。item 为 None 时 reason 说明剔除原因。
        """
        term = str(payload.get("term", "")).strip()
        if not term:
            return None, "空词"
        reject = _term_reject_reason(term)
        if reject:
            return None, reject
        dup = str(payload.get("duplicate_of", "") or "").strip()
        if dup:
            return None, f"与已收录「{dup}」语义重复"
        try:
            conf = float(payload.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            return None, "置信度不足"
        usage = str(payload.get("usage", "") or "").strip()
        origin = str(payload.get("origin", "") or "").strip()
        example = str(payload.get("example", "") or "").strip()
        if len(usage) < 15:
            return None, "用法/场景说明太短"
        if usage.startswith(("字面", "顾名思义", "从字面上", "字面上")):
            return None, "释义停留在字面"
        vague = ("不详", "未知", "不确定", "无法确定", "不清楚", "不明", "猜测", "大概是", "可能是")
        if not origin or any(v in origin for v in vague):
            return None, "出处不明确"
        if len(example) < 6:
            return None, "缺少例句"
        if example.strip("。！？…~ ") == term:
            return None, "例句是空壳"
        category = str(payload.get("category", "其他梗")).strip()
        if category not in ("谐音梗", "行为梗", "抽象梗", "表情包梗", "其他梗"):
            category = "其他梗"
        usage = usage.rstrip("。.；;")
        return {
            "term": term,
            "meaning": f"{usage}。出处：{origin[:80]}。例：{example[:60]}"[:300],
            "category": category,
            "source": "自动采集",
            "confidence": conf,
        }, None

    async def _llm_extract(self, trends: list[dict], existing_terms: list[str]) -> tuple[list, list]:
        """两阶段提炼：先筛候选梗词，再逐条结构化核验。

        Stage 1 从热榜标题里挑出【可能是梗的词汇/短语】，并把已收录词表一起给它，
                避免重复提出已有的词或它的同义变体。
        Stage 2 逐条核验，要求给出 usage（怎么用）/origin（出处）/example（例句），
                并让它指出与已收录词重复的项。校验不过或没把握的进 unverified，
                交给联网查证用真实语料补出处，而不是直接丢弃。

        Args:
            trends: fetch_trends 的结果。
            existing_terms: 已收录的梗词（供 LLM 判重参考）。

        Returns:
            (items, unverified_terms)。
        """
        llm = await self._get_llm()
        if not llm:
            logger.warning("无可用 LLM，跳过提炼（可手动导入）")
            return [], []

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

        known = "、".join(existing_terms[:120]) if existing_terms else "（暂无）"

        # ---------- Stage 1：从标题中挑候选梗词 ----------
        stage1_prompt = (
            "以下是一批热搜/热榜标题。请找出其中【可能是网络梗、黑话、缩写、流行语】的词或短语。\n\n"
            "合格条件（要同时满足）：\n"
            "- 是词或短语本身，不是整句标题，不带标点感叹号\n"
            "- 有独立含义，被网友反复用来刷屏/玩梗/评论\n"
            "- 单独拿出来网友能心领神会，常见于弹幕和评论区\n\n"
            "严格排除：\n"
            "- 新闻事件叙述（某地发生某事、官方通报、某公司宣布……）\n"
            "- 人名/地名/机构名、作品名或视频标题本身\n"
            "- 只是普通热门话题、没有玩梗性质的词\n"
            "- 与【已收录】里的词重复，或只是它的同义变体（如已有「栓Q」就不要再报「栓Q了」）\n\n"
            "只返回 JSON 数组：[{\"term\": \"候选词\", \"reason\": \"为什么可能是梗，一句话\"}]\n"
            "最多 12 个。宁可少报，不可错报。\n\n"
            f"【已收录】（勿重复）：{known}\n\n标题列表：\n"
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
            if _term_reject_reason(term):
                continue
            cand_terms.append(term)
        cand_terms = list(dict.fromkeys(cand_terms))
        if not cand_terms:
            logger.info("Stage1 未筛出候选梗词")
            return [], []
        logger.info(f"Stage1 筛出 {len(cand_terms)} 个候选梗词")

        # ---------- Stage 2：结构化核验 ----------
        stage2_prompt = (
            "请对下列候选词逐条核验：它是不是真·网络梗/黑话，并给出有据可依的解释。\n\n"
            "每个词返回这些字段：\n"
            "- is_meme: 是否真是网络梗（不是新闻事件、作品名、普通词就填 false）\n"
            "- usage: 20-60 字。它在什么场景用、表达什么情绪/态度、网友具体怎么用它。"
            "必须具体，禁止「字面意思是…」「顾名思义…」这种望文生义\n"
            "- origin: 出处（哪个事件/视频/台词/歌曲/社区/是谁的缩写）。真的不知道就留空，绝不编造\n"
            "- example: 一句有真实语感的例句，像弹幕或评论区原话，6-30 字\n"
            "- duplicate_of: 若它和【已收录】中某个词语义等价，填那个词；否则留空\n"
            "- category: 谐音梗|行为梗|抽象梗|表情包梗|其他梗\n"
            "- confidence: 0-1\n\n"
            "拿不准的词把 confidence 打到 0.2-0.6 并把 origin 留空，不要硬写释义。\n"
            "只返回 JSON 数组，候选词有多少个就返回多少条。\n\n"
            f"【已收录】：{known}\n\n候选词列表：\n"
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
            if term not in cand_terms:
                continue
            parsed_terms.add(term)
            if p.get("is_meme") is False:
                logger.info(f"剔除 [{term}]：判定不是梗")
                continue
            item, reason = self._compose_item(p, min_conf=0.8)
            if item:
                items.append(item)
            else:
                logger.info(f"待查证 [{term}]：{reason}")
                unverified.append(term)
        for t in cand_terms:
            if t not in parsed_terms:
                unverified.append(t)
        unverified = list(dict.fromkeys(unverified))
        logger.info(f"Stage2 直接通过 {len(items)} 条，待查证 {len(unverified)} 条")
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
        """联网查证一个候选词：抓真实语料 → LLM 依据语料补出来源并释义。"""
        snippets = await self._search_snippets(term)
        if not snippets:
            logger.info(f"查证 [{term}]：无搜索结果，跳过")
            return None
        prompt = (
            f"需要查证的词：{term}\n\n"
            "以下是搜索引擎返回的关于该词的真实网页标题与摘要（真实语料）：\n"
            + "\n".join(f"- {s}" for s in snippets)
            + "\n\n请仅依据上述语料判断它是否是真实的网络梗/流行语，并解释清楚。\n"
            "返回字段：\n"
            "- is_meme: 语料显示它在玩梗/社区语境里被广泛使用才为 true；"
            "只是无关词条、无玩梗语境、证据不足则为 false\n"
            "- usage: 20-60 字，说明使用场景和表达的情绪/态度，禁止望文生义\n"
            "- origin: 出处，必须能在语料里找到依据（哪个事件/视频/台词/社区/谁的缩写）；"
            "语料里没有就留空，不要编\n"
            "- example: 一句有真实语感的例句（可参考语料措辞），6-30 字\n"
            "- category: 谐音梗|行为梗|抽象梗|表情包梗|其他梗\n"
            "- confidence: 0-1\n"
            "禁止使用语料之外的猜测，宁可 is_meme=false 也不编造。\n"
            "只返回 JSON 数组：[{...}]"
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
            item, reason = self._compose_item(p, min_conf=0.7)
            if not item:
                logger.info(f"查证 [{term}]：{reason}，剔除")
                return None
            # 语料查证回来的词同样要和已有梗库比对一次
            dup = await asyncio.to_thread(
                self.db_manager.find_similar_glossary, term, item["meaning"])
            if dup:
                logger.info(f"查证 [{term}]：与已有「{dup[0]['term']}」重复，剔除")
                return None
            # 有真实语料支撑的词，质量比纯模型记忆更硬，排序时优先
            item["confidence"] = min(0.99, item.get("confidence", 0) + 0.05)
            logger.info(f"查证 [{term}]：确认为梗并入库")
            return item
        return None

    async def run_once(self) -> dict:
        """执行一次完整采集：抓取 → 两阶段提炼 → 待查证词联网核实 → 去重限量入库。"""
        if not self.config.get("glossary_collect_enabled", True):
            return {"status": "disabled"}
        trends = await self.fetch_trends()
        if not trends:
            return {"status": "no_data", "count": 0}

        existing = await asyncio.to_thread(self.db_manager.get_all_glossary_terms)
        existing_terms = [str(e.get("term") or "") for e in existing if e.get("term")]
        items, unverified = await self._llm_extract(trends, existing_terms)

        # 对没把握的词联网查证：抓真实语料 → 判断 → 能确认则补入
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

        # 单轮内再按"比较骨架"去一次重，按置信度排序后限量，避免一轮刷进一堆
        from .databases.db_manager import canonical_term
        uniq = {}
        for it in items:
            key = canonical_term(it["term"])
            if not key:
                continue
            if key not in uniq or it.get("confidence", 0) > uniq[key].get("confidence", 0):
                uniq[key] = it
        max_per_run = int(self.config.get("glossary_collect_max_per_run", 8) or 8)
        ordered = sorted(uniq.values(), key=lambda x: -x.get("confidence", 0))[:max_per_run]

        imported = 0
        skipped = 0
        for it in ordered:
            try:
                result = await asyncio.to_thread(
                    self.db_manager.add_glossary,
                    it["term"],
                    it["category"],
                    it["meaning"],
                    it.get("source", "自动采集"),
                    "",
                    True   # fuzzy_dedup：词形变体/释义重复也算重复
                )
                if result == "already_exists":
                    skipped += 1
                else:
                    imported += 1
            except Exception as e:
                logger.warning(f"入库失败 {it['term']}: {e}")
                skipped += 1
        logger.info(f"热榜采集完成: 新增 {imported}（其中联网查证 {verified_count}）, "
                    f"跳过重复 {skipped}, 单轮上限 {max_per_run}")
        return {"status": "ok", "imported": imported, "skipped": skipped,
                "verified": verified_count}


async def run_collect_once(db_manager, context=None, config=None):
    """供外部调用的采集入口。"""
    collector = HotTrendCollector(db_manager, context, config)
    return await collector.run_once()
