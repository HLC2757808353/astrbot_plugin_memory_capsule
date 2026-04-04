import sqlite3
import os
import json
import re
import time
from datetime import datetime

# 容错处理
try:
    from astrbot import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

# 导入分词库（延迟加载，节省启动内存）
_jieba_initialized = False
_jieba_instance = None
_pseg_instance = None

def _get_jieba():
    """延迟获取jieba实例（首次调用时才加载）"""
    global _jieba_initialized, _jieba_instance, _pseg_instance
    
    if not _jieba_initialized:
        try:
            import jieba
            import jieba.posseg as pseg
            _jieba_instance = jieba
            _pseg_instance = pseg
            _jieba_initialized = True
            logger.info("jieba分词库已延迟加载")
        except ImportError:
            logger.warning("jieba库未安装，标签提取功能将受限")
    
    return _jieba_instance, _pseg_instance


# 导入拼音库（延迟加载）
_pypinyin_initialized = False
_pypinyin_instance = None

def _get_pypinyin():
    """延迟获取pypinyin实例"""
    global _pypinyin_initialized, _pypinyin_instance
    
    if not _pypinyin_initialized:
        try:
            from pypinyin import pypinyin as pypy
            _pypinyin_instance = pypy
            _pypinyin_initialized = True
        except ImportError:
            logger.warning("pypinyin库未安装，拼音匹配功能将受限")
    
    return _pypinyin_instance


from .backup import BackupManager


class TTLCache:
    """带过期时间的LRU缓存"""
    
    def __init__(self, max_size=1000, default_ttl=300):
        """
        参数：
        - max_size: 最大缓存条目数
        - default_ttl: 默认过期时间（秒），默认5分钟
        """
        self.cache = {}
        self.max_size = max_size
        self.default_ttl = default_ttl
    
    def get(self, key):
        """获取缓存值，如果已过期则删除并返回None"""
        if key in self.cache:
            item = self.cache[key]
            current_time = time.time()
            
            if current_time < item['expires_at']:
                return item['value']
            else:
                del self.cache[key]
                return None
        
        return None
    
    def set(self, key, value, ttl=None):
        """设置缓存值
        
        参数：
        - key: 缓存键
        - value: 缓存值
        - ttl: 过期时间（秒），使用默认值如果未指定
        """
        if len(self.cache) >= self.max_size:
            self._evict_oldest()
        
        expires_at = time.time() + (ttl or self.default_ttl)
        self.cache[key] = {
            'value': value,
            'expires_at': expires_at,
            'created_at': time.time()
        }
    
    def _evict_oldest(self):
        """淘汰最旧的缓存条目"""
        if not self.cache:
            return
        
        oldest_key = min(self.cache.keys(), 
                        key=lambda k: self.cache[k]['created_at'])
        del self.cache[oldest_key]
    
    def invalidate(self, key):
        """使指定缓存失效"""
        if key in self.cache:
            del self.cache[key]
    
    def clear(self):
        """清空所有缓存"""
        self.cache.clear()
    
    def __contains__(self, key):
        """检查键是否存在且未过期"""
        return self.get(key) is not None
    
    def __len__(self):
        """返回当前缓存大小（仅有效条目）"""
        current_time = time.time()
        valid_items = {k: v for k, v in self.cache.items() 
                      if current_time < v['expires_at']}
        return len(valid_items)

class MemoryColumn:
    """记忆表列索引常量 - 必须与SELECT语句的列顺序完全一致！
    
    重要：由于历史数据库迁移问题，物理表列顺序可能与CREATE TABLE不同。
    因此所有查询必须使用显式列名：SELECT id, category, content, tags, importance, created_at, updated_at, access_count
    
    索引对应：
    0 = id
    1 = category  
    2 = content
    3 = tags
    4 = importance
    5 = created_at
    6 = updated_at
    7 = access_count
    """
    ID = 0
    CATEGORY = 1
    CONTENT = 2
    TAGS = 3
    IMPORTANCE = 4
    CREATED_AT = 5
    UPDATED_AT = 6
    ACCESS_COUNT = 7


class DatabaseManager:
    def __init__(self, config=None, context=None):
        app_data_dir = os.path.join(os.path.dirname(__file__), "..", "..")
        os.makedirs(app_data_dir, exist_ok=True)
        self.db_path = os.path.join(app_data_dir, "memory_capsule.db")
        self.config = config or {}
        self.context = context
        backup_config = {
            'interval': self.config.get('backup_interval', 24),
            'max_count': self.config.get('backup_max_count', 10)
        }
        self.backup_manager = BackupManager(self.db_path, backup_config)
        
        self._initialize_database_structure()
        
        self.search_weights = self.config.get('search_weights', {
            'tag_match': 5.0,
            'recent_boost': 3.0,
            'mid_boost': 2.0,
            'popularity': 1.0,
            'category_match': 2.0,
            'full_match_bonus': 10.0
        })
        
        self.search_strategy = self.config.get('search_strategy', {
            'match_type': 'AND',
            'synonym_expansion': True,
            'time_decay': True,
            'category_filter': False,
            'enable_fallback': True
        })
        
        # 初始化TTL缓存（5分钟过期）
        cache_ttl = self.config.get('cache_ttl', 300)
        self.cache = TTLCache(
            max_size=self.config.get('max_cache_size', 1000),
            default_ttl=cache_ttl
        )
    
    def extract_tags_optimized(self, content):
        """智能标签提取器
        
        使用混合策略：规则 + jieba分词 + 实体识别
        """
        tags = []
        
        # 延迟获取jieba实例
        _, pseg_instance = _get_jieba()
        
        # 策略1：jieba分词提取名词（最可靠）
        if pseg_instance:
            words = pseg_instance.cut(content)
            for word, flag in words:
                if flag.startswith('n') and len(word) >= 2:  # 只取2字以上的名词
                    tags.append(word)
        
        # 策略2：提取技术术语（编程相关）
        tech_terms = re.findall(r'\b(python|java|git|sql|api|json|xml|html|css)\b', content, re.IGNORECASE)
        tags.extend([t.lower() for t in tech_terms])
        
        # 策略3：版本号识别（Python3, Java8等）
        versions = re.findall(r'[a-zA-Z]+\d+', content)
        tags.extend(versions)
        
        # 去重，限制数量
        max_tags = self.config.get('max_extracted_tags', 10)
        return list(dict.fromkeys(tags))[:max_tags]
    
    def add_synonym_pair(self, word1, word2):
        """添加同义词对，确保不重复存储"""
        # 统一存储方向：总是按字母顺序存小的在前
        if word1 < word2:
            base, synonym = word1, word2
        else:
            base, synonym = word2, word1
        
        # 插入数据库（已通过CHECK约束确保不会重复）
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO synonyms (word, synonym, source)
                VALUES (?, ?, ?)
            ''', (base, synonym, 'auto'))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"添加同义词对失败: {e}")
    
    def get_all_synonyms(self, word):
        """获取一个词的所有同义词（双向查找）"""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            # 查询word作为基础词的同义词
            cursor.execute('''
                SELECT synonym FROM synonyms WHERE word = ?
                UNION
                SELECT word FROM synonyms WHERE synonym = ?
            ''', (word, word))
            
            results = [row[0] for row in cursor.fetchall()]
            conn.close()
            return [word] + results  # 总是包含自己
        except Exception as e:
            logger.error(f"获取同义词失败: {e}")
            return [word]


    
    def _migrate_old_data(self):
        """迁移旧数据到新表结构"""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 检查并添加缺失的列
            try:
                # 检查memories表的列结构
                cursor.execute('PRAGMA table_info(memories)')
                columns = [column[1] for column in cursor.fetchall()]
                
                # 添加缺失的created_at列（不能用CURRENT_TIMESTAMP，用NULL）
                if 'created_at' not in columns:
                    logger.info("添加缺失的created_at列...")
                    cursor.execute('ALTER TABLE memories ADD COLUMN created_at TIMESTAMP')
                    conn.commit()
                    logger.info("成功添加created_at列")
                
                # 添加缺失的updated_at列
                if 'updated_at' not in columns:
                    logger.info("添加缺失的updated_at列...")
                    cursor.execute('ALTER TABLE memories ADD COLUMN updated_at TIMESTAMP')
                    conn.commit()
                    logger.info("成功添加updated_at列")
                
                # 添加缺失的tags列
                if 'tags' not in columns:
                    logger.info("添加缺失的tags列...")
                    cursor.execute('ALTER TABLE memories ADD COLUMN tags TEXT')
                    conn.commit()
                    logger.info("成功添加tags列")
                
                # 添加缺失的importance列
                if 'importance' not in columns:
                    logger.info("添加缺失的importance列...")
                    cursor.execute('ALTER TABLE memories ADD COLUMN importance INTEGER DEFAULT 5')
                    conn.commit()
                    logger.info("成功添加importance列")
                
                # 添加缺失的access_count列
                if 'access_count' not in columns:
                    logger.info("添加缺失的access_count列...")
                    cursor.execute('ALTER TABLE memories ADD COLUMN access_count INTEGER DEFAULT 0')
                    conn.commit()
                    logger.info("成功添加access_count列")
            except Exception as e:
                logger.error(f"添加memories列失败: {e}")
            
            try:
                cursor.execute('PRAGMA table_info(relationships)')
                columns = [column[1] for column in cursor.fetchall()]
                
                expected_columns = ['user_id', 'nickname', 'relation_type', 'summary', 'first_met_location', 'known_contexts', 'updated_at']
                
                if len(columns) != len(expected_columns) or not all(col in columns for col in expected_columns):
                    logger.info("relationships表结构不正确，重新创建...")
                    
                    cursor.execute('SELECT * FROM relationships')
                    relationships_data = cursor.fetchall()
                    
                    cursor.execute('DROP TABLE IF EXISTS relationships')
                    
                    cursor.execute('''
                    CREATE TABLE IF NOT EXISTS relationships (
                        user_id TEXT PRIMARY KEY,
                        nickname TEXT,
                        relation_type TEXT,
                        summary TEXT,
                        first_met_location TEXT,
                        known_contexts TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    ''')
                    
                    for data in relationships_data:
                        try:
                            if len(data) >= 7:
                                cursor.execute('''
                                INSERT INTO relationships (user_id, nickname, relation_type, summary, first_met_location, known_contexts, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                ''', (data[0], data[1], data[2], data[4] if len(data) > 4 else '', data[5] if len(data) > 5 else '', data[6] if len(data) > 6 else '', data[7] if len(data) > 7 else None))
                            elif len(data) >= 5:
                                cursor.execute('''
                                INSERT INTO relationships (user_id, nickname, relation_type, summary, first_met_location)
                                VALUES (?, ?, ?, ?, ?)
                                ''', (data[0], data[1], data[2], data[4] if len(data) > 4 else '', data[5] if len(data) > 5 else ''))
                            else:
                                cursor.execute('''
                                INSERT INTO relationships (user_id, nickname, relation_type, summary)
                                VALUES (?, ?, ?, ?)
                                ''', (data[0], data[1] if len(data) > 1 else '', data[2] if len(data) > 2 else '', data[4] if len(data) > 4 else ''))
                        except Exception as e:
                            logger.error(f"恢复关系数据失败: {e}")
                    
                    conn.commit()
                    logger.info("成功重建relationships表")
            except Exception as e:
                logger.error(f"处理relationships表失败: {e}")
            
            # 检查是否需要迁移（如果tags表为空且memories表有数据）
            cursor.execute('SELECT COUNT(*) FROM tags')
            tags_count = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM memories')
            memories_count = cursor.fetchone()[0]
            
            if tags_count == 0 and memories_count > 0:
                logger.info("开始迁移旧数据到新表结构...")
                
                # 迁移标签数据
                cursor.execute('SELECT id, tags FROM memories WHERE tags IS NOT NULL AND tags != ""')
                memory_tags = cursor.fetchall()
                
                for memory_id, tags_str in memory_tags:
                    try:
                        tags = tags_str.split(',')
                        for tag in tags:
                            tag = tag.strip()
                            if tag:
                                cursor.execute('''
                                INSERT OR IGNORE INTO tags (memory_id, tag, source)
                                VALUES (?, ?, ?)
                                ''', (memory_id, tag, 'auto'))
                    except Exception as e:
                        logger.error(f"迁移标签失败: {memory_id}, {e}")
                
                conn.commit()
                logger.info(f"成功迁移 {len(memory_tags)} 条记忆的标签数据")
            
            conn.close()
        except Exception as e:
            logger.error(f"数据迁移失败: {e}")
    
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
                user_id TEXT PRIMARY KEY,
                nickname TEXT,
                relation_type TEXT,
                summary TEXT,
                first_met_location TEXT,
                known_contexts TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # 创建记忆表 (memories) —— 笔记本
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键，唯一标识
                category TEXT,                         -- 分类（AI指定，如"技术笔记"、"生活记录"等）
                content TEXT NOT NULL,                 -- 记忆内容（AI给什么存什么）
                tags TEXT,                             -- 标签（逗号分隔）
                importance INTEGER DEFAULT 5,          -- 重要性（1-10）
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 创建时间
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 更新时间
                access_count INTEGER DEFAULT 0         -- 被搜索到的次数（用于热度统计）
            )
            ''')
            
            # 创建标签表 (tags) —— 用于智能标签管理
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS tags (
                memory_id INTEGER NOT NULL,            -- 关联哪条记忆
                tag TEXT NOT NULL,                     -- 标签内容
                source TEXT DEFAULT 'auto',            -- 来源：auto=自动，manual=手动
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                UNIQUE(memory_id, tag)
            )
            ''')
            
            # 创建同义词表 (synonyms) —— 用于搜索扩展
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS synonyms (
                word TEXT NOT NULL,                    -- 基础词（总是较小的词）
                synonym TEXT NOT NULL,                 -- 同义词（总是较大的词）
                source TEXT DEFAULT 'rule',            -- 来源：rule=规则，learned=学习
                strength FLOAT DEFAULT 1.0,            -- 同义强度（0.0-1.0）
                CHECK (word < synonym),                -- 强制统一存储方向
                UNIQUE(word, synonym)
            )
            ''')
            
            # 创建活动记录表 (activities)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,           -- 操作类型 (添加记忆, 更新关系, 删除记忆等)
                details TEXT,                   -- 操作详情
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # 创建身份别名映射表 (identity_aliases) —— 智能身份识别
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS identity_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,              -- 关联的用户ID（主键）
                alias TEXT NOT NULL,               -- 别名/昵称/群名
                alias_type TEXT DEFAULT 'nickname', -- 别名类型: nickname/group_name/platform_id
                source_context TEXT,                -- 来源场景（哪个群/平台看到的）
                is_current INTEGER DEFAULT 1,       -- 是否为当前使用的名称 (1=是, 0=历史)
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES relationships(user_id) ON DELETE CASCADE,
                UNIQUE(user_id, alias, source_context)  -- 同一用户在同一场景下别名唯一
            )
            ''')
            
            # 建立索引加速搜索
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mem_cate ON memories(category)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mem_time ON memories(created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mem_access ON memories(access_count)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tags_memory ON tags(memory_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_synonyms_word ON synonyms(word)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_synonyms_synonym ON synonyms(synonym)')
            
            # 身份映射表索引
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_alias_user ON identity_aliases(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_alias_name ON identity_aliases(alias)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_alias_type ON identity_aliases(alias_type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_alias_current ON identity_aliases(is_current)')
            
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
            # 首先确保表结构正确
            self._initialize_database_structure()
            # 然后迁移旧数据
            self._migrate_old_data()
            
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

    def _record_activity(self, action, details):
        """记录活动
        
        参数说明：
        - action: 操作类型
        - details: 操作详情
        """
        try:
            # 输入验证（确保数据合法性，包括importance范围检查）
            content, category, tags, importance = self.validate_memory_input(
                content, category, tags, importance
            )
            
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('INSERT INTO activities (action, details) VALUES (?, ?)', (action, details))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"记录活动失败: {e}")

    def validate_memory_input(self, content, category=None, tags=None, importance=None):
        """输入验证和清理
        
        参数说明：
        - content: 记忆内容
        - category: 分类
        - tags: 标签
        - importance: 重要性
        
        返回值：
        - (content, category, tags, importance): 验证后的值
        - 或抛出 ValueError 异常
        """
        # 1. 内容验证
        if not content or not str(content).strip():
            raise ValueError("记忆内容不能为空")
        
        content = str(content).strip()
        
        if len(content) > 10000:
            raise ValueError(f"记忆内容过长（{len(content)}字符），请控制在10000字以内")
        
        if len(content) < 2:
            raise ValueError("记忆内容过短，至少需要2个字符")
        
        # 2. 清理危险字符（防XSS）
        import re
        content = re.sub(r'<script.*?>.*?</script>', '', content, flags=re.I | re.S)
        content = re.sub(r'on\w+\s*=', '', content)  # 移除事件处理器
        
        # 3. 分类验证
        valid_categories = self.get_memory_categories()
        if category and category not in valid_categories:
            logger.warning(f"未知分类 '{category}'，将使用默认分类")
            category = None
        
        # 4. 重要性范围验证
        if importance is not None:
            try:
                importance = int(importance)
                if importance < 1 or importance > 10:
                    logger.warning(f"重要性值 {importance} 超出范围(1-10)，已自动调整为有效值")
                    importance = max(1, min(10, importance))
            except (ValueError, TypeError):
                logger.warning(f"重要性值格式错误，使用默认值1")
                importance = 1
        else:
            importance = 1
        
        # 5. 标签清理
        if tags:
            tag_list = [t.strip() for t in str(tags).split(',') if t.strip()]
            # 过滤长度不合法的标签
            tag_list = [t for t in tag_list if 1 <= len(t) <= 20]
            # 去重并限制数量
            tag_list = list(dict.fromkeys(tag_list))[:15]
            tags = ','.join(tag_list)
        else:
            tags = ""
        
        return content, category, tags, importance

    def write_memory(self, content, category=None, tags="", importance=1):
        """存储记忆
        
        参数说明：
        - content: 记忆内容
        - category: 分类 (AI指定)
        - tags: 标签 (逗号分隔)
        - importance: 重要性 (默认 1)
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 处理标签
            tag_list = []
            
            # 1. 添加用户指定的标签
            if tags:
                user_tags = tags.split(',')
                for tag in user_tags:
                    tag = tag.strip()
                    if tag:
                        tag_list.append(tag)
            
            # 2. 智能提取标签
            auto_tags = self.extract_tags_optimized(content)
            tag_list.extend(auto_tags)
            
            # 3. 去重并合并标签
            unique_tags = list(dict.fromkeys(tag_list))
            
            # 4. 限制标签数量
            max_tags = self.config.get('max_extracted_tags', 10)
            unique_tags = unique_tags[:max_tags]
            
            tags = ','.join(unique_tags)
            
            if category is None:
                category = self.get_default_category()
            
            # 插入数据
            cursor.execute('''
            INSERT INTO memories (category, content, tags, importance)
            VALUES (?, ?, ?, ?)
            ''', (category, content, tags, importance))
            
            memory_id = cursor.lastrowid
            
            conn.commit()
            conn.close()
            
            # 记录活动
            self._record_activity("添加记忆", f"ID: {memory_id}, 分类: {category}")
            
            return f"记忆存储成功，ID: {memory_id}"
        except Exception as e:
            logger.error(f"存储记忆失败: {e}")
            return f"存储失败: {e}"

    def _parse_relative_time(self, query):
        """解析查询中的相对时间表达
        
        返回：
        - (parsed_query, date_str): 处理后的查询和日期字符串
        """
        from datetime import datetime, timedelta
        
        today = datetime.now().date()
        date_str = None
        parsed_query = query
        
        relative_time_patterns = [
            (r'今天', today.strftime('%Y-%m-%d')),
            (r'昨天', (today - timedelta(days=1)).strftime('%Y-%m-%d')),
            (r'前天', (today - timedelta(days=2)).strftime('%Y-%m-%d')),
            (r'大前天', (today - timedelta(days=3)).strftime('%Y-%m-%d')),
            (r'上周|上个星期', (today - timedelta(weeks=1)).strftime('%Y-%m-%d')),
            (r'上个月', (today - timedelta(days=30)).strftime('%Y-%m')),
        ]
        
        for pattern, date in relative_time_patterns:
            if re.search(pattern, query):
                date_str = date
                break
        
        return parsed_query, date_str

    def _build_fts_query(self, query_text, query_tags):
        """构建FTS5查询语句
        
        将用户查询转换为FTS5的MATCH语法
        
        注意事项：
        - FTS5对特殊字符敏感，需要过滤
        - 中文长句需要拆分为关键词
        - 避免使用FTS5保留字（AND, OR, NOT等）
        """
        import re
        
        terms = []
        
        # 添加分词后的标签（更可靠）
        for tag in query_tags:
            if len(tag) >= 2:
                # 过滤FTS5特殊字符
                clean_tag = re.sub(r'[\"*():]', '', tag)
                if clean_tag and len(clean_tag) >= 2:
                    terms.append(clean_tag)
        
        # 对于原始查询文本，只添加短文本或拆分后的词
        if query_text and len(query_text) >= 2:
            # 如果查询文本较短（<=10个字符），直接添加
            if len(query_text) <= 10:
                clean_query = re.sub(r'[\"*():]', '', query_text)
                if clean_query:
                    terms.append(clean_query)
            # 否则不添加原始文本（依赖分词结果即可）
        
        if not terms:
            return None
        
        # 用空格连接，FTS5默认是AND逻辑
        fts_query = ' '.join(terms)
        return fts_query
    
    def _fts_search(self, fts_query, category_filter=None, limit=50):
        """使用FTS5进行快速预筛选
        
        返回候选记忆ID列表
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            if category_filter:
                cursor.execute('''
                    SELECT m.id FROM memories m
                    JOIN memories_fts f ON m.id = f.rowid
                    WHERE memories_fts MATCH ? AND m.category = ?
                    LIMIT ?
                ''', (fts_query, category_filter, limit))
            else:
                cursor.execute('''
                    SELECT m.id FROM memories m
                    JOIN memories_fts f ON m.id = f.rowid
                    WHERE memories_fts MATCH ?
                    LIMIT ?
                ''', (fts_query, limit))
            
            results = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            return results
        except Exception as e:
            logger.error(f"FTS5搜索失败: {e}")
            return []
    
    def _fallback_search(self, query_terms, category_filter=None, limit=50):
        """回退到传统LIKE搜索（当FTS5不可用时）"""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            if category_filter:
                cursor.execute('SELECT id FROM memories WHERE category = ?', (category_filter,))
            else:
                cursor.execute('SELECT id FROM memories')
            
            all_ids = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            if not query_terms:
                return all_ids[:limit]
            
            conn = self._get_connection()
            cursor = conn.cursor()
            
            placeholders = ' OR '.join(['content LIKE ?'] * len(query_terms))
            params = [f'%{term}%' for term in query_terms]
            
            if category_filter:
                cursor.execute(f'''
                    SELECT id FROM memories 
                    WHERE category = ? AND ({placeholders})
                    LIMIT ?
                ''', [category_filter] + params + [limit])
            else:
                cursor.execute(f'''
                    SELECT id FROM memories 
                    WHERE {placeholders}
                    LIMIT ?
                ''', params + [limit])
            
            results = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            return results if results else all_ids[:limit]
        except Exception as e:
            logger.error(f"回退搜索失败: {e}")
            return []
    
    def search_memory(self, query, category_filter=None, limit=None):
        """智能搜索记忆（FTS5预筛选 + 精细评分）
        
        优化后的搜索流程：
        1. FTS5全文索引快速预筛选（毫秒级）
        2. 对候选结果进行多维度相关性评分
        3. 返回Top N结果
        
        参数说明：
        - query: 搜索关键词或句子
        - category_filter: 分类过滤
        - limit: 返回结果数量限制（默认使用配置）
        """
        try:
            if limit is None:
                limit = self.config.get('search_max_results', 5)
            
            parsed_query, date_filter = self._parse_relative_time(query)
            
            cache_key = f"search_{query}_{category_filter}_{limit}"
            if cache_key in self.cache:
                logger.info(f"使用缓存的搜索结果: {query}")
                return self.cache[cache_key]
            
            query_tags = self.extract_tags_optimized(parsed_query)
            
            expanded_terms = []
            if self.search_strategy.get('synonym_expansion', True):
                for term in query_tags:
                    expanded_terms.extend(self.get_all_synonyms(term))
            else:
                expanded_terms = list(query_tags)
            
            if parsed_query:
                expanded_terms.append(parsed_query)
            
            if date_filter:
                expanded_terms.append(date_filter)
            
            candidate_ids = []
            
            fts_query = self._build_fts_query(parsed_query, query_tags)
            
            if fts_query:
                candidate_ids = self._fts_search(fts_query, category_filter, limit * 3)
                
                if not candidate_ids and self.search_strategy.get('enable_fallback', True):
                    logger.info("FTS5未找到结果，使用回退搜索")
                    candidate_ids = self._fallback_search(expanded_terms, category_filter, limit * 3)
            else:
                candidate_ids = self._fallback_search(expanded_terms, category_filter, limit * 3)
            
            if not candidate_ids:
                return []
            
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute(f'''SELECT id, category, content, tags, importance, created_at, updated_at, access_count 
                              FROM memories WHERE id IN ({",".join(["?"]*len(candidate_ids))})''', tuple(candidate_ids))
            candidate_memories = cursor.fetchall()
            conn.close()
            
            scored_memories = []
            for row in candidate_memories:
                if row:
                    try:
                        tags_str = row[MemoryColumn.TAGS] or ""
                        tags = tags_str.split(',') if tags_str else []
                        tags = [tag.strip() for tag in tags if tag.strip()]
                        
                        memory = {
                            "id": row[MemoryColumn.ID],
                            "category": row[MemoryColumn.CATEGORY] or self.get_default_category(),
                            "tags": tags,
                            "description": row[MemoryColumn.CONTENT] or "无内容",
                            "importance": row[MemoryColumn.IMPORTANCE] or 1,
                            "created_at": row[MemoryColumn.CREATED_AT],
                            "updated_at": row[MemoryColumn.UPDATED_AT],
                            "access_count": row[MemoryColumn.ACCESS_COUNT],
                            "source_platform": "Web"
                        }
                        
                        score = self._calculate_relevance_score_v2(memory, expanded_terms, parsed_query)
                        
                        if date_filter and memory.get('created_at'):
                            created_at_str = str(memory['created_at'])
                            if date_filter in created_at_str:
                                score += 15.0
                        
                        if score > 0:
                            memory['relevance_score'] = score
                            scored_memories.append(memory)
                    except Exception as e:
                        logger.error(f"处理记忆失败: {e}")
            
            scored_memories.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
            
            # 应用MMR多样性策略（如果配置启用）
            mmr_enabled = self.config.get('mmr_enabled', True)
            if mmr_enabled and len(scored_memories) > limit:
                lambda_param = self.config.get('mmr_lambda', 0.7)
                top_memories = self._apply_mmr_diversity(
                    scored_memories, 
                    parsed_query, 
                    lambda_param=lambda_param, 
                    top_k=limit
                )
                logger.info(f"MMR多样性筛选完成，从{len(scored_memories)}条中选择了{len(top_memories)}条")
            else:
                top_memories = scored_memories[:limit]
            
            if top_memories:
                conn = self._get_connection()
                cursor = conn.cursor()
                for memory in top_memories:
                    memory_id = memory['id']
                    cursor.execute('UPDATE memories SET access_count = access_count + 1 WHERE id = ?', (memory_id,))
                conn.commit()
                conn.close()
            
            return_results = []
            for memory in top_memories:
                return_results.append({
                    "id": memory.get('id'),
                    "category": memory.get('category'),
                    "content": memory.get('description'),
                    "tags": memory.get('tags'),
                    "importance": memory.get('importance'),
                    "created_at": memory.get('created_at'),
                    "updated_at": memory.get('updated_at'),
                    "access_count": memory.get('access_count'),
                    "source_platform": memory.get('source_platform')
                })
            
            self._update_cache(cache_key, return_results)
            
            return return_results
        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return []
    
    def _update_cache(self, key, value, ttl=None):
        """更新缓存（使用TTLCache）
        
        参数说明：
        - key: 缓存键
        - value: 缓存值
        - ttl: 过期时间（秒），可选
        """
        self.cache.set(key, value, ttl)
    
    def _calculate_relevance_score(self, memory, terms, query):
        """计算记忆与查询的相关性分数（V1版本-保留兼容）
        
        参数说明：
        - memory: 记忆对象
        - terms: 扩展后的关键词列表
        - query: 原始查询
        
        返回值：
        - 相关性分数
        """
        score = 0
        
        # 1. 标签匹配分数
        tag_match_score = 0
        for term in terms:
            if any(term.lower() in tag.lower() for tag in memory['tags']):
                tag_match_score += self.search_weights.get('tag_match', 5.0)
        score += tag_match_score
        
        # 2. 内容匹配分数
        content = memory['description'].lower()
        content_match_score = 0
        for term in terms:
            if term.lower() in content:
                content_match_score += self.search_weights.get('tag_match', 5.0) * 0.8
        score += content_match_score
        
        # 3. 分类匹配分数
        category = memory['category'].lower()
        category_match_score = 0
        for term in terms:
            if term.lower() in category:
                category_match_score += self.search_weights.get('category_match', 2.0)
        score += category_match_score
        
        # 4. 重要性分数 - 确保是数字类型
        try:
            importance = float(memory.get('importance', 5))
            score += importance * 0.5
        except (ValueError, TypeError):
            score += 5 * 0.5
        
        # 5. 访问次数（流行度）分数 - 确保是数字类型
        try:
            access_count = float(memory.get('access_count', 0))
            score += access_count * self.search_weights.get('popularity', 1.0) * 0.1
        except (ValueError, TypeError):
            score += 0
        
        # 6. 完整匹配奖励
        if query and query.lower() in memory['description'].lower():
            score += self.search_weights.get('full_match_bonus', 10.0)
        
        return score
    
    def _calculate_relevance_score_v2(self, memory, terms, query):
        """计算记忆与查询的相关性分数（V2优化版本）
        
        优化点：
        1. 降低基础分（importance默认值从5改为1）
        2. 使用对数压缩access_count（防止"富者越富"）
        3. 调整权重配比，更注重内容相关性
        
        参数说明：
        - memory: 记忆对象
        - terms: 扩展后的关键词列表
        - query: 原始查询
        
        返回值：
        - 相关性分数
        """
        import math
        
        score = 0
        
        # 1. 标签匹配分数（核心指标）
        tag_match_score = 0
        matched_tags = []
        for term in terms:
            for tag in memory['tags']:
                if term.lower() in tag.lower():
                    tag_match_score += self.search_weights.get('tag_match', 5.0)
                    if term not in matched_tags:
                        matched_tags.append(term)
        
        score += tag_match_score
        
        # 2. 内容匹配分数（次要指标）
        content = memory['description'].lower()
        content_match_score = 0
        matched_content_terms = []
        for term in terms:
            if term.lower() in content:
                content_match_score += self.search_weights.get('tag_match', 5.0) * 0.8
                if term not in matched_content_terms:
                    matched_content_terms.append(term)
        
        score += content_match_score
        
        # 3. 分类匹配分数（辅助指标）
        category = memory['category'].lower()
        category_match_score = 0
        for term in terms:
            if term.lower() in category:
                category_match_score += self.search_weights.get('category_match', 2.0)
        score += category_match_score
        
        # 4. 重要性分数（降低基础分，默认值从5改为1）
        try:
            importance = float(memory.get('importance', 1))
            score += importance * 0.3  # 系数从0.5降到0.3
        except (ValueError, TypeError):
            score += 1 * 0.3  # 默认值改为1
        
        # 5. 访问次数（使用log2对数压缩，防止热门记忆霸占）
        # log2比log10增长更慢，让新记忆更容易追上
        try:
            access_count = float(memory.get('access_count', 0))
            if access_count > 0:
                log_score = math.log2(access_count + 1) * self.search_weights.get('popularity', 1.0) * 0.3
                score += min(log_score, 3)  # 上限3分，防止过高
        except (ValueError, TypeError):
            score += 0
        
        # 6. 完整匹配奖励（保持不变）
        if query and query.lower() in memory['description'].lower():
            score += self.search_weights.get('full_match_bonus', 10.0)
        
        return score
    
    def _calculate_text_similarity(self, text1, text2):
        """计算两个文本的相似度（简化版Jaccard相似度）
        
        参数：
        - text1: 文本1
        - text2: 文本2
        
        返回值：
        - 相似度 (0.0-1.0)
        """
        if not text1 or not text2:
            return 0.0
        
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        
        if not words1 or not words2:
            return 0.0
        
        intersection = words1 & words2
        union = words1 | words2
        
        return len(intersection) / len(union)
    
    def _apply_mmr_diversity(self, scored_memories, query, lambda_param=0.7, top_k=5):
        """使用MMR（最大边际相关性）策略选择多样化结果
        
        MMR公式：MMR = λ × Relevance(d,q) - (1-λ) × Similarity(d,d_selected)
        
        参数：
        - scored_memories: 已评分的记忆列表 [{memory, relevance_score}, ...]
        - query: 查询文本
        - lambda_param: 平衡参数 (0-1)，默认0.7
        - top_k: 返回数量
        
        返回值：
        - 多样化后的记忆列表
        """
        if len(scored_memories) <= top_k:
            return scored_memories
        
        selected = []
        remaining = list(scored_memories)
        
        while len(selected) < top_k and remaining:
            best_mmr_score = -float('inf')
            best_candidate = None
            best_idx = -1
            
            for idx, candidate in enumerate(remaining):
                relevance = candidate.get('relevance_score', 0)
                
                max_similarity_to_selected = 0
                for sel in selected:
                    sim = self._calculate_text_similarity(
                        candidate.get('memory', {}).get('description', ''),
                        sel.get('memory', {}).get('description', '')
                    )
                    max_similarity_to_selected = max(max_similarity_to_selected, sim)
                
                mmr_score = lambda_param * relevance - (1 - lambda_param) * max_similarity_to_selected
                
                if mmr_score > best_mmr_score:
                    best_mmr_score = mmr_score
                    best_candidate = candidate
                    best_idx = idx
            
            if best_candidate:
                selected.append(best_candidate)
                remaining.pop(best_idx)
        
        return selected
    
    def get_recent_memories(self, limit=5):
        """获取最近的记忆"""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('''SELECT id, category, content, tags, importance, created_at, updated_at, access_count 
                              FROM memories ORDER BY created_at DESC LIMIT ?''', (limit,))
            results = cursor.fetchall()
            conn.close()
            
            memory_list = []
            for row in results:
                if row:
                    try:
                        tags_str = row[3] or ""
                        tags = tags_str.split(',') if tags_str else []
                        tags = [tag.strip() for tag in tags if tag.strip()]
                        
                        memory = {
                            "id": row[0],
                            "category": row[1] or self.get_default_category(),
                            "content": row[2] or "无内容",
                            "tags": tags,
                            "importance": row[4] or 5,
                            "created_at": row[5],
                            "updated_at": row[6],
                            "access_count": row[7] or 0,
                            "source_platform": "Web"
                        }
                        memory_list.append(memory)
                    except Exception as e:
                        logger.error(f"处理最近记忆失败: {e}")
            
            return memory_list
        except Exception as e:
            logger.error(f"获取最近记忆失败: {e}")
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
            
            # 记录活动
            self._record_activity("删除记忆", f"ID: {memory_id}")
            
            return "删除成功"
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            return f"删除失败: {e}"
    
    def update_memory(self, memory_id, content, category=None, tags="", importance=5):
        """更新记忆
        
        参数说明：
        - memory_id: 记忆的 ID
        - content: 记忆内容
        - category: 分类 (AI指定)
        - tags: 标签 (逗号分隔)
        - importance: 重要性 (默认 5)
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 处理标签
            tag_list = []
            
            # 1. 添加用户指定的标签
            if tags:
                user_tags = tags.split(',')
                for tag in user_tags:
                    tag = tag.strip()
                    if tag:
                        tag_list.append(tag)
            
            # 2. 智能提取标签
            auto_tags = self.extract_tags_optimized(content)
            tag_list.extend(auto_tags)
            
            # 3. 去重并合并标签
            unique_tags = list(dict.fromkeys(tag_list))
            
            # 4. 限制标签数量
            max_tags = self.config.get('max_extracted_tags', 10)
            unique_tags = unique_tags[:max_tags]
            
            tags = ','.join(unique_tags)
            
            # 使用默认分类如果未指定
            if category is None:
                category = self.get_default_category()
            
            # 更新数据
            cursor.execute('''
            UPDATE memories SET 
                category = ?, 
                content = ?, 
                tags = ?, 
                importance = ?, 
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''', (category, content, tags, importance, memory_id))
            
            # 检查是否更新成功
            if cursor.rowcount == 0:
                conn.close()
                return f"未找到ID为 {memory_id} 的记忆"
            
            conn.commit()
            conn.close()
            
            # 记录活动
            self._record_activity("更新记忆", f"ID: {memory_id}, 分类: {category}, 重要性: {importance}")
            
            return "更新成功"
        except Exception as e:
            logger.error(f"更新记忆失败: {e}")
            return f"更新失败: {e}"

    def update_relationship(self, user_id, relation_type=None, summary_update=None, nickname=None, first_met_location=None, known_contexts=None):
        """更新关系
        
        参数说明：
        - user_id: 目标用户 ID
        - relation_type: 新的关系定义
        - summary_update: 新的印象总结 (会覆盖旧的)
        - nickname: AI 对 TA 的称呼
        - first_met_location: 初次见面地点 (仅存储ID)
        - known_contexts: 多次相遇群组 (逗号分隔的群ID数组)
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT nickname, relation_type, summary, first_met_location, known_contexts FROM relationships WHERE user_id=?', (user_id,))
            existing = cursor.fetchone()
            
            if existing:
                old_nickname, old_relation_type, old_summary, old_first_met_location, old_known_contexts = existing
                
                new_nickname = nickname or old_nickname
                new_relation_type = relation_type or old_relation_type
                new_summary = summary_update or old_summary
                
                if first_met_location:
                    new_first_met_location = first_met_location.split('+')[0].strip()
                else:
                    new_first_met_location = old_first_met_location
                
                if known_contexts:
                    new_groups = []
                    for group in known_contexts.split(','):
                        group = group.strip()
                        if group:
                            group_id = group.split('+')[0].strip()
                            new_groups.append(group_id)
                    new_known_contexts = ','.join(new_groups)
                else:
                    new_known_contexts = old_known_contexts
                
                cursor.execute('''
                UPDATE relationships SET 
                    nickname = ?, 
                    relation_type = ?, 
                    summary = ?, 
                    first_met_location = ?, 
                    known_contexts = ?, 
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                ''', (new_nickname, new_relation_type, new_summary, new_first_met_location, new_known_contexts, user_id))
            else:
                if first_met_location:
                    first_met_location = first_met_location.split('+')[0].strip()
                
                if known_contexts:
                    new_groups = []
                    for group in known_contexts.split(','):
                        group = group.strip()
                        if group:
                            group_id = group.split('+')[0].strip()
                            new_groups.append(group_id)
                    known_contexts = ','.join(new_groups)
                
                cursor.execute('''
                INSERT INTO relationships (user_id, nickname, relation_type, summary, first_met_location, known_contexts)
                VALUES (?, ?, ?, ?, ?, ?)
                ''', (user_id, nickname or "", relation_type or "", summary_update or "", first_met_location, known_contexts))
            
            conn.commit()
            conn.close()
            
            self._record_activity("更新关系", f"用户ID: {user_id}, 关系类型: {relation_type or '未知'}")
            
            return "关系更新成功"
        except Exception as e:
            logger.error(f"更新关系失败: {e}")
            return f"更新失败: {e}"
    
    # ==================== 身份映射系统方法 ====================
    
    def add_identity_alias(self, user_id, alias, alias_type='nickname', source_context=None):
        """添加或更新用户别名（最多保留3个）
        
        参数：
        - user_id: 用户唯一标识
        - alias: 别名/昵称
        - alias_type: 别名类型 (nickname/group_name/platform_id)
        - source_context: 来源场景（群ID等）
        
        策略：
        - 每个用户最多保存N个别名（默认3个，可通过配置 max_aliases_per_user 修改）
        - 新别名加入时，如果已满则删除最旧的
        - 当前使用的别名标记为 is_current=1
        
        返回值：
        - 操作结果字符串
        """
        # 从配置读取最大别名数（默认3）
        MAX_ALIASES_PER_USER = self.config.get('max_aliases_per_user', 3)
        
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 检查是否已存在相同的别名
            cursor.execute('''
                SELECT id FROM identity_aliases 
                WHERE user_id = ? AND alias = ? AND (source_context = ? OR source_context IS NULL)
            ''', (user_id, alias, source_context))
            existing = cursor.fetchone()
            
            if existing:
                # 更新最后使用时间，并设为当前
                cursor.execute('''
                    UPDATE identity_aliases 
                    SET last_seen_at = CURRENT_TIMESTAMP, is_current = 1
                    WHERE id = ?
                ''', (existing[0],))
                
                # 将同类型的其他别名标记为非当前
                cursor.execute('''
                    UPDATE identity_aliases 
                    SET is_current = 0 
                    WHERE user_id = ? AND id != ? AND alias_type = ?
                ''', (user_id, existing[0], alias_type))
                
                result = f"别名已更新: {alias}"
            
            else:
                # 检查当前用户的别名数量
                cursor.execute('''
                    SELECT COUNT(*) FROM identity_aliases 
                    WHERE user_id = ?
                ''', (user_id,))
                current_count = cursor.fetchone()[0]
                
                if current_count >= MAX_ALIASES_PER_USER:
                    # 已达到上限，删除最旧的别名（非当前使用的）
                    cursor.execute('''
                        DELETE FROM identity_aliases 
                        WHERE user_id = ? AND id IN (
                            SELECT id FROM identity_aliases 
                            WHERE user_id = ? AND is_current = 0
                            ORDER BY last_seen_at ASC 
                            LIMIT 1
                        )
                    ''', (user_id, user_id))
                    logger.info(f"用户 {user_id} 别名已达上限({MAX_ALIASES_PER_USER})，已删除最旧别名")
                
                # 插入新别名
                cursor.execute('''
                    INSERT INTO identity_aliases (user_id, alias, alias_type, source_context, is_current)
                    VALUES (?, ?, ?, ?, 1)
                ''', (user_id, alias, alias_type, source_context))
                
                result = f"新别名已添加: {alias} (当前共{min(current_count + 1, MAX_ALIASES_PER_USER)}/{MAX_ALIASES_PER_USER}个)"
            
            conn.commit()
            conn.close()
            
            self._record_activity("添加别名", f"用户:{user_id}, 别名:{alias}")
            logger.info(f"身份映射更新: {user_id} -> {alias} ({alias_type})")
            
            return result
        except Exception as e:
            logger.error(f"添加别名失败: {e}")
            return f"添加别名失败: {e}"
    
    def get_user_aliases(self, user_id, include_history=False):
        """获取用户的所有别名
        
        参数：
        - user_id: 用户ID
        - include_history: 是否包含历史别名
        
        返回值：
        - 别名列表 [{alias, type, is_current, source_context}, ...]
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            if include_history:
                cursor.execute('''
                    SELECT alias, alias_type, is_current, source_context, first_seen_at, last_seen_at
                    FROM identity_aliases 
                    WHERE user_id = ?
                    ORDER BY is_current DESC, last_seen_at DESC
                ''', (user_id,))
            else:
                cursor.execute('''
                    SELECT alias, alias_type, is_current, source_context, first_seen_at, last_seen_at
                    FROM identity_aliases 
                    WHERE user_id = ? AND is_current = 1
                    ORDER BY last_seen_at DESC
                ''', (user_id,))
            
            results = cursor.fetchall()
            conn.close()
            
            aliases = []
            for row in results:
                aliases.append({
                    'alias': row[0],
                    'type': row[1],
                    'is_current': bool(row[2]),
                    'source_context': row[3],
                    'first_seen_at': row[4],
                    'last_seen_at': row[5]
                })
            
            return aliases
        except Exception as e:
            logger.error(f"获取用户别名失败: {e}")
            return []
    
    def smart_resolve_identity(self, query):
        """智能解析身份（支持多种输入格式）
        
        支持的输入：
        - 用户ID: "QQ_123456" / "123456"
        - 昵称: "小明"
        - 群名称: "Python学习群"
        - 群ID: "group_789"
        
        参数：
        - query: 查询关键词
        
        返回值：
        - 匹配结果列表 [{user_id, nickname, match_type, match_score}, ...]
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            results = []
            
            query_lower = str(query).lower().strip()
            
            if not query_lower:
                return results
            
            # 策略1：精确匹配用户ID
            cursor.execute('''
                SELECT r.user_id, r.nickname, r.relation_type, r.summary
                FROM relationships r
                WHERE r.user_id = ?
            ''', (query,))
            exact_id_match = cursor.fetchone()
            if exact_id_match:
                results.append({
                    'user_id': exact_id_match[0],
                    'nickname': exact_id_match[1],
                    'relation_type': exact_id_match[2],
                    'summary': exact_id_match[3],
                    'match_type': 'exact_id',
                    'match_score': 100
                })
            
            # 策略2：精确匹配昵称/别名
            cursor.execute('''
                SELECT ia.user_id, r.nickname, ia.alias, ia.alias_type
                FROM identity_aliases ia
                LEFT JOIN relationships r ON ia.user_id = r.user_id
                WHERE LOWER(ia.alias) = ?
                AND ia.is_current = 1
            ''', (query_lower,))
            exact_name_matches = cursor.fetchall()
            for match in exact_name_matches:
                existing = next((r for r in results if r['user_id'] == match[0]), None)
                if not existing:
                    results.append({
                        'user_id': match[0],
                        'nickname': match[1] or match[2],
                        'relation_type': None,
                        'summary': None,
                        'match_type': 'exact_name',
                        'match_score': 95
                    })
            
            # 策略3：模糊匹配昵称/别名（包含关系）
            cursor.execute('''
                SELECT ia.user_id, r.nickname, ia.alias, ia.alias_type
                FROM identity_aliases ia
                LEFT JOIN relationships r ON ia.user_id = r.user_id
                WHERE LOWER(ia.alias) LIKE ?
                AND ia.is_current = 1
                LIMIT 10
            ''', (f'%{query_lower}%',))
            fuzzy_matches = cursor.fetchall()
            for match in fuzzy_matches:
                existing = next((r for r in results if r['user_id'] == match[0]), None)
                if not existing:
                    alias = match[2] or ''
                    similarity = self._calculate_string_similarity(query_lower, alias.lower())
                    if similarity > 0.5:
                        results.append({
                            'user_id': match[0],
                            'nickname': match[1] or alias,
                            'relation_type': None,
                            'summary': None,
                            'match_type': 'fuzzy_name',
                            'match_score': int(similarity * 80)
                        })
            
            # 策略4：在关系的 summary/known_contexts 中搜索
            cursor.execute('''
                SELECT user_id, nickname, relation_type, summary, known_contexts
                FROM relationships
                WHERE LOWER(summary) LIKE ? OR LOWER(known_contexts) LIKE ?
                LIMIT 5
            ''', (f'%{query_lower}%', f'%{query_lower}%'))
            context_matches = cursor.fetchall()
            for match in context_matches:
                existing = next((r for r in results if r['user_id'] == match[0]), None)
                if not existing:
                    results.append({
                        'user_id': match[0],
                        'nickname': match[1],
                        'relation_type': match[2],
                        'summary': match[3],
                        'match_type': 'context',
                        'match_score': 60
                    })
            
            conn.close()
            
            # 按分数排序
            results.sort(key=lambda x: x.get('match_score', 0), reverse=True)
            
            return results[:5]  # 返回Top 5匹配
            
        except Exception as e:
            logger.error(f"智能身份解析失败: {e}")
            return []
    
    def _calculate_string_similarity(self, s1, s2):
        """计算两个字符串的相似度"""
        if not s1 or not s2:
            return 0.0
        
        s1, s2 = s1.lower(), s2.lower()
        
        # 完全包含
        if s1 in s2 or s2 in s1:
            return 1.0
        
        # Jaccard相似度
        words1 = set(s1.split())
        words2 = set(s2.split())
        
        if not words1 or not words2:
            return 0.0
        
        intersection = words1 & words2
        union = words1 | words2
        
        return len(intersection) / len(union)
    
    def get_relationship_with_identity(self, user_id_or_alias):
        """获取关系信息（支持ID和别名查询）
        
        这是 get_relationship_by_user_id 的增强版，
        支持通过别名查找用户。
        
        参数：
        - user_id_or_alias: 用户ID或别名
        
        返回值：
        - 关系字典，如果不存在则返回None
        """
        # 先尝试直接用ID查询
        relation = self.get_relationship_by_user_id(user_id_or_alias)
        if relation:
            return relation
        
        # 如果不是，尝试作为别名解析
        matches = self.smart_resolve_identity(user_id_or_alias)
        if matches and len(matches) > 0:
            best_match = matches[0]
            if best_match['match_score'] >= 70:  # 高置信度匹配
                return self.get_relationship_by_user_id(best_match['user_id'])
        
        return None
    
    def update_relationship_enhanced(self, user_id, relation_type=None, 
                                       summary_update=None, nickname=None, 
                                       first_met_location=None, known_contexts=None,
                                       aliases=None):
        """增强版的关系更新（同时处理别名）
        
        参数：
        - user_id: 用户ID
        - relation_type: 关系定义
        - summary_update: 印象总结
        - nickname: 当前昵称（会自动记录到别名表）
        - first_met_location: 初次见面地点
        - known_contexts: 已知场景（群组列表）
        - aliases: 额外的别名列表
        
        返回值：
        - 操作结果
        """
        try:
            # 先调用原始的更新方法
            result = self.update_relationship(
                user_id, relation_type, summary_update, 
                nickname, first_met_location, known_contexts
            )
            
            # 如果提供了昵称，自动添加到别名表
            if nickname:
                self.add_identity_alias(user_id, nickname, 'nickname')
            
            # 处理额外的别名
            if aliases:
                if isinstance(aliases, str):
                    aliases = [aliases]
                
                for alias in aliases:
                    if alias and alias.strip():
                        self.add_identity_alias(user_id, alias.strip(), 'nickname')
            
            # 解析并记录群组信息到别名表
            if known_contexts:
                group_list = [g.strip() for g in str(known_contexts).split(',') if g.strip()]
                for group_id in group_list:
                    # 尝试提取群名称（如果有）
                    group_name = None
                    if '_' in group_id:
                        parts = group_id.split('_', 1)
                        if len(parts) > 1 and parts[1]:
                            group_name = parts[1]
                    
                    if group_name:
                        self.add_identity_alias(user_id, group_name, 'group_name', group_id)
                    else:
                        self.add_identity_alias(user_id, group_id, 'platform_id', None)
            
            return result + " (已同步更新身份映射)"
            
        except Exception as e:
            logger.error(f"增强版关系更新失败: {e}")
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
            conn = self._get_connection()
            cursor = conn.cursor()
            
            if category:
                cursor.execute('''SELECT id, category, content, tags, importance, created_at, updated_at, access_count 
                                  FROM memories WHERE category=? ORDER BY created_at DESC LIMIT ? OFFSET ?''', (category, limit, offset))
            else:
                cursor.execute('''SELECT id, category, content, tags, importance, created_at, updated_at, access_count 
                                  FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?''', (limit, offset))
            
            results = cursor.fetchall()
            conn.close()
            
            memory_list = []
            for row in results:
                tags_str = row[3] or ""
                tags = tags_str.split(',') if tags_str else []
                tags = [tag.strip() for tag in tags if tag.strip()]
                
                memory = {
                    "id": row[0],
                    "category": row[1] or self.get_default_category(),
                    "content": row[2] or "无内容",
                    "tags": tags,
                    "importance": row[4] or 5,
                    "created_at": row[5],
                    "updated_at": row[6],
                    "access_count": row[7] or 0,
                    "source_platform": "Web"
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
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT user_id, nickname, relation_type, summary, first_met_location, known_contexts, updated_at FROM relationships ORDER BY updated_at DESC LIMIT ? OFFSET ?', (limit, offset))
            results = cursor.fetchall()
            conn.close()
            
            relationship_list = []
            for row in results:
                relationship = {
                    "user_id": row[0],
                    "nickname": row[1],
                    "relation_type": row[2],
                    "summary": row[3],
                    "first_met_location": row[4],
                    "known_contexts": row[5],
                    "updated_at": row[6]
                }
                relationship_list.append(relationship)
            
            return relationship_list
        except Exception as e:
            logger.error(f"获取关系失败: {e}")
            return []
    
    def get_relationship_by_user_id(self, user_id):
        """根据用户ID精确查询关系信息
        
        参数：
        - user_id: 用户ID
        
        返回：
        - 关系字典，如果不存在则返回None
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT user_id, nickname, relation_type, summary, first_met_location, known_contexts, updated_at 
                FROM relationships 
                WHERE user_id = ?
            ''', (user_id,))
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                return {
                    "user_id": row[0],
                    "nickname": row[1],
                    "relation_type": row[2],
                    "summary": row[3],
                    "first_met_location": row[4],
                    "known_contexts": row[5],
                    "updated_at": row[6]
                }
            
            return None
        except Exception as e:
            logger.error(f"根据用户ID查询关系失败: {e}")
            return None
    
    def search_relationship(self, query, limit=5):
        """模糊搜索关系
        
        参数说明：
        - query: 搜索关键词（可以是ID、昵称、关系类型等）
        - limit: 返回结果数量限制
        
        返回：
        - 匹配的关系列表
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            query_lower = query.lower()
            
            cursor.execute('SELECT user_id, nickname, relation_type, summary, first_met_location, known_contexts, updated_at FROM relationships')
            all_results = cursor.fetchall()
            conn.close()
            
            scored_results = []
            for row in all_results:
                score = 0
                user_id = row[0] or ""
                nickname = row[1] or ""
                relation_type = row[2] or ""
                summary = row[3] or ""
                first_met_location = row[4] or ""
                
                if query == user_id:
                    score += 100
                
                if query_lower == user_id.lower():
                    score += 80
                
                if query_lower in nickname.lower():
                    score += 50
                    if nickname.lower().startswith(query_lower):
                        score += 20
                
                if query_lower in relation_type.lower():
                    score += 30
                
                if query_lower in summary.lower():
                    score += 10
                
                if query_lower in first_met_location.lower():
                    score += 15
                
                if score > 0:
                    relationship = {
                        "user_id": row[0],
                        "nickname": row[1],
                        "relation_type": row[2],
                        "summary": row[3],
                        "first_met_location": row[4],
                        "known_contexts": row[5],
                        "updated_at": row[6],
                        "match_score": score
                    }
                    scored_results.append(relationship)
            
            scored_results.sort(key=lambda x: x.get('match_score', 0), reverse=True)
            
            for r in scored_results:
                del r['match_score']
            
            return scored_results[:limit]
        except Exception as e:
            logger.error(f"搜索关系失败: {e}")
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

    def get_recent_activities(self, limit=10):
        """获取最近活动
        
        参数说明：
        - limit: 返回的活动数量
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT action, details, created_at FROM activities ORDER BY created_at DESC LIMIT ?', (limit,))
            results = cursor.fetchall()
            conn.close()
            
            activities = []
            for row in results:
                activity = {
                    "action": row[0],
                    "details": row[1],
                    "created_at": row[2]
                }
                activities.append(activity)
            
            return activities
        except Exception as e:
            logger.error(f"获取最近活动失败: {e}")
            return []
    
    def cleanup_memories(self):
        """清理旧记忆
        
        根据配置的清理策略，清理旧的记忆数据
        """
        try:
            # 获取清理配置
            enabled = self.config.get('memory_cleanup_enabled', True)
            
            if not enabled:
                return "清理功能已禁用"
            
            days_old = self.config.get('memory_cleanup_days', 365)
            max_count = self.config.get('memory_cleanup_max', 10000)
            strategy = self.config.get('memory_cleanup_strategy', 'unaccessed')
            
            conn = self._get_connection()
            cursor = conn.cursor()
            deleted_count = 0
            
            # 按时间清理
            if days_old > 0:
                cursor.execute('''
                DELETE FROM memories 
                WHERE created_at < datetime('now', '-' || ? || ' days')
                ''', (days_old,))
                deleted_count += cursor.rowcount
            
            # 按数量清理
            if max_count > 0:
                # 获取当前记忆数量
                cursor.execute('SELECT COUNT(*) FROM memories')
                current_count = cursor.fetchone()[0]
                
                if current_count > max_count:
                    to_delete = current_count - max_count
                    
                    # 根据策略选择要删除的记忆
                    if strategy == 'unaccessed':
                        # 清理未访问的记忆
                        cursor.execute('''
                        DELETE FROM memories 
                        ORDER BY access_count ASC, created_at ASC
                        LIMIT ?
                        ''', (to_delete,))
                    elif strategy == 'oldest':
                        # 清理最旧的记忆
                        cursor.execute('''
                        DELETE FROM memories 
                        ORDER BY created_at ASC
                        LIMIT ?
                        ''', (to_delete,))
                    else:  # random
                        # 随机清理
                        cursor.execute('''
                        DELETE FROM memories 
                        WHERE id IN (
                            SELECT id FROM memories 
                            ORDER BY RANDOM()
                            LIMIT ?
                        )
                        ''', (to_delete,))
                    
                    deleted_count += cursor.rowcount
            
            conn.commit()
            conn.close()
            
            if deleted_count > 0:
                self._record_activity("清理记忆", f"删除了 {deleted_count} 条旧记忆")
                return f"成功清理了 {deleted_count} 条旧记忆"
            else:
                return "没有需要清理的记忆"
        except Exception as e:
            logger.error(f"清理记忆失败: {e}")
            return f"清理失败: {e}"
    
    def get_memory_categories(self):
        """获取记忆分类
        
        从配置中获取记忆分类列表
        """
        try:
            # 从配置中获取分类
            categories = self.config.get('memory_categories', ["技术笔记", "生活记录", "学习资料", "个人想法"])
            return categories
        except Exception as e:
            logger.error(f"获取记忆分类失败: {e}")
            return ["技术笔记", "生活记录", "学习资料", "个人想法"]
    
    def get_default_category(self):
        """获取默认记忆分类
        
        返回配置中的第一个分类作为默认分类
        """
        categories = self.get_memory_categories()
        return categories[0] if categories else "技术笔记"
    
    def optimize_synonyms(self):
        """优化同义词库"""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 1. 合并大小写变体
            cursor.execute('''
                SELECT DISTINCT tag FROM tags
            ''')
            tags = [row[0] for row in cursor.fetchall()]
            
            # 处理大小写变体
            for tag in tags:
                lowercase_tag = tag.lower()
                if tag != lowercase_tag:
                    self.add_synonym_pair(tag, lowercase_tag)
            
            # 2. 移除低强度同义词
            cursor.execute('''
                DELETE FROM synonyms WHERE strength < 0.3
            ''')
            
            conn.commit()
            conn.close()
            logger.info("同义词库优化完成")
        except Exception as e:
            logger.error(f"优化同义词库失败: {e}")
    
    def analyze_search_patterns(self):
        """从搜索模式中学习"""
        try:
            # 这里可以实现从搜索历史中学习的逻辑
            # 例如分析高频搜索词，找出经常一起出现的词语
            logger.info("搜索模式分析完成")
        except Exception as e:
            logger.error(f"分析搜索模式失败: {e}")
    
    def self_optimize(self):
        """执行自我优化"""
        try:
            logger.info("开始执行自我优化...")
            
            # 1. 清理旧记忆
            cleanup_result = self.cleanup_memories()
            logger.info(f"清理结果: {cleanup_result}")
            
            # 2. 优化同义词库
            self.optimize_synonyms()
            
            # 3. 分析搜索模式
            self.analyze_search_patterns()
            
            # 4. 数据库维护
            self._maintain_database()
            
            logger.info("自我优化完成")
        except Exception as e:
            logger.error(f"执行自我优化失败: {e}")
    
    def _maintain_database(self):
        """数据库维护"""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 执行数据库优化命令
            cursor.execute('VACUUM')
            cursor.execute('ANALYZE')
            cursor.execute('PRAGMA optimize')
            
            conn.commit()
            conn.close()
            logger.info("数据库维护完成")
        except Exception as e:
            logger.error(f"数据库维护失败: {e}")
    
    def update_search_weights(self, **kwargs):
        """更新搜索权重配置"""
        for key, value in kwargs.items():
            if key in self.search_weights:
                self.search_weights[key] = value
        logger.info(f"搜索权重已更新: {self.search_weights}")
    
    def update_search_strategy(self, **kwargs):
        """更新搜索策略配置"""
        for key, value in kwargs.items():
            if key in self.search_strategy:
                self.search_strategy[key] = value
        logger.info(f"搜索策略已更新: {self.search_strategy}")
