from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import os
import threading
import asyncio
import json
import time

from .security import validate_content, sanitize_content, filter_relationship_content

_IMPORTANT_KEYWORDS = frozenset(['约定','承诺','重要','记得','提醒','待办'])

@register("memory_capsule", "引灯续昼", "记忆胶囊插件", "v0.23.0", "https://github.com/HLC2757808353/astrbot_plugin_memory_capsule")
class MemoryCapsulePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.db_manager = None
        self.webui_server = None
        self.config = config or {}
        self.webui_host = self.config.get('webui_host', '0.0.0.0')
        self.webui_port = self.config.get('webui_port', 5000)
        self.relation_injection_cache = {}
        self.last_relation_user_id = None
        self.relation_injection_refresh_time = self.config.get('relation_injection_refresh_time', 3600)
        self._last_injection_text = ""
        self._last_injection_time = 0
        self._injection_dedup_ttl = 30
        self._wm_cache = None
        self._wm_cache_time = 0
        self._wm_cache_ttl = 60
        self._last_decay_time = 0
        self._decay_interval = 86400
        self._auto_summary_task = None
        self._auto_summary_interval = self.config.get('auto_summary_interval_hours', 6) * 3600

    async def initialize(self):
        self._create_directories()
        persistent_data_dir = self._get_persistent_data_dir()
        from .databases.db_manager import DatabaseManager
        self.db_manager = DatabaseManager(self.config, self.context)
        self.db_manager.initialize(persistent_data_dir)
        from . import set_global_manager
        set_global_manager(self.db_manager)
        self._start_webui()
        if self.config.get('auto_summary_enabled', True):
            self._auto_summary_task = asyncio.create_task(self._auto_summary_loop())

    async def _auto_summary_loop(self):
        await asyncio.sleep(300)
        while True:
            try:
                interval = self.config.get('auto_summary_interval_hours', 6) * 3600
                await asyncio.sleep(interval)
                logger.info("定时自动总结开始...")
                await asyncio.to_thread(self.db_manager.generate_all_groups_daily_summaries, 24)
                await asyncio.to_thread(self.db_manager.apply_memory_decay)
                await asyncio.to_thread(self.db_manager.cleanup_old_conversation_logs)
                await asyncio.to_thread(self.db_manager.cleanup_old_daily_summaries)
                logger.info("定时自动总结完成")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时自动总结出错: {e}")
                await asyncio.sleep(3600)

    def _create_directories(self):
        for d in ["databases", "webui/templates", "webui/static"]:
            os.makedirs(os.path.join(os.path.dirname(__file__), d.replace('/', os.sep)), exist_ok=True)

    def _get_persistent_data_dir(self):
        try:
            from astrbot.api.star import StarTools
            return str(StarTools.get_data_dir("memory_capsule"))
        except Exception:
            return os.path.join(os.path.dirname(__file__), "data")

    def _start_webui(self):
        data_dir = self._get_persistent_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        try:
            from .webui.auth import AuthManager
            auth_manager = AuthManager(data_dir)
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
                data_dir=data_dir, existing_auth=auth_manager
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
        if self._auto_summary_task:
            self._auto_summary_task.cancel()
            self._auto_summary_task = None
        if self.webui_server:
            try: self.webui_server.stop()
            except Exception: pass
            self.webui_server = None
        if self.db_manager:
            try: self.db_manager.close()
            except Exception: pass
            self.db_manager = None
        self.relation_injection_cache.clear()
        self.last_relation_user_id = None

    @filter.llm_tool(name="update_relationship")
    async def update_relationship(self, event, user_id, relation_type=None, summary=None, nickname=None, first_met_location=None, known_contexts=None):
        """
        记录或更新与某人的关系信息（印象、约定、习惯等）。当用户提到关于人的信息时使用此工具。客观知识用write_memory。

        Args:
            user_id(str): 用户ID
            relation_type(str): 关系类型（如朋友、老师、同事等）
            summary(str): 对此人的印象总结
            nickname(str): 昵称
            first_met_location(str): 初次见面地点
            known_contexts(str): 共同所在的群组（逗号分隔，会追加不覆盖）
        Returns:
            str
        """
        try:
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

            if current_group and not known_contexts:
                known_contexts = current_group
            elif current_group and known_contexts:
                groups = [g.strip() for g in known_contexts.split(',') if g.strip()]
                if current_group not in groups:
                    groups.append(current_group)
                known_contexts = ','.join(groups)

            if not first_met_location and current_group:
                existing = await asyncio.to_thread(
                    self.db_manager.get_relationship_by_user_id, str(user_id)
                )
                if not existing:
                    first_met_location = current_group

            return await asyncio.to_thread(
                self.db_manager.update_relationship_enhanced,
                str(user_id),
                relation_type,
                summary,
                nickname,
                str(first_met_location) if first_met_location else None,
                str(known_contexts) if known_contexts else None
            )
        except Exception as e:
            return f"Failed: {e}"

    @filter.llm_tool(name="write_memory")
    async def write_memory(self, event, content, importance=None):
        """
        记录信息到长期记忆。AI只需传入内容，可选传入重要性（1-10，不传则自动评估）。当用户提到重要信息、知识、约定、待办时必须使用此工具。关于人的印象用update_relationship。

        Args:
            content(str): 要记住的内容
            importance(int): 重要性1-10（可选，不传则自动判断。10=核心身份/约定/密码等极重要信息，7-9=重要偏好/知识，4-6=普通笔记/记录，1-3=闲聊参数）
        Returns:
            str
        """
        if not self.config.get('memory_palace', True):
            return "Memory palace disabled"
        content = str(content)

        is_valid, reason = validate_content(content)
        if not is_valid:
            logger.warning(f"Memory blocked: {reason}")
            return "Content blocked (security filter)"

        content = sanitize_content(content)

        if importance is not None:
            importance = max(1, min(10, int(importance)))
        else:
            importance = 5
            for kw in _IMPORTANT_KEYWORDS:
                if kw in content:
                    importance = 7
                    break

        category = None
        category_model = self.config.get('category_model', '')
        if category_model and self.context:
            try:
                categories = await asyncio.to_thread(self.db_manager.get_memory_categories)
                if categories:
                    provider = self.context.get_provider_by_id(category_model)
                    if provider:
                        resp = await provider.text_chat(
                            prompt=f'Analyze content, pick category from [{",".join(categories)}].\nContent:{content}\nReturn JSON:{{"category":"cat"}}',
                            system_prompt="Strict JSON format"
                        )
                        if resp and resp.completion_text:
                            r = json.loads(resp.completion_text.strip())
                            if r.get('category') in categories:
                                category = r['category']
            except Exception:
                pass
        try:
            return await asyncio.to_thread(self.db_manager.write_memory, content, category, importance=importance)
        except Exception as e:
            return f"Failed: {e}"

    @filter.llm_tool(name="search_memory")
    async def search_memory(self, event, query, category_filter=None, limit=None, tags=None):
        """
        搜索记忆。支持关键词、分类、标签多种方式查询。当需要回忆之前记录的信息时使用此工具。
        重要：query参数请尽量丰富！用多个同义词/相关词一起搜索效果更好。
        例如：用户说"我喜欢编程"→搜索query填"编程 写代码 开发 程序"而不是只填"编程"。

        Args:
            query(str): 搜索关键词（必填，支持多个词空格分隔，越多越准。请包含同义词和相关词）
            category_filter(str): 按分类筛选（可选，如：技术笔记、生活记录、dream等）
            limit(int): 返回条数（可选，默认5）
            tags(str): 按标签筛选（可选，逗号分隔多个标签）
        Returns:
            dict
        """
        if not self.config.get('memory_palace', True):
            return '{"results":[]}'
        try:
            results = await asyncio.to_thread(
                self.db_manager.search_memory, str(query),
                str(category_filter) if category_filter else None,
                int(limit) if limit else None
            )
            if tags:
                tag_list = [t.strip() for t in str(tags).split(',') if t.strip()]
                if tag_list:
                    filtered = []
                    for r in results:
                        r_tags = set(t.strip() for t in r.get('tags', '').split(',') if t.strip())
                        if any(t in r_tags for t in tag_list):
                            filtered.append(r)
                    results = filtered
            return json.dumps({"results": results}, ensure_ascii=False)
        except Exception:
            return '{"results":[]}'

    @filter.llm_tool(name="delete_memory")
    async def delete_memory(self, event, memory_id):
        """
        删除一条记忆

        Args:
            memory_id(int): 记忆ID
        Returns:
            str
        """
        try:
            return await asyncio.to_thread(self.db_manager.delete_memory, int(memory_id))
        except Exception as e:
            return f"Failed: {e}"

    @filter.llm_tool(name="get_all_relationships")
    async def get_all_relationships(self, event):
        """
        获取所有关系列表（仅ID和昵称）。详细信息用search_relationship。

        Returns:
            dict
        """
        try:
            results = await asyncio.to_thread(self.db_manager.get_all_relationships)
            return json.dumps(
                {"relationships": [{"user_id": r["user_id"], "nickname": r.get("nickname") or "Unknown"} for r in results]},
                ensure_ascii=False
            )
        except Exception:
            return '{"relationships":[]}'

    @filter.llm_tool(name="search_relationship")
    async def search_relationship(self, event, query, limit=3):
        """
        搜索关系信息（ID、昵称、关系类型等）

        Args:
            query(str): 搜索关键词
            limit(int): 返回条数，默认3
        Returns:
            dict
        """
        try:
            results = await asyncio.to_thread(self.db_manager.search_relationship, str(query), int(limit))
            return json.dumps({"results": results}, ensure_ascii=False)
        except Exception:
            return '{"results":[]}'

    @filter.llm_tool(name="delete_relationship")
    async def delete_relationship(self, event, user_id):
        """
        删除一条关系记录

        Args:
            user_id(str): 用户ID
        Returns:
            str
        """
        try:
            return await asyncio.to_thread(self.db_manager.delete_relationship, str(user_id))
        except Exception as e:
            return f"Failed: {e}"

    @filter.llm_tool(name="add_knowledge")
    async def add_knowledge(self, event, subject, predicate, obj):
        """
        添加知识三元组（主体-谓词-客体）到知识图谱。用于记录实体之间的客观关系。

        Args:
            subject(str): 主体实体
            predicate(str): 关系/谓词
            obj(str): 客体实体
        Returns:
            str
        """
        try:
            return await asyncio.to_thread(
                self.db_manager.add_triple,
                str(subject), str(predicate), str(obj)
            )
        except Exception as e:
            return f"Failed: {e}"

    @filter.llm_tool(name="search_knowledge")
    async def search_knowledge(self, event, entity, depth=1):
        """
        搜索知识图谱中与某实体相关的三元组。通过图遍历获取关联知识。

        Args:
            entity(str): 要搜索的实体
            depth(int): 遍历深度，默认1（最大2）
        Returns:
            dict
        """
        try:
            depth = min(int(depth), 2)
            results = await asyncio.to_thread(
                self.db_manager.get_related_triples, str(entity), depth
            )
            if not results:
                return '{"triples":[]}'
            return json.dumps({"triples": results}, ensure_ascii=False)
        except Exception:
            return '{"triples":[]}'

    @filter.llm_tool(name="daily_summary")
    async def daily_summary(self, event):
        """
        自动生成今日对话的每日摘要。一键调用，系统自动对本群对话进行抽取式摘要+关键词提取，零LLM消耗。在一天结束时调用此工具来压缩今天的记忆。

        Returns:
            dict
        """
        try:
            group_id = ""
            try: group_id = event.get_group_id() or ""
            except Exception: pass

            whitelist = self.config.get('daily_summary_group_whitelist', [])
            if whitelist and group_id and group_id not in whitelist:
                return json.dumps({"status": "skipped", "hint": "此群不在每日总结白名单中"})

            result = await asyncio.to_thread(
                self.db_manager.generate_group_daily_summary, group_id, 24
            )
            if result:
                return json.dumps({
                    "status": "ok",
                    "summary": result['summary'],
                    "key_topics": result['key_topics'],
                    "message_count": result['message_count'],
                    "active_users": result['active_users']
                }, ensure_ascii=False)
            return json.dumps({"status": "no_data", "hint": "今天没有对话记录"})
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)[:100]})

    @filter.llm_tool(name="get_daily_summary")
    async def get_daily_summary(self, event, days=3):
        """
        获取最近N天的每日总结。用于快速回顾近期对话背景。

        Args:
            days(int): 获取最近几天的总结，默认3
        Returns:
            dict
        """
        try:
            group_id = ""
            try: group_id = event.get_group_id() or ""
            except Exception: pass
            results = await asyncio.to_thread(
                self.db_manager.get_daily_summaries, int(days), group_id
            )
            return json.dumps({"summaries": results}, ensure_ascii=False)
        except Exception:
            return '{"summaries":[]}'

    @filter.llm_tool(name="get_daily_digest")
    async def get_daily_digest(self, event, days=3):
        """
        获取最近N天的全局日报（跨群汇总）。包含所有群的合并话题、总消息数、活跃用户。用于了解整体动态。

        Args:
            days(int): 获取最近几天的全局日报，默认3
        Returns:
            dict
        """
        try:
            results = await asyncio.to_thread(
                self.db_manager.get_global_daily_digest, int(days)
            )
            return json.dumps({"digests": results}, ensure_ascii=False)
        except Exception:
            return '{"digests":[]}'

    @filter.llm_tool(name="dream")
    async def dream(self, event):
        """
        进入梦境模式。返回今天所有群的完整对话数据，供你自由生成梦境叙事。
        你需要做的事：阅读所有数据→发现跨群模式/隐藏关联/遗漏的重要信息→生成一段有洞察的梦境叙事。
        叙事完成后调用save_dream保存。后台会自动执行每日总结、记忆衰减、日志清理等机械操作，你不需要手动调用这些。

        梦境是全局的（跨所有群），不是一个群的。
        就像人睡觉时大脑会整合一天所有经历——你在A群聊的技术、B群聊的生活、C群聊的游戏都会在梦中交织。

        Returns:
            dict
        """
        if not self.config.get('dream_mode_enabled', True):
            return json.dumps({"status": "disabled"})
        try:
            materials = await asyncio.to_thread(self.db_manager.get_dream_materials, 24)
            conv_count = materials.get('conversation_count', 0)
            mem_count = len(materials.get('memories', []))
            if conv_count == 0 and mem_count == 0:
                return json.dumps({"status": "no_data"})

            grouped_convs = {}
            for c in materials.get('conversations', []):
                gid = c.get('group_id', '') or '私聊'
                if gid not in grouped_convs:
                    grouped_convs[gid] = []
                role_label = '用户' if c['role'] == 'user' else 'AI'
                grouped_convs[gid].append(f"[{role_label}] {c['content']}")

            conv_dump = []
            for gid, msgs in grouped_convs.items():
                conv_dump.append(f"=== {gid}（{len(msgs)}条）===")
                conv_dump.extend(msgs[:80])

            mem_summary = []
            for m in materials.get('memories', []):
                mem_summary.append(
                    f"[{m.get('category','')}] {m['content']} (重要性{m.get('importance',5)})"
                )

            daily_sums = await asyncio.to_thread(
                self.db_manager.get_daily_summaries, 1
            )

            all_facts = []
            try:
                all_facts = await asyncio.to_thread(
                    self.db_manager.search_auto_facts, '', None, 50
                )
            except Exception:
                pass

            digest_list = await asyncio.to_thread(
                self.db_manager.get_global_daily_digest, 1
            )

            return json.dumps({
                "status": "dreaming",
                "conversations_by_group": '\n'.join(conv_dump[:300]),
                "memories": mem_summary[:20],
                "daily_summaries": [s.get('summary','')[:200] for s in (daily_sums or [])[:5]],
                "auto_facts": all_facts[:30],
                "global_digest": digest_list[0] if digest_list else None,
                "total_conversations": conv_count,
                "total_groups": len(grouped_convs),
                "active_users": materials.get('active_users', []),
                "instruction": (
                    "你是梦境叙事者。上面是今天所有群的完整对话和数据。\n"
                    "请生成一段梦境叙事（200-500字），内容包括：\n"
                    "1. 今天最重要的主题是什么？（跨群提炼）\n"
                    "2. 有哪些跨群的隐藏关联？（同一人在不同群的表现）\n"
                    "3. 有哪些重要但尚未记录的信息？\n"
                    "4. 对未来的洞察或预测\n"
                    "叙事风格：自由、有洞察力，像人类做梦一样可以跳跃联想。"
                    "完成叙事后调用 save_dream(summary=叙事, insights=核心洞察, new_facts_count=新记录的记忆数)。"
                )
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)[:100]})

    @filter.llm_tool(name="save_dream")
    async def save_dream(self, event, summary, insights, new_facts_count=0):
        """
        保存梦境叙事并自动触发记忆巩固。在dream回顾完成、写好了梦境叙事之后调用。
        调用此工具后，系统会自动执行每日总结→记忆衰减→日志清理→相似合并，你不需要手动调用这些机械操作。

        Args:
            summary(str): 你生成的梦境叙事（200-500字，你自由写的梦境回顾）
            insights(str): 核心洞察（发现的模式、关联、遗漏信息，简短总结）
            new_facts_count(int): 梦境中你新记录的记忆条数（你调用了write_memory的次数）
        Returns:
            str
        """
        try:
            from datetime import date
            today = date.today().isoformat()
            materials = await asyncio.to_thread(self.db_manager.get_dream_materials, 24)
            mem_count = len(materials.get('memories', []))
            conv_count = materials.get('conversation_count', 0)

            await asyncio.to_thread(self.db_manager.consolidate_memories, 24)

            if summary and len(summary) > 10:
                dream_content = f"[梦境 {today}] {summary[:300]}"
                await asyncio.to_thread(
                    self.db_manager.write_memory,
                    dream_content, 'dream', 8, 'dream,梦境,洞察', 'dream'
                )

            await asyncio.to_thread(self.db_manager.compress_conversation_logs, 24)
            await asyncio.to_thread(self.db_manager.cleanup_old_conversation_logs)
            await asyncio.to_thread(self.db_manager.cleanup_old_daily_summaries)

            if self.config.get('daily_global_digest_enabled', True):
                await asyncio.to_thread(
                    self.db_manager.generate_all_groups_daily_summaries, 24
                )

            await asyncio.to_thread(self.db_manager.apply_memory_decay)
            await asyncio.to_thread(self.db_manager.merge_similar_memories)

            await asyncio.to_thread(
                self.db_manager.save_dream_log,
                today, str(summary)[:500], mem_count, str(insights)[:300],
                conv_count, int(new_facts_count), 1
            )

            return json.dumps({
                "status": "dream_saved",
                "consolidated": True,
                "summaries_generated": True,
                "decay_applied": True,
                "memory_decay_applied": True,
                "message": "梦境已保存。每日总结、记忆衰减、日志清理已自动完成。"
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)[:100]})

    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get('auto_inject_enabled', True):
            return req
        try:
            user_id = event.get_sender_id()
            user_message = event.message_str or ""
            current_time = time.time()

            if self.config.get('conversation_logging_enabled', True) and user_message.strip():
                try:
                    group_id = ""
                    try: group_id = event.get_group_id() or ""
                    except Exception: pass
                    await asyncio.to_thread(
                        self.db_manager.log_conversation, 'user', user_message, user_id, group_id
                    )
                except Exception:
                    pass

            parts = []

            should_inject_relation = False
            if self.relation_injection_refresh_time == -1:
                should_inject_relation = True
            elif user_id != self.last_relation_user_id:
                should_inject_relation = True
            elif "injection_last" in self.relation_injection_cache:
                if current_time - self.relation_injection_cache["injection_last"] >= self.relation_injection_refresh_time:
                    should_inject_relation = True
            else:
                should_inject_relation = True

            if should_inject_relation:
                user_relation = await asyncio.to_thread(self.db_manager.get_relationship_with_identity, user_id)
                current_group = ""
                try: current_group = event.get_group_id() or ""
                except Exception: pass

                if user_relation:
                    parts.append(self._build_relation_xml(user_relation, current_group))
                else:
                    parts.append(f"<relationship>Partner: ID={user_id}, first meeting</relationship>")

                self.relation_injection_cache["injection_last"] = current_time
                self.last_relation_user_id = user_id

            if self.config.get('memory_palace', True):
                mem_limit = self.config.get('working_memory_limit', 6)
                mem_chars = self.config.get('working_memory_max_chars', 800)
                if (self._wm_cache is None or
                    current_time - self._wm_cache_time >= self._wm_cache_ttl):
                    self._wm_cache = await asyncio.to_thread(
                        self.db_manager.get_working_memories, user_message, mem_limit, mem_chars
                    )
                    self._wm_cache_time = current_time
                if self._wm_cache:
                    parts.append(f"<memory>{'; '.join(m['content'] for m in self._wm_cache)}</memory>")

            if self.config.get('auto_fact_extraction_enabled', True):
                try:
                    user_facts = await asyncio.to_thread(
                        self.db_manager.get_user_facts, user_id, 10
                    )
                    if user_facts:
                        fact_strs = [f"{f['subject']}{f['predicate']}{f['object']}" for f in user_facts[:8]]
                        parts.append(f"<facts>{'; '.join(fact_strs)}</facts>")

                    if self.config.get('cross_group_association_enabled', True):
                        try:
                            current_group = ""
                            try: current_group = event.get_group_id() or ""
                            except Exception: pass
                            if current_group:
                                cross_facts = await asyncio.to_thread(
                                    self.db_manager.get_cross_group_facts, user_id, current_group, 5
                                )
                                if cross_facts:
                                    cross_strs = [f"[{f['group_id']}]{f['subject']}{f['predicate']}{f['object']}" for f in cross_facts[:5]]
                                    parts.append(f"<cross_group_facts>{'; '.join(cross_strs)}</cross_group_facts>")
                        except Exception:
                            pass
                except Exception:
                    pass

            if self.config.get('recent_utterances_enabled', True):
                try:
                    user_msg = req.prompt or ''
                    current_group = ""
                    try: current_group = event.get_group_id() or ""
                    except Exception: pass
                    recent = await asyncio.to_thread(
                        self.db_manager.search_recent_utterances, user_id, user_msg, current_group, 5
                    )
                    if recent:
                        utter_strs = []
                        for r in recent:
                            gid = r.get('group_id', '')
                            time_str = r.get('created_at', '')[:16] if r.get('created_at') else ''
                            utter_strs.append(f"[{gid}]{r['content'][:60]}({time_str})")
                        parts.append(f"<recent_utterances>{'; '.join(utter_strs)}</recent_utterances>")
                except Exception:
                    pass

            if self.config.get('daily_summary_injection_enabled', True):
                try:
                    current_group = ""
                    try: current_group = event.get_group_id() or ""
                    except Exception: pass
                    today_summary = await asyncio.to_thread(
                        self.db_manager.get_today_daily_summary, current_group
                    )
                    if today_summary and today_summary.get('summary'):
                        s = today_summary['summary'][:300]
                        parts.append(f"<daily_summary>{s}</daily_summary>")
                except Exception:
                    pass

            if current_time - self._last_decay_time >= self._decay_interval:
                try:
                    await asyncio.to_thread(self.db_manager.apply_memory_decay)
                    self._last_decay_time = current_time
                except Exception:
                    pass

            if not parts:
                return req

            injection_text = " ".join(parts)

            if (injection_text == self._last_injection_text and
                current_time - self._last_injection_time < self._injection_dedup_ttl):
                return req

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

            self._last_injection_text = injection_text
            self._last_injection_time = current_time

        except Exception as e:
            logger.error(f"Injection failed: {e}")
        return req

    def _build_relation_xml(self, relation, current_group=""):
        nickname = relation.get('nickname') or 'Friend'
        relation_type = relation.get('relation_type') or 'Friend'
        summary = relation.get('summary') or ''
        first_met = relation.get('first_met_location') or ''
        known_contexts = relation.get('known_contexts') or ''

        parts = [f'Partner: nick={nickname}, rel={relation_type}']
        if summary:
            if len(summary) > 120: summary = summary[:117] + "..."
            parts.append(f'impression={summary}')
        if first_met:
            parts.append(f'met_at={first_met}')
        if known_contexts:
            cl = [c.strip() for c in known_contexts.split(',') if c.strip()]
            if len(cl) == 1:
                parts.append(f'group={cl[0]}')
            elif cl:
                if current_group and current_group in known_contexts:
                    oc = len([c for c in cl if c != current_group])
                    parts.append(f'group={current_group}' + (f'(also in {oc} other groups)' if oc else ''))
                else:
                    parts.append(f'groups={", ".join(cl[:3])}')

        if any(kw in summary for kw in _IMPORTANT_KEYWORDS):
            parts.append('note=has important promise')

        return f"<relationship>{', '.join(parts)}</relationship>"
