from .databases.db_manager import DatabaseManager

__all__ = ["get_memory_manager", "DatabaseManager"]

_manager_instance = None

def set_global_manager(manager):
    global _manager_instance
    _manager_instance = manager

def get_memory_manager():
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = DatabaseManager()
    return _manager_instance
