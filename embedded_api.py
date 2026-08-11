"""嵌入式 Pages API（AstrBot Dashboard 插件页面）。

本模块将记忆胶囊的全部 Web API 注册到 AstrBot 的 register_web_api，
页面通过 window.AstrBotPluginPage bridge 调用，无需开放独立端口。
路由前缀 /memory_capsule 对应插件名 memory_capsule。
"""
from __future__ import annotations

import json

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

PLUGIN_NAME = "astrbot_plugin_memory_capsule"


class EmbeddedAPI:
    def __init__(self, db_manager, config=None):
        self.db_manager = db_manager
        self.config = config or {}

    @property
    def _req(self):
        """惰性获取当前请求（astrbot.api.web.request 在 handler 调用时才有上下文）。"""
        from astrbot.api.web import request
        return request

    # ==================== 工具方法 ====================

    def _ok(self, **kwargs):
        return kwargs

    def _err(self, message):
        return {"status": "error", "message": str(message)}

    # ==================== 仪表盘 ====================

    async def api_stats(self):
        try:
            stats = await self._to_thread(self.db_manager.get_memory_stats)
            return self._ok(**stats)
        except Exception as e:
            return self._err(e)

    async def api_activities(self):
        limit = self._req.query.get("limit", 30, type=int)
        try:
            items = await self._to_thread(self.db_manager.get_recent_activities, limit)
            return self._ok(activities=items)
        except Exception as e:
            return self._err(e)

    # ==================== 记忆 ====================

    async def api_memories_list(self):
        page = self._req.query.get("page", 1, type=int)
        limit = self._req.query.get("limit", 12, type=int)
        offset = (page - 1) * limit
        category = self._req.query.get("category")
        try:
            memories = await self._to_thread(self.db_manager.get_all_memories, limit, offset, category)
            total = await self._to_thread(self.db_manager.get_memories_count)
            return self._ok(memories=memories, total=total, page=page, limit=limit,
                            total_pages=(total + limit - 1) // limit if limit else 1)
        except Exception as e:
            return self._err(e)

    async def api_memories_add(self):
        data = await self._req.json() or {}
        try:
            result = await self._to_thread(
                self.db_manager.write_memory,
                data.get("content", ""),
                data.get("category"),
                data.get("importance", 5),
                data.get("tags")
            )
            return self._ok(result=result)
        except Exception as e:
            return self._err(e)

    async def api_memories_import(self):
        try:
            data = await self._req.json() or {}
            items = data.get("memories", [])
            if not items or not isinstance(items, list):
                return self._err("No memories array provided")
            if len(items) > 500:
                return self._err(f"Too many items ({len(items)}), max 500 per batch")
            result = await self._to_thread(self.db_manager.bulk_import_memories, items)
            return self._ok(result=result)
        except Exception as e:
            return self._err(e)

    async def api_memories_update(self, memory_id):
        data = await self._req.json() or {}
        try:
            result = await self._to_thread(
                self.db_manager.update_memory,
                int(memory_id),
                content=data.get("content"),
                category=data.get("category"),
                importance=data.get("importance"),
                tags=data.get("tags")
            )
            return self._ok(result=result)
        except Exception as e:
            return self._err(e)

    async def api_memories_delete(self, memory_id):
        try:
            result = await self._to_thread(self.db_manager.delete_memory, int(memory_id))
            return self._ok(result=result)
        except Exception as e:
            return self._err(e)

    async def api_memories_search(self):
        query = self._req.query.get("q", "")
        category = self._req.query.get("category")
        limit = self._req.query.get("limit", None, type=int)
        try:
            results = await self._to_thread(self.db_manager.search_memory, str(query), category, limit)
            # 联想记忆
            related = []
            try:
                exclude_ids = [r["id"] for r in results if r.get("id")]
                rel_limit = self.config.get("search_related_limit", 3)
                if rel_limit > 0:
                    related = await self._to_thread(
                        self.db_manager.search_memory_related, str(query), exclude_ids, int(rel_limit)
                    )
            except Exception:
                related = []
            return self._ok(results=results, related=related)
        except Exception as e:
            return self._err(e)

    async def api_categories(self):
        try:
            cats = await self._to_thread(self.db_manager.get_memory_categories)
            cfg = self.config.get("memory_categories", [])
            all_cats = list(dict.fromkeys(cfg + cats))
            if not all_cats:
                all_cats = ["技术笔记", "生活记录", "学习资料", "个人想法", "待办事项", "general"]
            return self._ok(categories=all_cats)
        except Exception as e:
            return self._err(e)

    # ==================== 关系 ====================

    async def api_relationships_list(self):
        page = self._req.query.get("page", 1, type=int)
        limit = self._req.query.get("limit", 12, type=int)
        offset = (page - 1) * limit
        try:
            items = await self._to_thread(self.db_manager.get_all_relationships, limit, offset)
            total = await self._to_thread(self.db_manager.get_relationships_count)
            return self._ok(relationships=items, total=total, page=page, limit=limit,
                            total_pages=(total + limit - 1) // limit if limit else 1)
        except Exception as e:
            return self._err(e)

    async def api_relationships_add(self):
        data = await self._req.json() or {}
        try:
            result = await self._to_thread(
                self.db_manager.update_relationship_enhanced,
                str(data.get("user_id", "")),
                data.get("relation_type"),
                data.get("summary"),
                data.get("nickname"),
                data.get("first_met_location"),
                data.get("notes")
            )
            return self._ok(result=result)
        except Exception as e:
            return self._err(e)

    async def api_relationships_delete(self, user_id):
        try:
            result = await self._to_thread(self.db_manager.delete_relationship, str(user_id))
            return self._ok(result=result)
        except Exception as e:
            return self._err(e)

    async def api_relationships_search(self):
        query = self._req.query.get("q", "")
        try:
            results = await self._to_thread(self.db_manager.search_relationship, str(query), 10)
            return self._ok(results=results)
        except Exception as e:
            return self._err(e)

    # ==================== 梗库 ====================

    async def api_glossary_list(self):
        page = self._req.query.get("page", 1, type=int)
        limit = self._req.query.get("limit", 28, type=int)
        offset = (page - 1) * limit
        category = self._req.query.get("category") or None
        query = self._req.query.get("q") or None
        try:
            items = await self._to_thread(self.db_manager.get_glossaries, limit, offset, category, query)
            total = await self._to_thread(self.db_manager.get_glossaries_count, category, query)
            return self._ok(items=items, total=total, page=page, limit=limit,
                            total_pages=(total + limit - 1) // limit if limit else 1)
        except Exception as e:
            return self._err(e)

    async def api_glossary_add(self):
        data = await self._req.json() or {}
        try:
            result = await self._to_thread(
                self.db_manager.add_glossary,
                data.get("term", ""),
                data.get("category", "其他梗"),
                data.get("meaning", ""),
                data.get("source", ""),
                data.get("tags", "")
            )
            if result == "already_exists":
                return self._err("已存在相同梗词")
            return self._ok(result=result)
        except Exception as e:
            return self._err(e)

    async def api_glossary_update(self, glossary_id):
        data = await self._req.json() or {}
        try:
            result = await self._to_thread(
                self.db_manager.update_glossary,
                int(glossary_id),
                term=data.get("term"),
                category=data.get("category"),
                meaning=data.get("meaning"),
                source=data.get("source"),
                tags=data.get("tags")
            )
            return self._ok(result=result)
        except Exception as e:
            return self._err(e)

    async def api_glossary_delete(self, glossary_id):
        try:
            result = await self._to_thread(self.db_manager.delete_glossary, int(glossary_id))
            return self._ok(result=result)
        except Exception as e:
            return self._err(e)

    async def api_glossary_categories(self):
        try:
            cats = await self._to_thread(self.db_manager.get_glossary_categories)
            return self._ok(categories=cats)
        except Exception as e:
            return self._err(e)

    async def api_glossary_stats(self):
        try:
            stats = await self._to_thread(self.db_manager.get_glossary_stats)
            return self._ok(**stats)
        except Exception as e:
            return self._err(e)

    async def api_glossary_import(self):
        try:
            data = await self._req.json() or {}
            items = data.get("items", [])
            if not items or not isinstance(items, list):
                return self._err("No items array provided")
            if len(items) > 5000:
                return self._err(f"Too many items ({len(items)}), max 5000 per batch")
            result = await self._to_thread(self.db_manager.bulk_import_glossary, items)
            return self._ok(result=result)
        except Exception as e:
            return self._err(e)

    async def api_glossary_export(self):
        try:
            items = await self._to_thread(self.db_manager.get_glossaries, 99999, 0, None, None)
            lines = []
            for i in items:
                lines.append(json.dumps({
                    "term": i.get("term", ""),
                    "category": i.get("category", ""),
                    "meaning": i.get("meaning", ""),
                    "source": i.get("source", ""),
                    "tags": i.get("tags", "")
                }, ensure_ascii=False))
            return self._ok(jsonl="\n".join(lines), count=len(lines))
        except Exception as e:
            return self._err(e)

    # ==================== 设置 ====================

    async def api_settings_get(self):
        try:
            cfg = self.config or {}
            return self._ok(settings=cfg)
        except Exception as e:
            return self._err(e)

    async def api_settings_save(self):
        try:
            data = await self._req.json() or {}
            if self.db_manager and self.db_manager.config is not None:
                for key, value in data.items():
                    self.db_manager.config[key] = value
            return self._ok(result="Saved")
        except Exception as e:
            return self._err(e)

    # ==================== 辅助 ====================

    async def _to_thread(self, func, *args, **kwargs):
        import asyncio
        return await asyncio.to_thread(func, *args, **kwargs)


def register_embedded_apis(context, db_manager, config=None):
    """注册全部嵌入式 Web API 到 AstrBot。"""
    api = EmbeddedAPI(db_manager, config)
    routes = [
        (f"/{PLUGIN_NAME}/api/stats", api.api_stats, ["GET"], "统计信息"),
        (f"/{PLUGIN_NAME}/api/activities", api.api_activities, ["GET"], "最近活动"),
        (f"/{PLUGIN_NAME}/api/memories", api.api_memories_list, ["GET"], "记忆列表"),
        (f"/{PLUGIN_NAME}/api/memories", api.api_memories_add, ["POST"], "新增记忆"),
        (f"/{PLUGIN_NAME}/api/memories/search", api.api_memories_search, ["GET"], "搜索记忆"),
        (f"/{PLUGIN_NAME}/api/memories/import", api.api_memories_import, ["POST"], "记忆批量导入"),
        (f"/{PLUGIN_NAME}/api/memories/<memory_id>/update", api.api_memories_update, ["POST"], "更新记忆"),
        (f"/{PLUGIN_NAME}/api/memories/<memory_id>/delete", api.api_memories_delete, ["POST"], "删除记忆"),
        (f"/{PLUGIN_NAME}/api/categories", api.api_categories, ["GET"], "记忆分类"),
        (f"/{PLUGIN_NAME}/api/relationships", api.api_relationships_list, ["GET"], "关系列表"),
        (f"/{PLUGIN_NAME}/api/relationships", api.api_relationships_add, ["POST"], "新增关系"),
        (f"/{PLUGIN_NAME}/api/relationships/search", api.api_relationships_search, ["GET"], "搜索关系"),
        (f"/{PLUGIN_NAME}/api/relationships/<user_id>/delete", api.api_relationships_delete, ["POST"], "删除关系"),
        (f"/{PLUGIN_NAME}/api/glossary", api.api_glossary_list, ["GET"], "梗列表"),
        (f"/{PLUGIN_NAME}/api/glossary", api.api_glossary_add, ["POST"], "新增梗"),
        (f"/{PLUGIN_NAME}/api/glossary/categories", api.api_glossary_categories, ["GET"], "梗分类"),
        (f"/{PLUGIN_NAME}/api/glossary/stats", api.api_glossary_stats, ["GET"], "梗统计"),
        (f"/{PLUGIN_NAME}/api/glossary/import", api.api_glossary_import, ["POST"], "梗批量导入"),
        (f"/{PLUGIN_NAME}/api/glossary/export", api.api_glossary_export, ["GET"], "梗导出"),
        (f"/{PLUGIN_NAME}/api/glossary/<glossary_id>/update", api.api_glossary_update, ["POST"], "更新梗"),
        (f"/{PLUGIN_NAME}/api/glossary/<glossary_id>/delete", api.api_glossary_delete, ["POST"], "删除梗"),
        (f"/{PLUGIN_NAME}/api/settings", api.api_settings_get, ["GET"], "读取设置"),
        (f"/{PLUGIN_NAME}/api/settings", api.api_settings_save, ["POST"], "保存设置"),
    ]
    for route, handler, methods, desc in routes:
        try:
            context.register_web_api(route, handler, methods, desc)
        except Exception as e:
            logger.error(f"注册 Web API 失败 {route}: {e}")
    logger.info(f"嵌入式 Web API 注册完成（{len(routes)} 个路由）")
