import os
import yaml

# 缓存版本号，避免重复读取文件
_version_cache = None


def get_plugin_version():
    """
    获取插件版本号（从 metadata.yaml 读取）
    
    这是唯一的版本号真相源，所有其他地方都应该调用这个函数
    
    Returns:
        str: 版本号字符串 (如 "v0.9.5")
    """
    global _version_cache
    
    if _version_cache is not None:
        return _version_cache
    
    try:
        metadata_path = os.path.join(os.path.dirname(__file__), "..", "metadata.yaml")
        
        if os.path.exists(metadata_path):
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = yaml.safe_load(f)
                _version_cache = metadata.get('version', 'v0.0.0')
                return _version_cache
    except Exception:
        pass
    
    _version_cache = 'v0.0.0'
    return _version_cache


def clear_version_cache():
    """清除版本号缓存（用于测试或强制刷新）"""
    global _version_cache
    _version_cache = None
