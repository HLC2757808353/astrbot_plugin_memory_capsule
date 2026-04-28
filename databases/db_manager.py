import sqlite3
import os
import re
import math
import hashlib
import threading
from datetime import datetime, timedelta
from cachetools import TTLCache
from .vector_search import VectorSearch

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

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
        self.cache = TTLCache(
            maxsize=self.config.get('max_cache_size', 50),
            ttl=self.config.get('cache_ttl', 120)
        )
        self.backup_manager = None
        self.vector_search = VectorSearch(self, config)

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
        conn.execute('PRAGMA busy_timeout = 30000')
        conn.execute('PRAGMA cache_size = -2000')
        return conn

    def _execute_write(self, func, max_retries=3):
        import time
        for attempt in range(max_retries):
            conn = None
            try:
                conn = self._get_connection()
                result = func(conn)
                conn.commit()
                return result
            except sqlite3.IntegrityError:
                return "already_exists"
            except Exception as e:
                err_msg = str(e).lower()
                if 'malformed' in err_msg:
                    logger.warning("Database malformed, attempting repair...")
                    if conn:
                        try: conn.close()
                        except Exception: pass
                    conn = None
                    self._repair_database()
                    try:
                        conn = self._get_connection()
                        result = func(conn)
                        conn.commit()
                        return result
                    except Exception as e2:
                        logger.error(f"Retry after repair failed: {e2}")
                        return None
                if 'locked' in err_msg and attempt < max_retries - 1:
                    logger.warning(f"Database locked attempt {attempt+1}/{max_retries}, retrying...")
                    time.sleep(0.5 * (attempt + 1))
                    continue
                logger.error(f"Write error: {e}")
                return None
            finally:
                if conn:
                    try: conn.close()
                    except Exception: pass
        logger.error(f"Write failed after {max_retries} attempts")
        return None

    def _execute_read(self, func, max_retries=2):
        import time
        for attempt in range(max_retries):
            conn = None
            try:
                conn = self._get_connection()
                return func(conn)
            except Exception as e:
                err_msg = str(e).lower()
                if ('locked' in err_msg or 'malformed' in err_msg) and attempt < max_retries - 1:
                    logger.debug(f"Read error attempt {attempt+1}: {e}")
                    time.sleep(0.2 * (attempt + 1))
                    continue
                logger.debug(f"Read error: {e}")
                return None
            finally:
                if conn:
                    try: conn.close()
                    except Exception: pass
        return None

    def _repair_database(self):
        if not self.db_path or not os.path.exists(self.db_path):
            return
        logger.warning(f"Attempting database repair: {self.db_path}")
        for ext in ['-wal', '-shm']:
            p = self.db_path + ext
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass
        try:
            test_conn = sqlite3.connect(self.db_path, timeout=10)
            test_cur = test_conn.cursor()
            test_cur.execute('PRAGMA integrity_check')
            result = test_cur.fetchone()
            test_conn.close()
            if result and result[0] == 'ok':
                logger.info("Database OK after WAL cleanup")
                return
        except Exception:
            pass
        logger.warning("WAL cleanup not enough, attempting dump recovery...")
        try:
            import shutil
            bak_path = self.db_path + '.bak'
            if os.path.exists(bak_path): os.remove(bak_path)
            shutil.copy2(self.db_path, bak_path)
            for ext in ['-wal', '-shm']:
                p = self.db_path + ext
                if os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            dump_lines = []
            try:
                old_conn = sqlite3.connect(bak_path)
                old_conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
                for line in old_conn.iterdump():
                    dump_lines.append(line)
                old_conn.close()
            except Exception as e:
                logger.warning(f"Dump from backup failed: {e}")
            if os.path.exists(self.db_path): os.remove(self.db_path)
            new_conn = sqlite3.connect(self.db_path)
            if dump_lines:
                new_conn.executescript('\n'.join(dump_lines))
            new_conn.close()
            logger.info("Database dump recovery completed")
        except Exception as e:
            logger.error(f"Database dump recovery failed: {e}")
            if os.path.exists(self.db_path):
                try: os.remove(self.db_path)
                except Exception: pass
            if os.path.exists(bak_path):
                shutil.copy2(bak_path, self.db_path)

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
        if not self.config.get('lightweight_mode', False):
            _get_jieba()
        from .backup import BackupManager
        self.backup_manager = BackupManager(self.db_path, self.config)
        self.backup_manager.start_auto_backup()
        vec_dir = os.path.dirname(self.db_path)
        self.vector_search.load_index(vec_dir)
        if self.vector_search.available:
            logger.info(f"RAG vector search enabled (faiss+embedding)")
        else:
            logger.info(f"RAG vector search disabled (no embedding provider), using BM25/TF-IDF")
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
                self._repair_database()
                self._initialize_database_structure()
            else:
                logger.info("Database integrity check passed")
        except Exception as e:
            logger.warning(f"Database integrity check error: {e}")
            self._repair_database()
            self._initialize_database_structure()

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

    def _initialize_database_structure(self):
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute('PRAGMA journal_mode = WAL')
            conn.execute('PRAGMA foreign_keys = ON')
            conn.execute('PRAGMA busy_timeout = 30000')
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
                category TEXT DEFAULT 'general', importance INTEGER DEFAULT 5,
                tags TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, access_count INTEGER DEFAULT 0,
                last_accessed TIMESTAMP, source TEXT DEFAULT 'user', hash TEXT UNIQUE)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS relationships (
                user_id TEXT PRIMARY KEY, nickname TEXT, relation_type TEXT DEFAULT 'friend',
                summary TEXT DEFAULT '', first_met_location TEXT, known_contexts TEXT DEFAULT '',
                identity_aliases TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, interaction_count INTEGER DEFAULT 0)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER,
                activity_type TEXT NOT NULL, description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS synonyms (
                id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT NOT NULL,
                synonym TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(word, synonym))''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS triples (
                id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
                predicate TEXT NOT NULL, object TEXT NOT NULL,
                source_memory_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE CASCADE)''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object)')
            cursor.execute('''CREATE TABLE IF NOT EXISTS dream_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, dream_date TEXT NOT NULL,
                summary TEXT DEFAULT '', memories_reviewed INTEGER DEFAULT 0,
                conversations_reviewed INTEGER DEFAULT 0, insights TEXT DEFAULT '',
                new_memories_created INTEGER DEFAULT 0, consolidation_done INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(dream_date))''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS conversation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL,
                content TEXT NOT NULL, user_id TEXT DEFAULT '', group_id TEXT DEFAULT '',
                topics TEXT DEFAULT '', compressed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_convlogs_time ON conversation_logs(created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_convlogs_user ON conversation_logs(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_convlogs_group ON conversation_logs(group_id)')
            cursor.execute('''CREATE TABLE IF NOT EXISTS daily_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, summary_date TEXT NOT NULL,
                summary TEXT NOT NULL, key_topics TEXT DEFAULT '', group_id TEXT DEFAULT '',
                active_users TEXT DEFAULT '', importance INTEGER DEFAULT 5,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(summary_date, group_id))''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_dailysumm_date ON daily_summaries(summary_date)')
            cursor.execute('''CREATE TABLE IF NOT EXISTS daily_global_digest (
                id INTEGER PRIMARY KEY AUTOINCREMENT, digest_date TEXT NOT NULL UNIQUE,
                total_groups INTEGER DEFAULT 0, total_messages INTEGER DEFAULT 0,
                merged_topics TEXT DEFAULT '', global_summary TEXT DEFAULT '',
                active_users TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS auto_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
                predicate TEXT NOT NULL, object TEXT NOT NULL, fact_type TEXT DEFAULT 'extracted',
                user_id TEXT DEFAULT '', confidence REAL DEFAULT 1.0, source TEXT DEFAULT 'auto',
                hash TEXT UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_accessed TIMESTAMP, access_count INTEGER DEFAULT 0)''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_facts_subject ON auto_facts(subject)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_facts_user ON auto_facts(user_id)')
            cursor.execute('''CREATE TABLE IF NOT EXISTS recent_utterances (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                content TEXT NOT NULL, group_id TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_utterances_user ON recent_utterances(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_utterances_time ON recent_utterances(created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_relationships_nickname ON relationships(nickname)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_activities_memory ON activities(memory_id)')
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
        if self.vector_search and self.db_path:
            self.vector_search.save_index(os.path.dirname(self.db_path))
        self.cache.clear()
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
        '待办事项': ['记得','记住','提醒','不要','必须','需要','别忘了','记得做','时间','日期','号','周','月','年','schedule','deadline'],
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

    def _table_exists(self, conn, table_name):
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        return cursor.fetchone() is not None

    # ==================== Memory CRUD ====================

    def write_memory(self, content, category=None, importance=5, tags=None, source='user'):
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
                'INSERT INTO memories (content, category, importance, tags, source, hash) VALUES (?, ?, ?, ?, ?, ?)',
                (content, _category, importance, ','.join(_tags) if isinstance(_tags, list) else _tags, source, content_hash))
            memory_id = cursor.lastrowid
            _memory_id[0] = memory_id
            cursor.execute('INSERT INTO activities (memory_id, activity_type, description) VALUES (?, ?, ?)',
                         (memory_id, 'create', content[:50]))
            cache_key = f"search_{content[:20]}"
            if cache_key in self.cache: del self.cache[cache_key]
            return f"Memory saved (ID:{memory_id})"
        result = self._execute_write(_do_op)
        if result == "already_exists": return "Memory already exists"
        if result is not None and self.vector_search.available and _memory_id[0]:
            try:
                import asyncio
                asyncio.ensure_future(self.vector_search.add_embedding(_memory_id[0], content))
            except Exception:
                pass
        return result if result is not None else "Error: database write failed"

    def search_memory(self, query, category_filter=None, limit=None):
        def _do_op(conn):
            _limit = limit if limit is not None else self.config.get('search_max_results', 5)
            result_lists = []
            vec_results = self._vector_search_sync(conn, query, _limit * 2)
            if vec_results:
                result_lists.append(vec_results)
            if not vec_results or len(vec_results) < _limit:
                fts_results = self._fts_search(conn, query, _limit * 3)
                if fts_results: result_lists.append(fts_results)
            if not result_lists:
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
            cursor.execute('DELETE FROM triples WHERE source_memory_id = ?', (memory_id,))
            self.cache.clear()
            return f"Memory deleted (ID:{memory_id})"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: operation failed"

    def update_memory(self, memory_id, content=None, category=None, importance=None, tags=None):
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
            if _tags is not None and _tags != '':
                updates.append("tags = ?"); params.append(','.join(_tags) if isinstance(_tags, list) else _tags)
            updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
            params.append(memory_id)
            cursor.execute(f'UPDATE memories SET {", ".join(updates)} WHERE id = ?', params)
            cursor.execute('INSERT INTO activities (memory_id, activity_type, description) VALUES (?, ?, ?)',
                         (memory_id, 'update', f'importance={importance}' if importance else 'content updated'))
            self.cache.clear()
            return f"Memory updated (ID:{memory_id})"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: operation failed"

    def get_all_memories(self, limit=100, offset=0, category=None):
        def _do_op(conn):
            cursor = conn.cursor()
            if category:
                cursor.execute('SELECT id, content, category, importance, tags, access_count, created_at FROM memories WHERE category = ? ORDER BY created_at DESC LIMIT ? OFFSET ?', (category, limit, offset))
            else:
                cursor.execute('SELECT id, content, category, importance, tags, access_count, created_at FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?', (limit, offset))
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

    def _vector_search_sync(self, conn, query, limit):
        if not self.vector_search.available:
            return []
        try:
            import asyncio
            loop = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                return []
            results = asyncio.get_event_loop().run_until_complete(
                self.vector_search.search(query, limit)
            )
            if not results:
                return []
            cursor = conn.cursor()
            memory_results = []
            for r in results:
                mid = r['id']
                cursor.execute(
                    'SELECT id, content, category, importance, tags, created_at, access_count FROM memories WHERE id = ?',
                    (mid,))
                row = cursor.fetchone()
                if row:
                    m = dict(row)
                    m['vector_score'] = r['score']
                    memory_results.append(m)
            return memory_results
        except Exception as e:
            logger.debug(f"Vector search sync error: {e}")
            return []

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
                'SELECT m.id, m.content, m.category, m.importance, m.tags, m.created_at, m.access_count, '
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
                f'SELECT id, content, category, importance, tags, created_at, access_count FROM memories WHERE {where} ORDER BY importance DESC, created_at DESC LIMIT ?',
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
                f'SELECT id, content, category, importance, tags, created_at, access_count FROM memories WHERE {where} ORDER BY importance DESC LIMIT ?',
                params + [limit])
            return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []

    def _tfidf_search(self, conn, query, limit):
        if not self.config.get('tfidf_search_enabled', True): return []
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT id, content, category, importance, tags, created_at, access_count FROM memories LIMIT ?',
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

    # ==================== Working Memory ====================

    def get_working_memories(self, context_query="", limit=6, max_chars=800):
        def _do_op(conn):
            result_lists = []
            cursor = conn.cursor()
            cursor.execute('SELECT id, content, category, importance, tags, created_at, access_count FROM memories WHERE importance >= 8 ORDER BY importance DESC LIMIT 3')
            core = [dict(row) for row in cursor.fetchall()]
            if core: result_lists.append(core)
            cursor.execute('SELECT id, content, category, importance, tags, created_at, access_count FROM memories ORDER BY created_at DESC LIMIT 5')
            recent = [dict(row) for row in cursor.fetchall()]
            if recent: result_lists.append(recent)
            if context_query:
                vec = self._vector_search_sync(conn, context_query, limit * 2)
                if vec: result_lists.append(vec)
                if not vec or len(vec) < limit:
                    fts = self._fts_search(conn, context_query, limit * 2)
                    if fts: result_lists.append(fts)
                if not result_lists:
                    tag = self._tag_retrieve(conn, context_query, limit)
                    if tag: result_lists.append(tag)
                    tfidf = self._tfidf_search(conn, context_query, limit)
                    if tfidf: result_lists.append(tfidf)
            fused = self._rrf_fuse(result_lists, k=self.config.get('rrf_k', 60)) if result_lists else []
            scored = []
            for m in fused:
                score = self._score_memory(m, context_query)
                scored.append((score, m))
            scored.sort(key=lambda x: x[0], reverse=True)
            top = [m for _, m in scored[:limit * 2]]
            if context_query and top:
                activated_tags = set()
                for m in top[:limit]:
                    for tag in m.get('tags', '').split(','):
                        tag = tag.strip()
                        if tag: activated_tags.add(tag)
                if activated_tags:
                    seen_ids = {m['id'] for m in top}
                    for atag in list(activated_tags)[:3]:
                        ph = ','.join('?' * len(seen_ids)) if seen_ids else ''
                        if ph:
                            cursor.execute(
                                f'SELECT id, content, category, importance, tags, created_at, access_count FROM memories WHERE id NOT IN ({ph}) AND tags LIKE ? LIMIT 2',
                                list(seen_ids) + [f'%{atag}%'])
                        else:
                            cursor.execute(
                                'SELECT id, content, category, importance, tags, created_at, access_count FROM memories WHERE tags LIKE ? LIMIT 2',
                                [f'%{atag}%'])
                        for row in cursor.fetchall():
                            m = dict(row)
                            if m['id'] not in seen_ids:
                                top.append(m)
                                seen_ids.add(m['id'])
            total_chars = 0
            result = []
            for m in top[:limit]:
                content = m.get('content', '')
                if total_chars + len(content) > max_chars:
                    content = content[:max_chars - total_chars]
                    if content: result.append({'id': m['id'], 'content': content, 'category': m.get('category', '')})
                    break
                result.append({'id': m['id'], 'content': content, 'category': m.get('category', '')})
                total_chars += len(content)
            return result
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def _score_memory(self, memory, context_query=""):
        score = 0.0
        importance = memory.get('importance', 5)
        score += importance * 2.0
        recency_hours = 999
        created = memory.get('created_at', '')
        if created:
            try:
                dt = datetime.fromisoformat(created)
                recency_hours = (datetime.now() - dt).total_seconds() / 3600
            except Exception: pass
        if recency_hours < 1: score += 5.0
        elif recency_hours < 24: score += 3.0
        elif recency_hours < 168: score += 1.5
        score += min(memory.get('access_count', 0) * 0.5, 5.0)
        if context_query:
            content = memory.get('content', '').lower()
            tags = memory.get('tags', '').lower()
            query_words = set(re.findall(r'\w+', context_query.lower()))
            overlap = query_words & set(re.findall(r'\w+', content))
            if overlap: score += len(overlap) * 2.0
            for tag in tags.split(','):
                tag = tag.strip()
                if tag and tag in context_query.lower(): score += 3.0
        return score

    # ==================== Knowledge Graph ====================

    def add_triple(self, subject, predicate, obj, source_memory_id=None):
        def _do_op(conn):
            conn.execute('INSERT INTO triples (subject, predicate, object, source_memory_id) VALUES (?, ?, ?, ?)',
                         (subject, predicate, obj, source_memory_id))
            return f"Triple added: {subject} -> {predicate} -> {obj}"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: add triple failed"

    def query_triples(self, subject=None, predicate=None, obj=None, limit=10):
        def _do_op(conn):
            cursor = conn.cursor()
            conditions = []
            params = []
            if subject: conditions.append("subject LIKE ?"); params.append(f'%{subject}%')
            if predicate: conditions.append("predicate LIKE ?"); params.append(f'%{predicate}%')
            if obj: conditions.append("object LIKE ?"); params.append(f'%{obj}%')
            if not conditions:
                cursor.execute('SELECT * FROM triples ORDER BY created_at DESC LIMIT ?', (limit,))
            else:
                where = ' AND '.join(conditions)
                cursor.execute(f'SELECT * FROM triples WHERE {where} ORDER BY created_at DESC LIMIT ?', params + [limit])
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_related_triples(self, entity, depth=1, limit=15):
        def _do_op(conn):
            cursor = conn.cursor()
            visited = set()
            result = []
            frontier = [entity]
            for _ in range(depth):
                next_frontier = []
                for node in frontier:
                    if node in visited: continue
                    visited.add(node)
                    cursor.execute('SELECT * FROM triples WHERE subject LIKE ? OR object LIKE ? LIMIT ?',
                                 (f'%{node}%', f'%{node}%', limit))
                    for row in cursor.fetchall():
                        t = dict(row)
                        if t['id'] not in {r['id'] for r in result}:
                            result.append(t)
                            next_frontier.extend([t['subject'], t['object']])
                frontier = next_frontier
            return result[:limit]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def delete_triple(self, triple_id):
        def _do_op(conn):
            conn.execute('DELETE FROM triples WHERE id = ?', (triple_id,))
            return f"Triple deleted (ID:{triple_id})"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: delete triple failed"

    # ==================== Conversation Logs ====================

    _IMPORTANT_PATTERNS = [
        (r'(?:记住|记得|别忘了|提醒我|必须|一定要)(.{2,50})', 'reminder', 8),
        (r'(?:我的名字(?:叫|是)|我叫|我是)(.{1,20})', 'identity', 9),
        (r'(?:我的生日(?:是|在))(.{1,30})', 'personal', 9),
        (r'(?:我(?:喜欢|爱|偏好|讨厌|恨|最(?:喜欢|讨厌)))(.{1,30})', 'preference', 7),
        (r'(?:我(?:住在|来自|在|搬到了))(.{1,30})', 'location', 6),
        (r'(?:我(?:会|能|擅长|精通))(.{1,30})', 'ability', 6),
        (r'(?:约定|承诺|答应|说好了)(.{2,50})', 'promise', 9),
    ]

    _GARBAGE_FILTERS = [
        r'^(嗯|啊|哦|好|行|是|对|嗯嗯|哈哈|呵呵|嘿嘿|ok|OK|yes|no|不是|没有|什么|怎么|为什么|哪|谁|几|多少|吗|呢|吧|啊|哦|额|em|emmm)',
        r'^.{1,3}$',
        r'^(谢谢|感谢|不好意思|抱歉|打扰了|没关系|不客气|好的|收到|明白|了解|知道了)',
    ]

    def _is_garbage(self, content):
        content = content.strip()
        if len(content) < 4: return True
        for pattern in self._GARBAGE_FILTERS:
            if re.match(pattern, content): return True
        return False

    def _get_nickname(self, conn, user_id):
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT nickname FROM relationships WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            return row[0] if row and row[0] else None
        except Exception:
            return None

    _TIME_WORDS = {
        '明天': None, '后天': None, '大后天': None,
        '下周': None, '下个月': None, '今晚': None, '今明': None,
    }

    def _resolve_time_words(self, text):
        from datetime import date, timedelta
        today = date.today()
        replacements = {
            '今天': today.isoformat(),
            '今晚': f"{today.isoformat()}晚",
            '明天': (today + timedelta(days=1)).isoformat(),
            '后天': (today + timedelta(days=2)).isoformat(),
            '大后天': (today + timedelta(days=3)).isoformat(),
            '下周': f"周{(today + timedelta(days=7)).strftime('%m-%d')}",
            '下个月': f"{(today.replace(day=1) + timedelta(days=32)).strftime('%Y-%m')}",
        }
        for word, resolved in replacements.items():
            if word in text:
                text = text.replace(word, f"({resolved})")
        return text

    def _auto_save_important_memory(self, conn, content, user_id=''):
        if self._is_garbage(content): return
        if not self.config.get('passive_memory_enabled', True): return
        try:
            from ..security import is_passive_memory_safe
            if not is_passive_memory_safe(content): return
        except Exception: pass
        nickname = self._get_nickname(conn, user_id)
        cursor = conn.cursor()
        for pattern, mem_type, importance in self._IMPORTANT_PATTERNS:
            try:
                matches = re.findall(pattern, content)
                for match in matches:
                    match = match.strip()
                    if len(match) < 2: continue
                    match = self._resolve_time_words(match)
                    display_name = nickname or user_id or '用户'
                    if mem_type == 'reminder':
                        memory_text = f"[待办] {display_name}: {match}"
                    elif mem_type == 'identity':
                        memory_text = f"[身份] {display_name}的名字是{match}"
                    elif mem_type == 'personal':
                        memory_text = f"[个人信息] {display_name}的生日{match}"
                    elif mem_type == 'preference':
                        memory_text = f"[偏好] {display_name}{match}"
                    elif mem_type == 'location':
                        memory_text = f"[位置] {display_name}在{match}"
                    elif mem_type == 'ability':
                        memory_text = f"[能力] {display_name}会{match}"
                    elif mem_type == 'promise':
                        memory_text = f"[约定] {display_name}: {match}"
                    else: continue
                    content_hash = hashlib.md5(memory_text.encode('utf-8')).hexdigest()
                    cursor.execute('SELECT id FROM memories WHERE hash = ?', (content_hash,))
                    if cursor.fetchone(): continue
                    tags = self._extract_tags(memory_text)
                    category = self._guess_category(memory_text)
                    cursor.execute(
                        'INSERT INTO memories (content, category, importance, tags, source, hash) VALUES (?, ?, ?, ?, ?, ?)',
                        (memory_text, category, importance, ','.join(tags), 'passive', content_hash))
                    logger.debug(f"Passive memory saved: {memory_text[:30]}")
            except Exception: pass

    _FACT_PATTERNS = [
        (r'(.{1,10}?)(喜欢|爱|偏好|讨厌|恨|最爱|最讨厌)(.{1,20})', 'preference'),
        (r'(.{1,10}?)(是|叫|名为|叫做|就是)(.{1,20})', 'identity'),
        (r'(.{1,10}?)的(.{1,10}?)(是|为|有)(.{1,20})', 'attribute'),
        (r'(记住|记得|别忘了|提醒我|待办|必须)(.{1,30})', 'reminder'),
        (r'(.{1,10}?)(在|来自|住在|去了|搬到)(.{1,20})', 'location'),
        (r'(.{1,10}?)(会|能|可以|擅长|精通)(.{1,20})', 'ability'),
        (r'(我的|我的名字|我叫|我是)(.{1,20})', 'self_identity'),
    ]

    def _extract_facts(self, content, user_id=''):
        nickname = None
        try:
            def _get_nick(conn):
                cursor = conn.cursor()
                cursor.execute('SELECT nickname FROM relationships WHERE user_id = ?', (user_id,))
                row = cursor.fetchone()
                return row[0] if row else None
            nickname = self._execute_read(_get_nick)
        except Exception:
            pass
        display_name = nickname or user_id or '用户'
        facts = []
        for pattern, fact_type in self._FACT_PATTERNS:
            try:
                matches = re.findall(pattern, content)
                for match in matches:
                    if isinstance(match, tuple):
                        parts = [p.strip() for p in match if p.strip()]
                        if len(parts) >= 2:
                            if fact_type == 'reminder':
                                resolved = self._resolve_time_words(parts[-1][:50])
                                subj = display_name; pred = '待办'; obj = resolved
                            elif fact_type == 'self_identity':
                                subj = display_name; pred = '身份'; obj = parts[-1][:50]
                            elif len(parts) >= 3:
                                raw_subj = parts[0]
                                if raw_subj in ('我', '我的'):
                                    subj = display_name
                                else:
                                    subj = raw_subj if raw_subj else display_name
                                pred = parts[1]
                                obj = self._resolve_time_words(parts[2])
                            else:
                                subj = display_name; pred = parts[0]; obj = parts[1] if len(parts) > 1 else ''
                            if subj and pred and obj and len(obj) > 0:
                                facts.append({'subject': subj[:30], 'predicate': pred[:15], 'object': obj[:50], 'fact_type': fact_type, 'user_id': str(user_id)})
            except Exception: pass
        return facts[:5]

    def _save_auto_facts_internal(self, conn, content, user_id=''):
        if not self.config.get('auto_fact_extraction_enabled', True): return 0
        if self._is_garbage(content): return 0
        try:
            from ..security import is_passive_memory_safe
            if not is_passive_memory_safe(content): return 0
        except Exception: pass
        facts = self._extract_facts(content, user_id)
        if not facts: return 0
        saved = 0
        cursor = conn.cursor()
        for fact in facts:
            fact_hash = hashlib.md5(f"{fact['subject']}|{fact['predicate']}|{fact['object']}".encode()).hexdigest()
            cursor.execute('SELECT id FROM auto_facts WHERE hash = ?', (fact_hash,))
            if cursor.fetchone(): continue
            cursor.execute(
                'INSERT OR IGNORE INTO auto_facts (subject, predicate, object, fact_type, user_id, confidence, hash) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (fact['subject'], fact['predicate'], fact['object'], fact['fact_type'], fact['user_id'], 1.0, fact_hash))
            saved += 1
        return saved

    def log_conversation(self, role, content, user_id='', group_id=''):
        def _do_op(conn):
            topics = ''
            if not self.config.get('lightweight_mode', False):
                try:
                    jieba_mod, _ = _get_jieba()
                    if jieba_mod:
                        words = [w for w in jieba_mod.cut(content) if len(w) > 1][:5]
                        topics = ','.join(words)
                except Exception: pass
            if not topics:
                topics = ','.join(re.findall(r'\w{2,}', content)[:5])
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO conversation_logs (role, content, user_id, group_id, topics) VALUES (?, ?, ?, ?, ?)',
                (role, content[:500], str(user_id), str(group_id), topics))
            if role == 'user' and content.strip() and user_id:
                try:
                    cursor.execute(
                        'INSERT INTO recent_utterances (user_id, content, group_id) VALUES (?, ?, ?)',
                        (str(user_id), content[:300], str(group_id)))
                    max_pool = self.config.get('recent_utterances_pool_size', 100)
                    cursor.execute('SELECT COUNT(*) FROM recent_utterances')
                    count = cursor.fetchone()[0]
                    if count > max_pool * 1.5:
                        cursor.execute(
                            'DELETE FROM recent_utterances WHERE id NOT IN (SELECT id FROM recent_utterances ORDER BY created_at DESC LIMIT ?)',
                            (max_pool,))
                except Exception: pass
            if role == 'user' and content.strip():
                try: self._save_auto_facts_internal(conn, content, user_id)
                except Exception: pass
                try: self._auto_save_important_memory(conn, content, user_id)
                except Exception: pass
        result = self._execute_write(_do_op)
        if result is None: logger.debug("Log conversation write failed")

    def get_conversation_logs(self, hours=24, limit=200):
        def _do_op(conn):
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, role, content, user_id, group_id, topics, created_at FROM conversation_logs WHERE created_at > ? ORDER BY created_at ASC LIMIT ?',
                (cutoff, limit))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def compress_conversation_logs(self, hours=24):
        def _do_op(conn):
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
            cursor = conn.cursor()
            cursor.execute('SELECT topics FROM conversation_logs WHERE created_at > ? AND compressed = 0 AND topics != ""', (cutoff,))
            all_topics = []
            for row in cursor.fetchall():
                if row[0]: all_topics.extend(row[0].split(','))
            from collections import Counter
            topic_counts = Counter(t.strip() for t in all_topics if t.strip())
            top_topics = topic_counts.most_common(10)
            cursor.execute('UPDATE conversation_logs SET compressed = 1 WHERE created_at < ? AND compressed = 0',
                         ((datetime.now() - timedelta(hours=hours)).isoformat(),))
            return top_topics
        result = self._execute_write(_do_op)
        return result if result is not None else []

    def cleanup_old_conversation_logs(self, days=None):
        if days is None: days = self.config.get('conversation_log_retention_days', 7)
        if days <= 0: return
        def _do_op(conn):
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM conversation_logs WHERE created_at < ? AND compressed = 1', (cutoff,))
            deleted = cursor.rowcount
            if deleted > 0: logger.info(f"Cleaned up {deleted} old conversation logs")
        self._execute_write(_do_op)

    def cleanup_old_daily_summaries(self, days=None):
        if days is None: days = self.config.get('daily_summary_retention_days', 30)
        if days <= 0: return
        def _do_op(conn):
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM daily_summaries WHERE created_at < ?', (cutoff,))
            cursor.execute('DELETE FROM daily_global_digest WHERE created_at < ?', (cutoff,))
        self._execute_write(_do_op)

    # ==================== Dream Mode ====================

    def get_dream_materials(self, hours=24):
        def _do_op(conn):
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, content, category, importance, tags, access_count FROM memories '
                'WHERE created_at > ? OR importance >= 7 OR access_count > 3 ORDER BY importance DESC, created_at DESC LIMIT 30', (cutoff,))
            memories = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                'SELECT role, content, user_id, group_id, topics, created_at FROM conversation_logs WHERE created_at > ? ORDER BY created_at ASC LIMIT 200', (cutoff,))
            conversations = [dict(row) for row in cursor.fetchall()]
            user_messages = [c for c in conversations if c['role'] == 'user']
            topic_counter = {}
            for c in conversations:
                if c.get('topics'):
                    for t in c['topics'].split(','):
                        t = t.strip()
                        if t: topic_counter[t] = topic_counter.get(t, 0) + 1
            hot_topics = sorted(topic_counter.items(), key=lambda x: x[1], reverse=True)[:10]
            active_users = list(set(c['user_id'] for c in user_messages if c.get('user_id')))
            return {'memories': memories, 'conversations': conversations, 'conversation_count': len(conversations),
                    'user_message_count': len(user_messages), 'hot_topics': hot_topics, 'active_users': active_users}
        result = self._execute_read(_do_op)
        return result if result is not None else {'memories': [], 'conversations': [], 'conversation_count': 0,
                'user_message_count': 0, 'hot_topics': [], 'active_users': []}

    def consolidate_memories(self, hours=24):
        def _do_op(conn):
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
            cursor = conn.cursor()
            cursor.execute('SELECT id, tags, importance FROM memories WHERE last_accessed > ? AND access_count > 0', (cutoff,))
            accessed = cursor.fetchall()
            boosted = 0
            for row in accessed:
                mid, tags, importance = row[0], row[1], row[2]
                new_importance = min(importance + 1, 10)
                if new_importance > importance:
                    cursor.execute('UPDATE memories SET importance = ? WHERE id = ?', (new_importance, mid))
                    boosted += 1
            links_created = 0
            cursor.execute('SELECT m1.id, m1.tags, m2.id, m2.tags FROM memories m1, memories m2 WHERE m1.id < m2.id AND m1.created_at > ? AND m2.created_at > ?', (cutoff, cutoff))
            for pair in cursor.fetchall()[:50]:
                tags1 = set(t.strip() for t in (pair[1] or '').split(',') if t.strip())
                tags2 = set(t.strip() for t in (pair[3] or '').split(',') if t.strip())
                shared = tags1 & tags2
                if len(shared) >= 2:
                    for tag in shared:
                        try:
                            cursor.execute('INSERT OR IGNORE INTO triples (subject, predicate, object) VALUES (?, ?, ?)',
                                         (f'mem:{pair[0]}', f'shared:{tag}', f'mem:{pair[2]}'))
                            links_created += 1
                        except Exception: pass
            return {'boosted': boosted, 'links_created': links_created}
        result = self._execute_write(_do_op)
        return result if result is not None else {'boosted': 0, 'links_created': 0}

    def save_dream_log(self, dream_date, summary, memories_reviewed, insights, conversations_reviewed=0, new_memories_created=0, consolidation_done=0):
        def _do_op(conn):
            conn.execute(
                'INSERT OR REPLACE INTO dream_logs (dream_date, summary, memories_reviewed, conversations_reviewed, insights, new_memories_created, consolidation_done) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (dream_date, summary, memories_reviewed, conversations_reviewed, insights, new_memories_created, consolidation_done))
            return f"Dream log saved for {dream_date}"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: save dream log failed"

    def get_dream_logs(self, limit=7):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM dream_logs ORDER BY created_at DESC LIMIT ?', (limit,))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_dream_logs_for_web(self, limit=50):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT id, dream_date, summary, memories_reviewed, conversations_reviewed, insights, new_memories_created, consolidation_done, created_at FROM dream_logs ORDER BY created_at DESC LIMIT ?', (limit,))
            dreams = [dict(r) for r in cursor.fetchall()]
            cursor.execute('SELECT COUNT(*) FROM dream_logs')
            total = cursor.fetchone()[0]
            return {'dreams': dreams, 'total': total}
        result = self._execute_read(_do_op)
        return result if result is not None else {'dreams': [], 'total': 0}

    def get_dream_detail(self, dream_id):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM dream_logs WHERE id = ?', (dream_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        return self._execute_read(_do_op)

    # ==================== Daily Summaries ====================

    def _extractive_summarize(self, sentences, top_n=5):
        if not sentences or len(sentences) <= top_n: return sentences
        all_text = ' '.join(sentences)
        tokens = self._tokenize(all_text.lower())
        if not tokens: return sentences[:top_n]
        from collections import Counter
        tf = Counter(tokens)
        N = len(sentences)
        df = Counter()
        for s in sentences:
            st = set(self._tokenize(s.lower()))
            for t in st: df[t] += 1
        scored = []
        for s in sentences:
            st = self._tokenize(s.lower())
            if not st: scored.append((0, s)); continue
            score = sum(tf.get(t, 0) * math.log(N / (1 + df.get(t, 1))) for t in st) / len(st)
            scored.append((score, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[:top_n]]

    def _textrank_keywords(self, texts, top_k=10, window=4):
        words_list = []
        for text in texts:
            words = [w for w in self._tokenize(text.lower()) if len(w) >= 2]
            words_list.append(words)
        graph = {}
        for words in words_list:
            for i, w1 in enumerate(words):
                if w1 not in graph: graph[w1] = {}
                for j in range(i + 1, min(i + window, len(words))):
                    w2 = words[j]
                    if w2 not in graph: graph[w2] = {}
                    graph[w1][w2] = graph[w1].get(w2, 0) + 1
                    graph[w2][w1] = graph[w2].get(w1, 0) + 1
        if not graph: return []
        scores = {w: 1.0 for w in graph}
        for _ in range(20):
            for w in graph:
                s = sum(scores.get(n, 0) * graph[w].get(n, 0) for n in graph[w])
                d = sum(graph[w].get(n, 0) for n in graph[w]) or 1
                scores[w] = 0.85 * (s / d) + 0.15
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [w for w, _ in ranked[:top_k]]

    def _save_daily_summary_internal(self, conn, summary_date, summary, key_topics='', group_id='', active_users='', importance=5):
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO daily_summaries (summary_date, summary, key_topics, group_id, active_users, importance) VALUES (?, ?, ?, ?, ?, ?)',
            (summary_date, str(summary)[:2000], str(key_topics)[:200], str(group_id), str(active_users)[:300], int(importance or 5)))

    def _stratified_sample(self, rows, max_count):
        if len(rows) <= max_count:
            return rows
        n_bins = min(8, max_count // 10)
        if n_bins < 2:
            n_bins = 2
        bin_size = len(rows) // n_bins
        per_bin = max_count // n_bins
        sampled = []
        for i in range(n_bins):
            start = i * bin_size
            end = start + bin_size if i < n_bins - 1 else len(rows)
            bin_rows = rows[start:end]
            step = max(1, len(bin_rows) // per_bin)
            sampled.extend(bin_rows[::step][:per_bin])
        if len(sampled) > max_count:
            sampled = sampled[:max_count]
        return sampled

    def _generate_group_summary_internal(self, conn, group_id, hours):
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        cursor = conn.cursor()
        max_messages = self.config.get('summary_max_messages', 500)
        max_chars = self.config.get('summary_max_chars', 100000)
        if group_id:
            cursor.execute("SELECT content, user_id, created_at FROM conversation_logs WHERE created_at > ? AND group_id = ? AND role = 'user' ORDER BY created_at ASC", (cutoff, str(group_id)))
        else:
            cursor.execute("SELECT content, user_id, created_at FROM conversation_logs WHERE created_at > ? AND role = 'user' ORDER BY created_at ASC", (cutoff,))
        all_rows = cursor.fetchall()
        if not all_rows: return None
        rows = self._stratified_sample(all_rows, max_messages)
        contents = []
        total_chars = 0
        for r in rows:
            c = r[0]
            if total_chars + len(c) > max_chars:
                break
            contents.append(c)
            total_chars += len(c)
        all_users = list(set(r[1] for r in rows[:len(contents)] if r[1]))
        raw_sentences = []
        for c in contents:
            for p in re.split(r'[。！？\n.!?]', c):
                p = p.strip()
                if len(p) > 4: raw_sentences.append(p)
        top_sentences = self._extractive_summarize(raw_sentences, top_n=8)
        keywords = self._textrank_keywords(contents, top_k=10)
        summary = '。'.join(top_sentences[:6]) + '。'
        key_topics = ','.join(keywords[:8])
        from datetime import date
        today = date.today().isoformat()
        self._save_daily_summary_internal(conn, today, summary, key_topics, group_id, ','.join(all_users[:15]), 5)
        return {'summary': summary, 'key_topics': key_topics, 'message_count': len(contents), 'active_users': all_users[:10], 'group_id': group_id}

    def generate_group_daily_summary(self, group_id='', hours=24):
        def _do_op(conn):
            return self._generate_group_summary_internal(conn, group_id, hours)
        result = self._execute_write(_do_op)
        return result

    def generate_all_groups_daily_summaries(self, hours=24):
        def _do_op(conn):
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT group_id FROM conversation_logs WHERE created_at > ? AND group_id != ''", (cutoff,))
            groups = [r[0] for r in cursor.fetchall()]
            cursor.execute("SELECT COUNT(*) FROM conversation_logs WHERE created_at > ? AND group_id = '' AND role = 'user'", (cutoff,))
            if cursor.fetchone()[0] > 0: groups.append('')
            whitelist = self.config.get('daily_summary_group_whitelist', [])
            if whitelist: groups = [g for g in groups if g in whitelist or g == '']
            max_groups = self.config.get('summary_max_groups', 50)
            groups = groups[:max_groups]
            results = []
            total_messages = 0
            all_users = set()
            all_keywords = []
            for gid in groups:
                r = self._generate_group_summary_internal(conn, gid, hours)
                if r:
                    results.append(r)
                    total_messages += r.get('message_count', 0)
                    for u in r.get('active_users', []): all_users.add(u)
                    if r.get('key_topics'): all_keywords.extend(r['key_topics'].split(','))
            merged_topics = ''
            if results:
                from collections import Counter
                kw_counter = Counter(k.strip() for k in all_keywords if k.strip())
                merged_topics = ','.join(t for t, _ in kw_counter.most_common(15))
                global_parts = []
                for r in results:
                    gid = r.get('group_id', '')
                    label = f"群{gid}" if gid else "私聊"
                    global_parts.append(f"[{label}] {r['summary'][:100]}")
                global_summary = ' | '.join(global_parts[:10])
                from datetime import date
                today = date.today().isoformat()
                cursor.execute(
                    'INSERT OR REPLACE INTO daily_global_digest (digest_date, total_groups, total_messages, merged_topics, global_summary, active_users) VALUES (?, ?, ?, ?, ?, ?)',
                    (today, len(results), total_messages, merged_topics, global_summary[:2000], ','.join(list(all_users)[:30])))
            return {'groups_processed': len(results), 'total_messages': total_messages, 'unique_users': len(all_users), 'merged_topics': merged_topics}
        result = self._execute_write(_do_op)
        return result if result is not None else {'groups_processed': 0}

    def get_global_daily_digest(self, days=3):
        def _do_op(conn):
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor = conn.cursor()
            cursor.execute('SELECT digest_date, total_groups, total_messages, merged_topics, global_summary, active_users FROM daily_global_digest WHERE digest_date > ? ORDER BY digest_date DESC', (cutoff,))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def save_daily_summary(self, summary_date, summary, key_topics='', group_id='', active_users='', importance=5):
        def _do_op(conn):
            self._save_daily_summary_internal(conn, summary_date, summary, key_topics, group_id, active_users, importance)
            return f"Daily summary saved for {summary_date}"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: save daily summary failed"

    def get_daily_summaries(self, days=7, group_id=None):
        def _do_op(conn):
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor = conn.cursor()
            if group_id:
                cursor.execute('SELECT summary_date, summary, key_topics, group_id, active_users FROM daily_summaries WHERE summary_date > ? AND (group_id = ? OR group_id = "") ORDER BY summary_date DESC LIMIT 30', (cutoff, str(group_id)))
            else:
                cursor.execute('SELECT summary_date, summary, key_topics, group_id, active_users FROM daily_summaries WHERE summary_date > ? ORDER BY summary_date DESC LIMIT 30', (cutoff,))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_today_daily_summary(self, group_id=''):
        def _do_op(conn):
            from datetime import date
            today = date.today().isoformat()
            cursor = conn.cursor()
            if group_id:
                cursor.execute('SELECT summary, key_topics, active_users FROM daily_summaries WHERE summary_date = ? AND group_id = ?', (today, str(group_id)))
                row = cursor.fetchone()
                if row: return dict(row)
                cursor.execute('SELECT summary, key_topics, active_users FROM daily_summaries WHERE summary_date = ? AND group_id = ""', (today,))
            else:
                cursor.execute('SELECT summary, key_topics, active_users FROM daily_summaries WHERE summary_date = ?', (today,))
            row = cursor.fetchone()
            return dict(row) if row else None
        return self._execute_read(_do_op)

    # ==================== Relationships ====================

    def update_relationship_enhanced(self, user_id, relation_type=None, summary=None, nickname=None, first_met_location=None, known_contexts=None):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM relationships WHERE user_id = ?', (user_id,))
            existing = cursor.fetchone()
            if existing:
                existing = dict(existing)
                updates = []
                params = []
                if relation_type: updates.append("relation_type = ?"); params.append(relation_type)
                if summary: updates.append("summary = ?"); params.append(summary)
                if nickname: updates.append("nickname = ?"); params.append(nickname)
                if first_met_location: updates.append("first_met_location = ?"); params.append(first_met_location)
                if known_contexts:
                    new_groups = [g.strip() for g in known_contexts.split(',') if g.strip()]
                    old_groups = [g.strip() for g in (existing.get('known_contexts') or '').split(',') if g.strip()]
                    merged = list(dict.fromkeys(old_groups + new_groups))
                    updates.append("known_contexts = ?"); params.append(','.join(merged))
                updates.append("interaction_count = interaction_count + 1")
                updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
                params.append(user_id)
                cursor.execute(f'UPDATE relationships SET {", ".join(updates)} WHERE user_id = ?', params)
                cursor.execute('INSERT INTO activities (memory_id, activity_type, description) VALUES (?, ?, ?)',
                             (0, 'update_relation', f'{nickname or user_id}: {summary[:30] if summary else ""}'))
                return f"Relationship updated: {nickname or user_id}"
            else:
                cursor.execute(
                    'INSERT INTO relationships (user_id, nickname, relation_type, summary, first_met_location, known_contexts) VALUES (?, ?, ?, ?, ?, ?)',
                    (user_id, nickname or '', relation_type or 'friend', summary or '', first_met_location or '', known_contexts or ''))
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
                'SELECT * FROM relationships WHERE user_id LIKE ? OR nickname LIKE ? OR relation_type LIKE ? OR summary LIKE ? LIMIT ?',
                (f'%{query}%', f'%{query}%', f'%{query}%', f'%{query}%', limit))
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
            self.cache.clear()
            return f"Relationship deleted: {user_id}"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: delete relationship failed"

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

    # ==================== Activity & Synonym ====================

    def get_recent_activities(self, limit=20):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute(
                'SELECT a.*, m.content FROM activities a LEFT JOIN memories m ON a.memory_id = m.id ORDER BY a.created_at DESC LIMIT ?', (limit,))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def add_synonym_pair(self, word, synonym):
        def _do_op(conn):
            conn.execute('INSERT OR IGNORE INTO synonyms (word, synonym) VALUES (?, ?)', (word, synonym))
            conn.execute('INSERT OR IGNORE INTO synonyms (word, synonym) VALUES (?, ?)', (synonym, word))
            return f"Synonym added: {word} <-> {synonym}"
        result = self._execute_write(_do_op)
        return result if result is not None else "Error: add synonym failed"

    def get_all_synonyms(self):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT word, synonym FROM synonyms')
            result = {}
            for row in cursor.fetchall():
                word, synonym = row[0], row[1]
                if word not in result: result[word] = []
                result[word].append(synonym)
            return result
        result = self._execute_read(_do_op)
        return result if result is not None else {}

    # ==================== Auto Facts ====================

    def save_auto_facts(self, content, user_id=''):
        def _do_op(conn):
            return self._save_auto_facts_internal(conn, content, user_id)
        result = self._execute_write(_do_op)
        return result if result is not None else 0

    def search_auto_facts(self, query, user_id=None, limit=10):
        def _do_op(conn):
            cursor = conn.cursor()
            if user_id:
                cursor.execute(
                    'SELECT id, subject, predicate, object, fact_type, confidence FROM auto_facts WHERE user_id = ? AND (subject LIKE ? OR predicate LIKE ? OR object LIKE ?) ORDER BY confidence DESC, created_at DESC LIMIT ?',
                    (user_id, f'%{query}%', f'%{query}%', f'%{query}%', limit))
            else:
                if query:
                    cursor.execute(
                        'SELECT id, subject, predicate, object, fact_type, confidence FROM auto_facts WHERE subject LIKE ? OR predicate LIKE ? OR object LIKE ? ORDER BY confidence DESC, created_at DESC LIMIT ?',
                        (f'%{query}%', f'%{query}%', f'%{query}%', limit))
                else:
                    cursor.execute('SELECT id, subject, predicate, object, fact_type, confidence FROM auto_facts ORDER BY created_at DESC LIMIT ?', (limit,))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_user_facts(self, user_id, limit=20):
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT subject, predicate, object, fact_type FROM auto_facts WHERE user_id = ? ORDER BY created_at DESC LIMIT ?', (user_id, limit))
            return [dict(row) for row in cursor.fetchall()]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def get_cross_group_facts(self, user_id, current_group, limit=5):
        def _do_op(conn):
            cutoff = (datetime.now() - timedelta(hours=72)).isoformat()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT content FROM conversation_logs WHERE user_id = ? AND group_id != ? AND group_id != '' AND created_at > ? ORDER BY created_at DESC LIMIT 50",
                (user_id, str(current_group), cutoff))
            other_contents = [r[0] for r in cursor.fetchall()]
            if not other_contents: return []
            cursor.execute(
                "SELECT DISTINCT group_id FROM conversation_logs WHERE user_id = ? AND group_id != ? AND group_id != '' AND created_at > ? LIMIT 5",
                (user_id, str(current_group), cutoff))
            other_groups = [r[0] for r in cursor.fetchall()]
            all_facts = []
            for content in other_contents:
                facts = self._extract_facts(content, user_id)
                for f in facts[:2]:
                    f['group_id'] = other_groups[0] if other_groups else ''
                    all_facts.append(f)
                if len(all_facts) >= limit: break
            seen = set()
            unique = []
            for f in all_facts:
                key = f"{f['subject']}|{f['predicate']}|{f['object']}"
                if key not in seen:
                    seen.add(key)
                    unique.append(f)
            return unique[:limit]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    def search_recent_utterances(self, user_id, query, current_group='', limit=5):
        def _do_op(conn):
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id, content, group_id, created_at FROM recent_utterances WHERE user_id = ? AND created_at > ? ORDER BY created_at DESC LIMIT ?",
                (str(user_id), cutoff, limit * 3))
            user_recent = [dict(r) for r in cursor.fetchall()]
            query_words = set(re.findall(r'\w{2,}', query.lower()))
            if not query_words: return user_recent[:limit]
            scored = []
            for u in user_recent:
                content_words = set(re.findall(r'\w{2,}', u['content'].lower()))
                overlap = len(query_words & content_words)
                if overlap > 0: scored.append((overlap, u))
            scored.sort(key=lambda x: x[0], reverse=True)
            results = [u for _, u in scored[:limit]]
            if len(results) < limit and self.config.get('cross_group_association_enabled', True):
                cursor.execute(
                    "SELECT user_id, content, group_id, created_at FROM recent_utterances WHERE content LIKE ? AND created_at > ? AND user_id != ? ORDER BY created_at DESC LIMIT ?",
                    (f'%{list(query_words)[0]}%', cutoff, str(user_id), limit))
                other_recent = [dict(r) for r in cursor.fetchall()]
                results.extend(other_recent[:limit - len(results)])
            return results[:limit]
        result = self._execute_read(_do_op)
        return result if result is not None else []

    # ==================== Memory Decay ====================

    def apply_memory_decay(self):
        if not self.config.get('memory_decay_enabled', True): return {'decayed': 0, 'removed': 0}
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT id, importance, access_count, last_accessed, created_at FROM memories WHERE importance < 9')
            memories = cursor.fetchall()
            decayed = 0
            removed = 0
            base_stability = self.config.get('memory_decay_base_stability', 48)
            now = datetime.now()
            for row in memories:
                mid, importance, access_count, last_accessed, created_at = row
                last = last_accessed or created_at or now.isoformat()
                try:
                    last_dt = datetime.fromisoformat(last)
                    hours_elapsed = max((now - last_dt).total_seconds() / 3600, 0)
                except Exception: hours_elapsed = 0
                stability = base_stability * (1 + math.log1p(access_count))
                retention = math.exp(-hours_elapsed / stability)
                if retention < 0.1 and importance <= 2:
                    cursor.execute('DELETE FROM memories WHERE id = ?', (mid,))
                    cursor.execute('DELETE FROM activities WHERE memory_id = ?', (mid,))
                    removed += 1
                elif retention < 0.3 and importance > 1:
                    new_imp = max(1, importance - 1)
                    cursor.execute('UPDATE memories SET importance = ? WHERE id = ?', (new_imp, mid))
                    decayed += 1
            cursor.execute('DELETE FROM auto_facts WHERE confidence < 0.3')
            self.cache.clear()
            return {'decayed': decayed, 'removed': removed}
        result = self._execute_write(_do_op)
        return result if result is not None else {'decayed': 0, 'removed': 0}

    def merge_similar_memories(self, threshold=None):
        if not self.config.get('auto_merge_similar_enabled', False): return {'merged': 0}
        if threshold is None: threshold = self.config.get('similarity_threshold', 0.7)
        def _do_op(conn):
            cursor = conn.cursor()
            cursor.execute('SELECT id, content, tags, importance FROM memories ORDER BY created_at DESC LIMIT ?',
                           (self.config.get('merge_scan_limit', 100),))
            memories = cursor.fetchall()
            merged = 0
            for i in range(len(memories)):
                if memories[i] is None: continue
                id1, content1, tags1, imp1 = memories[i]
                tokens1 = set(self._tokenize(content1.lower()))
                for j in range(i + 1, len(memories)):
                    if memories[j] is None: continue
                    id2, content2, tags2, imp2 = memories[j]
                    tokens2 = set(self._tokenize(content2.lower()))
                    if not tokens1 or not tokens2: continue
                    jaccard = len(tokens1 & tokens2) / len(tokens1 | tokens2)
                    if jaccard >= threshold:
                        keep_id = id1 if imp1 >= imp2 else id2
                        remove_id = id2 if keep_id == id1 else id1
                        new_imp = max(imp1, imp2)
                        cursor.execute('UPDATE memories SET importance = ? WHERE id = ?', (new_imp, keep_id))
                        cursor.execute('DELETE FROM memories WHERE id = ?', (remove_id,))
                        cursor.execute('DELETE FROM activities WHERE memory_id = ?', (remove_id,))
                        merged += 1
                        memories[j] = None
            self.cache.clear()
            return {'merged': merged}
        result = self._execute_write(_do_op)
        return result if result is not None else {'merged': 0}

    # ==================== Utility ====================

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
            self.cache.clear()
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
                cursor.execute(
                    'INSERT INTO memories (content, category, importance, tags, source, hash) VALUES (?, ?, ?, ?, ?, ?)',
                    (content, category, importance, ','.join(tags) if isinstance(tags, list) else tags, source, content_hash))
                imported += 1
            self.cache.clear()
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
            cursor.execute('SELECT COUNT(*) FROM triples')
            tri_count = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM dream_logs')
            dream_count = cursor.fetchone()[0]
            import sys
            return {
                'memories': mem_count, 'relationships': rel_count, 'triples': tri_count,
                'dream_logs': dream_count,
                'auto_facts': cursor.execute('SELECT COUNT(*) FROM auto_facts').fetchone()[0] if self._table_exists(conn, 'auto_facts') else 0,
                'daily_summaries': cursor.execute('SELECT COUNT(*) FROM daily_summaries').fetchone()[0] if self._table_exists(conn, 'daily_summaries') else 0,
                'cache_items': len(self.cache), 'python_version': sys.version.split()[0]
            }
        result = self._execute_read(_do_op)
        return result if result is not None else {'error': 'stats read failed'}
