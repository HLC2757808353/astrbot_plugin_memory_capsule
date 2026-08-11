from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import os
import threading
import asyncio
import json
import time
from datetime import datetime, timedelta

from .security import validate_content, sanitize_content, filter_relationship_content, sanitize_injection_text

_IMPORTANT_KEYWORDS = frozenset(['约定','承诺','重要','记得','提醒','待办'])

@register("memory_capsule", "引灯续昼", "记忆胶囊插件", "v0.24.0", "https://github.com/HLC2757808353/astrbot_plugin_memory_capsule")
class MemoryCapsulePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.db_manager = None
        self.webui_server = None
        self.config = config or {}
        self.webui_host = self.config.get('webui_host', '0.0.0.0')
        self.webui_port = self.config.get('webui_port', 5000)
        self.last_relation_user_id = None
        self.relation_injection_refresh_time = self.config.get('relation_injection_refresh_time', 3600)
        self._relation_cache = None
        self._relation_cache_user_id = None
        self._relation_cache_time = 0
        self._relation_cache_ttl = self.config.get('relation_cache_ttl', 300)
        self._relation_injection_last_time = 0
        self._glossary_cache = None
        self._glossary_cache_time = 0
        self._glossary_cache_ttl = self.config.get('glossary_cache_ttl', 120)

    async def initialize(self):
        self._create_directories()
        persistent_data_dir = self._get_persistent_data_dir()
        from .databases.db_manager import DatabaseManager
        self.db_manager = DatabaseManager(self.config, self.context)
        self.db_manager.initialize(persistent_data_dir)
        from . import set_global_manager
        set_global_manager(self.db_manager)
        self._start_webui()

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
        if self.webui_server:
            try: self.webui_server.stop()
            except Exception: pass
            self.webui_server = None
        if self.db_manager:
            try: self.db_manager.close()
            except Exception: pass
            self.db_manager = None
        self.last_relation_user_id = None

    # ==================== AI 可用工具 ====================

    @filter.llm_tool(name="update_relationship")
    async def update_relationship(self, event, user_id, relation_type=None, summary=None, nickname=None, first_met_location=None, notes=None):
        """
        记录或更新对方的档案。当用户透露了任何关于人的信息（称呼、关系、印象、约定）时调用。
        至少要填 user_id。其他有就填，没有就不用填。

        Args:
            user_id(str): 对方ID，必填
            nickname(str): 对方的称呼或名字
            relation_type(str): 关系，如：朋友、同事、家人、同学
            summary(str): 印象总结，如：性格开朗、喜欢猫咪
            notes(str): 约定或备注，如：每周五一起打球
            first_met_location(str): 初次见面的群组或地点
        Returns:
            操作结果
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
                str(notes) if notes else None
            )
        except Exception as e:
            return f"失败: {e}"

    @filter.llm_tool(name="write_memory")
    async def write_memory(self, event, content, importance=None):
        """
        记录一条信息到长期记忆。当用户提到重要内容、知识、约定、待办时调用。

        Args:
            content(str): 要记住的内容
            importance(int): 重要性 1-10，不填自动判断。10=极重要如密码/核心约定，5=普通笔记，1=闲聊
        Returns:
            操作结果
        """
        if not self.config.get('memory_palace', True):
            return "记忆宫殿已关闭"
        content = str(content)

        is_valid, reason = validate_content(content)
        if not is_valid:
            logger.warning(f"Memory blocked: {reason}")
            return "内容被安全过滤拦截"

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
                            prompt=f'把以下内容分到最合适的类别：[{",".join(categories)}]\n内容:{content}\n只返回JSON:{{"category":"类别名"}}',
                            system_prompt="只返回JSON"
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
            return f"失败: {e}"

    @filter.llm_tool(name="search_memory")
    async def search_memory(self, event, query, category_filter=None, limit=None, tags=None):
        """
        搜索记忆。用多个相关词一起搜效果更好。例如搜"吃饭口味"不如搜"吃饭 口味 喜欢 爱吃的"。
        除了精确匹配的结果，还会额外返回几条不完全相关但可能有用的"联想记忆"（associative=true）。

        Args:
            query(str): 搜索关键词，多个词用空格隔开，越多越准
            category_filter(str): 按分类筛选，可选
            limit(int): 返回条数，默认5
            tags(str): 按标签筛选，多个标签逗号分隔
        Returns:
            搜索结果JSON，含 results 与 related 两个数组
        """
        if not self.config.get('memory_palace', True):
            return '{"results":[],"related":[]}'
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
            # 联想记忆：不完全相关但可能有用的
            related = []
            try:
                exclude_ids = [r['id'] for r in results if r.get('id')]
                rel_limit = self.config.get('search_related_limit', 3)
                if rel_limit > 0:
                    related = await asyncio.to_thread(
                        self.db_manager.search_memory_related,
                        str(query), exclude_ids, int(rel_limit)
                    )
            except Exception:
                related = []
            return json.dumps({"results": results, "related": related}, ensure_ascii=False)
        except Exception:
            return '{"results":[],"related":[]}'

    @filter.llm_tool(name="get_all_memories")
    async def get_all_memories(self, event, limit=50, min_importance=None, category=None):
        """
        一次性批量取出笔记/记忆，用于集中查看、整理或搬运大量内容。当需要回顾多个记忆、做总结、
        或用户要求"把所有记录给我看看"时使用。注意此工具会返回较多内容，请合理设置 limit。

        Args:
            limit(int): 返回条数，默认50，最大200
            min_importance(int): 只取重要性 >= 此值的记忆，可选
            category(str): 只取某分类，可选
        Returns:
            记忆列表JSON
        """
        if not self.config.get('memory_palace', True):
            return '{"memories":[]}'
        try:
            limit = max(1, min(int(limit), 200))
            memories = await asyncio.to_thread(
                self.db_manager.get_all_memories, limit, 0, category
            )
            if min_importance is not None:
                mi = int(min_importance)
                memories = [m for m in memories if (m.get('importance') or 0) >= mi]
            return json.dumps({"memories": memories}, ensure_ascii=False)
        except Exception:
            return '{"memories":[]}'

    @filter.llm_tool(name="delete_memory")
    async def delete_memory(self, event, memory_id):
        """
        删除一条记忆

        Args:
            memory_id(int): 记忆ID
        Returns:
            操作结果
        """
        try:
            return await asyncio.to_thread(self.db_manager.delete_memory, int(memory_id))
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
            return json.dumps({"results": results}, ensure_ascii=False)
        except Exception:
            return '{"results":[]}'

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

    # ==================== 关系被动注入 ====================

    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get('auto_inject_enabled', True):
            return req
        try:
            user_id = event.get_sender_id()
            current_time = time.time()

            await asyncio.to_thread(self.db_manager.auto_update_last_interaction, user_id)

            injection_parts = []

            # ---------- 梗库匹配注入（每次消息独立检查，不受关系注入节流影响） ----------
            try:
                user_message = event.message_str or ""
                if user_message.strip():
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
            except Exception as e:
                logger.error(f"梗匹配注入失败: {e}")

            # ---------- 关系注入（按原有节流逻辑） ----------
            should_inject = False
            if self.relation_injection_refresh_time == -1:
                should_inject = True
            elif user_id != self.last_relation_user_id:
                should_inject = True
            elif current_time - self._relation_injection_last_time >= self.relation_injection_refresh_time:
                should_inject = True

            if should_inject:
                if (self._relation_cache is not None and
                    self._relation_cache_user_id == user_id and
                    current_time - self._relation_cache_time < self._relation_cache_ttl):
                    user_relation = self._relation_cache
                else:
                    user_relation = await asyncio.to_thread(self.db_manager.get_relationship_with_identity, user_id)
                    self._relation_cache = user_relation
                    self._relation_cache_user_id = user_id
                    self._relation_cache_time = current_time

                current_group = ""
                try: current_group = event.get_group_id() or ""
                except Exception: pass

                if user_relation:
                    relation_xml = self._build_relation_xml(user_relation, current_group)
                else:
                    sender_name = ""
                    try: sender_name = event.get_sender_name() or ""
                    except Exception: pass
                    if sender_name:
                        relation_xml = f"<relationship>ID={user_id}, 名称={sender_name}, 该对象暂未存入档案</relationship>"
                    else:
                        relation_xml = f"<relationship>ID={user_id}, 该对象暂未存入档案</relationship>"

                self._relation_injection_last_time = current_time
                self.last_relation_user_id = user_id

                injection_parts.append(
                    "<对当前对话对象的了解>\n"
                    "以下是关于正在和你聊天的这个人的信息，是你之前和TA相处时记下的印象。请自然地融入对话中，不要像报档案一样逐条念出来。\n"
                    f"{relation_xml}\n"
                    "</对当前对话对象的了解>"
                )

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

    def _match_glossary(self, text):
        """匹配用户消息中出现的梗词，返回命中的梗列表。"""
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
                    self.db_manager.increment_glossary_hit(g['id'])
                except Exception:
                    pass
        return hits[:10]

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

    def _build_relation_xml(self, relation, current_group=""):
        user_id = relation.get('user_id') or ''
        nickname = relation.get('nickname') or ''
        relation_type = relation.get('relation_type') or ''
        summary = relation.get('summary') or ''
        notes = relation.get('notes') or ''
        first_met = relation.get('first_met_location') or ''
        last_interaction = relation.get('last_interaction') or ''

        time_offset = self.config.get('time_offset', 8)

        parts = []
        if user_id:
            parts.append(f'ID={user_id}')
        if nickname:
            parts.append(f'称呼={nickname}')
        if relation_type:
            parts.append(f'关系={relation_type}')
        if summary:
            if len(summary) > 120: summary = summary[:117] + "..."
            parts.append(f'印象={summary}')
        if notes:
            if len(notes) > 120: notes = notes[:117] + "..."
            parts.append(f'备注={notes}')
        if first_met:
            parts.append(f'初识于={first_met}')
        if last_interaction:
            try:
                iso_time = str(last_interaction)
                if len(iso_time) > 19:
                    iso_time = iso_time[:19]
                dt = datetime.fromisoformat(iso_time)
                dt = dt + timedelta(hours=time_offset)
                relative = self._format_relative_time(dt)
                parts.append(f'上次互动={relative}')
            except Exception:
                pass
        if parts:
            return f"<relationship>对方: {', '.join(parts)}</relationship>"
        else:
            return f"<relationship>对方: ID={user_id}, 已记录但暂无详细信息</relationship>"