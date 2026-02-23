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
        self.db_manager = DatabaseManager()
        self.db_manager.initialize()
        
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
            self.webui_thread = threading.Thread(target=self.webui_server.run, daemon=True)
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
    async def store_memory(self, content: str, plugin_name: str = "system", data_type: str = "note", metadata: dict = None):
        """
        存储记忆到记忆胶囊
        
        Args:
            content(string): 记忆内容
            plugin_name(string): 插件名称，默认为"system"
            data_type(string): 数据类型，默认为"note"
            metadata(dict): 元数据，默认为None
            
        Returns:
            string: 存储结果
        """
        try:
            result = self.db_manager.store_plugin_data(plugin_name, data_type, content, metadata)
            logger.info(f"存储记忆成功: {plugin_name}/{data_type}")
            return result
        except Exception as e:
            logger.error(f"存储记忆失败: {e}")
            return f"存储失败: {e}"

    @filter.llm_tool(name="query_memory")
    async def query_memory(self, query_keyword: str, plugin_name: str = None, data_type: str = None):
        """
        查询记忆胶囊中的记忆
        
        Args:
            query_keyword(string): 查询关键词
            plugin_name(string): 插件名称，默认为None
            data_type(string): 数据类型，默认为None
            
        Returns:
            list: 查询结果列表
        """
        try:
            results = self.db_manager.query_plugin_data(query_keyword, plugin_name, data_type)
            logger.info(f"查询记忆成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"查询记忆失败: {e}")
            return []

    @filter.llm_tool(name="update_relation")
    async def update_relation(self, user_id: str, group_id: str, nickname: str = None, favor_change: int = 0, impression: str = None):
        """
        更新用户关系
        
        Args:
            user_id(string): 用户ID
            group_id(string): 群组ID
            nickname(string): 昵称，默认为None
            favor_change(integer): 好感度变化，默认为0
            impression(string): 印象描述，默认为None
            
        Returns:
            string: 更新结果
        """
        try:
            result = self.db_manager.update_relation(user_id, group_id, nickname, favor_change, impression)
            logger.info(f"更新关系成功: {user_id}@{group_id}")
            return result
        except Exception as e:
            logger.error(f"更新关系失败: {e}")
            return f"更新失败: {e}"

    @filter.llm_tool(name="query_relation")
    async def query_relation(self, query_keyword: str):
        """
        查询用户关系
        
        Args:
            query_keyword(string): 查询关键词
            
        Returns:
            list: 查询结果列表
        """
        try:
            results = self.db_manager.query_relation(query_keyword)
            logger.info(f"查询关系成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"查询关系失败: {e}")
            return []

    @filter.llm_tool(name="get_all_memories")
    async def get_all_memories(self, limit: int = 100):
        """
        获取所有记忆
        
        Args:
            limit(integer): 限制数量，默认为100
            
        Returns:
            list: 记忆列表
        """
        try:
            results = self.db_manager.get_all_plugin_data(limit)
            logger.info(f"获取所有记忆成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"获取所有记忆失败: {e}")
            return []

    @filter.llm_tool(name="get_all_relations")
    async def get_all_relations(self):
        """
        获取所有关系
        
        Returns:
            list: 关系列表
        """
        try:
            results = self.db_manager.get_all_relations()
            logger.info(f"获取所有关系成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"获取所有关系失败: {e}")
            return []

    @filter.llm_tool(name="delete_memory")
    async def delete_memory(self, data_id: int):
        """
        删除记忆
        
        Args:
            data_id(integer): 数据ID
            
        Returns:
            string: 删除结果
        """
        try:
            result = self.db_manager.delete_plugin_data(data_id)
            logger.info(f"删除记忆成功: ID={data_id}")
            return result
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            return f"删除失败: {e}"

    @filter.llm_tool(name="delete_relation")
    async def delete_relation(self, user_id: str, group_id: str):
        """
        删除关系
        
        Args:
            user_id(string): 用户ID
            group_id(string): 群组ID
            
        Returns:
            string: 删除结果
        """
        try:
            result = self.db_manager.delete_relation(user_id, group_id)
            logger.info(f"删除关系成功: {user_id}@{group_id}")
            return result
        except Exception as e:
            logger.error(f"删除关系失败: {e}")
            return f"删除失败: {e}"

    @filter.llm_tool(name="backup_database")
    async def backup_database(self):
        """
        备份数据库
        
        Returns:
            str: 备份结果
        """
        try:
            result = self.db_manager.backup()
            logger.info("备份数据库成功")
            return result
        except Exception as e:
            logger.error(f"备份数据库失败: {e}")
            return f"备份失败: {e}"

# 外部接口，供其他插件调用
def get_memory_manager():
    """获取记忆管理器实例"""
    from .databases.db_manager import DatabaseManager
    return DatabaseManager()

def store_plugin_data(plugin_name, data_type, content, metadata=None):
    """存储插件数据"""
    db_manager = get_memory_manager()
    return db_manager.store_plugin_data(plugin_name, data_type, content, metadata)

def query_plugin_data(query_keyword, plugin_name=None, data_type=None):
    """查询插件数据"""
    db_manager = get_memory_manager()
    return db_manager.query_plugin_data(query_keyword, plugin_name, data_type)
