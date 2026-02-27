from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import os
import threading

@register("memory_capsule", "引灯续昼", "记忆胶囊插件，用于存储和检索记忆", "v0.0.1", "https://github.com/HLC2757808353/astrbot_plugin_memory_capsule")
class MemoryCapsulePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.db_manager = None
        self.webui_server = None
        self.webui_thread = None
        self.config = config
        # 获取WebUI端口配置，默认为5000
        self.webui_port = config.get('webui_port', 5000) if config else 5000

    async def initialize(self):
        """插件初始化方法"""
        logger.info("记忆胶囊插件正在初始化...")
        
        # 创建必要的目录结构
        self._create_directories()
        
        # 初始化数据库管理
        from .databases.db_manager import DatabaseManager
        self.db_manager = DatabaseManager(self.config)
        self.db_manager.initialize()
        
        # 将实例注册到全局，供 __init__.py 调用
        from . import set_global_manager
        set_global_manager(self.db_manager)
        
        # 启动WebUI服务
        self._start_webui()
        
        logger.info("记忆胶囊插件初始化完成")

    def _create_directories(self):
        """创建必要的目录结构"""
        # 创建databases目录
        databases_dir = os.path.join(os.path.dirname(__file__), "databases")
        os.makedirs(databases_dir, exist_ok=True)
        
        # 创建webui目录
        webui_dir = os.path.join(os.path.dirname(__file__), "webui")
        os.makedirs(webui_dir, exist_ok=True)
        os.makedirs(os.path.join(webui_dir, "templates"), exist_ok=True)
        os.makedirs(os.path.join(webui_dir, "static"), exist_ok=True)
        
        # 创建data目录
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(data_dir, exist_ok=True)

    def _start_webui(self):
        """启动WebUI服务"""
        try:
            from .webui.server import WebUIServer
            self.webui_server = WebUIServer(self.db_manager, port=self.webui_port)
            self.webui_thread = threading.Thread(target=self.webui_server.run, daemon=True, name='WebUI Server')
            self.webui_thread.start()
            logger.info(f"WebUI服务已启动，端口: {self.webui_port}")
        except Exception as e:
            logger.error(f"启动WebUI服务失败: {e}")

    @filter.command("memory")
    async def memory_command(self, event: AstrMessageEvent):
        """记忆胶囊指令，用于测试和管理记忆"""
        user_name = event.get_sender_name()
        message_str = event.message_str
        
        # 简单的测试功能
        if "test" in message_str:
            yield event.plain_result(f"{user_name}, 记忆胶囊测试成功！")
        elif "status" in message_str:
            yield event.plain_result(f"{user_name}, 记忆胶囊运行正常，WebUI服务已启动")
        else:
            yield event.plain_result(f"{user_name}, 记忆胶囊命令格式：/memory test 或 /memory status")

    async def terminate(self):
        """插件销毁方法"""
        logger.info("记忆胶囊插件正在关闭...")
        
        # 关闭WebUI服务
        if self.webui_server:
            self.webui_server.stop()
        
        # 关闭数据库连接
        if self.db_manager:
            self.db_manager.close()
        
        logger.info("记忆胶囊插件已关闭")

    @filter.llm_tool(name="update_relationship")
    async def update_relationship(self, event: AstrMessageEvent, user_id: str, relation_type: str = None, tags_update: str = None, summary_update: str = None, intimacy_change: int = 0):
        """
        更新对某人的印象或关系
        
        Args:
            user_id: 目标用户 ID
            relation_type: 新的关系定义
            tags_update: 新的标签 (会覆盖旧的)
            summary_update: 新的印象总结 (会覆盖旧的)
            intimacy_change: 好感度变化值 (如 +5, -10)
            
        Returns:
            更新结果
        """
        # 类型注释确保参数类型正确
        user_id: str = user_id
        relation_type: str = relation_type
        tags_update: str = tags_update
        summary_update: str = summary_update
        intimacy_change: int = intimacy_change
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.update_relationship, user_id, relation_type, tags_update, summary_update, intimacy_change)
            logger.info(f"更新关系成功: {user_id}")
            return result
        except Exception as e:
            logger.error(f"更新关系失败: {e}")
            return f"更新失败: {e}"

    @filter.llm_tool(name="write_memory")
    async def write_memory(self, event: AstrMessageEvent, content: str, category: str = "日常", tags: str = "", target_user_id: str = None):
        """
        记下一个永久知识点
        
        Args:
            content: 要记住的内容
            category: 分类 (默认 "日常")
            tags: 标签 (逗号分隔)
            target_user_id: 如果是关于特定人的记忆，填这里
            
        Returns:
            存储结果
        """
        # 类型注释确保参数类型正确
        content: str = content
        category: str = category
        tags: str = tags
        target_user_id: str = target_user_id
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.write_memory, content, category, tags, target_user_id)
            logger.info("存储记忆成功")
            return result
        except Exception as e:
            logger.error(f"存储记忆失败: {e}")
            return f"存储失败: {e}"

    @filter.llm_tool(name="search_memory")
    async def search_memory(self, event: AstrMessageEvent, query: str, target_user_id: str = None):
        """
        搜索过去的记忆
        
        Args:
            query: 搜索关键词或句子
            target_user_id: 限定搜索某人的相关记忆
            
        Returns:
            搜索结果列表
        """
        # 类型注释确保参数类型正确
        query: str = query
        target_user_id: str = target_user_id
        try:
            import asyncio
            results = await asyncio.to_thread(self.db_manager.search_memory, query, target_user_id)
            logger.info(f"搜索记忆成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return []

    @filter.llm_tool(name="delete_memory")
    async def delete_memory(self, event: AstrMessageEvent, memory_id: int):
        """
        遗忘某条记忆
        
        Args:
            memory_id: 记忆的 ID (通常 AI 需要先搜到才能删)
            
        Returns:
            删除结果
        """
        # 类型注释确保参数类型正确
        memory_id: int = memory_id
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.delete_memory, memory_id)
            logger.info(f"删除记忆成功: ID={memory_id}")
            return result
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            return f"删除失败: {e}"

    @filter.llm_tool(name="get_all_memories")
    async def get_all_memories(self, event: AstrMessageEvent, limit: int = 100):
        """
        获取所有记忆
        
        Args:
            limit: 限制数量，默认为100
            
        Returns:
            记忆列表
        """
        # 类型注释确保参数类型正确
        limit: int = limit
        try:
            import asyncio
            results = await asyncio.to_thread(self.db_manager.get_all_memories, limit)
            logger.info(f"获取所有记忆成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"获取所有记忆失败: {e}")
            return []

    @filter.llm_tool(name="get_all_relationships")
    async def get_all_relationships(self, event: AstrMessageEvent):
        """
        获取所有关系
        
        Returns:
            关系列表
        """
        try:
            import asyncio
            results = await asyncio.to_thread(self.db_manager.get_all_relationships)
            logger.info(f"获取所有关系成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"获取所有关系失败: {e}")
            return []

    @filter.llm_tool(name="delete_relationship")
    async def delete_relationship(self, event: AstrMessageEvent, user_id: str):
        """
        删除关系
        
        Args:
            user_id: 用户ID
            
        Returns:
            删除结果
        """
        # 类型注释确保参数类型正确
        user_id: str = user_id
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.delete_relationship, user_id)
            logger.info(f"删除关系成功: {user_id}")
            return result
        except Exception as e:
            logger.error(f"删除关系失败: {e}")
            return f"删除失败: {e}"

    @filter.llm_tool(name="backup_database")
    async def backup_database(self, event: AstrMessageEvent):
        """
        备份数据库
        
        Returns:
            备份结果
        """
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.backup)
            logger.info("数据库备份成功")
            return result
        except Exception as e:
            logger.error(f"数据库备份失败: {e}")
            return f"备份失败: {e}"

    @filter.on_decorating_result()
    async def inject_relation_context(self, event: AstrMessageEvent):
        """
        注入用户关系信息到AI上下文
        
        每次对话时，自动获取用户的关系信息并注入到系统提示词中
        如果用户没有关系信息，自动添加一个新的关系记录
        """
        try:
            # 获取用户信息
            user_id = event.get_sender_id()
            user_name = event.get_sender_name()
            group_id = event.get_group_id() or "private"
            platform = event.get_platform() or "qq"
            
            # 查找用户关系信息
            import asyncio
            relations = await asyncio.to_thread(self.db_manager.query_relation, user_id)
            
            # 如果没有关系信息，自动添加
            if not relations:
                logger.info(f"用户 {user_id} 首次对话，自动添加关系记录")
                await self.update_relation(
                    event=event,
                    user_id=user_id,
                    group_id=group_id,
                    platform=platform,
                    nickname=user_name,
                    first_meet_group=group_id,
                    first_meet_time=event.get_timestamp()
                )
                # 重新获取关系信息
                relations = await asyncio.to_thread(self.db_manager.query_relation, user_id)
            
            # 构建关系信息上下文
            if relations:
                relation = relations[0]
                relation_context = f"\n\n用户关系信息:\n"
                relation_context += f"- 用户ID: {relation['user_id']}\n"
                relation_context += f"- 昵称: {relation['nickname']}\n"
                if relation['nicknames']:
                    relation_context += f"- 昵称列表: {', '.join(relation['nicknames'])}\n"
                if relation['first_meet_group']:
                    relation_context += f"- 初次见面群组: {relation['first_meet_group']}\n"
                if relation['first_meet_time']:
                    relation_context += f"- 初次见面时间: {relation['first_meet_time']}\n"
                relation_context += f"- 平台: {relation['platform']}\n"
                relation_context += f"- 好感度: {relation['favor_level']}\n"
                if relation['relationship']:
                    relation_context += f"- 关系: {relation['relationship']}\n"
                if relation['remark']:
                    relation_context += f"- 备注: {relation['remark']}\n"
                
                # 注入到系统提示词
                # 注意：具体的注入方式可能需要根据AstrBot的API调整
                # 这里假设event对象有方法来修改系统提示词
                if hasattr(event, 'set_system_prompt'):
                    current_prompt = event.get_system_prompt() or ""
                    new_prompt = current_prompt + relation_context
                    event.set_system_prompt(new_prompt)
                elif hasattr(event, 'context') and hasattr(event.context, 'system_prompt'):
                    event.context.system_prompt = (event.context.system_prompt or "") + relation_context
                
                logger.info(f"成功注入用户 {user_id} 的关系信息到上下文")
        except Exception as e:
            logger.error(f"注入关系信息失败: {e}")
        
        # 继续处理消息
        return event

# 外部接口，供其他插件调用
# 注意：这些函数已在 __init__.py 中重新定义，使用单例模式
