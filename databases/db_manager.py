import sqlite3
import os
import json
from datetime import datetime
from astrbot import logger
from .backup import BackupManager

class DatabaseManager:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(__file__), "..", "data", "memory.db")
        self.backup_manager = BackupManager(self.db_path)
        
        # 初始化数据库结构
        self._initialize_database_structure()

    def _initialize_database_structure(self):
        """初始化数据库结构"""
        try:
            # 创建数据库目录
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            
            # 临时连接创建表结构
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 创建插件数据表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS plugin_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_name TEXT NOT NULL,
                data_type TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT,
                category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # 创建关系数据表
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                nickname TEXT,
                alias_history TEXT,
                impression_summary TEXT,
                favor_level INTEGER DEFAULT 50,
                interaction_count INTEGER DEFAULT 0,
                last_interaction_time TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, group_id)
            )
            ''')
            
            # 创建索引
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_plugin_data_category ON plugin_data(category)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_plugin_data_plugin ON plugin_data(plugin_name, data_type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_relations_user ON relations(user_id)')
            
            conn.commit()
            conn.close()
            
            logger.info(f"数据库结构初始化成功: {self.db_path}")
        except Exception as e:
            logger.error(f"数据库结构初始化失败: {e}")

    def initialize(self):
        """初始化数据库"""
        try:
            # 启动自动备份
            self.backup_manager.start_auto_backup()
            
            logger.info(f"数据库初始化成功: {self.db_path}")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")

    def _get_connection(self):
        """获取数据库连接（每个线程独立）"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def store_plugin_data(self, plugin_name, data_type, content, metadata=None):
        """存储插件数据"""
        try:
            # 生成分类路径
            category = f"{plugin_name}/{data_type}/{datetime.now().strftime('%Y/%m')}"
            
            # 处理元数据
            metadata_json = json.dumps(metadata) if metadata else None
            
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 插入数据
            cursor.execute('''
            INSERT INTO plugin_data (plugin_name, data_type, content, metadata, category, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (plugin_name, data_type, content, metadata_json, category))
            
            conn.commit()
            conn.close()
            
            return f"数据存储成功，分类: {category}"
        except Exception as e:
            logger.error(f"存储数据失败: {e}")
            return f"存储失败: {e}"

    def query_plugin_data(self, query_keyword, plugin_name=None, data_type=None):
        """查询插件数据"""
        try:
            # 构建查询语句
            query = "SELECT id, plugin_name, data_type, content, metadata, category, created_at FROM plugin_data WHERE 1=1"
            params = []
            
            if query_keyword:
                query += " AND (content LIKE ? OR metadata LIKE ?)"
                params.extend([f"%{query_keyword}%", f"%{query_keyword}%"])
            
            if plugin_name:
                query += " AND plugin_name = ?"
                params.append(plugin_name)
            
            if data_type:
                query += " AND data_type = ?"
                params.append(data_type)
            
            query += " ORDER BY created_at DESC LIMIT 10"
            
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 执行查询
            cursor.execute(query, params)
            results = cursor.fetchall()
            conn.close()
            
            # 处理结果
            data_list = []
            for row in results:
                data = {
                    "id": row[0],
                    "plugin_name": row[1],
                    "data_type": row[2],
                    "content": row[3],
                    "metadata": json.loads(row[4]) if row[4] else None,
                    "category": row[5],
                    "created_at": row[6]
                }
                data_list.append(data)
            
            return data_list
        except Exception as e:
            logger.error(f"查询数据失败: {e}")
            return []

    def update_relation(self, user_id, group_id, nickname=None, favor_change=0, impression=None, note=None):
        """更新关系"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 检查是否存在记录
            cursor.execute('SELECT id, nickname, alias_history, impression_summary, favor_level FROM relations WHERE user_id=? AND group_id=?', (user_id, group_id))
            existing = cursor.fetchone()
            
            if existing:
                # 更新现有记录
                relation_id, old_nickname, alias_history, old_impression, old_favor = existing
                
                # 处理昵称变化
                new_nickname = nickname or old_nickname
                new_alias_history = alias_history or ""
                if nickname and nickname != old_nickname:
                    if new_alias_history:
                        new_alias_history += f",{old_nickname}"
                    else:
                        new_alias_history = old_nickname
                
                # 处理好感度
                new_favor = old_favor + favor_change
                new_favor = max(0, min(100, new_favor))
                
                # 处理印象
                new_impression = old_impression or ""
                if impression:
                    if new_impression:
                        new_impression += f"\n{impression}"
                    else:
                        new_impression = impression
                
                # 执行更新
                cursor.execute('''
                UPDATE relations SET 
                    nickname = ?, 
                    alias_history = ?, 
                    impression_summary = ?, 
                    favor_level = ?, 
                    interaction_count = interaction_count + 1, 
                    last_interaction_time = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''', (new_nickname, new_alias_history, new_impression, new_favor, relation_id))
            else:
                # 创建新记录
                cursor.execute('''
                INSERT INTO relations (user_id, group_id, nickname, favor_level, interaction_count, last_interaction_time)
                VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                ''', (user_id, group_id, nickname or "Unknown", 50 + favor_change))
            
            conn.commit()
            conn.close()
            
            return "关系更新成功"
        except Exception as e:
            logger.error(f"更新关系失败: {e}")
            return f"更新失败: {e}"

    def query_relation(self, query_keyword):
        """查询关系"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 模糊搜索
            cursor.execute('''
            SELECT user_id, nickname, group_id, impression_summary, favor_level 
            FROM relations 
            WHERE nickname LIKE ? OR impression_summary LIKE ? OR alias_history LIKE ?
            ''', (f"%{query_keyword}%", f"%{query_keyword}%", f"%{query_keyword}%"))
            
            results = cursor.fetchall()
            conn.close()
            
            # 处理结果
            relation_list = []
            for row in results:
                relation = {
                    "user_id": row[0],
                    "nickname": row[1],
                    "group_id": row[2],
                    "impression": row[3],
                    "favor_level": row[4]
                }
                relation_list.append(relation)
            
            return relation_list
        except Exception as e:
            logger.error(f"查询关系失败: {e}")
            return []

    def get_all_plugin_data(self, limit=100):
        """获取所有插件数据"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT id, plugin_name, data_type, content, category, created_at FROM plugin_data ORDER BY created_at DESC LIMIT ?', (limit,))
            results = cursor.fetchall()
            conn.close()
            
            data_list = []
            for row in results:
                data = {
                    "id": row[0],
                    "plugin_name": row[1],
                    "data_type": row[2],
                    "content": row[3],
                    "category": row[4],
                    "created_at": row[5]
                }
                data_list.append(data)
            
            return data_list
        except Exception as e:
            logger.error(f"获取数据失败: {e}")
            return []

    def get_all_relations(self):
        """获取所有关系"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT id, user_id, nickname, group_id, impression_summary, favor_level, created_at FROM relations ORDER BY created_at DESC')
            results = cursor.fetchall()
            conn.close()
            
            relation_list = []
            for row in results:
                relation = {
                    "id": row[0],
                    "user_id": row[1],
                    "nickname": row[2],
                    "group_id": row[3],
                    "impression": row[4],
                    "favor_level": row[5],
                    "created_at": row[6]
                }
                relation_list.append(relation)
            
            return relation_list
        except Exception as e:
            logger.error(f"获取关系失败: {e}")
            return []

    def delete_plugin_data(self, data_id):
        """删除插件数据"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM plugin_data WHERE id = ?', (data_id,))
            conn.commit()
            conn.close()
            
            return "删除成功"
        except Exception as e:
            logger.error(f"删除数据失败: {e}")
            return f"删除失败: {e}"

    def delete_relation(self, user_id, group_id):
        """删除关系"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM relations WHERE user_id = ? AND group_id = ?', (user_id, group_id))
            conn.commit()
            conn.close()
            
            return "删除成功"
        except Exception as e:
            logger.error(f"删除关系失败: {e}")
            return f"删除失败: {e}"

    def close(self):
        """关闭数据库连接"""
        # 停止自动备份
        self.backup_manager.stop_auto_backup()
        logger.info("数据库连接已关闭")

    def backup(self):
        """手动执行备份"""
        return self.backup_manager.backup()

    def get_backup_list(self):
        """获取备份列表"""
        return self.backup_manager.get_backup_list()

    def restore_from_backup(self, backup_filename):
        """从备份恢复"""
        result = self.backup_manager.restore_from_backup(backup_filename)
        # 恢复后需要重新连接数据库
        if "成功" in result:
            self.conn = sqlite3.connect(self.db_path)
            self.cursor = self.conn.cursor()
        return result
