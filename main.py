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

    @filter.llm_tool(name="store_memory")
    async def store_memory(self, event: AstrMessageEvent, content: str, metadata: dict = None):
        """
        存储记忆到记忆胶囊
        
        Args:
            content(string): 记忆内容，固定为字符串类型
            metadata(dict): 元数据，用于存储额外信息，如标签、关键词等
            
        Returns:
            string: 存储结果
        """
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.store_plugin_data, content, metadata)
            logger.info("存储记忆成功")
            return result
        except Exception as e:
            logger.error(f"存储记忆失败: {e}")
            return f"存储失败: {e}"

    @filter.llm_tool(name="query_memory")
    async def query_memory(self, event: AstrMessageEvent, query_keyword: str, data_type: str = None):
        """
        查询记忆胶囊中的记忆
        
        Args:
            query_keyword(string): 查询关键词
            data_type(string): 数据类型，默认为None
            
        Returns:
            list: 查询结果列表
        """
        try:
            import asyncio
            results = await asyncio.to_thread(self.db_manager.query_plugin_data, query_keyword, data_type)
            logger.info(f"查询记忆成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"查询记忆失败: {e}")
            return []

    @filter.llm_tool(name="update_relation")
    async def update_relation(self, event: AstrMessageEvent, user_id: str, group_id: str, platform: str = "qq", nickname: str = None, nicknames: list = None, first_meet_group: str = None, first_meet_time: str = None, favor_change: int = 0, relationship: str = None, remark: str = None):
        """
        更新用户关系
        
        Args:
            user_id(string): 用户ID
            group_id(string): 群组ID
            platform(string): 平台，默认为"qq"
            nickname(string): 昵称，默认为None
            nicknames(list): 昵称数组，默认为None
            first_meet_group(string): 初次见面群组，默认为None
            first_meet_time(string): 初次见面时间，默认为None
            favor_change(int): 好感度变化，默认为0
            relationship(string): 关系，默认为None
            remark(string): 备注，默认为None
            
        Returns:
            string: 更新结果
        """
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.update_relation, user_id, group_id, platform, nickname, nicknames, first_meet_group, first_meet_time, favor_change, relationship, remark)
            logger.info(f"更新关系成功: {user_id}@{group_id}@{platform}")
            return result
        except Exception as e:
            logger.error(f"更新关系失败: {e}")
            return f"更新失败: {e}"

    @filter.llm_tool(name="query_relation")
    async def query_relation(self, event: AstrMessageEvent, query_keyword: str):
        """
        查询用户关系
        
        Args:
            query_keyword(string): 查询关键词
            
        Returns:
            list: 查询结果列表
        """
        try:
            import asyncio
            results = await asyncio.to_thread(self.db_manager.query_relation, query_keyword)
            logger.info(f"查询关系成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"查询关系失败: {e}")
            return []

    @filter.llm_tool(name="get_all_memories")
    async def get_all_memories(self, event: AstrMessageEvent, limit: int = 100):
        """
        获取所有记忆
        
        Args:
            limit(int): 限制数量，默认为100
            
        Returns:
            list: 记忆列表
        """
        try:
            import asyncio
            results = await asyncio.to_thread(self.db_manager.get_all_plugin_data, limit)
            logger.info(f"获取所有记忆成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"获取所有记忆失败: {e}")
            return []

    @filter.llm_tool(name="get_all_relations")
    async def get_all_relations(self, event: AstrMessageEvent):
        """
        获取所有关系
        
        Returns:
            list: 关系列表
        """
        try:
            import asyncio
            results = await asyncio.to_thread(self.db_manager.get_all_relations)
            logger.info(f"获取所有关系成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"获取所有关系失败: {e}")
            return []

    @filter.llm_tool(name="delete_memory")
    async def delete_memory(self, event: AstrMessageEvent, data_id: int):
        """
        删除记忆
        
        Args:
            data_id(int): 数据ID
            
        Returns:
            string: 删除结果
        """
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.delete_plugin_data, data_id)
            logger.info(f"删除记忆成功: ID={data_id}")
            return result
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            return f"删除失败: {e}"

    @filter.llm_tool(name="delete_relation")
    async def delete_relation(self, event: AstrMessageEvent, user_id: str, platform: str = "qq"):
        """
        删除关系
        
        Args:
            user_id(string): 用户ID
            platform(string): 平台，默认为"qq"
            
        Returns:
            string: 删除结果
        """
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.delete_relation, user_id, platform)
            logger.info(f"删除关系成功: {user_id}@{platform}")
            return result
        except Exception as e:
            logger.error(f"删除关系失败: {e}")
            return f"删除失败: {e}"

    @filter.llm_tool(name="backup_database")
    async def backup_database(self, event: AstrMessageEvent):
        """
        备份数据库
        
        Returns:
            str: 备份结果
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
