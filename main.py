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

@register("memory_capsule", "引灯续昼", "记忆胶囊插件", "v0.14.0", "https://github.com/HLC2757808353/astrbot_plugin_memory_capsule")
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

    async def initialize(self):
        self._create_directories()
        from .databases.db_manager import DatabaseManager
        self.db_manager = DatabaseManager(self.config, self.context)
        self.db_manager.initialize()
        from . import set_global_manager
        set_global_manager(self.db_manager)
        self._start_webui()

    def _create_directories(self):
        for d in ["databases", "webui/templates", "webui/static", "data"]:
            os.makedirs(os.path.join(os.path.dirname(__file__), d.replace('/', os.sep)), exist_ok=True)

    def _start_webui(self):
        data_dir = os.path.join(os.path.dirname(__file__), "data")
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
        Record interpersonal relationship (impression/promise/habit). For objective knowledge use write_memory

        Args:
            user_id(str): User ID
            relation_type(str): Relationship type
            summary(str): Impression summary
            nickname(str): Nickname
            first_met_location(str): First meeting location
            known_contexts(str): Shared groups (append, comma separated)
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
    async def write_memory(self, event, content):
        """
        Record objective info (knowledge/notes/URLs). For personal impressions use update_relationship

        Args:
            content(str): Content to remember
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

        category = None
        importance = 5
        category_model = self.config.get('category_model', '')
        if category_model and self.context:
            try:
                categories = await asyncio.to_thread(self.db_manager.get_memory_categories)
                if categories:
                    provider = self.context.get_provider_by_id(category_model)
                    if provider:
                        resp = await provider.text_chat(
                            prompt=f'Analyze content, pick category from [{",".join(categories)}], rate importance 1-10.\nContent:{content}\nReturn JSON:{{"category":"cat","importance":num}}',
                            system_prompt="Strict JSON format"
                        )
                        if resp and resp.completion_text:
                            r = json.loads(resp.completion_text.strip())
                            if r.get('category') in categories:
                                category = r['category']
                            if r.get('importance'):
                                importance = max(1, min(10, int(r['importance'])))
            except Exception:
                pass
        try:
            return await asyncio.to_thread(self.db_manager.write_memory, content, category, importance=importance)
        except Exception as e:
            return f"Failed: {e}"

    @filter.llm_tool(name="search_memory")
    async def search_memory(self, event, query, category_filter=None, limit=None):
        """
        Search memories

        Args:
            query(str): Keywords
            category_filter(str): Category (optional)
            limit(int): Count (optional)
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
            return json.dumps({"results": results}, ensure_ascii=False)
        except Exception:
            return '{"results":[]}'

    @filter.llm_tool(name="delete_memory")
    async def delete_memory(self, event, memory_id):
        """
        Delete a memory

        Args:
            memory_id(int): Memory ID
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
        Get all relationships (ID and nickname only). For details use search_relationship

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
        Search relationship info (ID/nickname/type)

        Args:
            query(str): Keywords
            limit(int): Count, default 3
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
        Delete a relationship

        Args:
            user_id(str): User ID
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
        Add a knowledge triple (subject-predicate-object) to the knowledge graph. Use to record factual relationships between entities

        Args:
            subject(str): Subject entity
            predicate(str): Relationship/predicate
            obj(str): Object entity
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
        Search knowledge graph for triples related to an entity. Returns connected facts via graph traversal

        Args:
            entity(str): Entity to search
            depth(int): Traversal depth, default 1 (max 2)
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

    @filter.llm_tool(name="dream")
    async def dream(self, event):
        """
        Enter dream mode: review today's memories and generate insights. Call at night to consolidate the day's experiences into deeper understanding

        Returns:
            dict
        """
        if not self.config.get('dream_mode_enabled', True):
            return '{"status":"disabled"}'
        try:
            candidates = await asyncio.to_thread(self.db_manager.get_dream_candidates, 24, 20)
            if not candidates:
                return '{"status":"no_candidates","memories":[]}'
            return json.dumps({
                "status": "dreaming",
                "memories": candidates,
                "instruction": "Review these memories, find patterns and insights, then call save_dream to save your reflections"
            }, ensure_ascii=False)
        except Exception as e:
            return f'{{"status":"error","message":"{e}"}}'

    @filter.llm_tool(name="save_dream")
    async def save_dream(self, event, summary, insights):
        """
        Save dream insights after reviewing memories in dream mode

        Args:
            summary(str): Brief summary of the dream review
            insights(str): Key insights and patterns discovered
        Returns:
            str
        """
        try:
            from datetime import date
            today = date.today().isoformat()
            candidates = await asyncio.to_thread(self.db_manager.get_dream_candidates, 24, 20)
            return await asyncio.to_thread(
                self.db_manager.save_dream_log,
                today, str(summary), len(candidates), str(insights)
            )
        except Exception as e:
            return f"Failed: {e}"

    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get('auto_inject_enabled', True):
            return req
        try:
            user_id = event.get_sender_id()
            user_message = event.message_str or ""
            current_time = time.time()

            should_inject = False
            if self.relation_injection_refresh_time == -1:
                should_inject = True
            elif user_id != self.last_relation_user_id:
                should_inject = True
            elif "injection_last" in self.relation_injection_cache:
                if current_time - self.relation_injection_cache["injection_last"] >= self.relation_injection_refresh_time:
                    should_inject = True
            else:
                should_inject = True

            if not should_inject:
                return req

            parts = []

            user_relation = await asyncio.to_thread(self.db_manager.get_relationship_with_identity, user_id)
            current_group = ""
            try: current_group = event.get_group_id() or ""
            except Exception: pass

            if user_relation:
                parts.append(self._build_relation_xml(user_relation, current_group))
            else:
                parts.append(f"<relationship>Partner: ID={user_id}, first meeting</relationship>")

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

            self.relation_injection_cache["injection_last"] = current_time
            self.last_relation_user_id = user_id
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
