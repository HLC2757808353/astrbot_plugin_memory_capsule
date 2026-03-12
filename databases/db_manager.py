import sqlite3
import os
import json
import re
from datetime import datetime

# 容错处理
try:
    from astrbot import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

# 导入分词库
try:
    import jieba
    import jieba.posseg as pseg
except ImportError:
    logger.warning("jieba库未安装，标签提取功能将受限")
    jieba = None
    pseg = None

# 导入拼音库
try:
    import pypinyin
except ImportError:
    logger.warning("pypinyin库未安装，拼音匹配功能将受限")
    pypinyin = None

from .backup import BackupManager

class DatabaseManager:
    def __init__(self, config=None, context=None):
        # 将数据库文件存储在插件目录的上上个目录中，确保跨平台兼容
        # 路径：d:\Astrbot\AstrBot\data\memory_capsule.db
        app_data_dir = os.path.join(os.path.dirname(__file__), "..", "..")
        os.makedirs(app_data_dir, exist_ok=True)
        self.db_path = os.path.join(app_data_dir, "memory_capsule.db")
        self.config = config or {}
        self.context = context
        # 使用新的备份配置路径
        backup_config = {
            'interval': self.config.get('backup_interval', 24),
            'max_count': self.config.get('backup_max_count', 10)
        }
        self.backup_manager = BackupManager(self.db_path, backup_config)
        
        # 初始化数据库结构
        self._initialize_database_structure()
        
        # 初始化搜索权重配置
        self.search_weights = self.config.get('search_weights', {
            'tag_match': 5.0,
            'recent_boost': 3.0,
            'mid_boost': 2.0,
            'popularity': 1.0,
            'category_match': 2.0,
            'full_match_bonus': 10.0
        })
        
        # 初始化搜索策略配置
        self.search_strategy = self.config.get('search_strategy', {
            'match_type': 'AND',
            'synonym_expansion': True,
            'time_decay': True,
            'category_filter': False,
            'enable_fallback': True
        })
        
        # 初始化缓存
        self.cache = {}
        self.cache_max_size = self.config.get('max_cache_size', 1000)
    
    def extract_tags_optimized(self, content):
        """智能标签提取器
        
        使用混合策略：规则 + jieba分词 + 实体识别
        """
        tags = []
        
        # 策略1：jieba分词提取名词（最可靠）
        if pseg:
            words = pseg.cut(content)
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
                
                # 添加缺失的access_count列
                if 'access_count' not in columns:
                    logger.info("添加缺失的access_count列...")
                    cursor.execute('ALTER TABLE memories ADD COLUMN access_count INTEGER DEFAULT 0')
                    conn.commit()
                    logger.info("成功添加access_count列")
                
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
            except Exception as e:
                logger.error(f"添加memories列失败: {e}")
            
            try:
                # 检查relationships表的列结构
                cursor.execute('PRAGMA table_info(relationships)')
                columns = [column[1] for column in cursor.fetchall()]
                
                # 确保relationships表的列结构正确
                expected_columns = ['user_id', 'nickname', 'relation_type', 'intimacy', 'summary', 'first_met_location', 'known_contexts', 'updated_at']
                
                # 如果表结构不正确，重新创建表
                if len(columns) != len(expected_columns) or not all(col in columns for col in expected_columns):
                    logger.info("relationships表结构不正确，重新创建...")
                    
                    # 先备份数据
                    cursor.execute('SELECT * FROM relationships')
                    relationships_data = cursor.fetchall()
                    
                    # 删除旧表
                    cursor.execute('DROP TABLE IF EXISTS relationships')
                    
                    # 创建新表
                    cursor.execute('''
                    CREATE TABLE IF NOT EXISTS relationships (
                        user_id TEXT PRIMARY KEY,       -- 对方 QQ 号
                        nickname TEXT,                  -- AI 对 TA 的称呼
                        relation_type TEXT,             -- 关系 (如: 朋友, 损友)
                        intimacy INTEGER DEFAULT 0,     -- 好感度 (0-100)
                        summary TEXT,                   -- 核心印象 (覆盖式更新，不追加)
                        first_met_location TEXT,        -- 初次见面地点 (如: "QQ群:12345")
                        known_contexts TEXT,            -- 遇到过的场景 (JSON列表)
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    ''')
                    
                    # 恢复数据
                    for data in relationships_data:
                        try:
                            # 根据数据长度确定插入方式
                            if len(data) == 8:
                                # 完整数据
                                cursor.execute('''
                                INSERT INTO relationships (user_id, nickname, relation_type, intimacy, summary, first_met_location, known_contexts, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ''', data)
                            elif len(data) == 7:
                                # 缺少updated_at列
                                cursor.execute('''
                                INSERT INTO relationships (user_id, nickname, relation_type, intimacy, summary, first_met_location, known_contexts)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                ''', data)
                            elif len(data) == 6:
                                # 缺少updated_at和known_contexts列
                                cursor.execute('''
                                INSERT INTO relationships (user_id, nickname, relation_type, intimacy, summary, first_met_location)
                                VALUES (?, ?, ?, ?, ?, ?)
                                ''', data)
                            else:
                                # 其他情况，只插入必要字段
                                cursor.execute('''
                                INSERT INTO relationships (user_id, nickname, relation_type, intimacy, summary)
                                VALUES (?, ?, ?, ?, ?)
                                ''', data[:5])
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
                user_id TEXT PRIMARY KEY,       -- 对方 QQ 号
                nickname TEXT,                  -- AI 对 TA 的称呼
                relation_type TEXT,             -- 关系 (如: 朋友, 损友)
                intimacy INTEGER DEFAULT 0,     -- 好感度 (0-100)
                summary TEXT,                   -- 核心印象 (覆盖式更新，不追加)
                first_met_location TEXT,        -- 初次见面地点 (如: "QQ群:12345")
                known_contexts TEXT,            -- 遇到过的场景 (JSON列表)
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
            
            # 建立索引加速搜索
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mem_cate ON memories(category)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mem_time ON memories(created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_mem_access ON memories(access_count)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tags_memory ON tags(memory_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_synonyms_word ON synonyms(word)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_synonyms_synonym ON synonyms(synonym)')
            
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
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('INSERT INTO activities (action, details) VALUES (?, ?)', (action, details))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"记录活动失败: {e}")

    def write_memory(self, content, category=None, tags="", importance=5):
        """存储记忆
        
        参数说明：
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
            
            # 使用配置的大模型进行自动分类
            if category is None:
                category_model = self.config.get('category_model', '')
                if category_model and self.context:
                    try:
                        # 获取分类列表
                        categories = self.get_memory_categories()
                        if categories:
                            # 构建分类提示词
                            categories_str = '、'.join(categories)
                            prompt = f"请从以下分类中为内容选择最合适的分类：{categories_str}\n\n内容：{content}\n\n请只返回分类名称，不要返回其他任何内容。"
                            
                            # 使用配置的大模型进行分类
                            logger.info(f"使用大模型 {category_model} 进行分类")
                            
                            # 获取大模型提供商
                            provider = self.context.get_provider_by_id(category_model)
                            if provider:
                                # 调用大模型进行分类
                                llm_resp = provider.text_chat(
                                    prompt=prompt,
                                    system_prompt="你是一个分类助手，只需要从给定的分类列表中选择最合适的分类，并只返回分类名称。"
                                )
                                
                                # 处理大模型返回的结果
                                if llm_resp and llm_resp.completion_text:
                                    predicted_category = llm_resp.completion_text.strip()
                                    # 验证返回的分类是否在分类列表中
                                    if predicted_category in categories:
                                        category = predicted_category
                                        logger.info(f"自动分类结果: {category}")
                                    else:
                                        logger.warning(f"大模型返回的分类 '{predicted_category}' 不在分类列表中，使用默认分类")
                                        category = self.get_default_category()
                                else:
                                    logger.warning("大模型分类无返回结果，使用默认分类")
                                    category = self.get_default_category()
                            else:
                                logger.warning(f"未找到大模型提供商: {category_model}，使用默认分类")
                                category = self.get_default_category()
                    except Exception as e:
                        logger.error(f"自动分类失败: {e}")
                        category = self.get_default_category()
                else:
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

    def search_memory(self, query, category_filter=None, limit=5):
        """智能搜索记忆
        
        参数说明：
        - query: 搜索关键词或句子
        - category_filter: 分类过滤
        - limit: 返回结果数量限制
        """
        try:
            # 1. 检查缓存
            cache_key = f"search_{query}_{category_filter}_{limit}"
            if cache_key in self.cache:
                logger.info(f"使用缓存的搜索结果: {query}")
                return self.cache[cache_key]
            
            # 2. 从查询中提取关键词
            query_tags = self.extract_tags_optimized(query)
            
            # 3. 扩展同义词
            expanded_terms = []
            if self.search_strategy.get('synonym_expansion', True):
                for term in query_tags:
                    expanded_terms.extend(self.get_all_synonyms(term))
            else:
                expanded_terms = query_tags
            
            # 4. 确保查询本身也被包含在搜索中
            if query:
                expanded_terms.append(query)
            
            # 5. 构建SQL查询
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 获取所有记忆以进行权重计算
            if category_filter:
                cursor.execute('SELECT * FROM memories WHERE category = ?', (category_filter,))
            else:
                cursor.execute('SELECT * FROM memories')
            
            all_memories = cursor.fetchall()
            
            # 6. 计算相关性分数
            scored_memories = []
            for row in all_memories:
                if row:
                    try:
                        # 从数据库读取标签并处理
                        tags_str = row[5] or ""
                        tags = tags_str.split(',') if tags_str else []
                        tags = [tag.strip() for tag in tags if tag.strip()]
                        
                        # 使用正确的列索引获取数据
                        memory = {
                            "id": row[0],
                            "category": row[1] or self.get_default_category(),  # 默认分类
                            "tags": tags,  # 处理后的标签列表
                            "description": row[6] or "无内容",  # 默认内容
                            "importance": row[2] or 5,  # 从数据库读取重要性
                            "created_at": row[3],
                            "updated_at": row[4],
                            "access_count": row[7],
                            "source_platform": "Web"  # 默认来源
                        }
                        
                        # 计算相关性分数
                        score = self._calculate_relevance_score(memory, expanded_terms, query)
                        if score > 0:
                            memory['relevance_score'] = score
                            scored_memories.append(memory)
                    except Exception as e:
                        logger.error(f"处理记忆失败: {e}")
            
            # 7. 按相关性分数排序
            scored_memories.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
            
            # 8. 限制结果数量
            top_memories = scored_memories[:limit]
            
            # 9. 更新访问次数
            for memory in top_memories:
                memory_id = memory['id']
                cursor.execute('UPDATE memories SET access_count = access_count + 1 WHERE id = ?', (memory_id,))
            
            conn.commit()
            conn.close()
            
            # 10. 格式化返回结果
            formatted_results = []
            for i, memory in enumerate(top_memories, 1):
                formatted_result = f"[{i}] 分类：{memory['category']}\n"
                formatted_result += f"    标签：{', '.join(memory['tags']) if memory['tags'] else '无'}\n"
                formatted_result += f"    描述：{memory['description']}\n"
                formatted_results.append(formatted_result)
            
            # 11. 缓存结果
            self._update_cache(cache_key, formatted_results)
            
            return formatted_results
        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            # 出错时返回空列表
            return []
    
    def _update_cache(self, key, value):
        """更新缓存
        
        参数说明：
        - key: 缓存键
        - value: 缓存值
        """
        # 检查缓存大小
        if len(self.cache) >= self.cache_max_size:
            # 删除最旧的缓存项
            oldest_key = next(iter(self.cache))
            del self.cache[oldest_key]
        # 添加新缓存
        self.cache[key] = value
    
    def _calculate_relevance_score(self, memory, terms, query):
        """计算记忆与查询的相关性分数
        
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
                content_match_score += self.search_weights.get('tag_match', 5.0) * 0.8  # 内容匹配权重稍低
        score += content_match_score
        
        # 3. 分类匹配分数
        category = memory['category'].lower()
        category_match_score = 0
        for term in terms:
            if term.lower() in category:
                category_match_score += self.search_weights.get('category_match', 2.0)
        score += category_match_score
        
        # 4. 重要性分数
        score += memory['importance'] * 0.5
        
        # 5. 访问次数（流行度）分数
        score += memory['access_count'] * self.search_weights.get('popularity', 1.0) * 0.1
        
        # 6. 完整匹配奖励
        if query and query.lower() in memory['description'].lower():
            score += self.search_weights.get('full_match_bonus', 10.0)
        
        return score
    
    def get_recent_memories(self, limit=5):
        """获取最近的记忆"""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM memories ORDER BY created_at DESC LIMIT ?', (limit,))
            results = cursor.fetchall()
            conn.close()
            
            memory_list = []
            for row in results:
                # 确保row不是None
                if row:
                    try:
                        # 使用正确的列索引获取数据
                        memory = {
                            "id": row[0],
                            "category": row[1] or self.get_default_category(),  # 默认分类
                            "importance": row[2] or 5,  # 从数据库读取重要性
                            "created_at": row[3],
                            "updated_at": row[4],
                            "tags": row[5] or "",  # 从数据库读取标签
                            "content": row[6] or "无内容",  # 默认内容
                            "access_count": row[7],
                            "source_platform": "Web"  # 默认来源
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

    def update_relationship(self, user_id, relation_type=None, summary_update=None, intimacy_change=0, nickname=None, first_met_location=None, known_contexts=None):
        """更新关系
        
        参数说明：
        - user_id: 目标用户 ID
        - relation_type: 新的关系定义
        - summary_update: 新的印象总结 (会覆盖旧的)
        - intimacy_change: 好感度变化值 (如 +5, -10)
        - nickname: AI 对 TA 的称呼
        - first_met_location: 初次见面地点 (仅存储ID)
        - known_contexts: 多次相遇群组 (逗号分隔的群ID数组)
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 检查是否存在记录
            cursor.execute('SELECT nickname, relation_type, intimacy, summary, first_met_location, known_contexts FROM relationships WHERE user_id=?', (user_id,))
            existing = cursor.fetchone()
            
            if existing:
                # 更新现有记录
                old_nickname, old_relation_type, old_intimacy, old_summary, old_first_met_location, old_known_contexts = existing
                
                # 处理各字段
                new_nickname = nickname or old_nickname
                new_relation_type = relation_type or old_relation_type
                new_intimacy = old_intimacy + intimacy_change
                new_intimacy = max(0, min(100, new_intimacy))  # 限制在 0-100
                new_summary = summary_update or old_summary
                
                # 处理初次见面地点（仅存储ID或使用"private"表示私聊认识）
                if first_met_location:
                    # 提取ID部分（如果包含群名称），如果是私聊认识可以使用"private"
                    new_first_met_location = first_met_location.split('+')[0].strip()
                else:
                    new_first_met_location = old_first_met_location
                
                # 处理多次相遇群组（完全替换，不合并）
                if known_contexts:
                    # 提取ID部分（如果包含群名称）
                    new_groups = []
                    for group in known_contexts.split(','):
                        group = group.strip()
                        if group:
                            # 提取ID部分
                            group_id = group.split('+')[0].strip()
                            new_groups.append(group_id)
                    new_known_contexts = ','.join(new_groups)
                else:
                    new_known_contexts = old_known_contexts
                
                # 执行更新
                cursor.execute('''
                UPDATE relationships SET 
                    nickname = ?, 
                    relation_type = ?, 
                    intimacy = ?, 
                    summary = ?, 
                    first_met_location = ?, 
                    known_contexts = ?, 
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                ''', (new_nickname, new_relation_type, new_intimacy, new_summary, new_first_met_location, new_known_contexts, user_id))
            else:
                # 创建新记录，默认好感度为20
                new_intimacy = 20 + intimacy_change
                new_intimacy = max(0, min(100, new_intimacy))  # 限制在 0-100
                
                # 处理初次见面地点（仅存储ID或使用"private"表示私聊认识）
                if first_met_location:
                    # 提取ID部分（如果包含群名称），如果是私聊认识可以使用"private"
                    first_met_location = first_met_location.split('+')[0].strip()
                
                # 处理多次相遇群组（仅存储ID）
                if known_contexts:
                    # 提取ID部分（如果包含群名称）
                    new_groups = []
                    for group in known_contexts.split(','):
                        group = group.strip()
                        if group:
                            # 提取ID部分
                            group_id = group.split('+')[0].strip()
                            new_groups.append(group_id)
                    known_contexts = ','.join(new_groups)
                
                cursor.execute('''
                INSERT INTO relationships (user_id, nickname, relation_type, intimacy, summary, first_met_location, known_contexts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (user_id, nickname or "", relation_type or "", new_intimacy, summary_update or "", first_met_location, known_contexts))
            
            conn.commit()
            conn.close()
            
            # 记录活动
            self._record_activity("更新关系", f"用户ID: {user_id}, 关系类型: {relation_type or '未知'}")
            
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
                cursor.execute('SELECT * FROM memories WHERE category=? ORDER BY created_at DESC LIMIT ? OFFSET ?', (category, limit, offset))
            else:
                cursor.execute('SELECT * FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?', (limit, offset))
            
            results = cursor.fetchall()
            conn.close()
            
            memory_list = []
            for row in results:
                memory = {
                    "id": row[0],
                    "category": row[1] or self.get_default_category(),  # 默认分类
                    "importance": row[2] or 5,  # 从数据库读取重要性
                    "created_at": row[3],
                    "updated_at": row[4],
                    "tags": row[5] or "",  # 从数据库读取标签
                    "content": row[6] or "无内容",  # 默认内容
                    "access_count": row[7],
                    "source_platform": "Web"  # 默认来源
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
            
            cursor.execute('SELECT user_id, nickname, relation_type, intimacy, summary, first_met_location, known_contexts, updated_at FROM relationships ORDER BY updated_at DESC LIMIT ? OFFSET ?', (limit, offset))
            results = cursor.fetchall()
            conn.close()
            
            relationship_list = []
            for row in results:
                relationship = {
                    "user_id": row[0],
                    "nickname": row[1],
                    "relation_type": row[2],
                    "intimacy": row[3],
                    "summary": row[4],
                    "first_met_location": row[5],
                    "known_contexts": row[6],
                    "updated_at": row[7]
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
