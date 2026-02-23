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
