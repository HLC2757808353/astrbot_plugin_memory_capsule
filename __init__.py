# 记忆胶囊插件的初始化文件
# 提供外部接口，供其他插件调用

__all__ = [
    "get_memory_manager",
    "store_plugin_data",
    "query_plugin_data"
]

# 版本信息
__version__ = "v0.0.1"

# 插件名称
__plugin_name__ = "记忆胶囊"

# 全局数据库管理器实例
_manager_instance = None

def set_global_manager(manager):
    """设置全局数据库管理器实例"""
    global _manager_instance
    _manager_instance = manager

def get_memory_manager():
    """获取记忆管理器实例（单例模式）"""
    global _manager_instance
    if _manager_instance is None:
        from .databases.db_manager import DatabaseManager
        _manager_instance = DatabaseManager()
    return _manager_instance

def store_plugin_data(content, metadata=None):
    """存储笔记数据"""
    return get_memory_manager().store_plugin_data(content, metadata)

def query_plugin_data(query_keyword, data_type=None):
    """查询笔记数据"""
    return get_memory_manager().query_plugin_data(query_keyword, data_type)
