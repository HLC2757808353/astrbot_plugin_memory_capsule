from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import os
import re
import threading
import asyncio
import json
import time
from datetime import datetime

from .security import (
    filter_relationship_content,
    sanitize_injection_text,
)

_IMPORTANT_KEYWORDS = frozenset(['约定','承诺','重要','记得','提醒','待办'])

# 用户这句话看起来是在"纠正上一条回复"的信号词（用于即时学习）
_CORRECTION_HINTS = re.compile(
    r"(不对|不是这样|说错|答错|不应该|别这样|别这么说|不要这样说|不要这样回|换个说法|换一种说法|"
    r"语气(太|有点|怎么)|太生硬|太机械|太官方|太啰嗦|太长了|太短了|太冷淡|太热情|太油|"
    r"我不喜欢|我不爱|以后别|下次别|记住别|别老是|不要老是|别总是|你说话|你回得|"
    r"能不能别|可以别|不用这么|应该这么|应该这样回)")


@register("memory_capsule", "引灯续昼", "记忆胶囊插件", "v0.35.2", "https://github.com/HLC2757808353/astrbot_plugin_memory_capsule")
class MemoryCapsulePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.db_manager = None
        self.webui_server = None
        self.config = config or {}
        self.webui_host = self.config.get('webui_host', '0.0.0.0')
        self.webui_port = self.config.get('webui_port', 5000)
        self.relation_injection_refresh_time = self.config.get('relation_injection_refresh_time', 3600)
        self._relation_cache = {}
        # 本群最近的发言者，用于给模型"刚才谁在说话"的事实（不落库、不加指令）
        self._recent_speakers = {}
        # 日报：今天跑过没有、每个会话上次注入的时间（用于冷却）
        self._digest_last_date = ""
        self._digest_inject_ts = {}
        self._digest_retry_ts = 0.0
        self._glossary_cache = None
        self._glossary_cache_time = 0
        self._glossary_cache_ttl = self.config.get('glossary_cache_ttl', 120)
        self._collect_task = None
        self._background_task = None

    async def initialize(self):
        self._create_directories()
        persistent_data_dir = self._get_persistent_data_dir()
        from .databases.db_manager import DatabaseManager
        self.db_manager = DatabaseManager(self.config, self.context)
        self.db_manager.initialize(persistent_data_dir)
        from . import set_global_manager
        set_global_manager(self.db_manager)
        try:
            from .embedded_api import register_embedded_apis
            register_embedded_apis(self.context, self.db_manager, self.config)
        except Exception as e:
            logger.error(f"嵌入式 Web API 注册失败（不影响核心功能）: {e}")
        if self.config.get('webui_enabled', False):
            self._start_webui()
        else:
            logger.info("独立 WebUI 已关闭（嵌入式 Pages 已启用，可在 AstrBot Dashboard 插件页访问）")
        if self.config.get('glossary_collect_enabled', True) or self.config.get('digest_enabled', True):
            self._background_task = asyncio.create_task(self._background_loop())
            logger.info("后台调度已启动（热榜采集 + 日报 + 归档共用一条循环）")

    async def _background_loop(self):
        """统一的后台调度：热榜采集、日报、归档各按自己的周期跑，共用一条日志线。

        每条管线各自按周期触发、互不阻塞；任一管线抛异常只影响自己。
        """
        await asyncio.sleep(60)
        last_collect = 0.0
        last_archive = 0.0
        last_digest_archive = 0.0
        while True:
            try:
                now = time.time()
                if self.config.get('memory_archive_enabled', True):
                    if now - last_archive >= 24 * 3600:
                        last_archive = now
                        try:
                            n = await asyncio.to_thread(self.db_manager.archive_stale_memories)
                            if n:
                                logger.info(f"记忆归档：{n} 条长期没用到、重要度一般的记忆已归档")
                            # 念了很多次、却从没被真正用过的记忆：别再复读了
                            n2 = await asyncio.to_thread(
                                self.db_manager.archive_repetitive_memories)
                            if n2:
                                logger.info(f"防复读：{n2} 条被反复注入但从未用到的记忆已归档")
                            # 历史遗留的重复日报记忆（同一条新闻存了好几份）合并掉
                            n3 = await asyncio.to_thread(
                                self.db_manager.dedupe_digest_memories)
                            if n3:
                                logger.info(f"日报记忆去重：{n3} 条重复的已归档")
                        except Exception as e:
                            logger.error(f"记忆归档出错: {e}")
                if self.config.get('glossary_collect_enabled', True):
                    collect_hours = max(1, int(
                        self.config.get('glossary_collect_interval_hours', 72) or 72))
                    if now - last_collect >= collect_hours * 3600:
                        last_collect = now
                        try:
                            from .collector import run_collect_once
                            result = await run_collect_once(
                                self.db_manager, self.context, self.config)
                            logger.info(f"热榜采集结果: {result}")
                        except Exception as e:
                            logger.error(f"热榜采集出错: {e}")
                # 每日日报：只要库里没有今天这一批就补抓（启动晚、昨天失败都能自愈）
                if self.config.get('digest_enabled', True):
                    today_str = datetime.now().strftime('%Y-%m-%d')
                    digest_hour = int(self.config.get('digest_hour', 9) or 9)
                    retry_gap = max(600, int(
                        self.config.get('digest_retry_minutes', 30) or 30) * 60)
                    try:
                        latest_day = await asyncio.to_thread(
                            self.db_manager.get_latest_digest_date)
                    except Exception:
                        latest_day = ''
                    due = (latest_day != today_str
                           and datetime.now().hour >= digest_hour
                           and now - self._digest_retry_ts >= retry_gap)
                    if due:
                        self._digest_retry_ts = now
                        try:
                            from .digest import run_digest_once, resolve_digest_provider
                            provider = resolve_digest_provider(self.context, self.config)
                            result = await run_digest_once(
                                self.db_manager, provider, self.config, "每日")
                            logger.info(f"日报结果: {result}")
                            if isinstance(result, dict) and result.get('status') == 'ok':
                                self._digest_last_date = today_str
                        except Exception as e:
                            logger.error(f"日报出错: {e}")
                    if now - last_digest_archive >= 24 * 3600:
                        last_digest_archive = now
                        try:
                            n = await asyncio.to_thread(
                                self.db_manager.archive_stale_digest,
                                int(self.config.get('digest_keep_days', 21) or 21),
                                int(self.config.get('digest_keep_bonus_days', 35) or 35))
                            if n:
                                logger.info(f"日报归档：{n} 条过期新闻已归档")
                        except Exception as e:
                            logger.error(f"日报归档出错: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"学习调度出错: {e}")
            # 5 分钟一跳：改配置不必等一整个周期才生效
            await asyncio.sleep(300)

    def _create_directories(self):
        for d in ["databases", "webui/templates", "webui/static", "pages"]:
            os.makedirs(os.path.join(os.path.dirname(__file__), d.replace('/', os.sep)), exist_ok=True)

    def _get_persistent_data_dir(self):
        """返回插件独立数据目录（独立 WebUI 与数据库共用的持久目录）。"""
        try:
            from astrbot.api.star import StarTools
            return str(StarTools.get_data_dir("memory_capsule"))
        except Exception:
            return os.path.join(os.path.dirname(__file__), "data")

    def _start_webui(self):
        """启动独立的 WebUI 服务（默认关闭，用 AstrBot 内嵌页面时不会走这里）。"""
        try:
            from .webui.auth import AuthManager
            auth_manager = AuthManager(self._get_persistent_data_dir())
        except Exception:
            auth_manager = None
        try:
            if self.webui_server:
                self.webui_server.stop()
                time.sleep(1)
                self.webui_server = None
            from .webui.server import WebUIServer
            self.webui_server = WebUIServer(
                self.db_manager, host=self.webui_host, port=self.webui_port,
                data_dir=self._get_persistent_data_dir(), existing_auth=auth_manager
            )
            self.webui_server.server_thread = threading.Thread(
                target=self.webui_server.run, daemon=True, name='WebUI'
            )
            self.webui_server.server_thread.start()
            time.sleep(0.5)
            if self.webui_server.running:
                logger.info(f"WebUI: http://{self.webui_host}:{self.webui_port}")
            else:
                logger.error(f"WebUI failed to start on port {self.webui_port}")
        except Exception as e:
            logger.error(f"WebUI start failed: {e}")

    async def terminate(self):
        """卸载插件：停后台任务、关 WebUI、关数据库。"""
        task = getattr(self, '_background_task', None)
        if task:
            try:
                task.cancel()
            except Exception:
                pass
            self._background_task = None
        if self.webui_server:
            try:
                self.webui_server.stop()
            except Exception:
                pass
            self.webui_server = None
        if self.db_manager:
            try:
                self.db_manager.close()
            except Exception:
                pass
            self.db_manager = None
        self._relation_cache.clear()

    # ==================== AI 可用工具 ====================

    @filter.llm_tool(name="update_relationship")
    async def update_relationship(self, event, user_id=None, relation_type=None, summary=None, nickname=None, first_met_location=None, notes=None):
        """
        记录或更新对方的档案。当用户透露了任何关于人的信息（称呼、关系、印象、约定）时调用。
        正在和你说话的这个账号直接省略 user_id 即可；只有替别人建档时才需要显式填 ID。

        Args:
            user_id(str): 对方ID；省略=当前对话者本人（推荐省略，别把昵称当ID填）
            nickname(str): 对方的称呼或名字
            relation_type(str): 关系，如：朋友、同事、家人、同学
            summary(str): 印象总结，如：性格开朗、喜欢猫咪
            notes(str): 约定或备注，如：每周五一起打球
            first_met_location(str): 初次见面的群组或地点
        Returns:
            操作结果
        """
        try:
            # 没给 ID 就记当前对话者：让 AI 自己填 ID 时很容易把昵称当 ID 存进去，
            # 之后按真实 ID 查档案就查不到，于是出现"认错人/像不认识一样"。
            if not user_id:
                user_id = event.get_sender_id()
            user_id = str(user_id)

            relation_type, summary, nickname, warnings = filter_relationship_content(
                str(relation_type) if relation_type else None,
                str(summary) if summary else None,
                str(nickname) if nickname else None
            )
            if warnings:
                logger.warning(f"Relationship content filtered for {user_id}: {warnings}")

            current_group = ""
            try:
                current_group = event.get_group_id() or ""
            except Exception:
                pass

            if not first_met_location and current_group:
                existing = await asyncio.to_thread(
                    self.db_manager.get_relationship_by_user_id, user_id
                )
                if not existing:
                    first_met_location = current_group

            result = await asyncio.to_thread(
                self.db_manager.update_relationship_enhanced,
                user_id,
                relation_type,
                summary,
                nickname,
                str(first_met_location) if first_met_location else None,
                str(notes) if notes else None
            )
            # 档案变了，立刻让缓存失效，否则接下来几分钟还会读到旧档案
            self._relation_cache.pop(user_id, None)
            return result
        except Exception as e:
            return f"失败: {e}"

    # ==================== 笔记本（只写不读） ====================

    @filter.llm_tool(name="write_journal")
    async def write_journal(self, event, content):
        """
        想记点什么的时候用：今天发生的事、答应过的事、此刻的想法。
        写进去的东西只留在笔记本里，不会出现在之后的对话里，也不会被检索到——
        它是写给人看的，不是你的记忆。

        Args:
            content(str): 要记的内容，一两句话
        Returns:
            操作结果
        """
        try:
            text = str(content or '').strip()
            if not text:
                return "内容为空"
            tags = ["日记", datetime.now().strftime("%Y-%m-%d")]
            return await asyncio.to_thread(
                self.db_manager.write_memory,
                text, category="日记", importance=1, tags=tags,
                source="journal", visibility="private", owner_id="")
        except Exception as e:
            return f"失败: {e}"

    @filter.llm_tool(name="get_all_relationships")
    async def get_all_relationships(self, event):
        """
        列出所有已录入的关系（仅ID和昵称）。当前聊天对象的信息已经自动注入，此工具仅用于查看还有哪些人记录在案。

        Returns:
            关系列表JSON
        """
        try:
            results = await asyncio.to_thread(self.db_manager.get_all_relationships)
            return json.dumps(
                {"relationships": [{"user_id": r["user_id"], "nickname": r.get("nickname") or "未记录名称"} for r in results]},
                ensure_ascii=False
            )
        except Exception:
            return '{"relationships":[]}'

    @filter.llm_tool(name="search_relationship")
    async def search_relationship(self, event, query, limit=3):
        """
        查询某人的档案。注意：正在和你聊天的这个人，他的信息已经自动提供给你了，不要再查他。这个工具只用于查询不在场的其他人。

        Args:
            query(str): 要搜的昵称或用户ID（不能是当前聊天对象）
            limit(int): 返回条数，默认3
        Returns:
            档案信息JSON
        """
        try:
            results = await asyncio.to_thread(self.db_manager.search_relationship, str(query), int(limit))
            payload = {"results": results}
            if not results:
                payload["note"] = (
                    "小本本里没有这个人的档案。但这不代表你不认识他/她——"
                    "上下文里可能已有其他记忆模块提供的信息，请先依据上下文回答，"
                    "不要直接说“我没有关于TA的记忆”。"
                )
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            return json.dumps({
                "results": [],
                "note": f"小本本读取异常（{e}），不代表你没有相关记忆。",
            }, ensure_ascii=False)

    @filter.llm_tool(name="delete_relationship")
    async def delete_relationship(self, event, user_id):
        """
        删除一条关系档案

        Args:
            user_id(str): 用户ID
        Returns:
            操作结果
        """
        try:
            return await asyncio.to_thread(self.db_manager.delete_relationship, str(user_id))
        except Exception as e:
            return f"失败: {e}"

    # ==================== 日报查询 ====================

    @filter.llm_tool(name="search_digest")
    async def search_digest(self, event, query, limit=5):
        """
        查最近新闻/热点/二次元资讯。想知道某件事的来龙去脉、或者别人提到一个新东西你不确定时用。

        Args:
            query(str): 关键词，如 "大模型"、"某游戏"、"番剧"
            limit(int): 返回条数，默认5
        Returns:
            匹配到的新闻条目（含标题、摘要、来源、日期）
        """
        try:
            rows = await asyncio.to_thread(
                self.db_manager.search_digest, str(query), int(limit))
            if not rows:
                return json.dumps({"results": [], "note": f"最近没见过关于「{query}」的消息。"},
                                  ensure_ascii=False)
            await asyncio.to_thread(self.db_manager.bump_digest_hits, [r['id'] for r in rows])
            out = [{"date": r.get('digest_date'), "title": r.get('title'),
                    "summary": r.get('summary'), "source": r.get('source'),
                    "url": r.get('url')} for r in rows]
            return json.dumps({"results": out}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"results": [], "error": str(e)}, ensure_ascii=False)

    @filter.llm_tool(name="get_digest")
    async def get_digest(self, event, date=""):
        """
        查看某一天完整的日报（默认今天）。

        Args:
            date(str): 日期，格式 2026-11-03；留空=今天
        Returns:
            当天全部条目的标题与摘要
        """
        try:
            day = str(date or "").strip() or datetime.now().strftime('%Y-%m-%d')
            rows = await asyncio.to_thread(self.db_manager.get_digest_by_date, day)
            if not rows:
                return json.dumps({"date": day, "results": [],
                                   "note": f"{day} 没有日报记录。"}, ensure_ascii=False)
            await asyncio.to_thread(self.db_manager.bump_digest_hits, [r['id'] for r in rows])
            out = [{"title": r.get('title'), "summary": r.get('summary'),
                    "category": r.get('category'), "source": r.get('source'),
                    "url": r.get('url')} for r in rows]
            return json.dumps({"date": day, "results": out}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"date": date, "results": [], "error": str(e)}, ensure_ascii=False)

    # ==================== 关系被动注入 ====================
    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get('auto_inject_enabled', True):
            return req
        try:
            user_id = str(event.get_sender_id() or "")
            try:
                sender_name = event.get_sender_name() or ""
            except Exception:
                sender_name = ""
            try:
                current_group = event.get_group_id() or ""
            except Exception:
                current_group = ""
            is_group = bool(current_group)
            user_message = (event.message_str or "").strip()

            injection_parts = []

            # ---------- 梗库匹配注入（每次消息独立检查） ----------
            if user_message:
                try:
                    glossary_hits = await asyncio.to_thread(self._match_glossary, user_message)
                    if glossary_hits:
                        hit_lines = []
                        for g in glossary_hits:
                            line = f"{g['term']}：{g['meaning']}"
                            if g.get('source'):
                                line += f"（来源：{g['source']}）"
                            hit_lines.append(line)
                        injection_parts.append(
                            "<用户消息中出现了以下梗/黑话，含义如下>\n"
                            + "\n".join(hit_lines)
                            + "\n</梗/黑话释义>"
                        )
                        # 只统计真正注入的那几条（超上限被丢弃的不算"被使用"）
                        try:
                            await asyncio.to_thread(
                                self.db_manager.increment_glossary_hits,
                                [g['id'] for g in glossary_hits])
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"梗匹配注入失败: {e}")

            # ---------- 身份注入：只给"正在和谁说话"这一条事实 ----------
            # 系统提示词每轮都会重建，所以每轮都要给；但只给 ID 和名字。
            # 档案细节（印象/备注/曾用名）不在这里，改成被问到时用工具查——
            # 否则她会把别人的事迹套到当前对话者身上，还会追问"你为什么改名"。
            relation, db_ok = await asyncio.to_thread(self._load_relation_cached, user_id)
            if db_ok:
                scene = f"当前场景={current_group}群聊" if is_group else "当前场景=私聊"
                rel = dict(relation or {})
                rel.setdefault('user_id', user_id)
                injection_parts.append(
                    f"{self._build_relation_xml(rel, current_group, sender_name)}, {scene}"
                )
            else:
                logger.warning(f"关系档案读取失败，本轮跳过身份注入: {user_id}")

            # ---------- 群聊近况：刚才是谁在说话（只给事实，不加任何要求） ----------
            if is_group and self.config.get('group_context_enabled', True):
                try:
                    lines = self._recent_other_lines(current_group, user_id)
                    if lines:
                        injection_parts.append(
                            "<群聊近况>\n" + "\n".join(lines) + "\n</群聊近况>")
                except Exception as e:
                    logger.warning(f"群聊近况注入失败: {e}")

            # ---------- 今日日报目录：只给几条标题，详情靠她自己去查 ----------
            # 只在抓取后的窗口内、且同一会话冷却时间之外注入，避免整天反复灌同样的内容
            if self.config.get('digest_enabled', True):
                try:
                    digest_lines, digest_ids, digest_day = await self._digest_lines_for_injection(
                        event, current_group, user_id)
                    if digest_lines:
                        # 标记清楚这是自动抓来的外部资讯，不是对话内容，
                        # 免得被当成"某个人跟她说过的话"混进上下文
                        injection_parts.append(
                            f'<每日简报 日期="{digest_day}" 来源="系统自动抓取的公开资讯，不是对话内容">\n'
                            + "\n".join(digest_lines)
                            + "\n</每日简报>")
                        await asyncio.to_thread(self.db_manager.bump_digest_hits, digest_ids)
                        logger.info(f"已把 {digest_day} 的简报（{len(digest_lines)} 条）注入上下文")
                except Exception as e:
                    logger.warning(f"日报注入失败: {e}")

            # 更新时间放在取档案之后：先记一次互动再去读，会让"上次互动"永远显示"刚刚"
            try:
                await asyncio.to_thread(self.db_manager.auto_update_last_interaction, user_id)
            except Exception as e:
                logger.error(f"更新最近互动时间失败: {e}")

            # 记下这句是谁说的：下一轮判断"刚才还有谁在说话"要用。
            # 工具循环里同一条消息可能重复进到这里，所以按 人+内容 去重。
            if is_group and user_id and user_message:
                try:
                    buf = self._recent_speakers.setdefault(current_group, [])
                    if not (buf and buf[-1]['uid'] == user_id
                            and buf[-1]['text'] == user_message):
                        buf.append({'ts': time.time(), 'uid': user_id,
                                    'name': sender_name, 'text': user_message})
                        del buf[:-40]
                except Exception:
                    pass

            if not injection_parts:
                return req

            injection_text = "\n\n".join(injection_parts)
            injection_text = sanitize_injection_text(injection_text)

            inject_pos = self.config.get('context_inject_position', 'system_prompt')
            if inject_pos == 'user_prompt':
                req.prompt = injection_text + '\n' + (req.prompt or "")
            elif inject_pos == 'insert_system_prompt':
                if hasattr(req, 'messages') and req.messages:
                    req.messages.insert(0, {'role': 'system', 'content': injection_text})
                else:
                    req.system_prompt = (req.system_prompt or "") + "\n" + injection_text
            else:
                req.system_prompt = (req.system_prompt or "") + "\n" + injection_text

        except Exception as e:
            logger.error(f"注入失败: {e}")
        return req

    def _load_relation_cached(self, user_id):
        """读取当前对话者的关系档案（带 TTL 缓存）。

        关系注入现在是"每轮都注入、查库按间隔刷新"，缓存时长取「关系刷新间隔」与
        「关系缓存」中较小的正值；relation_injection_refresh_time=-1 表示每轮重查。
        （原先一个是"注不注入"一个是"缓存多久"，注入被节流后同一用户一小时内的
        后续消息就彻底没有档案了，这就是"像是失忆"的根因。）

        Args:
            user_id: 当前对话者 ID。

        Returns:
            (relation, ok)。ok=False 表示数据库读失败——此时不能当成"没有档案"，
            调用方应当跳过注入，而不是去扮演一个陌生人的角色。
        """
        refresh = self.config.get('relation_injection_refresh_time', 3600)
        cache_ttl = self.config.get('relation_cache_ttl', 300)
        if refresh == -1:
            ttl = 0
        elif not cache_ttl or cache_ttl <= 0:
            ttl = max(0, refresh)
        elif not refresh or refresh <= 0:
            ttl = max(0, cache_ttl)
        else:
            ttl = min(cache_ttl, refresh)

        now = time.time()
        entry = self._relation_cache.get(user_id)
        if entry is not None and now - entry['ts'] < ttl:
            return entry['rel'], True
        try:
            relation = self.db_manager.get_relationship_checked(user_id)
        except Exception as e:
            logger.error(f"关系档案查询失败 {user_id}: {e}")
            return (entry['rel'] if entry else None), entry is not None
        self._relation_cache[user_id] = {'ts': now, 'rel': relation}
        if len(self._relation_cache) > 500:
            for key in list(self._relation_cache)[:100]:
                self._relation_cache.pop(key, None)
        return relation, True

    def _recent_other_lines(self, group_id, current_uid):
        """取本群最近说过话的**其他人**，每人一条，用来给模型一个"刚才谁在说"的事实。

        按人去重、只回最近几条，所以人再多也不会线性膨胀；不含当前对话者。

        Args:
            group_id: 当前群 ID。
            current_uid: 当前对话者 ID，会被排除。

        Returns:
            list[str]: 形如 "小红(123)：我妈住院了" 的行，按时间从早到晚。
        """
        try:
            want = int(self.config.get('group_context_lines', 3))
        except (TypeError, ValueError):
            want = 3
        want = max(0, min(5, want))
        if want == 0:
            return []
        try:
            window = max(1, int(self.config.get('group_context_window_minutes', 10))) * 60
        except (TypeError, ValueError):
            window = 600
        now = time.time()
        buf = self._recent_speakers.get(group_id) or []
        seen = set()
        picked = []
        for item in reversed(buf):
            if now - item['ts'] > window:
                break
            if item['uid'] == current_uid or item['uid'] in seen:
                continue
            seen.add(item['uid'])
            picked.append(item)
            if len(picked) >= want:
                break
        picked.reverse()
        lines = []
        for item in picked:
            text = ' '.join(str(item['text']).split())
            if len(text) > 20:
                text = text[:20] + '…'
            lines.append(f"{item['name'] or item['uid']}({item['uid']})：{text}")
        return lines

    async def _digest_lines_for_injection(self, event, group_id, user_id):
        """决定这一轮要不要把今天的简报交给她，以及交哪些条目。

        给她的是**整批条目**（标题+一句摘要），不是几条目录——上次只给 3 条标题，
        她就会说"我得自己去查"。

        交付条件是"这批在这个会话里给过没有"，不看钟点：她什么时候开口是不确定的，
        等人来了才给才有用。每批每个会话只给一次，所以不会整天反复念叨同样的内容。

        Args:
            event: 消息事件（取会话标识用）。
            group_id: 当前群 ID，私聊为空。
            user_id: 当前用户 ID。

        Returns:
            tuple[list[str], list[int], str]: 要注入的行、这些条目的 id、这批的日期。
        """
        try:
            session_key = f"{event.get_platform_id()}:{group_id or user_id}"
        except Exception:
            session_key = group_id or user_id or "default"

        rows = await asyncio.to_thread(self.db_manager.get_injectable_digest, limit=30)
        if not rows:
            return [], [], ""

        # 只给最近那一批（同一日期）的条目，免得日期标错、把昨天的当今天的说
        latest_day = str(rows[0].get('digest_date') or '')
        rows = [r for r in rows if str(r.get('digest_date') or '') == latest_day]

        # 这一批已经在这个会话里给过了就不再给
        if self._digest_inject_ts.get(session_key) == latest_day:
            return [], [], ""

        lines = []
        for r in rows:
            title = str(r.get('title') or '').strip()
            if len(title) > 40:
                title = title[:40] + '…'
            summary = ' '.join(str(r.get('summary') or '').split())
            if len(summary) > 45:
                summary = summary[:45] + '…'
            # 每条都带日期：免得她把几天前的新闻当成今天的说
            line = f"· {title}"
            if summary:
                line += f"——{summary}"
            lines.append(line)
        self._digest_inject_ts[session_key] = latest_day
        if len(self._digest_inject_ts) > 500:
            for k in list(self._digest_inject_ts)[:100]:
                self._digest_inject_ts.pop(k, None)
        return lines, [r['id'] for r in rows], latest_day

    def _match_glossary(self, text):
        """匹配用户消息中出现的梗词，返回**真正会被注入**的那几条。

        计数不在这里做：命中但超出注入上限被丢弃的条目不该算"被使用"，
        由调用方对实际注入的条目统一计数。
        """
        current_time = time.time()
        if (self._glossary_cache is None or
            current_time - self._glossary_cache_time >= self._glossary_cache_ttl):
            self._glossary_cache = self.db_manager.get_all_glossary_terms()
            self._glossary_cache_time = current_time
        if not self._glossary_cache:
            return []
        hits = []
        seen = set()
        for g in self._glossary_cache:
            term = str(g.get('term', '')).strip()
            if not term or term in seen:
                continue
            if term in text:
                seen.add(term)
                hits.append(g)
        try:
            limit = int(self.config.get('glossary_inject_limit', 3))
        except (TypeError, ValueError):
            limit = 3
        return hits[:max(0, limit)]

    def _format_relative_time(self, dt):
        now = datetime.now()
        diff = now - dt
        seconds = diff.total_seconds()

        if seconds < 60:
            return "刚刚"
        elif seconds < 3600:
            return f"{int(seconds / 60)}分钟前"
        elif seconds < 86400:
            return f"{int(seconds / 3600)}小时前"
        elif seconds < 604800:
            return f"{int(seconds / 86400)}天前"
        elif seconds < 2592000:
            return f"{int(seconds / 604800)}周前"
        elif seconds < 31536000:
            return f"{int(seconds / 2592000)}个月前"
        else:
            years = int(seconds / 31536000)
            return f"{years}年前"

    def _build_relation_xml(self, relation, current_group="", sender_name=""):
        """只给出"正在和谁说话"这一条事实。

        以前这里会塞进称呼/关系/印象/备注/曾用名/上次互动，结果是她把别人的事迹
        往当前对话者身上套，还会追问"你为什么改名"。细节改成被问到时再查。

        Args:
            relation: 关系档案字典。
            current_group: 当前群 ID（私聊为空）。
            sender_name: 平台这次给出的名字。

        Returns:
            str: 一行身份标记。
        """
        user_id = relation.get('user_id') or ''
        nickname = relation.get('nickname') or sender_name or ''
        parts = []
        if user_id:
            parts.append(f'ID={user_id}')
        if nickname:
            parts.append(f'名字={nickname}')
        return f"<当前对话者 {', '.join(parts)}>"
