# 记忆胶囊插件的初始化文件
# 提供外部接口，供其他插件调用

from .main import get_memory_manager, store_plugin_data, query_plugin_data

__all__ = [
    "get_memory_manager",
    "store_plugin_data",
    "query_plugin_data"
]

# 版本信息
__version__ = "v0.0.1"

# 插件名称
__plugin_name__ = "记忆胶囊"
