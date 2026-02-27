import os
import shutil
import datetime
import time
import threading

# 容错处理
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
        self.auto_backup_interval = self.config.get('backup_interval', 24) * 60 * 60  # 转换为秒
        self.backup_thread = None
        self.running = False
        
        # 从配置中获取阶梯式备份策略
        self.backup_policy = {
            'hourly': self.config.get('backup_hourly', 24),  # 保留最近24小时的每小时备份
            'daily': self.config.get('backup_daily', 7),      # 保留最近7天的每天备份
            'weekly': self.config.get('backup_weekly', 4),     # 保留最近4周的每周备份
            'monthly': self.config.get('backup_monthly', 12)   # 保留最近12个月的每月备份
        }
        
        # 创建备份目录
        os.makedirs(self.backup_dir, exist_ok=True)

    def backup(self):
        """执行备份"""
        try:
            # 生成备份文件名
            now = datetime.datetime.now()
            timestamp = now.strftime("%Y%m%d_%H%M%S")
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
        """清理旧备份，实现阶梯式备份策略"""
        try:
            # 获取所有备份文件
            backups = []
            for file in os.listdir(self.backup_dir):
                if file.endswith('.db'):
                    file_path = os.path.join(self.backup_dir, file)
                    if os.path.isfile(file_path):
                        mtime = os.path.getmtime(file_path)
                        backups.append((file_path, mtime))
            
            # 按修改时间排序（最新的在前）
            backups.sort(key=lambda x: x[1], reverse=True)
            
            # 分类备份文件
            now = time.time()
            hourly_backups = []
            daily_backups = []
            weekly_backups = []
            monthly_backups = []
            
            for backup_path, mtime in backups:
                age = now - mtime
                if age < 24 * 3600:  # 1小时内
                    hourly_backups.append((backup_path, mtime))
                elif age < 7 * 24 * 3600:  # 1天内
                    daily_backups.append((backup_path, mtime))
                elif age < 4 * 7 * 24 * 3600:  # 1周内
                    weekly_backups.append((backup_path, mtime))
                elif age < 12 * 30 * 24 * 3600:  # 1个月内
                    monthly_backups.append((backup_path, mtime))
            
            # 清理超出保留数量的备份
            self._cleanup_backup_group(hourly_backups, self.backup_policy['hourly'])
            self._cleanup_backup_group(daily_backups, self.backup_policy['daily'])
            self._cleanup_backup_group(weekly_backups, self.backup_policy['weekly'])
            self._cleanup_backup_group(monthly_backups, self.backup_policy['monthly'])
            
        except Exception as e:
            logger.error(f"清理备份失败: {e}")
    
    def _cleanup_backup_group(self, backups, max_count):
        """清理指定组的备份文件，保留最新的max_count个"""
        if len(backups) > max_count:
            for backup_path, _ in backups[max_count:]:
                os.remove(backup_path)
                logger.info(f"删除旧备份: {os.path.basename(backup_path)}")

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
