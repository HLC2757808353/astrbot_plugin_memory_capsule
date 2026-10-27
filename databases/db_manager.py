import sqlite3
import os
import re
import math
import hashlib
import difflib
import unicodedata
from datetime import datetime, timedelta

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from ..security import SENSITIVE_KEYWORDS


def normalize_term(term):
    """归一化梗词用于去重比较：全角转半角、小写、去空格。"""
    if not term:
        return ""
    s = unicodedata.normalize("NFKC", str(term))
    return s.lower().replace(" ", "").strip()


# 只影响语气、不影响语义的尾巴（整活 / 整活啊 / 整活啦 应视为同一个梗）
_TERM_TAIL_PARTICLES = (
    "啊", "呀", "吧", "呢", "哦", "噢", "喔", "啦", "嘛", "哒", "咯", "嘞", "惹", "诶",
    "了", "的", "der",
)
_TERM_EDGE_CHARS = " \t　~～!！。.、,，…·-—_"


def canonical_term(term):
    """把梗词归一到"比较骨架"：NFKC、去标点空白、去掉纯语气尾巴。

    用于判重，不用于展示。例如「栓Q了」「栓q」「栓Q~」都归一到「栓q」。
    """
    if not term:
        return ""
    s = unicodedata.normalize("NFKC", str(term)).lower().strip(_TERM_EDGE_CHARS)
    s = re.sub(r"[\s　~～!！。.、,，…·\-—_]+", "", s)
    changed = True
    while changed and len(s) > 2:
        changed = False
        for p in _TERM_TAIL_PARTICLES:
            if s.endswith(p) and len(s) - len(p) >= 2:
                s = s[: -len(p)]
                changed = True
                break
    return s


_jieba_instance = None
_pseg_instance = None
_jieba_initialized = False

def _get_jieba():
    global _jieba_instance, _pseg_instance, _jieba_initialized
    if not _jieba_initialized:
        try:
            import jieba
            import jieba.posseg as pseg
            _jieba_instance = jieba
            _pseg_instance = pseg
            _jieba_initialized = True
            logger.info("jieba分词器已加载")
        except ImportError:
            logger.warning("jieba未安装，将使用正则分词")
    return _jieba_instance, _pseg_instance

class DatabaseManager:
    def __init__(self, config=None, context=None):
        self.config = config or {}
        self.context = context
        self.db_path = None
        self.backup_manager = None

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
        conn.execute('PRAGMA busy_timeout = 15000')
        conn.execute('PRAGMA cache_size = -2000')
        conn.execute('PRAGMA foreign_keys = ON')
        return conn

    def _execute_write(self, func):
        conn = None
        try:
            conn = self._get_connection()
            result = func(conn)
            conn.commit()
            return result
        except sqlite3.IntegrityError as e:
            err_msg = str(e).lower()
            if 'unique' in err_msg or 'hash' in err_msg or 'memories' in err_msg:
                return "already_exists"
            logger.error(f"Integrity error: {e}")
            return f"Error: integrity violation - {e}"
        except Exception as e:
            err_msg = str(e).lower()
            if 'malformed' in err_msg:
                logger.error(f"Database malformed, rebuilding...")
                if conn:
                    try: conn.close()
                    except Exception: pass
                conn = None
                self._rebuild_database()
                try:
                    conn = self._get_connection()
                    result = func(conn)
                    conn.commit()
                    return result
                except Exception as e2:
                    logger.error(f"Still failed after rebuild: {e2}")
                    return None
            if 'locked' in err_msg:
                import time
                time.sleep(0.3)
                try:
                    if conn:
                        try: conn.close()
                        except Exception: pass
                    conn = self._get_connection()
                    result = func(conn)
                    conn.commit()
                    return result
                except Exception as e2:
                    logger.error(f"Write failed after lock retry: {e2}")
                    return None
            logger.error(f"Write error: {e}")
            return None
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    def _execute_read(self, func):
        conn = None
        try:
            conn = self._get_connection()
            return func(conn)
        except Exception as e:
            logger.debug(f"Read error: {e}")
            return None
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    def _rebuild_database(self):
        logger.warning(f"Rebuilding database from scratch: {self.db_path}")
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            for ext in ['-wal', '-shm', '-journal']:
                p = self.db_path + ext
                if os.path.exists(p):
                    os.remove(p)
        except Exception as e:
            logger.error(f"Failed to delete corrupted database: {e}")
        self._initialize_database_structure()
        self._migrate_relationship_fields()
        self._migrate_memory_visibility()
        self._migrate_activities_fk()
        self._clean_dirty_categories()
        self._cleanup_blank_relationships()
        logger.info("Database rebuilt successfully")
        backup_dir = os.path.join(os.path.dirname(self.db_path), "backups")
        if os.path.exists(backup_dir):
            backups = sorted([f for f in os.listdir(backup_dir) if f.endswith('.db')], reverse=True)
            if backups:
                latest = os.path.join(backup_dir, backups[0])
                logger.warning(f"Data lost, restore manually from: {latest}")

    def initialize(self, data_dir=None):
        if data_dir:
            self.db_path = os.path.join(data_dir, "memory.db")
        else:
            try:
                from astrbot.api.star import StarTools
                data_dir = str(StarTools.get_data_dir("memory_capsule"))
                self.db_path = os.path.join(data_dir, "memory.db")
            except Exception:
                plugin_dir = os.path.dirname(os.path.dirname(__file__))
                data_dir = os.path.join(plugin_dir, "data")
                self.db_path = os.path.join(data_dir, "memory.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        old_db = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "memory.db")
        if os.path.exists(old_db) and not os.path.exists(self.db_path):
            import shutil
            shutil.copy2(old_db, self.db_path)
        self._initialize_database_structure()
        self._check_integrity()
        self._migrate_old_data()
        self._migrate_relationship_fields()
        self._migrate_memory_visibility()
        self._migrate_activities_fk()
        self._clean_dirty_categories()
        self._cleanup_blank_relationships()
        if not self.config.get('lightweight_mode', False):
            _get_jieba()
        from .backup import BackupManager
        self.backup_manager = BackupManager(self.db_path, self.config)
        self.backup_manager.start_auto_backup()
        logger.info(f"Database initialized: {self.db_path}")

    def _check_integrity(self):
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            cursor = conn.cursor()
            cursor.execute('PRAGMA integrity_check')
            result = cursor.fetchone()
            conn.close()
            if result and result[0] != 'ok':
                logger.warning(f"Database integrity check failed: {result[0]}")
                self._rebuild_database()
            else:
                logger.info("Database integrity check passed")
        except Exception as e:
            logger.warning(f"Database integrity check error: {e}")
            self._rebuild_database()

    def _migrate_old_data(self):
        def _do_migrate(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            existing = {r[0] for r in cursor.fetchall()}
            if 'plugin_data' in existing and 'memories' in existing:
                cursor.execute("SELECT COUNT(*) FROM memories")
                if cursor.fetchone()[0] == 0:
                    cursor.execute("SELECT content, category, created_at, updated_at FROM plugin_data")
                    for row in cursor.fetchall():
                        content = row[0] or ''
                        category = row[1] or 'general'
                        content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
                        try:
                            cursor.execute(
                                "INSERT OR IGNORE INTO memories (content, category, importance, tags, source, hash, created_at, updated_at) VALUES (?, ?, 5, '', 'migrated', ?, ?, ?)",
                                (content, category, content_hash, row[2], row[3])
                            )
                        except Exception:
                            pass
        self._execute_write(_do_migrate)

    def _migrate_relationship_fields(self):
        def _do_migrate(conn):
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(relationships)")
            columns = {row[1] for row in cursor.fetchall()}
            try:
                if 'notes' not in columns:
                    cursor.execute('ALTER TABLE relationships ADD COLUMN notes TEXT DEFAULT ""')
                    logger.info("Migrated: added relationships.notes")
            except Exception: pass
            try:
                if 'last_interaction' not in columns:
                    cursor.execute('ALTER TABLE relationships ADD COLUMN last_interaction TIMESTAMP')
                    logger.info("Migrated: added relationships.last_interaction")
            except Exception: pass
        self._execute_write(_do_migrate)

    def _migrate_memory_visibility(self):
        """补充 memories 的可见性与归属字段，并把明显私密的老记忆标为 private。

        可见性语义：public=任何场景（含群聊）都可被注入；private=只在和记忆归属人私聊时
        才会被自动注入，群聊里永远不出现，但仍可被显式搜索到。
        """
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('PRAGMA table_info(memories)')
            columns = {row[1] for row in cursor.fetchall()}
            if 'visibility' not in columns:
                cursor.execute("ALTER TABLE memories ADD COLUMN visibility TEXT DEFAULT 'public'")
                logger.info("Migrated: added memories.visibility")
            if 'owner_id' not in columns:
                cursor.execute("ALTER TABLE memories ADD COLUMN owner_id TEXT DEFAULT ''")
                logger.info("Migrated: added memories.owner_id")
            keywords = self._SENSITIVE_KEYWORDS
            conditions = ' OR '.join(['content LIKE ?'] * len(keywords))
            cursor.execute(
                f"UPDATE memories SET visibility = 'private' "
                f"WHERE (visibility IS NULL OR visibility = 'public') AND ({conditions})",
                [f'%{k}%' for k in keywords])
            if cursor.rowcount > 0:
                logger.info(f"Marked {cursor.rowcount} sensitive memories as private")
        self._execute_write(_do_op)

    def _migrate_activities_fk(self):
        def _do_migrate(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='activities'")
            row = cursor.fetchone()
            if row and row[0] and 'FOREIGN KEY' in row[0]:
                logger.info("Migrating: dropping FK from activities table")
                cursor.execute('ALTER TABLE activities RENAME TO activities_old')
                cursor.execute('''CREATE TABLE activities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER,
                    activity_type TEXT NOT NULL, description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
                cursor.execute('INSERT INTO activities SELECT * FROM activities_old')
                cursor.execute('DROP TABLE activities_old')
                logger.info("Activities FK migration completed")
        self._execute_write(_do_migrate)

    def _initialize_database_structure(self):
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute('PRAGMA journal_mode = WAL')
            conn.execute('PRAGMA foreign_keys = ON')
            conn.execute('PRAGMA busy_timeout = 15000')
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
                category TEXT DEFAULT 'general', importance INTEGER DEFAULT 5,
                tags TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, access_count INTEGER DEFAULT 0,
                last_accessed TIMESTAMP, source TEXT DEFAULT 'user', hash TEXT UNIQUE)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS relationships (
                user_id TEXT PRIMARY KEY, nickname TEXT, relation_type TEXT DEFAULT 'friend',
                summary TEXT DEFAULT '', notes TEXT DEFAULT '', first_met_location TEXT,
                identity_aliases TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, interaction_count INTEGER DEFAULT 0,
                last_interaction TIMESTAMP)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER,
                activity_type TEXT NOT NULL, description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS glossary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                term TEXT NOT NULL,
                category TEXT DEFAULT '其他梗',
                meaning TEXT DEFAULT '',
                source TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                hit_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            # 对话缓存：供"放养式自学"回看，定期清理
            cursor.execute('''CREATE TABLE IF NOT EXISTS learn_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT DEFAULT '', group_id TEXT DEFAULT '',
                user_msg TEXT DEFAULT '', bot_reply TEXT DEFAULT '',
                corrected INTEGER DEFAULT 0, correction TEXT DEFAULT '',
                processed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            # 学到的"说话方式"：rule=硬规则；example=场景示范对
            cursor.execute('''CREATE TABLE IF NOT EXISTS style_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT DEFAULT 'rule',
                category TEXT DEFAULT '表达方式',
                rule TEXT DEFAULT '',
                scene TEXT DEFAULT '', bad TEXT DEFAULT '', good TEXT DEFAULT '',
                evidence INTEGER DEFAULT 1, hits INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1, source TEXT DEFAULT 'auto',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_learn_turns_uid ON learn_turns(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_learn_turns_proc ON learn_turns(processed)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_style_rules_active ON style_rules(active)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_relationships_nickname ON relationships(nickname)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_glossary_term ON glossary(term)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_glossary_category ON glossary(category)')
            try:
                cursor.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content, tags, category, content='memories', content_rowid='id')''')
                cursor.execute('''CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content, tags, category) VALUES (new.id, new.content, new.tags, new.category); END''')
                cursor.execute('''CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, tags, category) VALUES('delete', old.id, old.content, old.tags, old.category); END''')
                cursor.execute('''CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, tags, category) VALUES('delete', old.id, old.content, old.tags, old.category);
                    INSERT INTO memories_fts(rowid, content, tags, category) VALUES (new.id, new.content, new.tags, new.category); END''')
            except Exception as e:
                logger.warning(f"FTS5 setup skipped: {e}")
            conn.commit()
        finally:
            if conn: conn.close()

    def close(self):
        if self.backup_manager: self.backup_manager.stop_auto_backup()
        logger.info("Database closed")

    def backup(self):
        if self.backup_manager: return self.backup_manager.backup()
        return "No backup manager"

    def get_backup_list(self):
        if self.backup_manager: return self.backup_manager.get_backup_list()
        return []

    def restore_from_backup(self, backup_filename):
        if self.backup_manager: return self.backup_manager.restore_from_backup(backup_filename)
        return "No backup manager"

    # ==================== Utility ====================

    def _rrf_fuse(self, result_lists, k=60):
        if not result_lists: return []
        rrf_scores = {}
        rrf_data = {}
        for result_list in result_lists:
            for rank, item in enumerate(result_list, 1):
                mid = item['id']
                if mid not in rrf_scores:
                    rrf_scores[mid] = 0.0
                    rrf_data[mid] = item
                rrf_scores[mid] += 1.0 / (k + rank)
        sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
        return [rrf_data[mid] for mid in sorted_ids]

    def _tokenize(self, text):
        if not self.config.get('lightweight_mode', False):
            jieba_mod, _ = _get_jieba()
            if jieba_mod:
                return [w for w in jieba_mod.cut(text) if len(w) > 1]
        return re.findall(r'\w{2,}', text)

    def _extract_tags(self, content):
        tags = []
        if not self.config.get('lightweight_mode', False):
            try:
                jieba_mod, pseg_mod = _get_jieba()
                if jieba_mod and pseg_mod:
                    for word, flag in pseg_mod.cut(content):
                        if flag in ('nr','ns','nt','nz','v','vn','a','an','n','ng','nl','eng') and len(word) > 1:
                            tags.append(word)
                    if not tags:
                        for word in jieba_mod.cut(content):
                            if len(word) > 1: tags.append(word)
            except Exception:
                tags = re.findall(r'\w{2,}', content)[:5]
        else:
            tags = re.findall(r'\w{2,}', content)[:5]
        return list(set(tags))[:self.config.get('max_extracted_tags', 6)]

    _CATEGORY_KEYWORDS = {
        '技术笔记': ['代码','编程','程序','API','bug','数据库','服务器','框架','Python','Java','部署','Docker','Git','算法','接口','配置','插件','开发','技术','软件','系统'],
        '生活记录': ['今天','昨天','去了','买了','吃了','玩了','看了','做了','出门','回家','上班','下班','天气','周末','假期','旅行','运动','做饭','睡觉'],
        '学习资料': ['学习','教程','课程','笔记','考试','复习','知识','原理','概念','理论','公式','方法','步骤','总结'],
        '个人想法': ['觉得','认为','想法','感觉','希望','想要','如果','应该','也许','可能','喜欢','讨厌','偏好','最爱','开心','难过','生气','害怕'],
        '待办事项': ['记得','记住','提醒','不要','必须','需要','别忘了','记得做','时间','日期','号','周','月','年'],
    }

    def _guess_category(self, content):
        content_lower = content.lower()
        best_cat = 'general'
        best_score = 0
        configured = self.config.get('memory_categories', [])
        for cat, keywords in self._CATEGORY_KEYWORDS.items():
            if configured and cat not in configured: continue
            score = sum(1 for kw in keywords if kw in content_lower)
            if score > best_score:
                best_score = score
                best_cat = cat
        if best_score == 0 and configured:
            return configured[0]
        return best_cat

    _VALID_CATEGORIES = set(_CATEGORY_KEYWORDS.keys()) | {'general'}

    _SENSITIVE_KEYWORDS = SENSITIVE_KEYWORDS

    # 自动注入时忽略的口语高频词，避免"今天""觉得"这类词把无关记忆全捞出来。
    # 只放"高频且几乎不携带信息"的词；具体名词/动词（冰美式、火锅、吉他）要留着当信号。
    _MATCH_STOPWORDS = frozenset((
        '我们', '你们', '他们', '她们', '什么', '怎么', '这个', '那个', '这些', '那些',
        '就是', '还是', '但是', '因为', '所以', '如果', '可以', '不是', '没有', '一个',
        '觉得', '感觉', '知道', '明白', '记得', '忘了', '打算', '准备', '好像', '可能',
        '应该', '需要', '想要', '希望', '一起', '一点', '一下', '一直', '然后', '而且',
        '不过', '其实', '只是', '已经', '还在', '时候', '现在', '今天', '昨天', '明天',
        '最近', '上次', '下次', '这次', '自己', '别人', '东西', '事情', '这样', '那样',
        '真的', '有点', '喜欢', '讨厌', '好玩', '好看', '好听', '好吃', '怎么办',
        '为什么', '是不是', '有没有', '要不要', '能不能', '会不会', '怎么样',
        '练得', '说了', '说是', '那种', '这种', '多少', '几个', '特别', '非常',
        '几点', '多久', '几天', '什么时候', '哪里', '哪儿', '哪天', '刚才',
    ))

    def _clean_dirty_categories(self):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT DISTINCT category FROM memories')
            for row in cursor.fetchall():
                cat = row[0]
                if cat not in self._VALID_CATEGORIES and self.config.get('memory_categories'):
                    if cat not in self.config.get('memory_categories', []):
                        cursor.execute("UPDATE memories SET category = 'general' WHERE category = ?", (cat,))
                        logger.info(f"Cleaned dirty category: {cat}")
        self._execute_write(_do_op)

    def _cleanup_blank_relationships(self):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM relationships WHERE (nickname IS NULL OR nickname = '') "
                "AND (summary IS NULL OR summary = '') AND (notes IS NULL OR notes = '')")
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"Cleaned {deleted} blank relationship records")
        self._execute_write(_do_op)

    # ==================== Memory CRUD ====================

    def write_memory(self, content, category=None, importance=5, tags=None, source='user', visibility='public', owner_id=''):
        _tags = tags
        _category = category
        _memory_id = [None]
        def _do_op(conn):
            nonlocal _tags, _category
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM memories WHERE hash = ?', (content_hash,))
            if cursor.fetchone(): return "Memory already exists"
            if _tags is None: _tags = self._extract_tags(content)
            if _category is None: _category = self._guess_category(content)
            cursor.execute(
                'INSERT INTO memories (content, category, importance, tags, source, hash, visibility, owner_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (content, _category, importance, ','.join(_tags) if isinstance(_tags, list) else _tags, source, content_hash,
                 'private' if str(visibility).lower() == 'private' else 'public', str(owner_id or '')))
            memory_id = cursor.lastrowid
            _memory_id[0] = memory_id
            cursor.execute('INSERT INTO activities (memory_id, activity_type, description) VALUES (?, ?, ?)',
                         (memory_id, 'create', content[:50]))
            return f"Memory saved (ID:{memory_id})"
        result = self._execute_write(_do_op)
        if result == "already_exists": return "Memory already exists"
        return result if result is not None else "Error: database write failed"

    def search_memory(self, query, category_filter=None, limit=None):
        def _do_op(conn):
            _limit = limit if limit is not None else self.config.get('search_max_results', 5)
            result_lists = []
            fts_results = self._fts_search(conn, query, _limit * 3)
            if fts_results: result_lists.append(fts_results)
            tag_results = self._tag_retrieve(conn, query, _limit * 2)
            if tag_results: result_lists.append(tag_results)
            tfidf_results = self._tfidf_search(conn, query, _limit * 2)
            if tfidf_results: result_lists.append(tfidf_results)
            if not result_lists:
                fallback = self._fallback_search(conn, query, _limit * 3)
                if fallback: result_lists.append(fallback)
            fused = self._rrf_fuse(result_lists, k=self.config.get('rrf_k', 60)) if result_lists else []
            if category_filter:
                fused = [r for r in fused if r.get('category') == category_filter]
            if self.config.get('mmr_enabled', True) and len(fused) > _limit:
                fused = self._mmr_rerank(fused, query, _limit)
            else:
                fused = fused[:_limit]
            for r in fused:
                r['content'] = r['content'][:80] + ('...' if len(r['content']) > 80 else '')
            if fused:
                ids = [r['id'] for r in fused]
                placeholders = ','.join('?' * len(ids))
                conn.execute(
                    f'UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id IN ({placeholders})',
                    [datetime.now().isoformat()] + ids)
            return fused
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def delete_memory(self, memory_id):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT content FROM memories WHERE id = ?', (memory_id,))
            row = cursor.fetchone()
            if not row: return "Memory not found"
            cursor.execute('DELETE FROM memories WHERE id = ?', (memory_id,))
            cursor.execute('INSERT INTO activities (memory_id, activity_type, description) VALUES (?, ?, ?)',
                         (memory_id, 'delete', f'deleted: {row[0][:30]}'))
            cursor.execute('DELETE FROM activities WHERE memory_id = ?', (memory_id,))
            return f"Memory deleted (ID:{memory_id})"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: operation failed"

    def delete_memories_many(self, ids):
        """批量删除记忆（一次一条 SQL，比前端逐条删快且不会漏）。

        Args:
            ids: 记忆 ID 列表。

        Returns:
            {"deleted": 实际删除条数}。
        """
        clean = []
        for i in ids or []:
            try:
                clean.append(int(i))
            except (TypeError, ValueError):
                continue
        if not clean:
            return {"deleted": 0}
        def _do_op(conn):
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(clean))
            cursor.execute(f'DELETE FROM activities WHERE memory_id IN ({placeholders})', clean)
            cursor.execute(f'DELETE FROM memories WHERE id IN ({placeholders})', clean)
            return {"deleted": cursor.rowcount}
        result = self._execute_write(_do_op)
        return result if isinstance(result, dict) else {"deleted": 0}

    def update_memory(self, memory_id, content=None, category=None, importance=None, tags=None, visibility=None):
        _tags = tags
        def _do_op(conn):
            nonlocal _tags
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM memories WHERE id = ?', (memory_id,))
            if not cursor.fetchone(): return "Memory not found"
            updates = []
            params = []
            if content is not None:
                updates.append("content = ?"); params.append(content)
                updates.append("hash = ?"); params.append(hashlib.md5(content.encode('utf-8')).hexdigest())
                if _tags is None: _tags = self._extract_tags(content)
            if category is not None: updates.append("category = ?"); params.append(category)
            if importance is not None: updates.append("importance = ?"); params.append(importance)
            if visibility is not None:
                updates.append("visibility = ?")
                params.append('private' if str(visibility).lower() == 'private' else 'public')
            if _tags is not None and _tags != '':
                updates.append("tags = ?"); params.append(','.join(_tags) if isinstance(_tags, list) else _tags)
            updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
            params.append(memory_id)
            cursor.execute(f'UPDATE memories SET {", ".join(updates)} WHERE id = ?', params)
            cursor.execute('INSERT INTO activities (memory_id, activity_type, description) VALUES (?, ?, ?)',
                         (memory_id, 'update', f'importance={importance}' if importance else 'content updated'))
            return f"Memory updated (ID:{memory_id})"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: operation failed"

    def get_all_memories(self, limit=100, offset=0, category=None):
        def _do_op(conn):
            cursor = conn.cursor()
            if category:
                cursor.execute('SELECT id, content, category, importance, tags, visibility, access_count, created_at FROM memories WHERE category = ? ORDER BY created_at DESC LIMIT ? OFFSET ?', (category, limit, offset))
            else:
                cursor.execute('SELECT id, content, category, importance, tags, visibility, access_count, created_at FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?', (limit, offset))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_memories_count(self):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM memories')
            return cursor.fetchone()[0]
        result = self._execute_read(_do_op)
        return result if result is not None else 0

    def get_recent_memories(self, limit=5):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT id, content, category, importance, created_at FROM memories ORDER BY created_at DESC LIMIT ?', (limit,))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_memory_categories(self):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT DISTINCT category FROM memories')
            return [row[0] for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_all_tags(self):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT tags FROM memories WHERE tags != ""')
            all_tags = set()
            for row in cursor.fetchall():
                for tag in row[0].split(','):
                    tag = tag.strip()
                    if tag: all_tags.add(tag)
            return list(all_tags)
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_memory_by_id(self, memory_id):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT id, content, category, importance, tags, access_count, created_at FROM memories WHERE id = ?', (memory_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        return self._execute_read(_do_op)

    # ==================== Search Engines ====================

    def _fts_search(self, conn, query, limit):
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'")
            if not cursor.fetchone(): return []
            if not self.config.get('lightweight_mode', False):
                jieba_mod, _ = _get_jieba()
                if jieba_mod:
                    words = list(jieba_mod.cut(query))
                    fts_query = ' OR '.join(f'"{w}"' for w in words if len(w) > 1)
                else:
                    fts_query = ' OR '.join(f'"{w}"' for w in query.split() if len(w) > 1)
            else:
                fts_query = ' OR '.join(f'"{w}"' for w in re.findall(r'\w{2,}', query))
            if not fts_query: return []
            cursor.execute(
                'SELECT m.id, m.content, m.category, m.importance, m.tags, m.visibility, m.created_at, m.access_count, '
                'bm25(memories_fts) as bm25_score FROM memories m JOIN memories_fts f ON m.id = f.rowid '
                'WHERE memories_fts MATCH ? ORDER BY bm25_score LIMIT ?', (fts_query, limit))
            return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return self._fallback_search(conn, query, limit)

    def _fallback_search(self, conn, query, limit):
        try:
            cursor = conn.cursor()
            keywords = query.split()
            conditions = []
            params = []
            for kw in keywords:
                conditions.append("(content LIKE ? OR tags LIKE ? OR category LIKE ?)")
                params.extend([f'%{kw}%', f'%{kw}%', f'%{kw}%'])
            where = ' OR '.join(conditions)
            cursor.execute(
                f'SELECT id, content, category, importance, tags, visibility, created_at, access_count FROM memories WHERE {where} ORDER BY importance DESC, created_at DESC LIMIT ?',
                params + [limit])
            return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []

    def _tag_retrieve(self, conn, query, limit):
        try:
            words = self._tokenize(query)
            if not words: return []
            cursor = conn.cursor()
            conditions = []
            params = []
            for w in words[:5]:
                conditions.append("tags LIKE ?")
                params.append(f'%{w}%')
            where = ' OR '.join(conditions)
            cursor.execute(
                f'SELECT id, content, category, importance, tags, visibility, created_at, access_count FROM memories WHERE {where} ORDER BY importance DESC LIMIT ?',
                params + [limit])
            return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []

    def _tfidf_search(self, conn, query, limit):
        if not self.config.get('tfidf_search_enabled', True): return []
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT id, content, category, importance, tags, visibility, created_at, access_count FROM memories LIMIT ?',
                           (self.config.get('tfidf_search_limit', 200),))
            all_memories = [dict(row) for row in cursor.fetchall()]
            if not all_memories: return []
            query_tokens = self._tokenize(query.lower())
            if not query_tokens: return []
            from collections import Counter
            doc_freq = Counter()
            doc_tokens = {}
            for m in all_memories:
                tokens = list(set(self._tokenize(m['content'].lower()) + self._tokenize(m.get('tags', '').lower())))
                doc_tokens[m['id']] = tokens
                for t in tokens: doc_freq[t] += 1
            N = len(all_memories)
            idf = {t: math.log(N / (1 + df)) for t, df in doc_freq.items()}
            query_tf = Counter(query_tokens)
            expanded_tokens = set(query_tokens)
            for m in all_memories:
                m_tags = set(self._tokenize(m.get('tags', '').lower()))
                if m_tags & expanded_tokens:
                    for ct in self._tokenize(m['content'].lower())[:8]: expanded_tokens.add(ct)
            for t in expanded_tokens - set(query_tokens): query_tf[t] = 0.5
            query_vec = {t: tf * idf[t] for t, tf in query_tf.items() if t in idf}
            if not query_vec: return []
            query_norm = math.sqrt(sum(v ** 2 for v in query_vec.values()))
            if query_norm == 0: return []
            scored = []
            for m in all_memories:
                doc_tf = Counter(doc_tokens.get(m['id'], []))
                doc_vec = {t: tf * idf[t] for t, tf in doc_tf.items() if t in idf}
                dot = sum(query_vec.get(t, 0) * doc_vec.get(t, 0) for t in query_vec)
                doc_norm = math.sqrt(sum(v ** 2 for v in doc_vec.values()))
                if doc_norm == 0: continue
                cosine = dot / (query_norm * doc_norm)
                if cosine > 0.03:
                    m['tfidf_score'] = cosine
                    scored.append(m)
            scored.sort(key=lambda x: x['tfidf_score'], reverse=True)
            return scored[:limit]
        except Exception:
            return []

    def _mmr_rerank(self, results, query, limit):
        if not results or len(results) <= limit: return results
        query_words = set(re.findall(r'\w+', query.lower()))
        selected = [results[0]]
        remaining = results[1:]
        lambda_param = self.config.get('mmr_lambda', 0.7)
        while len(selected) < limit and remaining:
            best_score = -float('inf')
            best_idx = 0
            for i, candidate in enumerate(remaining):
                cand_words = set(re.findall(r'\w+', candidate.get('content', '').lower()))
                relevance = len(query_words & cand_words) / max(len(query_words), 1)
                max_sim = 0
                for sel in selected:
                    sel_words = set(re.findall(r'\w+', sel.get('content', '').lower()))
                    sim = len(cand_words & sel_words) / max(len(cand_words | sel_words), 1)
                    max_sim = max(max_sim, sim)
                mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = i
            selected.append(remaining.pop(best_idx))
        return selected

    # ==================== Relationships ====================

    def update_relationship_enhanced(self, user_id, relation_type=None, summary=None, nickname=None, first_met_location=None, notes=None):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM relationships WHERE user_id = ?', (user_id,))
            existing = cursor.fetchone()
            if existing:
                existing = dict(existing)
                updates = []
                params = []
                if relation_type is not None: updates.append("relation_type = ?"); params.append(relation_type)
                if summary is not None: updates.append("summary = ?"); params.append(summary)
                if nickname is not None: updates.append("nickname = ?"); params.append(nickname)
                if first_met_location is not None: updates.append("first_met_location = ?"); params.append(first_met_location)
                if notes is not None: updates.append("notes = ?"); params.append(notes)
                updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
                params.append(user_id)
                cursor.execute(f'UPDATE relationships SET {", ".join(updates)} WHERE user_id = ?', params)
                cursor.execute('INSERT INTO activities (memory_id, activity_type, description) VALUES (?, ?, ?)',
                             (0, 'update_relation', f'{nickname or user_id}: {summary[:30] if summary else ""}'))
                return f"Relationship updated: {nickname or user_id}"
            else:
                cursor.execute(
                    'INSERT INTO relationships (user_id, nickname, relation_type, summary, notes, first_met_location, last_interaction) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (user_id, nickname or '', relation_type or 'friend', summary or '', notes or '', first_met_location or '', datetime.now().isoformat()))
                cursor.execute('INSERT INTO activities (memory_id, activity_type, description) VALUES (?, ?, ?)',
                             (0, 'create_relation', f'{nickname or user_id}'))
                return f"Relationship created: {nickname or user_id}"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: update relationship failed"

    def get_relationship_by_user_id(self, user_id):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM relationships WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        return self._execute_read(_do_op)

    def get_relationship_with_identity(self, user_id):
        rel = self.get_relationship_by_user_id(user_id)
        if not rel: return None
        aliases = rel.get('identity_aliases', '')
        if aliases:
            try: rel['identity_aliases'] = [a.strip() for a in aliases.split(',') if a.strip()]
            except Exception: rel['identity_aliases'] = []
        return rel

    def get_relationship_checked(self, user_id):
        """读取单个关系档案，读库异常时抛出异常而不是返回 None。

        注入逻辑需要区分"这个人确实没有档案"和"数据库读失败"：后者绝不能当成
        陌生人去对话，所以这里把异常交给调用方处理。

        Args:
            user_id: 目标用户 ID。

        Returns:
            关系字典；没有该用户时返回 None。

        Raises:
            Exception: 数据库读取失败时向上抛出。
        """
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM relationships WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            if not row: return None
            rel = dict(row)
            aliases = rel.get('identity_aliases') or ''
            rel['identity_aliases'] = [a.strip() for a in str(aliases).split(',') if a.strip()]
            return rel
        conn = self._get_connection()
        try:
            return _do_op(conn)
        finally:
            try: conn.close()
            except Exception: pass

    def get_all_relationships(self, limit=100, offset=0):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM relationships ORDER BY updated_at DESC LIMIT ? OFFSET ?', (limit, offset))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_relationships_count(self):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM relationships')
            return cursor.fetchone()[0]
        result = self._execute_read(_do_op)
        return result if result is not None else 0

    def search_relationship(self, query, limit=3):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute(
                'SELECT * FROM relationships WHERE user_id = ? OR nickname = ?',
                (query, query))
            exact = [dict(row) for row in cursor.fetchall()]
            if exact:
                return exact[:limit]
            cursor.execute(
                'SELECT * FROM relationships WHERE user_id LIKE ? OR nickname LIKE ? OR relation_type LIKE ? OR summary LIKE ? OR notes LIKE ? LIMIT ?',
                (f'%{query}%', f'%{query}%', f'%{query}%', f'%{query}%', f'%{query}%', limit))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def delete_relationship(self, user_id):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT nickname FROM relationships WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            if not row: return "Relationship not found"
            cursor.execute('DELETE FROM relationships WHERE user_id = ?', (user_id,))
            return f"Relationship deleted: {user_id}"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: delete relationship failed"

    def auto_update_last_interaction(self, user_id):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT user_id FROM relationships WHERE user_id = ?', (user_id,))
            if not cursor.fetchone():
                return
            cursor.execute(
                'UPDATE relationships SET last_interaction = ?, interaction_count = interaction_count + 1 WHERE user_id = ?',
                (datetime.now().isoformat(), user_id))
        self._execute_write(_do_op)

    def add_identity_alias(self, user_id, alias):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT identity_aliases FROM relationships WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            if not row: return "Relationship not found"
            current = row[0] or ''
            aliases = [a.strip() for a in current.split(',') if a.strip()]
            if alias not in aliases: aliases.append(alias)
            cursor.execute('UPDATE relationships SET identity_aliases = ?, updated_at = ? WHERE user_id = ?',
                         (','.join(aliases), datetime.now().isoformat(), user_id))
            return f"Alias added: {alias}"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: add alias failed"

    def get_user_aliases(self, user_id):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT identity_aliases FROM relationships WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            if not row or not row[0]: return []
            return [a.strip() for a in row[0].split(',') if a.strip()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def smart_resolve_identity(self, identifier):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, nickname FROM relationships WHERE user_id = ?', (identifier,))
            row = cursor.fetchone()
            if row: return {'user_id': row[0], 'nickname': row[1], 'match_type': 'exact'}
            cursor.execute('SELECT user_id, nickname FROM relationships WHERE nickname = ?', (identifier,))
            row = cursor.fetchone()
            if row: return {'user_id': row[0], 'nickname': row[1], 'match_type': 'nickname'}
            cursor.execute('SELECT user_id, nickname, identity_aliases FROM relationships')
            for row in cursor.fetchall():
                aliases = (row[2] or '').split(',')
                if identifier in [a.strip() for a in aliases]:
                    return {'user_id': row[0], 'nickname': row[1], 'match_type': 'alias'}
            cursor.execute('SELECT user_id, nickname FROM relationships WHERE nickname LIKE ?', (f'%{identifier}%',))
            row = cursor.fetchone()
            if row: return {'user_id': row[0], 'nickname': row[1], 'match_type': 'fuzzy'}
            return None
        return self._execute_read(_do_op)

    # ==================== Activities & Utils ====================

    def get_recent_activities(self, limit=20):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute(
                'SELECT a.*, m.content FROM activities a LEFT JOIN memories m ON a.memory_id = m.id ORDER BY a.created_at DESC LIMIT ?', (limit,))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def cleanup_memories(self, days=None, max_memories=None):
        days = days or self.config.get('memory_cleanup_days', 365)
        max_memories = max_memories or self.config.get('memory_cleanup_max', 10000)
        def _do_op(conn):
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor.execute('DELETE FROM memories WHERE importance < 3 AND access_count = 0 AND created_at < ?', (cutoff,))
            deleted = cursor.rowcount
            cursor.execute('SELECT COUNT(*) FROM memories')
            count = cursor.fetchone()[0]
            if count > max_memories:
                cursor.execute('DELETE FROM memories WHERE id IN (SELECT id FROM memories ORDER BY importance ASC, access_count ASC, created_at ASC LIMIT ?)', (count - max_memories,))
                deleted += cursor.rowcount
            return f"Cleaned {deleted} memories"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: cleanup failed"

    def bulk_import_memories(self, items):
        def _do_op(conn):
            cursor = conn.cursor()
            imported = 0
            skipped = 0
            for item in items:
                content = item.get('content', '').strip()
                if not content: continue
                content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
                cursor.execute('SELECT id FROM memories WHERE hash = ?', (content_hash,))
                if cursor.fetchone(): skipped += 1; continue
                category = item.get('category') or self._guess_category(content)
                importance = item.get('importance', 5)
                tags = item.get('tags')
                if tags is None: tags = self._extract_tags(content)
                elif isinstance(tags, str): tags = tags.split(',')
                source = item.get('source', 'import')
                visibility = 'private' if str(item.get('visibility', '')).lower() == 'private' else 'public'
                cursor.execute(
                    'INSERT INTO memories (content, category, importance, tags, source, hash, visibility, owner_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (content, category, importance, ','.join(tags) if isinstance(tags, list) else tags, source, content_hash,
                     visibility, str(item.get('owner_id', '') or '')))
                imported += 1
            return f"Imported: {imported}, Skipped (duplicate): {skipped}"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: bulk import failed"

    def get_memory_stats(self):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM memories')
            mem_count = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM relationships')
            rel_count = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM activities')
            act_count = cursor.fetchone()[0]
            import sys
            return {
                'memories': mem_count, 'relationships': rel_count,
                'activities': act_count, 'python_version': sys.version.split()[0]
            }
        result = self._execute_read(_do_op)
        return result if result is not None else {'error': 'stats read failed'}

    # ==================== Glossary (梗/黑话库) ====================

    def add_glossary(self, term, category='其他梗', meaning='', source='', tags='', fuzzy_dedup=False):
        term = str(term).strip()
        if not term:
            return "Error: term is empty"
        if isinstance(tags, list):
            tags = ','.join(tags)
        # 模糊去重：词面归一化 + 变体包含 + 释义近似的都算重复（采集/手动添加场景）
        if fuzzy_dedup:
            similar = self.find_similar_glossary(term, meaning)
            if similar:
                logger.info(f"Glossary duplicate skipped: {term} ~ {similar[0]['term']} "
                            f"({similar[0]['reason']})")
                return "already_exists"
        def _do_op(conn):
            cursor = conn.cursor()
            # 归一化判重：忽略大小写/全角半角/空格差异（如 yyds / YYDS / ｙｙｄｓ）
            norm = normalize_term(term)
            cursor.execute(
                'SELECT id FROM glossary WHERE LOWER(REPLACE(term, " ", "")) = ?',
                (norm,))
            if cursor.fetchone():
                return "already_exists"
            cursor.execute(
                'INSERT INTO glossary (term, category, meaning, source, tags) VALUES (?, ?, ?, ?, ?)',
                (term, category, meaning, source, tags))
            return f"Glossary saved (ID:{cursor.lastrowid})"
        result = self._execute_write(_do_op)
        if result == "already_exists":
            return "already_exists"
        return result if result is not None else "Error: database write failed"

    def find_similar_glossary(self, term, meaning=None, threshold=0.8, limit=1):
        """查找与 term 语义重复的已有梗（用于模糊去重）。

        判定顺序（命中任一即视为重复）：
        1. 归一化后完全相同（yyds / YYDS / ｙｙｄｓ）
        2. 去掉语气尾巴后相同（整活 / 整活啊 / 整活啦）
        3. 变体包含：短的完整出现在长的里，且长度差不超过 4（栓Q / 栓Q了）
        4. 归一化字符串相似度 >= threshold（整活 / 整烂活）
        5. 释义高度相似（换了个词头但解释一模一样）

        Args:
            term: 待判重的词。
            meaning: 待判重的释义，可为空；提供后额外做释义近似判重。
            threshold: 字符串相似度阈值。
            limit: 最多返回几条。

        Returns:
            相似条目列表，每项含 id / term / score / reason。
        """
        norm = normalize_term(term)
        canon = canonical_term(term)
        if not norm:
            return []
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT id, term, meaning FROM glossary')
            rows = cursor.fetchall()
            max_diff = max(2, len(canon) // 3)
            new_meaning = str(meaning or '').strip()
            candidates = []
            for row in rows:
                t_norm = normalize_term(row['term'])
                if not t_norm:
                    continue
                t_canon = canonical_term(row['term'])
                reason = None
                score = 1.0
                if t_norm == norm:
                    reason = "同词"
                elif canon and t_canon == canon:
                    reason = "语气变体"
                elif (canon and t_canon and len(canon) >= 2 and len(t_canon) >= 2
                      and abs(len(t_canon) - len(canon)) <= 4
                      and (canon in t_canon or t_canon in canon)):
                    reason = "包含变体"
                    score = 0.9
                if reason is None:
                    # 词形相似度算一次，既用于判重，也用于决定要不要比释义
                    t_ratio = difflib.SequenceMatcher(None, canon, t_canon).ratio()
                    if abs(len(t_canon) - len(canon)) <= max_diff and t_ratio >= threshold:
                        reason = "词形近似"
                        score = t_ratio
                    elif t_ratio >= 0.3 and len(new_meaning) >= 20:
                        # 只在"词本身已经像"时才比释义，避免把互不相干的两个梗
                        # 因为解释措辞相近而误判成重复（语义重复主要靠 LLM 判断）
                        old_meaning = str(row['meaning'] or '').strip()
                        if len(old_meaning) >= 20:
                            m_ratio = difflib.SequenceMatcher(
                                None, new_meaning[:100], old_meaning[:100]).ratio()
                            if m_ratio >= 0.88:
                                reason = "释义重复"
                                score = m_ratio
                if reason:
                    candidates.append((score, row['id'], row['term'], reason))
            candidates.sort(key=lambda x: -x[0])
            return [{'id': i, 'term': t, 'score': round(s, 3), 'reason': r}
                    for s, i, t, r in candidates[:limit]]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def update_glossary(self, glossary_id, term=None, category=None, meaning=None, source=None, tags=None):
        _tags = tags
        def _do_op(conn):
            nonlocal _tags
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM glossary WHERE id = ?', (glossary_id,))
            if not cursor.fetchone():
                return "Glossary not found"
            updates = []
            params = []
            if term is not None:
                updates.append("term = ?"); params.append(str(term).strip())
            if category is not None:
                updates.append("category = ?"); params.append(category)
            if meaning is not None:
                updates.append("meaning = ?"); params.append(meaning)
            if source is not None:
                updates.append("source = ?"); params.append(source)
            if _tags is not None:
                if isinstance(_tags, list): _tags = ','.join(_tags)
                updates.append("tags = ?"); params.append(_tags)
            updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
            params.append(glossary_id)
            cursor.execute(f'UPDATE glossary SET {", ".join(updates)} WHERE id = ?', params)
            return f"Glossary updated (ID:{glossary_id})"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: operation failed"

    def delete_glossary(self, glossary_id):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('DELETE FROM glossary WHERE id = ?', (glossary_id,))
            if cursor.rowcount == 0:
                return "Glossary not found"
            return f"Glossary deleted (ID:{glossary_id})"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: operation failed"

    def delete_glossary_by_source(self, source):
        """删除指定来源的全部梗（如清空 source='自动采集' 的低质数据）。"""
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('DELETE FROM glossary WHERE source = ?', (source,))
            return {"deleted": cursor.rowcount, "source": source}
        result = self._execute_write(_do_op)
        return result if result is not None else {"deleted": 0, "source": source}

    def get_glossary(self, glossary_id):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM glossary WHERE id = ?', (glossary_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        return self._execute_read(_do_op)

    def get_glossaries(self, limit=28, offset=0, category=None, query=None):
        def _do_op(conn):
            cursor = conn.cursor()
            conditions = []
            params = []
            if category:
                conditions.append("category = ?"); params.append(category)
            if query:
                q = str(query).strip()
                if q:
                    conditions.append("(term LIKE ? OR meaning LIKE ? OR tags LIKE ? OR source LIKE ?)")
                    params.extend([f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%'])
            where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
            cursor.execute(
                f'SELECT * FROM glossary{where} ORDER BY created_at DESC LIMIT ? OFFSET ?',
                params + [limit, offset])
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_glossaries_count(self, category=None, query=None):
        def _do_op(conn):
            cursor = conn.cursor()
            conditions = []
            params = []
            if category:
                conditions.append("category = ?"); params.append(category)
            if query:
                q = str(query).strip()
                if q:
                    conditions.append("(term LIKE ? OR meaning LIKE ? OR tags LIKE ? OR source LIKE ?)")
                    params.extend([f'%{q}%', f'%{q}%', f'%{q}%', f'%{q}%'])
            where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
            cursor.execute(f'SELECT COUNT(*) FROM glossary{where}', params)
            return cursor.fetchone()[0]
        result = self._execute_read(_do_op)
        return result if result is not None else 0

    def delete_glossary_many(self, ids):
        """批量删除梗库条目。

        Args:
            ids: 梗 ID 列表。

        Returns:
            {"deleted": 实际删除条数}。
        """
        clean = []
        for i in ids or []:
            try:
                clean.append(int(i))
            except (TypeError, ValueError):
                continue
        if not clean:
            return {"deleted": 0}
        def _do_op(conn):
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(clean))
            cursor.execute(f'DELETE FROM glossary WHERE id IN ({placeholders})', clean)
            return {"deleted": cursor.rowcount}
        result = self._execute_write(_do_op)
        return result if isinstance(result, dict) else {"deleted": 0}

    def find_duplicate_glossary_groups(self, threshold=0.8, max_terms=5000, max_pairs=300000):
        """扫描整个梗库，找出重复/近似重复的条目并分组（用于"一键去重"）。

        判定与入库去重同一套规则：去掉语气尾巴后完全相同 → 变体包含 → 词形相似度。
        为避免 O(n^2)，先用「归一化词 + 二元组倒排」把候选对缩小到同源词上，
        再对候选对算相似度并做并查集合并。组内保留释义最完整（其次 id 最小）的一条。

        Args:
            threshold: 词形相似度阈值。
            max_terms: 单次最多扫描多少条（超出会截断并在结果里标记）。
            max_pairs: 候选对上限，防止超大词库把按钮点成卡死。

        Returns:
            {"groups": [...], "scanned": n, "truncated": bool, "duplicate_count": n}
        """
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT id, term, meaning, category, source, hit_count, created_at '
                           'FROM glossary ORDER BY id')
            rows = [dict(r) for r in cursor.fetchall()]
            truncated = len(rows) > max_terms
            if truncated:
                rows = rows[:max_terms]
            if len(rows) < 2:
                return {"groups": [], "scanned": len(rows), "truncated": truncated,
                        "duplicate_count": 0}

            def key_of(row):
                return (canonical_term(row['term']) or normalize_term(row['term'])
                        or str(row['term']))

            canon = {r['id']: key_of(r) for r in rows}
            parent = {r['id']: r['id'] for r in rows}

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

            # 1) 归一化后完全相同的先合并（栓Q / 栓Q了 / 栓q~）
            by_key = {}
            for r in rows:
                by_key.setdefault(canon[r['id']], []).append(r['id'])
            for ids_ in by_key.values():
                for other in ids_[1:]:
                    union(ids_[0], other)

            # 2) 其余近似变体：先分桶缩小候选对，再按相似度合并。
            #    中文变体常是中间插字（破防→破大防、整活→整烂活），二元组会完全错开，
            #    所以同时用「首尾字」「前两字」和「二元组」三种桶，命中任一同桶即比较。
            buckets = {}
            for r in rows:
                s = canon[r['id']]
                keys = set()
                if len(s) >= 2:
                    keys.add("F" + s[0] + s[-1])
                    keys.add("P" + s[:2])
                for i in range(len(s) - 1):
                    keys.add(s[i:i + 2])
                for k in keys:
                    buckets.setdefault(k, []).append(r['id'])

            pairs = set()
            for ids_ in buckets.values():
                # 单个桶命中太多条（如常见字）说明没有区分度，跳过
                if len(ids_) < 2 or len(ids_) > 40:
                    continue
                for i in range(len(ids_)):
                    for j in range(i + 1, len(ids_)):
                        a, b = ids_[i], ids_[j]
                        pairs.add((a, b) if a < b else (b, a))
                        if len(pairs) >= max_pairs:
                            break
                if len(pairs) >= max_pairs:
                    break
            for a, b in pairs:
                sa, sb = canon[a], canon[b]
                if abs(len(sa) - len(sb)) > 4:
                    continue
                if difflib.SequenceMatcher(None, sa, sb).ratio() >= threshold:
                    union(a, b)

            clusters = {}
            for r in rows:
                clusters.setdefault(find(r['id']), []).append(r)
            groups = [items for items in clusters.values() if len(items) > 1]

            # 组内选"保留项"：释义最完整优先，其次最早入库
            out = []
            duplicate_count = 0
            for items in groups:
                items = sorted(items, key=lambda x: (-len(str(x.get('meaning') or '')), x['id']))
                keep = items[0]
                dups = items[1:]
                duplicate_count += len(dups)
                out.append({
                    "keep": {"id": keep['id'], "term": keep['term'],
                             "meaning": str(keep.get('meaning') or '')[:120]},
                    "duplicates": [{"id": d['id'], "term": d['term'],
                                    "meaning": str(d.get('meaning') or '')[:120]}
                                   for d in dups],
                })
            out.sort(key=lambda g: -len(g["duplicates"]))
            return {"groups": out, "scanned": len(rows), "truncated": truncated,
                    "duplicate_count": duplicate_count}
        result = self._execute_read(_do_op)
        return result if result is not None else {"groups": [], "scanned": 0,
                                                  "truncated": False, "duplicate_count": 0}

    def get_glossary_categories(self):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT DISTINCT category FROM glossary ORDER BY category')
            cats = [row[0] for row in cursor.fetchall()]
            ordered = ['谐音梗', '行为梗', '抽象梗', '表情包梗', '其他梗']
            return list(dict.fromkeys([c for c in ordered if c in cats] + [c for c in cats if c not in ordered]))
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_glossary_stats(self):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM glossary')
            total = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(DISTINCT category) FROM glossary')
            categories = cursor.fetchone()[0]
            from datetime import date
            today = date.today().isoformat()
            cursor.execute('SELECT COUNT(*) FROM glossary WHERE created_at >= ?', (today,))
            today_new = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM glossary WHERE hit_count > 0')
            used = cursor.fetchone()[0]
            cursor.execute('SELECT COALESCE(SUM(hit_count), 0) FROM glossary')
            hit_total = cursor.fetchone()[0]
            return {'total': total, 'categories': categories, 'today_new': today_new,
                    'used': used, 'hit_total': hit_total}
        result = self._execute_read(_do_op)
        return result if result is not None else {'total': 0, 'categories': 0, 'today_new': 0,
                                                  'used': 0, 'hit_total': 0}

    def get_all_glossary_terms(self):
        """返回所有梗词条目，用于对话匹配注入。"""
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT id, term, meaning, category, source FROM glossary')
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def increment_glossary_hit(self, glossary_id):
        def _do_op(conn):
            conn.execute('UPDATE glossary SET hit_count = hit_count + 1 WHERE id = ?', (glossary_id,))
            return "ok"
        self._execute_write(_do_op)

    def increment_glossary_hits(self, ids):
        """批量给命中并真正注入的梗计数（一次一条 SQL）。

        Args:
            ids: 梗 ID 列表。

        Returns:
            实际更新的条数。
        """
        clean = []
        for i in ids or []:
            try:
                clean.append(int(i))
            except (TypeError, ValueError):
                continue
        if not clean:
            return 0
        def _do_op(conn):
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(clean))
            cursor.execute(
                f'UPDATE glossary SET hit_count = hit_count + 1 WHERE id IN ({placeholders})', clean)
            return cursor.rowcount
        return self._execute_write(_do_op) or 0

    def bulk_import_glossary(self, items):
        def _do_op(conn):
            cursor = conn.cursor()
            imported = 0
            skipped = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                term = str(item.get('term', '')).strip()
                if not term:
                    continue
                norm = normalize_term(term)
                cursor.execute(
                    'SELECT id FROM glossary WHERE LOWER(REPLACE(term, " ", "")) = ?',
                    (norm,))
                if cursor.fetchone():
                    skipped += 1
                    continue
                category = item.get('category') or '其他梗'
                meaning = str(item.get('meaning', '') or '')
                source = str(item.get('source', '') or '')
                tags = item.get('tags')
                if isinstance(tags, list):
                    tags = ','.join(str(t) for t in tags)
                elif tags is None:
                    tags = ''
                else:
                    tags = str(tags)
                cursor.execute(
                    'INSERT INTO glossary (term, category, meaning, source, tags) VALUES (?, ?, ?, ?, ?)',
                    (term, category, meaning, source, tags))
                imported += 1
            return f"Imported: {imported}, Skipped (duplicate): {skipped}"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: bulk import failed"

    # ==================== Associative Memory (搜索联想) ====================

    def search_memory_related(self, query, exclude_ids=None, limit=3):
        """在精确搜索结果之外，返回一些不完全相关但可能有用的记忆（联想）。"""
        exclude_ids = exclude_ids or []
        def _do_op(conn):
            cursor = conn.cursor()
            excl_ph = ','.join('?' * len(exclude_ids)) if exclude_ids else ''
            excl_sql = f' AND id NOT IN ({excl_ph})' if excl_ph else ''
            excl_params = list(exclude_ids)

            related = []
            # 1) 同分类的高分记忆
            query_words = set(re.findall(r'\w{2,}', str(query).lower()))
            cats = set()
            if query_words:
                for w in query_words:
                    cursor.execute(
                        f'SELECT category, COUNT(*) as c FROM memories WHERE content LIKE ? GROUP BY category ORDER BY c DESC LIMIT 1',
                        (f'%{w}%',))
                    row = cursor.fetchone()
                    if row and row['category']:
                        cats.add(row['category'])
            if cats:
                cat_ph = ','.join('?' * len(cats))
                cursor.execute(
                    f'SELECT id, content, category, importance, tags, created_at FROM memories '
                    f'WHERE category IN ({cat_ph}) AND importance >= 6{excl_sql} '
                    f'ORDER BY importance DESC, created_at DESC LIMIT ?',
                    list(cats) + excl_params + [limit])
                related.extend(dict(r) for r in cursor.fetchall())
            # 2) 补充：随机"也许有用"的高分记忆（不同分类，避免完全无关）
            if len(related) < limit:
                seen = set(exclude_ids) | {r['id'] for r in related}
                seen_ph = ','.join('?' * len(seen)) if seen else ''
                seen_sql = f' AND id NOT IN ({seen_ph})' if seen_ph else ''
                cursor.execute(
                    f'SELECT id, content, category, importance, tags, created_at FROM memories '
                    f'WHERE importance >= 7{seen_sql} ORDER BY RANDOM() LIMIT ?',
                    list(seen) + [limit - len(related)])
                related.extend(dict(r) for r in cursor.fetchall())
            for r in related:
                r['content'] = r['content'][:80] + ('...' if len(r['content']) > 80 else '')
                r['associative'] = True
            return related[:limit]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def match_memories(self, query, limit=3, min_score=2, include_private=False, owner_id=""):
        """按词面重合度匹配与 query 相关的记忆，供对话中的自动记忆注入使用。

        与 search_memory 的区别：要求多个有效词真的同时出现在同一条记忆里（默认
        min_score=2），避免把无关记忆塞进上下文；并且不会累加 access_count。

        Args:
            query: 用户当前这句话。
            limit: 最多返回几条。
            min_score: 至少命中几个有效词才算相关；只有 1 个有效词的短句自动降为 1。
            include_private: 是否允许返回 private 记忆（私聊场景传 True）。
            owner_id: 当前对话者 ID，用于过滤 private 记忆的归属。

        Returns:
            记忆字典列表（content 截断到 120 字符）。
        """
        def _do_op(conn):
            words = [w for w in self._tokenize(str(query))
                     if len(w) >= 2 and w not in self._MATCH_STOPWORDS]
            words = list(dict.fromkeys(words))[:8]
            # 轻量模式（无 jieba）下中文会被正则切成一整串，退化为二元切片，否则永远匹配不上
            if len(words) == 1 and len(words[0]) >= 4 and all(ord(c) > 127 for c in words[0]):
                run = words[0]
                words = list(dict.fromkeys(run[i:i + 2] for i in range(len(run) - 1)))
            if not words: return []
            need = 1 if len(words) == 1 else max(1, int(min_score))
            conditions = []
            params = []
            for w in words:
                conditions.append('(content LIKE ? OR tags LIKE ?)')
                params.extend([f'%{w}%', f'%{w}%'])
            sql = (f"SELECT id, content, category, importance, tags, visibility, owner_id FROM memories "
                   f"WHERE ({' OR '.join(conditions)})")
            if include_private:
                # 私聊：公开记忆 + 归属自己的私有记忆（历史遗留的未归属私有记忆也放行）
                sql += " AND (visibility = 'public' OR visibility IS NULL OR owner_id = '' OR owner_id = ?)"
                params.append(str(owner_id or ''))
            else:
                sql += " AND (visibility = 'public' OR visibility IS NULL)"
            sql += " LIMIT 200"
            cursor = conn.cursor()
            cursor.execute(sql, params)
            scored = []
            for row in cursor.fetchall():
                blob = f"{row['content'] or ''} {row['tags'] or ''}"
                score = sum(1 for w in words if w in blob)
                if score < need: continue
                scored.append((score, row['importance'] or 0, row))
            scored.sort(key=lambda x: (-x[0], -x[1]))
            matches = []
            for _score, _importance, row in scored[:limit]:
                item = {
                    'id': row['id'],
                    'content': (row['content'] or '')[:120] + ('...' if len(row['content'] or '') > 120 else ''),
                    'category': row['category'],
                    'importance': row['importance'],
                }
                matches.append(item)
            return matches
        result = self._execute_read(_do_op)
        return result if result is not None else []

    # ==================== 自动学习（说话方式） ====================

    def push_learn_turn(self, user_id, group_id, user_msg, bot_reply):
        """缓存一轮对话（用户说了什么 + 她怎么回的），供之后回看学习。

        Args:
            user_id: 用户 ID。
            group_id: 群 ID，私聊传空串。
            user_msg: 用户这轮说的话。
            bot_reply: 她这轮的回复。

        Returns:
            新记录 ID；内容为空时返回 None。
        """
        user_msg = str(user_msg or '').strip()[:1000]
        bot_reply = str(bot_reply or '').strip()[:1500]
        if not user_msg or not bot_reply:
            return None
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO learn_turns (user_id, group_id, user_msg, bot_reply) VALUES (?, ?, ?, ?)',
                (str(user_id or ''), str(group_id or ''), user_msg, bot_reply))
            return cursor.lastrowid
        return self._execute_write(_do_op)

    def mark_last_turn_corrected(self, user_id, correction):
        """把该用户最近一轮标记为"被纠正"，并记下纠正原话。

        Args:
            user_id: 用户 ID。
            correction: 用户随后说的纠正内容。

        Returns:
            被标记的记录 ID；没有可标记的轮次时返回 None。
        """
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id FROM learn_turns WHERE user_id = ? ORDER BY id DESC LIMIT 1',
                (str(user_id or ''),))
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                'UPDATE learn_turns SET corrected = 1, correction = ? WHERE id = ?',
                (str(correction or '')[:500], row[0]))
            return row[0]
        return self._execute_write(_do_op)

    def get_unprocessed_turns(self, limit=40, max_age_hours=48):
        """取最近尚未学习过的对话轮次（含被纠正标记）。"""
        since = (datetime.now() - timedelta(hours=max_age_hours)).strftime('%Y-%m-%d %H:%M:%S')
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, user_id, group_id, user_msg, bot_reply, corrected, correction '
                'FROM learn_turns WHERE processed = 0 AND created_at >= ? ORDER BY id ASC LIMIT ?',
                (since, limit))
            return [dict(r) for r in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result or []

    def mark_turns_processed(self, ids):
        """标记这些轮次已学习过，避免重复喂给模型。"""
        clean = []
        for i in ids or []:
            try:
                clean.append(int(i))
            except (TypeError, ValueError):
                continue
        if not clean:
            return 0
        def _do_op(conn):
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(clean))
            cursor.execute(f'UPDATE learn_turns SET processed = 1 WHERE id IN ({placeholders})', clean)
            return cursor.rowcount
        return self._execute_write(_do_op) or 0

    def prune_learn_turns(self, days=7):
        """清理过期的对话缓存（默认保留 7 天）。"""
        cutoff = (datetime.now() - timedelta(days=max(1, int(days)))).strftime('%Y-%m-%d %H:%M:%S')
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('DELETE FROM learn_turns WHERE created_at < ?', (cutoff,))
            return cursor.rowcount
        return self._execute_write(_do_op) or 0

    @staticmethod
    def _style_text(row):
        """把一条 style_rules 记录拼成用于判重的文本。"""
        if (row.get('kind') or 'rule') == 'example':
            return f"{row.get('scene') or ''}{row.get('bad') or ''}{row.get('good') or ''}"
        return row.get('rule') or ''

    def add_style_rule(self, kind='rule', rule='', category='表达方式',
                       scene='', bad='', good='', source='auto'):
        """写入一条学到的表达要求或场景示范，自动处理重复与冲突。

        重复（相似度 >= 0.75）：只把 evidence +1，不重复存。
        冲突（新旧一条含否定、另一条不含，且文字高度重合）：下线旧的那条，
        避免"说话短一点"和"多解释些"两条并存把人搞精分。

        Args:
            kind: rule=硬性要求；example=场景示范对。
            rule: kind=rule 时的规则正文。
            category: 表达方式 / 长度 / 语气 / 称呼 / 禁忌。
            scene / bad / good: kind=example 时的场景、错误回法、更好回法。
            source: auto=自学；manual=手动。

        Returns:
            "added:<id>" 或 "updated:<id>"，失败返回 None。
        """
        kind = 'example' if str(kind).lower() == 'example' else 'rule'
        payload = {
            'kind': kind, 'category': str(category or '表达方式')[:20],
            'rule': str(rule or '').strip()[:300],
            'scene': str(scene or '').strip()[:200],
            'bad': str(bad or '').strip()[:300],
            'good': str(good or '').strip()[:300],
            'source': str(source or 'auto')[:20],
        }
        if kind == 'rule' and len(payload['rule']) < 4:
            return None
        if kind == 'example' and len(payload['good']) < 4:
            return None
        new_text = self._style_text(payload)
        new_canon = canonical_term(new_text) or normalize_term(new_text)
        negations = ('别', '不要', '禁止', '不能', '不许', '少用', '避免')

        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT id, kind, rule, scene, bad, good, evidence FROM style_rules '
                           'WHERE active = 1')
            for row in cursor.fetchall():
                old = dict(row)
                if (old.get('kind') or 'rule') != kind:
                    continue
                old_text = self._style_text(old)
                old_canon = canonical_term(old_text) or normalize_term(old_text)
                if not old_canon or not new_canon:
                    continue
                ratio = difflib.SequenceMatcher(None, new_canon, old_canon).ratio()
                # 先判冲突：讲同一件事但一条禁止、一条鼓励——新的覆盖旧的，
                # 否则两条并存在提示词里互相打架（"说话短一点" vs "多解释些"）
                polarity_differs = (any(n in new_text for n in negations)
                                    != any(n in old_text for n in negations))
                if ratio >= 0.5 and polarity_differs:
                    cursor.execute('UPDATE style_rules SET active = 0, updated_at = ? WHERE id = ?',
                                   (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), old['id']))
                    logger.info(f"风格规则冲突，旧规则已下线: {old_text[:30]}")
                    continue
                if ratio >= 0.75:
                    cursor.execute(
                        'UPDATE style_rules SET evidence = evidence + 1, updated_at = ? WHERE id = ?',
                        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), old['id']))
                    return f"updated:{old['id']}"
            cursor.execute(
                'INSERT INTO style_rules (kind, category, rule, scene, bad, good, source) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (payload['kind'], payload['category'], payload['rule'],
                 payload['scene'], payload['bad'], payload['good'], payload['source']))
            return f"added:{cursor.lastrowid}"
        result = self._execute_write(_do_op)
        return result if isinstance(result, str) else None

    def list_style_rules(self, limit=200, active_only=True, kind=None):
        """列出学到的规则/示范，用于面板查看与管理。"""
        def _do_op(conn):
            cursor = conn.cursor()
            conditions = []
            params = []
            if active_only:
                conditions.append('active = 1')
            if kind:
                conditions.append('kind = ?')
                params.append(kind)
            where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
            cursor.execute(
                f'SELECT * FROM style_rules{where} ORDER BY evidence DESC, id DESC LIMIT ?',
                params + [limit])
            return [dict(r) for r in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result or []

    def delete_style_rule(self, rule_id):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('DELETE FROM style_rules WHERE id = ?', (int(rule_id),))
            return {"deleted": cursor.rowcount}
        result = self._execute_write(_do_op)
        return result if isinstance(result, dict) else {"deleted": 0}

    def deactivate_style_rule(self, rule_id):
        """下线一条规则（不是删除，保留痕迹便于排查）。"""
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('UPDATE style_rules SET active = 0, updated_at = ? WHERE id = ?',
                           (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), int(rule_id)))
            return {"deactivated": cursor.rowcount}
        result = self._execute_write(_do_op)
        return result if isinstance(result, dict) else {"deactivated": 0}

    def find_style_rule(self, text, threshold=0.4):
        """按文字相似度为一句"要替换的旧要求"找到对应的已有规则。"""
        target = canonical_term(str(text or '')) or normalize_term(str(text or ''))
        if not target:
            return None
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT id, rule FROM style_rules WHERE active = 1 AND kind = ?', ('rule',))
            best = None
            best_ratio = 0.0
            for row in cursor.fetchall():
                cand = canonical_term(row['rule']) or normalize_term(row['rule'])
                if not cand:
                    continue
                ratio = difflib.SequenceMatcher(None, target, cand).ratio()
                if ratio > best_ratio:
                    best, best_ratio = row['id'], ratio
            return {"id": best, "ratio": round(best_ratio, 3)} if best and best_ratio >= threshold else None
        result = self._execute_read(_do_op)
        return result

    def get_rules_for_injection(self, limit=6):
        """取要常驻注入的硬性要求（证据多的优先）。"""
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, rule, category FROM style_rules WHERE active = 1 AND kind = ? '
                'ORDER BY evidence DESC, id DESC LIMIT ?', ('rule', max(1, int(limit))))
            return [dict(r) for r in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result or []

    def match_style_examples(self, query, limit=2):
        """按当前话题挑出最相似的场景示范对（词面重合，命中不足就不给）。"""
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, scene, bad, good FROM style_rules WHERE active = 1 AND kind = ? '
                'ORDER BY evidence DESC, id DESC LIMIT 200', ('example',))
            rows = [dict(r) for r in cursor.fetchall()]
            if not rows:
                return []
            words = [w for w in self._tokenize(str(query))
                     if len(w) >= 2 and w not in self._MATCH_STOPWORDS]
            words = list(dict.fromkeys(words))[:8]
            if not words:
                return []
            scored = []
            for row in rows:
                blob = f"{row.get('scene') or ''}{row.get('good') or ''}"
                score = sum(1 for w in words if w in blob)
                if score >= 1:
                    scored.append((score, row))
            scored.sort(key=lambda x: -x[0])
            return [row for _s, row in scored[:max(1, int(limit))]]
        result = self._execute_read(_do_op)
        return result or []

    def bump_style_hits(self, ids):
        """记录这些规则/示范被注入过，用于观察效果。"""
        clean = []
        for i in ids or []:
            try:
                clean.append(int(i))
            except (TypeError, ValueError):
                continue
        if not clean:
            return 0
        def _do_op(conn):
            cursor = conn.cursor()
            placeholders = ','.join('?' * len(clean))
            cursor.execute(f'UPDATE style_rules SET hits = hits + 1 WHERE id IN ({placeholders})', clean)
            return cursor.rowcount
        return self._execute_write(_do_op) or 0

    def get_learning_stats(self):
        """学习板块用的汇总：学到的规则/示范、梗总数、待学习轮次、最近一条对话时间。

        Returns:
            dict: rules / examples / glossary / pending_turns / last_turn。
        """
        def _do_op(conn):
            cursor = conn.cursor()
            stats = {}
            cursor.execute("SELECT COUNT(*) FROM style_rules WHERE active = 1 AND kind = 'rule'")
            stats['rules'] = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM style_rules WHERE active = 1 AND kind = 'example'")
            stats['examples'] = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM glossary")
            stats['glossary'] = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM learn_turns WHERE processed = 0")
            stats['pending_turns'] = cursor.fetchone()[0]
            cursor.execute("SELECT MAX(created_at) FROM learn_turns")
            stats['last_turn'] = cursor.fetchone()[0] or ''
            return stats
        result = self._execute_read(_do_op)
        return result or {"rules": 0, "examples": 0, "glossary": 0,
                          "pending_turns": 0, "last_turn": ""}

