import sqlite3
import os
import json
from datetime import datetime

# 容错处理
try:
    from astrbot import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

from .backup import BackupManager

class DatabaseManager:
    def __init__(self, config=None):
        # 将数据库文件存储在插件目录的上上个目录中，确保跨平台兼容
        # 路径：d:\Astrbot\AstrBot\data\memory_capsule.db
        app_data_dir = os.path.join(os.path.dirname(__file__), "..", "..")
        os.makedirs(app_data_dir, exist_ok=True)
        self.db_path = os.path.join(app_data_dir, "memory_capsule.db")
        self.config = config or {}
        self.backup_manager = BackupManager(self.db_path, self.config)
        
        # 初始化数据库结构
        self._initialize_database_structure()

    def _initialize_database_structure(self):
        """初始化数据库结构"""
        conn = None
        try:
            # 创建数据库目录
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            
            # 临时连接创建表结构
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 创建关系表 (relationships) —— 名片夹
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS relationships (
                user_id TEXT PRIMARY KEY,       -- 对方 QQ 号
                nickname TEXT,                  -- AI 对 TA 的称呼
                relation_type TEXT,             -- 关系 (如: 朋友, 损友)
                intimacy INTEGER DEFAULT 50,    -- 好感度 (0-100)
                tags TEXT,                      -- 印象标签 (如: "幽默,程序员")
                summary TEXT,                   -- 核心印象 (覆盖式更新，不追加)
                first_met_time TIMESTAMP,       -- 初次见面时间
                first_met_location TEXT,        -- 初次见面地点 (如: "QQ群:12345")
                known_contexts TEXT,            -- 遇到过的场景 (JSON列表)
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # 创建记忆表 (memories) —— 笔记本
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,                   -- 关联对象 (如果是关于某人的记忆)
                source_platform TEXT,           -- 来源 (QQ, Bilibili, Web)
                source_context TEXT,            -- 场景 (群号, 视频ID)
                category TEXT,                  -- 分类 (社交, 知识, 娱乐, 日记)
                tags TEXT,                      -- 标签 (方便检索)
                content TEXT NOT NULL,          -- 记忆正文
                importance INTEGER DEFAULT 5,   -- 重要性 (1-10)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # 建立索引加速搜索
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mem_cate ON memories(category)')
            
            # 全文搜索虚拟表 (SQLite FTS5)
            try:
                cursor.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, tags, category)
                ''')
            except:
                pass
            
            # 创建触发器，当memories表变化时更新全文搜索表
            try:
                cursor.execute('''
                CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories
                BEGIN
                    INSERT INTO memories_fts(rowid, content, tags, category) VALUES (new.id, new.content, new.tags, new.category);
                END
                ''')
            except:
                pass
            
            try:
                cursor.execute('''
                CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories
                BEGIN
                    UPDATE memories_fts SET content = new.content, tags = new.tags, category = new.category WHERE rowid = old.id;
                END
                ''')
            except:
                pass
            
            try:
                cursor.execute('''
                CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories
                BEGIN
                    DELETE FROM memories_fts WHERE rowid = old.id;
                END
                ''')
            except:
                pass
            
            conn.commit()
            logger.info(f"数据库结构初始化成功: {self.db_path}")
        except Exception as e:
            logger.error(f"数据库结构初始化失败: {e}")
        finally:
            if conn:
                conn.close()

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

    def write_memory(self, content, category="日常", tags="", target_user_id=None, source_platform="Web", source_context="", importance=5):
        """存储记忆
        
        参数说明：
        - content: 记忆内容
        - category: 分类 (默认 "日常")
        - tags: 标签 (逗号分隔)
        - target_user_id: 如果是关于特定人的记忆，填这里
        - source_platform: 来源 (默认 "Web")
        - source_context: 场景
        - importance: 重要性 (1-10，默认 5)
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 插入数据
            cursor.execute('''
            INSERT INTO memories (user_id, source_platform, source_context, category, tags, content, importance)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (target_user_id, source_platform, source_context, category, tags, content, importance))
            
            memory_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            return f"记忆存储成功，ID: {memory_id}"
        except Exception as e:
            logger.error(f"存储记忆失败: {e}")
            return f"存储失败: {e}"

    def search_memory(self, query, target_user_id=None):
        """搜索记忆
        
        参数说明：
        - query: 搜索关键词或句子
        - target_user_id: 限定搜索某人的相关记忆
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 使用全文搜索
            if query:
                search_query = "SELECT m.* FROM memories m JOIN memories_fts f ON m.id = f.rowid WHERE f.memories_fts MATCH ?"
                params = [f"{query}"]
                
                if target_user_id:
                    search_query += " AND m.user_id = ?"
                    params.append(target_user_id)
                
                search_query += " ORDER BY m.importance DESC, m.created_at DESC LIMIT 50"
            else:
                # 没有查询关键词，返回最新的记忆
                search_query = "SELECT * FROM memories WHERE 1=1"
                params = []
                
                if target_user_id:
                    search_query += " AND user_id = ?"
                    params.append(target_user_id)
                
                search_query += " ORDER BY importance DESC, created_at DESC LIMIT 50"
            
            cursor.execute(search_query, params)
            results = cursor.fetchall()
            conn.close()
            
            # 处理结果
            memory_list = []
            for row in results:
                memory = {
                    "id": row[0],
                    "user_id": row[1],
                    "source_platform": row[2],
                    "source_context": row[3],
                    "category": row[4],
                    "tags": row[5],
                    "content": row[6],
                    "importance": row[7],
                    "created_at": row[8]
                }
                memory_list.append(memory)
            
            return memory_list
        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return []

    def delete_memory(self, memory_id):
        """删除记忆
        
        参数说明：
        - memory_id: 记忆的 ID
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM memories WHERE id = ?', (memory_id,))
            conn.commit()
            conn.close()
            
            return "删除成功"
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            return f"删除失败: {e}"

    def update_relationship(self, user_id, relation_type=None, tags_update=None, summary_update=None, intimacy_change=0, nickname=None, first_met_time=None, first_met_location=None, known_contexts=None):
        """更新关系
        
        参数说明：
        - user_id: 目标用户 ID
        - relation_type: 新的关系定义
        - tags_update: 新的标签 (会覆盖旧的)
        - summary_update: 新的印象总结 (会覆盖旧的)
        - intimacy_change: 好感度变化值 (如 +5, -10)
        - nickname: AI 对 TA 的称呼
        - first_met_time: 初次见面时间
        - first_met_location: 初次见面地点
        - known_contexts: 遇到过的场景 (JSON列表)
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 检查是否存在记录
            cursor.execute('SELECT nickname, relation_type, intimacy, tags, summary, first_met_time, first_met_location, known_contexts FROM relationships WHERE user_id=?', (user_id,))
            existing = cursor.fetchone()
            
            if existing:
                # 更新现有记录
                old_nickname, old_relation_type, old_intimacy, old_tags, old_summary, old_first_met_time, old_first_met_location, old_known_contexts = existing
                
                # 处理各字段
                new_nickname = nickname or old_nickname
                new_relation_type = relation_type or old_relation_type
                new_intimacy = old_intimacy + intimacy_change
                new_intimacy = max(0, min(100, new_intimacy))  # 限制在 0-100
                new_tags = tags_update or old_tags
                new_summary = summary_update or old_summary
                new_first_met_time = first_met_time or old_first_met_time
                new_first_met_location = first_met_location or old_first_met_location
                new_known_contexts = known_contexts or old_known_contexts
                
                # 执行更新
                cursor.execute('''
                UPDATE relationships SET 
                    nickname = ?, 
                    relation_type = ?, 
                    intimacy = ?, 
                    tags = ?, 
                    summary = ?, 
                    first_met_time = ?, 
                    first_met_location = ?, 
                    known_contexts = ?, 
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                ''', (new_nickname, new_relation_type, new_intimacy, new_tags, new_summary, new_first_met_time, new_first_met_location, new_known_contexts, user_id))
            else:
                # 创建新记录
                new_intimacy = 50 + intimacy_change
                new_intimacy = max(0, min(100, new_intimacy))  # 限制在 0-100
                
                cursor.execute('''
                INSERT INTO relationships (user_id, nickname, relation_type, intimacy, tags, summary, first_met_time, first_met_location, known_contexts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (user_id, nickname or "", relation_type or "", new_intimacy, tags_update or "", summary_update or "", first_met_time, first_met_location, known_contexts))
            
            conn.commit()
            conn.close()
            
            return "关系更新成功"
        except Exception as e:
            logger.error(f"更新关系失败: {e}")
            return f"更新失败: {e}"
    
    def get_all_tags(self):
        """获取所有标签"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 获取所有标签
            cursor.execute('SELECT tags FROM memories WHERE tags IS NOT NULL AND tags != ""')
            results = cursor.fetchall()
            conn.close()
            
            # 提取标签
            tags = set()
            for row in results:
                try:
                    tag_list = row[0].split(',')
                    for tag in tag_list:
                        tag = tag.strip()
                        if tag:
                            tags.add(tag)
                except:
                    pass
            
            return list(tags)
        except Exception as e:
            logger.error(f"获取标签失败: {e}")
            return []
    
    def get_all_categories(self):
        """获取所有分类"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 获取所有分类
            cursor.execute('SELECT DISTINCT category FROM memories WHERE category IS NOT NULL')
            results = cursor.fetchall()
            conn.close()
            
            categories = [row[0] for row in results]
            return categories
        except Exception as e:
            logger.error(f"获取分类失败: {e}")
            return []

    def get_all_memories(self, limit=100, offset=0, category=None):
        """获取所有记忆"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            if category:
                cursor.execute('SELECT * FROM memories WHERE category=? ORDER BY importance DESC, created_at DESC LIMIT ? OFFSET ?', (category, limit, offset))
            else:
                cursor.execute('SELECT * FROM memories ORDER BY importance DESC, created_at DESC LIMIT ? OFFSET ?', (limit, offset))
            
            results = cursor.fetchall()
            conn.close()
            
            memory_list = []
            for row in results:
                memory = {
                    "id": row[0],
                    "user_id": row[1],
                    "source_platform": row[2],
                    "source_context": row[3],
                    "category": row[4],
                    "tags": row[5],
                    "content": row[6],
                    "importance": row[7],
                    "created_at": row[8]
                }
                memory_list.append(memory)
            
            return memory_list
        except Exception as e:
            logger.error(f"获取记忆失败: {e}")
            return []
    
    def get_memories_count(self, category=None):
        """获取记忆总数"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            if category:
                cursor.execute('SELECT COUNT(*) FROM memories WHERE category=?', (category,))
            else:
                cursor.execute('SELECT COUNT(*) FROM memories')
            
            count = cursor.fetchone()[0]
            conn.close()
            
            return count
        except Exception as e:
            logger.error(f"获取记忆总数失败: {e}")
            return 0

    def get_all_relationships(self, limit=100, offset=0):
        """获取所有关系"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT * FROM relationships ORDER BY updated_at DESC LIMIT ? OFFSET ?', (limit, offset))
            results = cursor.fetchall()
            conn.close()
            
            relationship_list = []
            for row in results:
                relationship = {
                    "user_id": row[0],
                    "nickname": row[1],
                    "relation_type": row[2],
                    "intimacy": row[3],
                    "tags": row[4],
                    "summary": row[5],
                    "first_met_time": row[6],
                    "first_met_location": row[7],
                    "known_contexts": row[8],
                    "updated_at": row[9]
                }
                relationship_list.append(relationship)
            
            return relationship_list
        except Exception as e:
            logger.error(f"获取关系失败: {e}")
            return []
    
    def get_relationships_count(self):
        """获取关系总数"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT COUNT(*) FROM relationships')
            count = cursor.fetchone()[0]
            conn.close()
            
            return count
        except Exception as e:
            logger.error(f"获取关系总数失败: {e}")
            return 0

    def delete_relationship(self, user_id):
        """删除关系"""
        try:
            # 使用独立连接
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM relationships WHERE user_id = ?', (user_id,))
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
