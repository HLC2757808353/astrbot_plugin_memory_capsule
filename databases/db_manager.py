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
            
            # 创建插件数据表（笔记表）
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS plugin_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id TEXT NOT NULL,
                data_type TEXT NOT NULL DEFAULT 'string',
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
                platform TEXT DEFAULT 'qq',
                nickname TEXT,
                alias_history TEXT,
                impression_summary TEXT,
                remark TEXT,
                favor_level INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, group_id, platform)
            )
            ''')
            
            # 创建索引
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_plugin_data_category ON plugin_data(category)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_plugin_data_note ON plugin_data(note_id, data_type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_relations_user ON relations(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_relations_platform ON relations(platform)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_relations_user_group_platform ON relations(user_id, group_id, platform)')
            
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

    def store_plugin_data(self, content, metadata=None):
        """存储笔记数据
        
        参数说明：
        - content: 笔记内容，固定为字符串类型
        - metadata: 元数据，用于存储笔记的额外信息，如标签、关键词等
        
        笔记表字段功能说明：
        - id: 主键ID
        - note_id: 笔记编号，唯一标识笔记
        - data_type: 数据类型，默认固定为'string'
        - content: 笔记内容，字符串类型
        - metadata: 元数据，用于存储额外信息
        - category: 分类路径
        - created_at: 笔记记录时间
        - updated_at: 更新时间
        """
        try:
            # 生成笔记编号
            note_id = f"NOTE_{datetime.now().strftime('%Y%m%d%H%M%S')}_{hash(content) % 10000:04d}"
            
            # 生成分类路径
            category = f"notes/{datetime.now().strftime('%Y/%m')}"
            
            # 处理元数据
            metadata_json = json.dumps(metadata) if metadata else None
            
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 插入数据
            cursor.execute('''
            INSERT INTO plugin_data (note_id, data_type, content, metadata, category, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (note_id, 'string', content, metadata_json, category))
            
            conn.commit()
            conn.close()
            
            return f"笔记存储成功，编号: {note_id}"
        except Exception as e:
            logger.error(f"存储笔记失败: {e}")
            return f"存储失败: {e}"

    def query_plugin_data(self, query_keyword, data_type=None):
        """查询笔记数据"""
        try:
            # 构建查询语句
            query = "SELECT id, note_id, data_type, content, metadata, category, created_at FROM plugin_data WHERE 1=1"
            params = []
            
            if query_keyword:
                query += " AND (content LIKE ? OR metadata LIKE ? OR note_id LIKE ?)"
                params.extend([f"%{query_keyword}%", f"%{query_keyword}%", f"%{query_keyword}%"])
            
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
                    "note_id": row[1],
                    "data_type": row[2],
                    "content": row[3],
                    "metadata": json.loads(row[4]) if row[4] else None,
                    "category": row[5],
                    "created_at": row[6]
                }
                data_list.append(data)
            
            return data_list
        except Exception as e:
            logger.error(f"查询笔记失败: {e}")
            return []

    def update_relation(self, user_id, group_id, platform='qq', nickname=None, favor_change=0, impression=None, remark=None):
        """更新关系
        
        参数说明：
        - user_id: 用户ID，唯一标识用户
        - group_id: 群组ID，标识用户所在的群组
        - platform: 平台字段，默认为'qq'，未来可能扩展其他平台
        - nickname: 用户昵称
        - favor_change: 好感度变化值，会累加到当前好感度
        - impression: 印象摘要，记录对用户的印象
        - remark: 备注字段，用于存储额外的备注信息
        
        关系表字段功能说明：
        - id: 主键ID
        - user_id: 用户ID，唯一标识用户
        - group_id: 群组ID，标识用户所在的群组
        - platform: 平台字段，默认为'qq'，未来可能扩展其他平台
        - nickname: 用户昵称
        - alias_history: 昵称历史，记录用户曾经使用过的昵称
        - impression_summary: 印象摘要，记录对用户的印象
        - remark: 备注字段，用于存储额外的备注信息
        - favor_level: 好感度，默认值为0，范围0-100
        - created_at: 创建时间
        """
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 检查是否存在记录
            cursor.execute('SELECT id, nickname, alias_history, impression_summary, remark, favor_level FROM relations WHERE user_id=? AND group_id=? AND platform=?', (user_id, group_id, platform))
            existing = cursor.fetchone()
            
            if existing:
                # 更新现有记录
                relation_id, old_nickname, alias_history, old_impression, old_remark, old_favor = existing
                
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
                
                # 处理备注
                new_remark = remark or old_remark
                
                # 执行更新
                cursor.execute('''
                UPDATE relations SET 
                    nickname = ?, 
                    alias_history = ?, 
                    impression_summary = ?, 
                    remark = ?, 
                    favor_level = ?
                WHERE id = ?
                ''', (new_nickname, new_alias_history, new_impression, new_remark, new_favor, relation_id))
            else:
                # 创建新记录
                cursor.execute('''
                INSERT INTO relations (user_id, group_id, platform, nickname, remark, favor_level)
                VALUES (?, ?, ?, ?, ?, ?)
                ''', (user_id, group_id, platform, nickname or "Unknown", remark or "", favor_change))
            
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
            SELECT user_id, nickname, group_id, platform, impression_summary, remark, favor_level, created_at 
            FROM relations 
            WHERE nickname LIKE ? OR impression_summary LIKE ? OR alias_history LIKE ? OR remark LIKE ?
            ''', (f"%{query_keyword}%", f"%{query_keyword}%", f"%{query_keyword}%", f"%{query_keyword}%"))
            
            results = cursor.fetchall()
            conn.close()
            
            # 处理结果
            relation_list = []
            for row in results:
                relation = {
                    "user_id": row[0],
                    "nickname": row[1],
                    "group_id": row[2],
                    "platform": row[3],
                    "impression": row[4],
                    "remark": row[5],
                    "favor_level": row[6],
                    "created_at": row[7]
                }
                relation_list.append(relation)
            
            return relation_list
        except Exception as e:
            logger.error(f"查询关系失败: {e}")
            return []

    def get_all_plugin_data(self, limit=100):
        """获取所有笔记数据"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT id, note_id, data_type, content, category, created_at FROM plugin_data ORDER BY created_at DESC LIMIT ?', (limit,))
            results = cursor.fetchall()
            conn.close()
            
            data_list = []
            for row in results:
                data = {
                    "id": row[0],
                    "note_id": row[1],
                    "data_type": row[2],
                    "content": row[3],
                    "category": row[4],
                    "created_at": row[5]
                }
                data_list.append(data)
            
            return data_list
        except Exception as e:
            logger.error(f"获取笔记失败: {e}")
            return []

    def get_all_relations(self):
        """获取所有关系"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT id, user_id, nickname, group_id, platform, impression_summary, remark, favor_level, created_at FROM relations ORDER BY created_at DESC')
            results = cursor.fetchall()
            conn.close()
            
            relation_list = []
            for row in results:
                relation = {
                    "id": row[0],
                    "user_id": row[1],
                    "nickname": row[2],
                    "group_id": row[3],
                    "platform": row[4],
                    "impression": row[5],
                    "remark": row[6],
                    "favor_level": row[7],
                    "created_at": row[8]
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

    def delete_relation(self, user_id, group_id, platform='qq'):
        """删除关系"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM relations WHERE user_id = ? AND group_id = ? AND platform = ?', (user_id, group_id, platform))
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
        # 恢复后不需要重新连接数据库，因为我们使用的是每个操作独立的连接
        return result
