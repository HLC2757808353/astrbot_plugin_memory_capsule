import os
import shutil
import datetime
import time
import threading

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

class BackupManager:
    def __init__(self, db_path, config=None):
        self.db_path = db_path
        self.backup_dir = os.path.join(os.path.dirname(db_path), "memory_capsule_backups")
        self.config = config or {}
        self.auto_backup_enabled = self.config.get('backup_enabled', True)
        self.auto_backup_interval = self.config.get('backup_interval', 24) * 60 * 60
        self.backup_thread = None
        self.running = False
        self._stop_event = threading.Event()
        os.makedirs(self.backup_dir, exist_ok=True)

    def backup(self):
        try:
            now = datetime.datetime.now()
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            backup_filename = f"memory_{timestamp}.db"
            backup_path = os.path.join(self.backup_dir, backup_filename)
            shutil.copy2(self.db_path, backup_path)
            self._cleanup_old_backups()
            logger.info(f"备份成功: {backup_path}")
            return f"备份成功: {backup_filename}"
        except Exception as e:
            logger.error(f"备份失败: {e}")
            return f"备份失败: {e}"

    def _cleanup_old_backups(self):
        try:
            backups = []
            for file in os.listdir(self.backup_dir):
                if file.endswith('.db'):
                    file_path = os.path.join(self.backup_dir, file)
                    if os.path.isfile(file_path):
                        mtime = os.path.getmtime(file_path)
                        backups.append((file_path, mtime))
            backups.sort(key=lambda x: x[1], reverse=True)
            max_backups = self.config.get('backup_max_count', 10)
            if len(backups) > max_backups:
                for backup_path, _ in backups[max_backups:]:
                    try:
                        os.remove(backup_path)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"清理备份失败: {e}")

    def start_auto_backup(self):
        if not self.auto_backup_enabled:
            return
        if self.running:
            return
        self.running = True
        self._stop_event.clear()
        self.backup_thread = threading.Thread(target=self._auto_backup_loop, daemon=True)
        self.backup_thread.start()

    def stop_auto_backup(self):
        self.auto_backup_enabled = False
        self.running = False
        self._stop_event.set()

    def _auto_backup_loop(self):
        while self.running:
            self.backup()
            if self._stop_event.wait(timeout=self.auto_backup_interval):
                break

    def get_backup_list(self):
        try:
            backups = []
            for file in os.listdir(self.backup_dir):
                if file.endswith('.db'):
                    file_path = os.path.join(self.backup_dir, file)
                    if os.path.isfile(file_path):
                        stat = os.stat(file_path)
                        backups.append({
                            'filename': file,
                            'size': f"{stat.st_size / 1024:.1f}KB",
                            'time': datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                        })
            backups.sort(key=lambda x: x['time'], reverse=True)
            return backups
        except Exception as e:
            logger.error(f"获取备份列表失败: {e}")
            return []

    def restore_from_backup(self, backup_filename):
        try:
            backup_path = os.path.join(self.backup_dir, backup_filename)
            if not os.path.exists(backup_path):
                return "备份文件不存在"
            shutil.copy2(backup_path, self.db_path)
            logger.info(f"从备份恢复成功: {backup_filename}")
            return f"从备份恢复成功: {backup_filename}"
        except Exception as e:
            logger.error(f"恢复失败: {e}")
            return f"恢复失败: {e}"
