"""Module: Storage Runtime — three-tier storage.
Memory -> Cache -> Persistent three-tier graph storage runtime.

Provides:
  - MemoryGraph: In-memory dict-based graph storage (fastest)
  - CacheGraph: LRU cache with TTL (bounded memory)
  - PersistentGraph: SQLite-backed persistent storage (durable)
  - StorageRuntime: Unified facade for all three tiers
"""

from __future__ import annotations
import json, logging, os, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class StorageTier(Enum):
    MEMORY = "memory"
    CACHE = "cache"
    PERSISTENT = "persistent"


@dataclass
class StorageStats:
    memory_entries: int = 0
    cache_entries: int = 0
    persistent_entries: int = 0
    memory_hits: int = 0
    cache_hits: int = 0
    persistent_hits: int = 0
    total_misses: int = 0
    total_latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "memory_entries": self.memory_entries,
            "cache_entries": self.cache_entries,
            "persistent_entries": self.persistent_entries,
            "memory_hits": self.memory_hits,
            "cache_hits": self.cache_hits,
            "persistent_hits": self.persistent_hits,
            "total_misses": self.total_misses,
            "total_latency_ms": round(self.total_latency_ms, 2),
        }


class MemoryGraph:
    """Tier 1: In-memory dict-based graph storage."""

    def __init__(self):
        self._store: Dict[str, Any] = {}
        self._hits = 0

    def put(self, key: str, value: Any) -> None:
        self._store[key] = value

    def get(self, key: str) -> Optional[Any]:
        val = self._store.get(key)
        if val is not None:
            self._hits += 1
        return val

    def remove(self, key: str) -> bool:
        return self._store.pop(key, None) is not None

    def contains(self, key: str) -> bool:
        return key in self._store

    def clear(self) -> None:
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)

    @property
    def hits(self) -> int:
        return self._hits

    def keys(self) -> List[str]:
        return list(self._store.keys())


@dataclass
class CacheEntry:
    key: str = ""
    value: Any = None
    ttl: float = 300.0
    created_at: float = 0.0
    access_count: int = 0

    @property
    def is_expired(self) -> bool:
        if self.ttl <= 0:
            return False
        return time.time() - self.created_at > self.ttl


class CacheGraph:
    """Tier 2: LRU cache with TTL support."""

    def __init__(self, max_size: int = 1000, default_ttl: float = 300.0):
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._store: Dict[str, CacheEntry] = {}
        self._access_order: List[str] = []
        self._hits = 0

    def put(self, key: str, value: Any, ttl: float | None = None) -> None:
        self._evict_expired()
        entry = CacheEntry(
            key=key,
            value=value,
            ttl=ttl if ttl is not None else self._default_ttl,
            created_at=time.time(),
            access_count=1,
        )
        self._store[key] = entry
        self._touch(key)
        if len(self._store) > self._max_size:
            self._evict_one()

    def get(self, key: str):
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.is_expired:
            self._store.pop(key, None)
            self._remove_order(key)
            return None
        entry.access_count += 1
        self._hits += 1
        self._touch(key)
        return entry.value

    def contains(self, key: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return False
        if entry.is_expired:
            self._store.pop(key, None)
            self._remove_order(key)
            return False
        return True

    def remove(self, key: str) -> bool:
        self._remove_order(key)
        return self._store.pop(key, None) is not None

    def clear(self) -> None:
        self._store.clear()
        self._access_order.clear()

    def _touch(self, key: str) -> None:
        self._remove_order(key)
        self._access_order.append(key)

    def _remove_order(self, key: str) -> None:
        if key in self._access_order:
            self._access_order.remove(key)

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [
            k
            for k, e in self._store.items()
            if e.ttl > 0 and now - e.created_at > e.ttl
        ]
        for k in expired:
            self._store.pop(k, None)
            self._remove_order(k)

    def _evict_one(self) -> None:
        if self._access_order:
            oldest = self._access_order.pop(0)
            self._store.pop(oldest, None)

    @property
    def size(self) -> int:
        return len(self._store)

    @property
    def hits(self) -> int:
        return self._hits


class PersistentGraph:
    """Tier 3: SQLite-backed persistent storage."""

    def __init__(self, db_path=None):
        self._db_path = db_path or ":memory:"
        self._conn = None
        self._lock = threading.Lock()
        self._hits = 0
        self._init_db()

    def _get_conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS storage (key TEXT PRIMARY KEY, value TEXT NOT NULL, value_type TEXT NOT NULL DEFAULT 'json', created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS storage_meta (key TEXT, meta_key TEXT, meta_value TEXT, PRIMARY KEY(key, meta_key))"
            )
            conn.commit()

    def put(self, key: str, value: Any) -> None:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO storage (key, value, value_type, created_at, updated_at) VALUES (?, ?, 'json', COALESCE((SELECT created_at FROM storage WHERE key=?), ?), ?)",
                (key, serialized, key, now, now),
            )
            conn.commit()

    def get(self, key):
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT value, value_type FROM storage WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        self._hits += 1
        return json.loads(row["value"])

    def contains(self, key: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            return (
                conn.execute("SELECT 1 FROM storage WHERE key=?", (key,)).fetchone()
                is not None
            )

    def remove(self, key: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute("DELETE FROM storage WHERE key=?", (key,))
            conn.commit()
        return cur.rowcount > 0

    def clear(self) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM storage")
            conn.execute("DELETE FROM storage_meta")
            conn.commit()

    def put_meta(self, key: str, mk: str, mv: str) -> None:
        with self._lock:
            self._get_conn().execute(
                "INSERT OR REPLACE INTO storage_meta (key, meta_key, meta_value) VALUES (?, ?, ?)",
                (key, mk, mv),
            )
            self._get_conn().commit()

    def get_meta(self, key: str, mk: str):
        with self._lock:
            row = (
                self._get_conn()
                .execute(
                    "SELECT meta_value FROM storage_meta WHERE key=? AND meta_key=?",
                    (key, mk),
                )
                .fetchone()
            )
        return row["meta_value"] if row else None

    def list_keys(self):
        with self._lock:
            rows = (
                self._get_conn()
                .execute("SELECT key FROM storage ORDER BY updated_at DESC")
                .fetchall()
            )
        return [r["key"] for r in rows]

    @property
    def size(self) -> int:
        with self._lock:
            row = (
                self._get_conn()
                .execute("SELECT COUNT(*) as cnt FROM storage")
                .fetchone()
            )
        return row["cnt"] if row else 0

    @property
    def hits(self) -> int:
        return self._hits

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


class StorageRuntime:
    """Unified three-tier storage runtime. Lookup: Memory -> Cache -> Persistent."""

    def __init__(self, memory_graph=None, cache_graph=None, persistent_graph=None):
        self.memory = memory_graph or MemoryGraph()
        self.cache = cache_graph or CacheGraph()
        self.persistent = persistent_graph or PersistentGraph()
        self._stats = StorageStats()

    def save(self, key: str, value: Any) -> None:
        start = time.time()
        self.memory.put(key, value)
        self.cache.put(key, value)
        self.persistent.put(key, value)
        self._update_stats(start)

    def load(self, key):
        start = time.time()
        val = self.memory.get(key)
        if val is not None:
            self._stats.memory_hits += 1
            self._update_stats(start)
            return val
        val = self.cache.get(key)
        if val is not None:
            self._stats.cache_hits += 1
            self.memory.put(key, val)
            self._update_stats(start)
            return val
        val = self.persistent.get(key)
        if val is not None:
            self._stats.persistent_hits += 1
            self.memory.put(key, val)
            self.cache.put(key, val)
            self._update_stats(start)
            return val
        self._stats.total_misses += 1
        self._update_stats(start)
        return None

    def contains(self, key: str) -> bool:
        return (
            self.memory.contains(key)
            or self.cache.contains(key)
            or self.persistent.contains(key)
        )

    def remove(self, key: str) -> bool:
        m = self.memory.remove(key)
        c = self.cache.remove(key)
        p = self.persistent.remove(key)
        return m or c or p

    def clear(self) -> None:
        self.memory.clear()
        self.cache.clear()
        self.persistent.clear()

    def clear_memory(self) -> None:
        self.memory.clear()

    def clear_cache(self) -> None:
        self.cache.clear()

    def clear_persistent(self) -> None:
        self.persistent.clear()

    def warmup(self, keys):
        count = 0
        for k in keys:
            if not self.memory.contains(k):
                val = self.persistent.get(k)
                if val is not None:
                    self.cache.put(k, val)
                    self.memory.put(k, val)
                    count += 1
        return count

    @property
    def stats(self) -> StorageStats:
        self._stats.memory_entries = self.memory.size
        self._stats.cache_entries = self.cache.size
        self._stats.persistent_entries = self.persistent.size
        return self._stats

    def _update_stats(self, start: float) -> None:
        self._stats.total_latency_ms += (time.time() - start) * 1000

    def close(self) -> None:
        self.persistent.close()


__all__ = [
    "StorageTier",
    "StorageStats",
    "MemoryGraph",
    "CacheGraph",
    "PersistentGraph",
    "StorageRuntime",
]
