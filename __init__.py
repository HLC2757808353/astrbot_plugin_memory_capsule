# 记忆胶囊插件的初始化文件
# 提供外部接口，供其他插件调用

__all__ = [
    "get_memory_manager",
    "store_plugin_data",
    "query_plugin_data"
]

# 插件名称
__plugin_name__ = "记忆胶囊"

# 版本信息（从 metadata.yaml 动态读取，唯一真相源）
def __get_version():
    """延迟获取版本号"""
    try:
        from .webui.version import get_plugin_version
        return get_plugin_version()
    except Exception:
        return "v0.0.0"

# 使用属性方式动态获取版本（兼容 import 方式）
class _VersionProxy:
    def __str__(self):
        return __get_version()
    def __repr__(self):
        return f"'{__get_version()}'"

__version__ = _VersionProxy()

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
