import os
import json
import threading
import numpy as np

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

_faiss_available = False
try:
    import faiss
    _faiss_available = True
except ImportError:
    pass


class VectorSearch:
    def __init__(self, db_manager, config=None):
        self.db_manager = db_manager
        self.config = config or {}
        self._index = None
        self._id_map = []
        self._dim = 0
        self._lock = threading.Lock()
        self._embedding_provider = None
        self._add_queue = []
        self._flush_interval = 5

    @property
    def available(self):
        if not self.config.get('vector_search_enabled', True):
            return False
        return _faiss_available and self._embedding_provider is not None

    def set_embedding_provider(self, provider):
        self._embedding_provider = provider
        try:
            dim = provider.get_dim()
            if dim and dim > 0:
                self._dim = dim
                logger.info(f"RAG: embedding provider ready, dim={dim}")
            else:
                logger.info("RAG: will detect dim from first embedding")
        except Exception as e:
            logger.debug(f"RAG: get_dim fallback: {e}")

    def _ensure_index(self, dim):
        if self._index is not None and self._dim == dim:
            return
        self._dim = dim
        self._index = faiss.IndexFlatIP(dim)
        self._id_map = []
        logger.info(f"RAG: FAISS index created dim={dim}")

    async def add_embedding(self, memory_id, content):
        if not self.available:
            return False
        try:
            embedding = await self._get_embedding(content)
            if embedding is None:
                return False
            vec = np.array([embedding], dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            with self._lock:
                self._ensure_index(vec.shape[1])
                self._index.add(vec)
                self._id_map.append(memory_id)
            return True
        except Exception as e:
            logger.debug(f"RAG add error: {e}")
            return False

    async def search(self, query, limit=5):
        if not self.available or self._index is None or len(self._id_map) == 0:
            return []
        try:
            embedding = await self._get_embedding(query)
            if embedding is None:
                return []
            vec = np.array([embedding], dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            threshold = self.config.get('vector_search_threshold', 0.3)
            with self._lock:
                k = min(limit, len(self._id_map))
                if k == 0:
                    return []
                scores, indices = self._index.search(vec, k)
            results = []
            for i in range(len(indices[0])):
                idx = int(indices[0][i])
                if idx < 0 or idx >= len(self._id_map):
                    continue
                score = float(scores[0][i])
                if score >= threshold:
                    results.append({'id': self._id_map[idx], 'score': score})
            return results
        except Exception as e:
            logger.debug(f"RAG search error: {e}")
            return []

    async def _get_embedding(self, text):
        if not self._embedding_provider:
            return None
        try:
            return await self._embedding_provider.get_embedding(text[:500])
        except Exception as e:
            logger.debug(f"Embedding API error: {e}")
            return None

    async def rebuild_index_from_db(self):
        if not self.available:
            return 0
        try:
            def _fetch_memories():
                return self.db_manager.get_all_memories(limit=50000)

            memories = await _fetch_memories()
            if not memories:
                return 0

            with self._lock:
                if self._dim > 0:
                    self._index = faiss.IndexFlatIP(self._dim)
                else:
                    self._index = None
                self._id_map = []

            count = 0
            batch_size = 20
            for i in range(0, len(memories), batch_size):
                batch = memories[i:i + batch_size]
                for m in batch:
                    try:
                        embedding = await self._get_embedding(m.get('content', ''))
                        if embedding is None:
                            continue
                        vec = np.array([embedding], dtype=np.float32)
                        norm = np.linalg.norm(vec)
                        if norm > 0:
                            vec = vec / norm
                        with self._lock:
                            self._ensure_index(vec.shape[1])
                            self._index.add(vec)
                            self._id_map.append(m['id'])
                        count += 1
                    except Exception:
                        continue
                if i % 100 == 0 and i > 0:
                    logger.info(f"RAG rebuild progress: {i}/{len(memories)}")

            logger.info(f"RAG: index rebuilt, {count}/{len(memories)} memories indexed")
            return count
        except Exception as e:
            logger.error(f"RAG rebuild error: {e}")
            return 0

    def save_index(self, path):
        if not _faiss_available or self._index is None:
            return
        try:
            with self._lock:
                index_path = os.path.join(path, "memory_vectors.index")
                map_path = os.path.join(path, "memory_vectors_map.json")
                faiss.write_index(self._index, index_path)
                with open(map_path, 'w') as f:
                    json.dump(self._id_map, f)
                logger.info(f"RAG: index saved ({len(self._id_map)} vectors)")
        except Exception as e:
            logger.debug(f"RAG save error: {e}")

    def load_index(self, path):
        if not _faiss_available:
            return False
        try:
            index_path = os.path.join(path, "memory_vectors.index")
            map_path = os.path.join(path, "memory_vectors_map.json")
            if not os.path.exists(index_path) or not os.path.exists(map_path):
                return False
            with self._lock:
                self._index = faiss.read_index(index_path)
                self._dim = self._index.d
                with open(map_path, 'r') as f:
                    self._id_map = json.load(f)
            logger.info(f"RAG: index loaded ({len(self._id_map)} vectors, dim={self._dim})")
            return True
        except Exception as e:
            logger.debug(f"RAG load error: {e}")
            return False

    def remove_by_memory_id(self, memory_id):
        with self._lock:
            if memory_id in self._id_map:
                idx = self._id_map.index(memory_id)
                self._id_map.pop(idx)
                if self._index is not None and self._index.ntotal > 0:
                    try:
                        self._index.remove_ids(np.array([idx]))
                    except Exception:
                        self._index = None
                        logger.debug("RAG: index cleared after removal, needs rebuild")
