from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import os
import threading
import asyncio
import json
import time
from datetime import datetime

from .security import (
    validate_content,
    sanitize_content,
    filter_relationship_content,
    sanitize_injection_text,
    SENSITIVE_KEYWORDS,
)

_IMPORTANT_KEYWORDS = frozenset(['约定','承诺','重要','记得','提醒','待办'])

@register("memory_capsule", "引灯续昼", "记忆胶囊插件", "v0.27.1", "https://github.com/HLC2757808353/astrbot_plugin_memory_capsule")
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
        self._glossary_cache = None
        self._glossary_cache_time = 0
        self._glossary_cache_ttl = self.config.get('glossary_cache_ttl', 120)
        self._collect_task = None

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
        if self.config.get('glossary_collect_enabled', True):
            self._collect_task = asyncio.create_task(self._collect_loop())
            logger.info("热榜采集任务已启动")

    async def _collect_loop(self):
        """低频热榜采集循环。间隔可配置，默认 24h。"""
        # 启动后先等一小段，避免与插件加载其他初始化冲突
        await asyncio.sleep(60)
        while True:
            try:
                from .collector import run_collect_once
                result = await run_collect_once(self.db_manager, self.context, self.config)
                logger.info(f"热榜采集结果: {result}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"热榜采集出错: {e}")
            interval_hours = self.config.get('glossary_collect_interval_hours', 24)
            await asyncio.sleep(max(1, int(interval_hours)) * 3600)

    def _create_directories(self):
        for d in ["databases", "webui/templates", "webui/static", "pages"]:
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
        if self._collect_task:
            try: self._collect_task.cancel()
            except Exception: pass
            self._collect_task = None
        if self.webui_server:
            try: self.webui_server.stop()
            except Exception: pass
            self.webui_server = None
        if self.db_manager:
            try: self.db_manager.close()
            except Exception: pass
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

    @filter.llm_tool(name="write_memory")
    async def write_memory(self, event, content, importance=None, visibility=None):
        """
        记录一条信息到长期记忆。当用户提到重要内容、知识、约定、待办时调用。

        Args:
            content(str): 要记住的内容
            importance(int): 重要性 1-10，不填自动判断。10=极重要如密码/核心约定，5=普通笔记，1=闲聊
            visibility(str): private=只能私下说（隐私、约定、某人的事），public=群里说也没关系。不填由内容自动判断
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

        # 可见性：AI 没明确指定时，命中隐私关键词就默认 private，免得在群里被念出来
        visibility = str(visibility).lower().strip() if visibility else ""
        if visibility not in ('public', 'private'):
            visibility = 'private' if any(kw in content for kw in SENSITIVE_KEYWORDS) else 'public'

        owner_id = ""
        try:
            owner_id = str(event.get_sender_id() or "")
        except Exception:
            pass

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
            return await asyncio.to_thread(
                self.db_manager.write_memory, content, category,
                importance=importance, visibility=visibility, owner_id=owner_id
            )
        except Exception as e:
            return f"失败: {e}"

    @filter.llm_tool(name="search_memory")
    async def search_memory(self, event, query, category_filter=None, limit=None, tags=None):
        """
        在"小本本"（长期笔记）里搜索记忆。用多个相关词一起搜效果更好。
        例如搜"吃饭口味"不如搜"吃饭 口味 喜欢 爱吃的"。

        重要：这是【小本本】的检索，不等于你的全部记忆。当前上下文里可能已经有
        其他记忆模块注入的内容。**搜不到不代表你不知道**——不要因为这里没有结果
        就说"我查不到""我没有记录"，请先依据上下文里已有的信息回答。
        需要回忆具体细节（时间、约定、偏好）时才调用；只想确认"有没有记忆"不必调用。
        结果里的 visibility 表示能不能当众说：private=只在私下提，群里别提。

        Args:
            query(str): 搜索关键词，多个词用空格隔开，越多越准
            category_filter(str): 按分类筛选，可选
            limit(int): 返回条数，默认5
            tags(str): 按标签筛选，多个标签逗号分隔
        Returns:
            搜索结果JSON，含 results / related，及说明性 note（可能还有 fallback）
        """
        if not self.config.get('memory_palace', True):
            return json.dumps({"results": [], "related": [],
                               "note": "记忆宫殿已关闭，小本本本轮不可用。"}, ensure_ascii=False)
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

            payload = {"results": results, "related": related}
            if results or related:
                payload["note"] = (
                    "以上是小本本（长期笔记）里的条目，可能并不完整。"
                    "如果上下文里还有其他来源的记忆，以更具体、更新的为准。"
                )
            else:
                # 一条都没匹配到：绝不能让它变成"我什么都不知道"的证据。
                # 给一点兜底（库中较重要的条目），并明确告诉模型空结果的含义。
                fallback = []
                try:
                    is_group = bool(event.get_group_id()) if event else False
                    recent = await asyncio.to_thread(
                        self.db_manager.get_all_memories, 30, 0, None)
                    for m in recent:
                        if (m.get('importance') or 0) < 7:
                            continue
                        if is_group and m.get('visibility') == 'private':
                            continue
                        fallback.append(m)
                        if len(fallback) >= 3:
                            break
                except Exception:
                    fallback = []
                payload["fallback"] = fallback
                payload["note"] = (
                    "小本本里没有匹配到这条查询。但这不代表你不知道："
                    "当前上下文里可能已经有其他记忆模块注入的相关内容，"
                    "请优先依据上下文回答，不要在回复里说“我查不到/我没有记录”。"
                    + ("下面几条是小本本里较重要的记录，仅供参考，不要硬套到当前话题上。"
                       if fallback else "需要细节的话，换个更具体的关键词再搜一次。")
                )
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            return json.dumps({
                "results": [], "related": [],
                "note": f"小本本读取异常（{e}），这不代表你没有记忆，请依据上下文回答。",
            }, ensure_ascii=False)

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
            return '{"memories":[],"note":"记忆宫殿已关闭。"}'
        try:
            limit = max(1, min(int(limit), 200))
            memories = await asyncio.to_thread(
                self.db_manager.get_all_memories, limit, 0, category
            )
            if min_importance is not None:
                mi = int(min_importance)
                memories = [m for m in memories if (m.get('importance') or 0) >= mi]
            return json.dumps({
                "memories": memories,
                "note": ("这里只是小本本（长期笔记）的内容，不代表你的全部记忆；"
                         "上下文里可能还有其他记忆模块提供的信息。"),
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({
                "memories": [],
                "note": f"小本本读取异常（{e}），不代表你没有记忆，请依据上下文回答。",
            }, ensure_ascii=False)

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
                except Exception as e:
                    logger.error(f"梗匹配注入失败: {e}")

            # ---------- 关系注入：每轮都注入，只有"查库"受刷新间隔限制 ----------
            # 系统提示词每轮都会重建，注入只对当前这一轮生效；所以这里绝不能节流，
            # 否则同一用户连续说话时，第二轮之后就完全没有对方档案了（表现为"失忆"）。
            relation, db_ok = await asyncio.to_thread(self._load_relation_cached, user_id)
            if db_ok:
                if relation:
                    relation_xml = self._build_relation_xml(relation, current_group, sender_name)
                else:
                    relation_xml = (
                        f"<relationship>ID={user_id}"
                        + (f", 名称={sender_name}" if sender_name else "")
                        + ", 小本本里还没有TA的档案（这并不等于你不认识TA——上下文里可能已有"
                          "其他记忆模块提供的信息，请沿用已知内容，不要因为这里为空就说"
                          "“我不了解你”；也不要凭空编造）</relationship>"
                    )
                scene = f"当前场景={current_group}群聊" if is_group else "当前场景=私聊"
                injection_parts.append(
                    "<对当前对话对象的了解>\n"
                    "以下是关于【正在和你说话的这个账号】的信息，来自你之前和TA相处时记下的印象。\n"
                    "请自然地融入对话，不要像念档案一样逐条复述；这些信息只属于这个人，"
                    "不要套用到群里的其他成员身上。群体聊天里若分不清谁说过的，就先问一句，别张冠李戴。\n"
                    "如果上下文里还有别的来源（比如其他记忆模块）提供的更新信息，以那些为准。\n"
                    f"{relation_xml}, {scene}\n"
                    "</对当前对话对象的了解>"
                )
            else:
                logger.warning(f"关系档案读取失败，本轮跳过关系注入: {user_id}")

            # 更新时间放在取档案之后：先记一次互动再去读，会让"上次互动"永远显示"刚刚"
            try:
                await asyncio.to_thread(self.db_manager.auto_update_last_interaction, user_id)
            except Exception as e:
                logger.error(f"更新最近互动时间失败: {e}")

            # ---------- 记忆自动匹配注入 ----------
            # 私聊里连私有记忆一起匹配；群聊只匹配 public（大家的记忆），
            # 免得把只该私下说的内容当众念出来。缺失的词面重合度不足就不注入。
            memory_limit = int(self.config.get('memory_auto_inject_limit', 3) or 0)
            if memory_limit > 0 and len(user_message) >= 3:
                try:
                    min_score = int(self.config.get('memory_auto_inject_min_score', 2) or 1)
                    matches = await asyncio.to_thread(
                        self.db_manager.match_memories,
                        user_message,
                        memory_limit,
                        min_score,
                        not is_group,
                        user_id,
                    )
                    if matches:
                        lines = []
                        for m in matches:
                            lines.append(f"- [#{m['id']}|{m.get('category') or 'general'}] {m['content']}")
                        injection_parts.append(
                            "<可能相关的历史记忆>\n"
                            "以下是从小本本里自动匹配到的条目，仅供参考：和当前话题无关就完全忽略，"
                            "不要生硬引用，也不要假设一定和对方有关（记忆库是共享的）。\n"
                            + "\n".join(lines)
                            + "\n这些已经给你了，不需要再调用 search_memory 查同一件事。\n"
                            "</可能相关的历史记忆>"
                        )
                except Exception as e:
                    logger.error(f"记忆匹配注入失败: {e}")

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

    def _build_relation_xml(self, relation, current_group="", sender_name=""):
        user_id = relation.get('user_id') or ''
        nickname = relation.get('nickname') or sender_name or ''
        relation_type = relation.get('relation_type') or ''
        summary = relation.get('summary') or ''
        notes = relation.get('notes') or ''
        first_met = relation.get('first_met_location') or ''
        last_interaction = relation.get('last_interaction') or ''

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
                # 库里存的就是本机本地时间，不能再叠加 time_offset，否则时间会跑到未来，
                # 相对时间算出来永远是"刚刚"。
                dt = datetime.fromisoformat(iso_time)
                parts.append(f'上次互动={self._format_relative_time(dt)}')
            except Exception:
                pass
        if parts:
            return f"<relationship>对方: {', '.join(parts)}</relationship>"
        else:
            return f"<relationship>对方: ID={user_id}, 已记录但暂无详细信息</relationship>"
