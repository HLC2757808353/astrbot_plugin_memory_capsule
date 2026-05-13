import sqlite3
import os
import re
import math
import hashlib
from datetime import datetime, timedelta

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
        self.backup_manager = None

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA synchronous = NORMAL')
        conn.execute('PRAGMA busy_timeout = 60000')
        conn.execute('PRAGMA cache_size = -2000')
        return conn

    def _execute_write(self, func):
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
            err_msg = str(e).lower()
            if 'malformed' in err_msg:
                logger.warning("Database malformed on read, attempting repair...")
                if conn:
                    try: conn.close()
                    except Exception: pass
                conn = None
                self._repair_database()
                try:
                    conn = self._get_connection()
                    return func(conn)
                except Exception as e2:
                    logger.error(f"Read retry after repair failed: {e2}")
                    return None
            logger.debug(f"Read error: {e}")
            return None
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

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
            conn = sqlite3.connect(self.db_path, timeout=60)
            conn.execute('PRAGMA journal_mode = WAL')
            conn.execute('PRAGMA foreign_keys = ON')
            conn.execute('PRAGMA busy_timeout = 60000')
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
                category TEXT DEFAULT 'general', importance INTEGER DEFAULT 5,
                tags TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, access_count INTEGER DEFAULT 0,
                last_accessed TIMESTAMP, source TEXT DEFAULT 'user', hash TEXT UNIQUE)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS relationships (
                user_id TEXT PRIMARY KEY, nickname TEXT, relation_type TEXT DEFAULT 'friend',
                summary TEXT DEFAULT '', first_met_location TEXT,
                identity_aliases TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, interaction_count INTEGER DEFAULT 0)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER,
                activity_type TEXT NOT NULL, description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE)''')
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

    # ==================== Relationships ====================

    def update_relationship_enhanced(self, user_id, relation_type=None, summary=None, nickname=None, first_met_location=None):
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
                updates.append("interaction_count = interaction_count + 1")
                updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
                params.append(user_id)
                cursor.execute(f'UPDATE relationships SET {", ".join(updates)} WHERE user_id = ?', params)
                cursor.execute('INSERT INTO activities (memory_id, activity_type, description) VALUES (?, ?, ?)',
                             (0, 'update_relation', f'{nickname or user_id}: {summary[:30] if summary else ""}'))
                return f"Relationship updated: {nickname or user_id}"
            else:
                cursor.execute(
                    'INSERT INTO relationships (user_id, nickname, relation_type, summary, first_met_location) VALUES (?, ?, ?, ?, ?)',
                    (user_id, nickname or '', relation_type or 'friend', summary or '', first_met_location or ''))
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
                cursor.execute(
                    'INSERT INTO memories (content, category, importance, tags, source, hash) VALUES (?, ?, ?, ?, ?, ?)',
                    (content, category, importance, ','.join(tags) if isinstance(tags, list) else tags, source, content_hash))
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