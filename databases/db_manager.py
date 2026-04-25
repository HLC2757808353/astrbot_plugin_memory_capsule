﻿import sqlite3
import os
import re
import math
import hashlib
import threading
from datetime import datetime, timedelta
from cachetools import TTLCache

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
            logger.debug("jieba loaded")
        except ImportError:
            pass
    return _jieba_instance, _pseg_instance

_pypinyin_initialized = False
_pypinyin_instance = None

def _get_pypinyin():
    global _pypinyin_initialized, _pypinyin_instance
    if not _pypinyin_initialized:
        try:
            from pypinyin import pypinyin as pypy
            _pypinyin_instance = pypy
            _pypinyin_initialized = True
        except ImportError:
            pass
    return _pypinyin_instance


class DatabaseManager:
    def __init__(self, config=None, context=None):
        self.config = config or {}
        self.context = context
        self.db_path = None
        self.cache = TTLCache(
            maxsize=self.config.get('max_cache_size', 200),
            ttl=self.config.get('cache_ttl', 300)
        )
        self._local = threading.local()
        self.backup_manager = None

    def _get_connection(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys = ON')
            conn.execute('PRAGMA journal_mode = WAL')
            conn.execute('PRAGMA synchronous = NORMAL')
            self._local.conn = conn
        return self._local.conn

    def initialize(self):
        app_data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        os.makedirs(app_data_dir, exist_ok=True)
        self.db_path = os.path.join(app_data_dir, "memory_capsule.db")
        self._initialize_database_structure()
        from .backup import BackupManager
        self.backup_manager = BackupManager(self.db_path, self.config)
        self.backup_manager.start_auto_backup()
        logger.info(f"Database initialized: {self.db_path}")

    def _initialize_database_structure(self):
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute('PRAGMA journal_mode = WAL')
            conn.execute('PRAGMA foreign_keys = ON')
            cursor = conn.cursor()

            cursor.execute('''CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                importance INTEGER DEFAULT 5,
                tags TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                access_count INTEGER DEFAULT 0,
                last_accessed TIMESTAMP,
                source TEXT DEFAULT 'user',
                hash TEXT UNIQUE
            )''')

            cursor.execute('''CREATE TABLE IF NOT EXISTS relationships (
                user_id TEXT PRIMARY KEY,
                nickname TEXT,
                relation_type TEXT DEFAULT 'friend',
                summary TEXT DEFAULT '',
                first_met_location TEXT,
                known_contexts TEXT DEFAULT '',
                identity_aliases TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                interaction_count INTEGER DEFAULT 0
            )''')

            cursor.execute('''CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER,
                activity_type TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
            )''')

            cursor.execute('''CREATE TABLE IF NOT EXISTS synonyms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL,
                synonym TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(word, synonym)
            )''')

            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)''')
            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance)''')
            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)''')
            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_memories_access ON memories(access_count)''')
            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_relationships_nickname ON relationships(nickname)''')
            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_activities_memory ON activities(memory_id)''')
            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_activities_type ON activities(activity_type)''')

            try:
                cursor.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content, tags, category, content='memories', content_rowid='id'
                )''')
                cursor.execute('''CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content, tags, category) VALUES (new.id, new.content, new.tags, new.category);
                END''')
                cursor.execute('''CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, tags, category) VALUES('delete', old.id, old.content, old.tags, old.category);
                END''')
                cursor.execute('''CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, tags, category) VALUES('delete', old.id, old.content, old.tags, old.category);
                    INSERT INTO memories_fts(rowid, content, tags, category) VALUES (new.id, new.content, new.tags, new.category);
                END''')
            except Exception as e:
                logger.warning(f"FTS5 setup skipped: {e}")

            conn.commit()
        finally:
            if conn:
                conn.close()

    def close(self):
        if self.backup_manager:
            self.backup_manager.stop_auto_backup()
        if hasattr(self._local, 'conn') and self._local.conn:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None
        self.cache.clear()
        logger.info("Database closed")

    def backup(self):
        if self.backup_manager:
            return self.backup_manager.backup()
        return "No backup manager"

    def get_backup_list(self):
        if self.backup_manager:
            return self.backup_manager.get_backup_list()
        return []

    def restore_from_backup(self, backup_filename):
        if self.backup_manager:
            return self.backup_manager.restore_from_backup(backup_filename)
        return "No backup manager"

    # ==================== Memory Operations ====================

    def write_memory(self, content, category=None, importance=5, tags=None, source='user'):
        conn = self._get_connection()
        try:
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM memories WHERE hash = ?', (content_hash,))
            if cursor.fetchone():
                return "Memory already exists"

            if tags is None:
                tags = self._extract_tags(content)
            if category is None:
                category = self._guess_category(content)

            cursor.execute(
                'INSERT INTO memories (content, category, importance, tags, source, hash) VALUES (?, ?, ?, ?, ?, ?)',
                (content, category, importance, ','.join(tags) if isinstance(tags, list) else tags, source, content_hash)
            )
            memory_id = cursor.lastrowid
            self._record_activity(memory_id, 'create', content[:50])
            conn.commit()

            cache_key = f"search_{content[:20]}"
            if cache_key in self.cache:
                del self.cache[cache_key]

            return f"Memory saved (ID:{memory_id})"
        except sqlite3.IntegrityError:
            return "Memory already exists"
        except Exception as e:
            logger.error(f"Write memory error: {e}")
            return f"Error: {e}"

    def search_memory(self, query, category_filter=None, limit=None):
        conn = self._get_connection()
        try:
            if limit is None:
                limit = self.config.get('search_max_results', 5)

            results = self._fts_search(conn, query, limit * 3)
            if not results:
                results = self._fallback_search(conn, query, limit * 3)

            if category_filter:
                results = [r for r in results if r.get('category') == category_filter]

            if self.config.get('mmr_enabled', True):
                results = self._mmr_rerank(results, query, limit)
            else:
                results = results[:limit]

            for r in results:
                r['content'] = r['content'][:80] + ('...' if len(r['content']) > 80 else '')

            if results:
                ids = [r['id'] for r in results]
                placeholders = ','.join('?' * len(ids))
                conn.execute(
                    f'UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id IN ({placeholders})',
                    [datetime.now().isoformat()] + ids
                )
                conn.commit()

            return results
        except Exception as e:
            logger.error(f"Search memory error: {e}")
            return []

    def delete_memory(self, memory_id):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT content FROM memories WHERE id = ?', (memory_id,))
            row = cursor.fetchone()
            if not row:
                return "Memory not found"
            cursor.execute('DELETE FROM memories WHERE id = ?', (memory_id,))
            cursor.execute('DELETE FROM activities WHERE memory_id = ?', (memory_id,))
            conn.commit()
            self.cache.clear()
            return f"Memory deleted (ID:{memory_id})"
        except Exception as e:
            logger.error(f"Delete memory error: {e}")
            return f"Error: {e}"

    def update_memory(self, memory_id, content=None, category=None, importance=None, tags=None):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM memories WHERE id = ?', (memory_id,))
            if not cursor.fetchone():
                return "Memory not found"

            updates = []
            params = []
            if content is not None:
                updates.append("content = ?")
                params.append(content)
                new_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
                updates.append("hash = ?")
                params.append(new_hash)
                if tags is None:
                    tags = self._extract_tags(content)
            if category is not None:
                updates.append("category = ?")
                params.append(category)
            if importance is not None:
                updates.append("importance = ?")
                params.append(importance)
            if tags is not None:
                updates.append("tags = ?")
                params.append(','.join(tags) if isinstance(tags, list) else tags)

            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(memory_id)

            cursor.execute(f'UPDATE memories SET {", ".join(updates)} WHERE id = ?', params)
            conn.commit()
            self.cache.clear()
            return f"Memory updated (ID:{memory_id})"
        except Exception as e:
            logger.error(f"Update memory error: {e}")
            return f"Error: {e}"

    def get_all_memories(self, limit=100, offset=0, category=None):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            if category:
                cursor.execute('SELECT id, content, category, importance, created_at FROM memories WHERE category = ? ORDER BY created_at DESC LIMIT ? OFFSET ?',
                             (category, limit, offset))
            else:
                cursor.execute('SELECT id, content, category, importance, created_at FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?',
                             (limit, offset))
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Get all memories error: {e}")
            return []

    def get_memories_count(self):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM memories')
            return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Get memories count error: {e}")
            return 0

    def get_recent_memories(self, limit=5):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT id, content, category, importance, created_at FROM memories ORDER BY created_at DESC LIMIT ?',
                         (limit,))
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Get recent memories error: {e}")
            return []

    def get_memory_categories(self):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT DISTINCT category FROM memories')
            return [row[0] for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Get categories error: {e}")
            return []

    def get_all_tags(self):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT tags FROM memories WHERE tags != ""')
            all_tags = set()
            for row in cursor.fetchall():
                for tag in row[0].split(','):
                    tag = tag.strip()
                    if tag:
                        all_tags.add(tag)
            return list(all_tags)
        except Exception as e:
            logger.error(f"Get tags error: {e}")
            return []

    # ==================== Working Memory ====================

    def get_working_memories(self, context_query="", limit=6, max_chars=800):
        conn = self._get_connection()
        try:
            candidates = []
            candidate_ids = set()

            cursor = conn.cursor()
            cursor.execute('SELECT id, content, category, importance, tags, created_at, access_count FROM memories WHERE importance >= 8 ORDER BY importance DESC LIMIT 3')
            for row in cursor.fetchall():
                m = dict(row)
                if m['id'] not in candidate_ids:
                    candidates.append(m)
                    candidate_ids.add(m['id'])

            cursor.execute('SELECT id, content, category, importance, tags, created_at, access_count FROM memories ORDER BY created_at DESC LIMIT 5')
            for row in cursor.fetchall():
                m = dict(row)
                if m['id'] not in candidate_ids:
                    candidates.append(m)
                    candidate_ids.add(m['id'])

            if context_query:
                fts_results = self._fts_search(conn, context_query, limit * 2)
                for m in fts_results:
                    if m['id'] not in candidate_ids:
                        candidates.append(m)
                        candidate_ids.add(m['id'])

                tag_results = self._tag_retrieve(conn, context_query, limit)
                for m in tag_results:
                    if m['id'] not in candidate_ids:
                        candidates.append(m)
                        candidate_ids.add(m['id'])

            scored = []
            for m in candidates:
                score = self._score_memory(m, context_query)
                scored.append((score, m))
            scored.sort(key=lambda x: x[0], reverse=True)

            top = [m for _, m in scored[:limit * 2]]

            if context_query and top:
                activated_tags = set()
                for m in top[:limit]:
                    for tag in m.get('tags', '').split(','):
                        tag = tag.strip()
                        if tag:
                            activated_tags.add(tag)

                if activated_tags:
                    tag_list = ','.join(f'"{t}"' for t in list(activated_tags)[:10])
                    cursor.execute(
                        f'SELECT id, content, category, importance, tags, created_at, access_count FROM memories WHERE id NOT IN ({",".join("?" * len(candidate_ids))}) AND (tags LIKE ?) LIMIT 3',
                        list(candidate_ids) + [f'%{list(activated_tags)[0]}%']
                    )
                    for row in cursor.fetchall():
                        m = dict(row)
                        if m['id'] not in candidate_ids:
                            top.append(m)
                            candidate_ids.add(m['id'])

            total_chars = 0
            result = []
            for m in top[:limit]:
                content = m.get('content', '')
                if total_chars + len(content) > max_chars:
                    content = content[:max_chars - total_chars]
                    if content:
                        result.append({'id': m['id'], 'content': content, 'category': m.get('category', '')})
                    break
                result.append({'id': m['id'], 'content': content, 'category': m.get('category', '')})
                total_chars += len(content)

            return result
        except Exception as e:
            logger.error(f"Working memory error: {e}")
            return []

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
            except Exception:
                pass
        if recency_hours < 1:
            score += 5.0
        elif recency_hours < 24:
            score += 3.0
        elif recency_hours < 168:
            score += 1.5

        access_count = memory.get('access_count', 0)
        score += min(access_count * 0.5, 5.0)

        if context_query:
            content = memory.get('content', '').lower()
            tags = memory.get('tags', '').lower()
            query_lower = context_query.lower()
            query_words = set(re.findall(r'\w+', query_lower))

            content_words = set(re.findall(r'\w+', content))
            overlap = query_words & content_words
            if overlap:
                score += len(overlap) * 2.0

            for tag in tags.split(','):
                tag = tag.strip()
                if tag and tag in query_lower:
                    score += 3.0

        return score

    def _fts_search(self, conn, query, limit):
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'")
            if not cursor.fetchone():
                return []

            jieba_mod, _ = _get_jieba()
            if jieba_mod:
                words = list(jieba_mod.cut(query))
                fts_query = ' OR '.join(f'"{w}"' for w in words if len(w) > 1)
            else:
                words = query.split()
                fts_query = ' OR '.join(f'"{w}"' for w in words if len(w) > 1)

            if not fts_query:
                return []

            cursor.execute(
                'SELECT m.id, m.content, m.category, m.importance, m.tags, m.created_at, m.access_count FROM memories m JOIN memories_fts f ON m.id = f.rowid WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?',
                (fts_query, limit)
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.debug(f"FTS search fallback: {e}")
            return []

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
                params + [limit]
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.debug(f"Fallback search error: {e}")
            return []

    def _tag_retrieve(self, conn, query, limit):
        try:
            jieba_mod, _ = _get_jieba()
            if jieba_mod:
                words = [w for w in jieba_mod.cut(query) if len(w) > 1]
            else:
                words = [w for w in query.split() if len(w) > 1]

            if not words:
                return []

            cursor = conn.cursor()
            conditions = []
            params = []
            for w in words[:5]:
                conditions.append("tags LIKE ?")
                params.append(f'%{w}%')

            where = ' OR '.join(conditions)
            cursor.execute(
                f'SELECT id, content, category, importance, tags, created_at, access_count FROM memories WHERE {where} ORDER BY importance DESC LIMIT ?',
                params + [limit]
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []

    def _mmr_rerank(self, results, query, limit):
        if not results or len(results) <= limit:
            return results

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

    # ==================== Relationship Operations ====================

    def update_relationship_enhanced(self, user_id, relation_type=None, summary=None,
                                     nickname=None, first_met_location=None, known_contexts=None):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM relationships WHERE user_id = ?', (user_id,))
            existing = cursor.fetchone()

            if existing:
                existing = dict(existing)
                updates = []
                params = []

                if relation_type:
                    updates.append("relation_type = ?")
                    params.append(relation_type)
                if summary:
                    updates.append("summary = ?")
                    params.append(summary)
                if nickname:
                    updates.append("nickname = ?")
                    params.append(nickname)
                if first_met_location:
                    updates.append("first_met_location = ?")
                    params.append(first_met_location)
                if known_contexts:
                    new_groups = [g.strip() for g in known_contexts.split(',') if g.strip()]
                    old_groups = [g.strip() for g in (existing.get('known_contexts') or '').split(',') if g.strip()]
                    merged = list(dict.fromkeys(old_groups + new_groups))
                    updates.append("known_contexts = ?")
                    params.append(','.join(merged))

                updates.append("interaction_count = interaction_count + 1")
                updates.append("updated_at = ?")
                params.append(datetime.now().isoformat())
                params.append(user_id)

                cursor.execute(f'UPDATE relationships SET {", ".join(updates)} WHERE user_id = ?', params)
                conn.commit()
                return f"Relationship updated: {nickname or user_id}"
            else:
                cursor.execute(
                    'INSERT INTO relationships (user_id, nickname, relation_type, summary, first_met_location, known_contexts) VALUES (?, ?, ?, ?, ?, ?)',
                    (user_id, nickname or '', relation_type or 'friend', summary or '',
                     first_met_location or '', known_contexts or '')
                )
                conn.commit()
                return f"Relationship created: {nickname or user_id}"
        except Exception as e:
            logger.error(f"Update relationship error: {e}")
            return f"Error: {e}"

    def get_relationship_by_user_id(self, user_id):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM relationships WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Get relationship error: {e}")
            return None

    def get_relationship_with_identity(self, user_id):
        rel = self.get_relationship_by_user_id(user_id)
        if not rel:
            return None

        aliases = rel.get('identity_aliases', '')
        if aliases:
            try:
                alias_list = [a.strip() for a in aliases.split(',') if a.strip()]
                rel['identity_aliases'] = alias_list
            except Exception:
                rel['identity_aliases'] = []

        return rel

    def get_all_relationships(self, limit=100, offset=0):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM relationships ORDER BY updated_at DESC LIMIT ? OFFSET ?', (limit, offset))
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Get all relationships error: {e}")
            return []

    def get_relationships_count(self):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM relationships')
            return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Get relationships count error: {e}")
            return 0

    def search_relationship(self, query, limit=3):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT * FROM relationships WHERE user_id LIKE ? OR nickname LIKE ? OR relation_type LIKE ? OR summary LIKE ? LIMIT ?',
                (f'%{query}%', f'%{query}%', f'%{query}%', f'%{query}%', limit)
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Search relationship error: {e}")
            return []

    def delete_relationship(self, user_id):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT nickname FROM relationships WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            if not row:
                return "Relationship not found"
            cursor.execute('DELETE FROM relationships WHERE user_id = ?', (user_id,))
            conn.commit()
            self.cache.clear()
            return f"Relationship deleted: {user_id}"
        except Exception as e:
            logger.error(f"Delete relationship error: {e}")
            return f"Error: {e}"

    def add_identity_alias(self, user_id, alias):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT identity_aliases FROM relationships WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            if not row:
                return "Relationship not found"
            current = row[0] or ''
            aliases = [a.strip() for a in current.split(',') if a.strip()]
            if alias not in aliases:
                aliases.append(alias)
            cursor.execute('UPDATE relationships SET identity_aliases = ?, updated_at = ? WHERE user_id = ?',
                         (','.join(aliases), datetime.now().isoformat(), user_id))
            conn.commit()
            return f"Alias added: {alias}"
        except Exception as e:
            logger.error(f"Add alias error: {e}")
            return f"Error: {e}"

    def get_user_aliases(self, user_id):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT identity_aliases FROM relationships WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            if not row or not row[0]:
                return []
            return [a.strip() for a in row[0].split(',') if a.strip()]
        except Exception as e:
            logger.error(f"Get aliases error: {e}")
            return []

    def smart_resolve_identity(self, identifier):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, nickname FROM relationships WHERE user_id = ?', (identifier,))
            row = cursor.fetchone()
            if row:
                return {'user_id': row[0], 'nickname': row[1], 'match_type': 'exact'}

            cursor.execute('SELECT user_id, nickname FROM relationships WHERE nickname = ?', (identifier,))
            row = cursor.fetchone()
            if row:
                return {'user_id': row[0], 'nickname': row[1], 'match_type': 'nickname'}

            cursor.execute('SELECT user_id, nickname, identity_aliases FROM relationships')
            for row in cursor.fetchall():
                aliases = (row[2] or '').split(',')
                if identifier in [a.strip() for a in aliases]:
                    return {'user_id': row[0], 'nickname': row[1], 'match_type': 'alias'}

            cursor.execute('SELECT user_id, nickname FROM relationships WHERE nickname LIKE ?', (f'%{identifier}%',))
            row = cursor.fetchone()
            if row:
                return {'user_id': row[0], 'nickname': row[1], 'match_type': 'fuzzy'}

            return None
        except Exception as e:
            logger.error(f"Resolve identity error: {e}")
            return None

    # ==================== Activity & Synonym ====================

    def _record_activity(self, memory_id, activity_type, description=''):
        conn = self._get_connection()
        try:
            conn.execute(
                'INSERT INTO activities (memory_id, activity_type, description) VALUES (?, ?, ?)',
                (memory_id, activity_type, description)
            )
            conn.commit()
        except Exception as e:
            logger.debug(f"Record activity error: {e}")

    def get_recent_activities(self, limit=20):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT a.*, m.content FROM activities a LEFT JOIN memories m ON a.memory_id = m.id ORDER BY a.created_at DESC LIMIT ?',
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Get activities error: {e}")
            return []

    def add_synonym_pair(self, word, synonym):
        conn = self._get_connection()
        try:
            conn.execute('INSERT OR IGNORE INTO synonyms (word, synonym) VALUES (?, ?)', (word, synonym))
            conn.execute('INSERT OR IGNORE INTO synonyms (word, synonym) VALUES (?, ?)', (synonym, word))
            conn.commit()
            return f"Synonym added: {word} <-> {synonym}"
        except Exception as e:
            return f"Error: {e}"

    def get_all_synonyms(self):
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT word, synonym FROM synonyms')
            result = {}
            for row in cursor.fetchall():
                word, synonym = row[0], row[1]
                if word not in result:
                    result[word] = []
                result[word].append(synonym)
            return result
        except Exception as e:
            logger.error(f"Get synonyms error: {e}")
            return {}

    # ==================== Utility Methods ====================

    def _extract_tags(self, content):
        tags = []
        try:
            jieba_mod, pseg_mod = _get_jieba()
            if jieba_mod and pseg_mod:
                words = pseg_mod.cut(content)
                for word, flag in words:
                    if flag in ('nr', 'ns', 'nt', 'nz', 'v', 'vn', 'a', 'an') and len(word) > 1:
                        tags.append(word)
        except Exception:
            words = re.findall(r'\w{2,}', content)
            tags = words[:5]

        pypinyin_mod = _get_pypinyin()
        if pypinyin_mod:
            try:
                pinyin_tags = []
                for char in content[:20]:
                    py = pypinyin_mod.lazy_pinyin(char)
                    if py and py[0].strip():
                        pinyin_tags.append(py[0])
                tags.extend(pinyin_tags[:3])
            except Exception:
                pass

        return list(set(tags))[:10]

    def _guess_category(self, content):
        category_rules = {
            'schedule': ['时间', '日期', '点', '号', '周', '月', '年', 'schedule', 'deadline'],
            'preference': ['喜欢', '讨厌', '偏好', '最爱', 'prefer', 'like', 'hate'],
            'fact': ['是', '叫做', '位于', '属于', 'means', 'is', 'called'],
            'instruction': ['记得', '记住', '提醒', '不要', '必须', 'remember', 'remind'],
            'emotion': ['开心', '难过', '生气', '害怕', 'happy', 'sad', 'angry'],
        }
        content_lower = content.lower()
        best_cat = 'general'
        best_score = 0
        for cat, keywords in category_rules.items():
            score = sum(1 for kw in keywords if kw in content_lower)
            if score > best_score:
                best_score = score
                best_cat = cat
        return best_cat

    def cleanup_memories(self, days=None, max_memories=None):
        conn = self._get_connection()
        try:
            days = days or self.config.get('memory_cleanup_days', 365)
            max_memories = max_memories or self.config.get('memory_cleanup_max', 10000)

            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor.execute(
                'DELETE FROM memories WHERE importance < 3 AND access_count = 0 AND created_at < ?',
                (cutoff,)
            )
            deleted = cursor.rowcount

            cursor.execute('SELECT COUNT(*) FROM memories')
            count = cursor.fetchone()[0]
            if count > max_memories:
                cursor.execute(
                    'DELETE FROM memories WHERE id IN (SELECT id FROM memories ORDER BY importance ASC, access_count ASC, created_at ASC LIMIT ?)',
                    (count - max_memories,)
                )
                deleted += cursor.rowcount

            conn.commit()
            self.cache.clear()
            return f"Cleaned {deleted} memories"
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            return f"Error: {e}"

    def _maintain_database(self):
        conn = self._get_connection()
        try:
            conn.execute('PRAGMA optimize')
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.commit()
            logger.debug("Database maintenance done")
        except Exception as e:
            logger.debug(f"Maintenance error: {e}")
