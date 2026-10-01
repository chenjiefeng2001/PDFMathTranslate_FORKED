"""CacheBackend — 统一缓存抽象。

支持：
    - MemoryCache (LRU)
    - DiskCache (SQLite)
    - RemoteCache (Redis)

用法：
    cache = MemoryCache(max_size=10000)
    cache.set("key", {"translation": "..."})
    value = cache.get("key")
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class CacheBackend(ABC):
    """缓存后端抽象。"""

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """获取缓存。"""

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """设置缓存。"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除缓存。"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """检查缓存是否存在。"""

    @abstractmethod
    def clear(self) -> None:
        """清空缓存。"""

    @abstractmethod
    def size(self) -> int:
        """缓存大小。"""

    def make_key(self, *args, **kwargs) -> str:
        """生成缓存 key。"""
        data = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()


class MemoryCache(CacheBackend):
    """内存缓存 (LRU)。"""

    def __init__(self, max_size: int = 10000) -> None:
        self._max_size = max(1, int(max_size))
        self._cache: OrderedDict[str, Dict] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            entry = self._cache[key]
            if entry.get("expires", 0) > time.time():
                self._cache.move_to_end(key)
                self._hits += 1
                return entry["value"]
            else:
                del self._cache[key]
        self._misses += 1
        return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        expires = time.time() + ttl if ttl else float("inf")
        self._cache[key] = {"value": value, "expires": expires}
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self._cache

    def clear(self) -> None:
        self._cache.clear()

    def size(self) -> int:
        return len(self._cache)

    @property
    def hit_ratio(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0


class DiskCache(CacheBackend):
    """磁盘缓存 (JSON 文件)。"""

    def __init__(self, cache_dir: str = ".cache", max_size: int = 10000) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._max_size = max(1, int(max_size))
        self._index_file = self._cache_dir / "_index.json"
        self._index: Dict[str, str] = self._load_index()

    def _load_index(self) -> Dict[str, str]:
        if self._index_file.exists():
            try:
                with open(self._index_file, "r", encoding="utf-8") as f:
                    value = json.load(f)
                if isinstance(value, dict):
                    return {
                        str(key): str(path)
                        for key, path in value.items()
                        if isinstance(path, str)
                    }
            except (OSError, TypeError, ValueError):
                pass
        return {}

    def _save_index(self) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix="._index_", suffix=".tmp", dir=str(self._cache_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._index, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self._index_file)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _key_to_path(self, key: str) -> Path:
        safe_key = hashlib.sha256(key.encode()).hexdigest()
        return self._cache_dir / f"{safe_key}.json"

    def get(self, key: str) -> Optional[Any]:
        if key not in self._index:
            return None
        path = self._key_to_path(key)
        if not path.exists():
            self._index.pop(key, None)
            try:
                self._save_index()
            except OSError:
                pass
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("expires", 0) < time.time():
                self.delete(key)
                return None
            return data.get("value")
        except Exception:
            return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        path = self._key_to_path(key)
        expires = time.time() + ttl if ttl else float("inf")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"value": value, "expires": expires}, f, ensure_ascii=False)
            self._index[key] = str(path)
            self._save_index()
            while len(self._index) > self._max_size:
                oldest = next(iter(self._index))
                self.delete(oldest)
        except Exception:
            pass

    def delete(self, key: str) -> None:
        path = self._key_to_path(key)
        if path.exists():
            path.unlink()
        self._index.pop(key, None)
        self._save_index()

    def exists(self, key: str) -> bool:
        if key not in self._index:
            return False
        path = self._key_to_path(key)
        if not path.exists():
            self._index.pop(key, None)
            try:
                self._save_index()
            except OSError:
                pass
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("expires", 0) < time.time():
                self.delete(key)
                return False
        except (OSError, TypeError, ValueError):
            return False
        return True

    def clear(self) -> None:
        for path in self._cache_dir.glob("*.json"):
            if path.name != "_index.json":
                path.unlink()
        self._index.clear()
        self._save_index()

    def size(self) -> int:
        return len(self._index)


class TieredCache(CacheBackend):
    """分层缓存。"""

    def __init__(
        self,
        memory_cache: Optional[MemoryCache] = None,
        disk_cache: Optional[DiskCache] = None,
    ) -> None:
        self._memory = memory_cache or MemoryCache()
        self._disk = disk_cache or DiskCache()

    def get(self, key: str) -> Optional[Any]:
        # 先查内存
        value = self._memory.get(key)
        if value is not None:
            return value
        # 再查磁盘
        value = self._disk.get(key)
        if value is not None:
            # 回填内存
            self._memory.set(key, value)
        return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._memory.set(key, value, ttl)
        self._disk.set(key, value, ttl)

    def delete(self, key: str) -> None:
        self._memory.delete(key)
        self._disk.delete(key)

    def exists(self, key: str) -> bool:
        return self._memory.exists(key) or self._disk.exists(key)

    def clear(self) -> None:
        self._memory.clear()
        self._disk.clear()

    def size(self) -> int:
        return self._disk.size()
