import os
import shutil
import datetime
import time
import threading
from astrbot.api import logger

class BackupManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self.backup_dir = os.path.join(os.path.dirname(db_path), "backups")
        self.auto_backup_enabled = False
        self.auto_backup_interval = 24 * 60 * 60  # 24小时
        self.max_backups = 10
        self.backup_thread = None
        self.running = False
        
        # 创建备份目录
        os.makedirs(self.backup_dir, exist_ok=True)

    def backup(self):
        """执行备份"""
        try:
            # 生成备份文件名
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_filename = f"memory_{timestamp}.db"
            backup_path = os.path.join(self.backup_dir, backup_filename)
            
            # 复制数据库文件
            shutil.copy2(self.db_path, backup_path)
            
            # 清理旧备份
            self._cleanup_old_backups()
            
            logger.info(f"备份成功: {backup_path}")
            return f"备份成功: {backup_filename}"
        except Exception as e:
            logger.error(f"备份失败: {e}")
            return f"备份失败: {e}"

    def _cleanup_old_backups(self):
        """清理旧备份，保留最新的几个"""
        try:
            # 获取所有备份文件
            backups = []
            for file in os.listdir(self.backup_dir):
                if file.endswith('.db'):
                    file_path = os.path.join(self.backup_dir, file)
                    if os.path.isfile(file_path):
                        backups.append((file_path, os.path.getmtime(file_path)))
            
            # 按修改时间排序
            backups.sort(key=lambda x: x[1], reverse=True)
            
            # 删除多余的备份
            for backup_path, _ in backups[self.max_backups:]:
                os.remove(backup_path)
                logger.info(f"删除旧备份: {os.path.basename(backup_path)}")
        except Exception as e:
            logger.error(f"清理备份失败: {e}")

    def start_auto_backup(self):
        """启动自动备份"""
        if self.auto_backup_enabled:
            return "自动备份已启动"
        
        self.auto_backup_enabled = True
        self.running = True
        self.backup_thread = threading.Thread(target=self._auto_backup_loop, daemon=True)
        self.backup_thread.start()
        logger.info("自动备份已启动，间隔: 24小时")
        return "自动备份已启动"

    def stop_auto_backup(self):
        """停止自动备份"""
        self.auto_backup_enabled = False
        self.running = False
        logger.info("自动备份已停止")
        return "自动备份已停止"

    def _auto_backup_loop(self):
        """自动备份循环"""
        while self.running:
            self.backup()
            # 等待指定时间
            for _ in range(self.auto_backup_interval):
                if not self.running:
                    break
                time.sleep(1)

    def get_backup_list(self):
        """获取备份列表"""
        try:
            backups = []
            for file in os.listdir(self.backup_dir):
                if file.endswith('.db'):
                    file_path = os.path.join(self.backup_dir, file)
                    if os.path.isfile(file_path):
                        stat = os.stat(file_path)
                        backups.append({
                            'filename': file,
                            'size': stat.st_size,
                            'mtime': datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                        })
            
            # 按修改时间排序
            backups.sort(key=lambda x: x['mtime'], reverse=True)
            return backups
        except Exception as e:
            logger.error(f"获取备份列表失败: {e}")
            return []

    def restore_from_backup(self, backup_filename):
        """从备份恢复"""
        try:
            backup_path = os.path.join(self.backup_dir, backup_filename)
            
            if not os.path.exists(backup_path):
                return "备份文件不存在"
            
            # 复制回数据库文件
            shutil.copy2(backup_path, self.db_path)
            logger.info(f"从备份恢复成功: {backup_filename}")
            return f"从备份恢复成功: {backup_filename}"
        except Exception as e:
            logger.error(f"恢复失败: {e}")
            return f"恢复失败: {e}"
