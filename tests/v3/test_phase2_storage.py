"""Tests for V3 Storage Runtime (Module: storage.py)."""

import os, time, tempfile, pytest
from pdf2zh.v3.storage import (
    StorageTier,
    StorageStats,
    MemoryGraph,
    CacheGraph,
    PersistentGraph,
    StorageRuntime,
)


class TestMemoryGraph:
    def test_put_and_get(self):
        mg = MemoryGraph()
        mg.put("k1", "v1")
        assert mg.get("k1") == "v1"

    def test_get_missing(self):
        mg = MemoryGraph()
        assert mg.get("missing") is None

    def test_remove(self):
        mg = MemoryGraph()
        mg.put("k1", "v1")
        assert mg.remove("k1") is True
        assert mg.get("k1") is None

    def test_remove_missing(self):
        mg = MemoryGraph()
        assert mg.remove("missing") is False

    def test_contains(self):
        mg = MemoryGraph()
        mg.put("k1", "v1")
        assert mg.contains("k1") is True
        assert mg.contains("missing") is False

    def test_clear(self):
        mg = MemoryGraph()
        mg.put("k1", "v1")
        mg.put("k2", "v2")
        mg.clear()
        assert mg.size == 0
        assert mg.hits == 0

    def test_size(self):
        mg = MemoryGraph()
        mg.put("k1", "v1")
        mg.put("k2", "v2")
        assert mg.size == 2

    def test_hits(self):
        mg = MemoryGraph()
        mg.put("k1", "v1")
        mg.get("k1")  # hit
        assert mg.hits == 1

    def test_keys(self):
        mg = MemoryGraph()
        mg.put("a", 1)
        mg.put("b", 2)
        assert set(mg.keys()) == {"a", "b"}

    def test_overwrite(self):
        mg = MemoryGraph()
        mg.put("k1", "v1")
        mg.put("k1", "v2")
        assert mg.get("k1") == "v2"


class TestCacheGraph:
    def test_put_and_get(self):
        cg = CacheGraph(max_size=100, default_ttl=300.0)
        cg.put("k1", "v1")
        assert cg.get("k1") == "v1"

    def test_get_missing(self):
        cg = CacheGraph()
        assert cg.get("missing") is None

    def test_ttl_expiry(self):
        cg = CacheGraph(default_ttl=0.01)
        cg.put("k1", "v1")
        time.sleep(0.02)
        assert cg.get("k1") is None

    def test_contains(self):
        cg = CacheGraph()
        cg.put("k1", "v1")
        assert cg.contains("k1") is True

    def test_remove(self):
        cg = CacheGraph()
        cg.put("k1", "v1")
        assert cg.remove("k1") is True
        assert cg.contains("k1") is False

    def test_clear(self):
        cg = CacheGraph()
        cg.put("k1", "v1")
        cg.put("k2", "v2")
        cg.clear()
        assert cg.size == 0

    def test_lru_eviction(self):
        cg = CacheGraph(max_size=2, default_ttl=300.0)
        cg.put("a", 1)
        cg.put("b", 2)
        cg.put("c", 3)
        assert cg.get("a") is None
        assert cg.get("b") == 2
        assert cg.get("c") == 3

    def test_size(self):
        cg = CacheGraph(max_size=10)
        cg.put("a", 1)
        cg.put("b", 2)
        assert cg.size == 2

    def test_hits(self):
        cg = CacheGraph()
        cg.put("k1", "v1")
        cg.get("k1")
        assert cg.hits == 1

    def test_custom_ttl(self):
        cg = CacheGraph(default_ttl=300.0)
        cg.put("k1", "v1", ttl=0.01)
        assert cg.get("k1") == "v1"
        time.sleep(0.02)
        assert cg.get("k1") is None


class TestPersistentGraph:
    @pytest.fixture
    def db_path(self):
        tmp = tempfile.mktemp(suffix=".db")
        yield tmp
        if os.path.exists(tmp):
            os.remove(tmp)

    def test_put_and_get(self, db_path):
        pg = PersistentGraph(db_path)
        pg.put("k1", "v1")
        assert pg.get("k1") == "v1"
        pg.close()

    def test_get_missing(self, db_path):
        pg = PersistentGraph(db_path)
        assert pg.get("missing") is None
        pg.close()

    def test_contains(self, db_path):
        pg = PersistentGraph(db_path)
        pg.put("k1", "v1")
        assert pg.contains("k1") is True
        pg.close()

    def test_remove(self, db_path):
        pg = PersistentGraph(db_path)
        pg.put("k1", "v1")
        assert pg.remove("k1") is True
        assert pg.contains("k1") is False
        pg.close()

    def test_clear(self, db_path):
        pg = PersistentGraph(db_path)
        pg.put("k1", "v1")
        pg.put("k2", "v2")
        pg.clear()
        assert pg.size == 0
        pg.close()

    def test_persistence(self, db_path):
        pg = PersistentGraph(db_path)
        pg.put("k1", "v1")
        pg.close()
        pg2 = PersistentGraph(db_path)
        assert pg2.get("k1") == "v1"
        pg2.close()

    def test_list_keys(self, db_path):
        pg = PersistentGraph(db_path)
        pg.put("a", 1)
        pg.put("b", 2)
        assert sorted(pg.list_keys()) == ["a", "b"]
        pg.close()

    def test_meta(self, db_path):
        pg = PersistentGraph(db_path)
        pg.put_meta("k1", "lang", "zh")
        assert pg.get_meta("k1", "lang") == "zh"
        pg.close()

    def test_size(self, db_path):
        pg = PersistentGraph(db_path)
        pg.put("a", 1)
        pg.put("b", 2)
        assert pg.size == 2
        pg.close()


class TestStorageRuntime:
    def test_load_miss(self):
        rt = StorageRuntime()
        assert rt.load("missing") is None

    def test_save_and_load(self):
        rt = StorageRuntime()
        rt.save("k1", "v1")
        assert rt.load("k1") == "v1"

    def test_contains(self):
        rt = StorageRuntime()
        rt.save("k1", 42)
        assert rt.contains("k1") is True

    def test_remove(self):
        rt = StorageRuntime()
        rt.save("k1", "v1")
        assert rt.remove("k1") is True
        assert rt.contains("k1") is False

    def test_clear(self):
        rt = StorageRuntime()
        rt.save("k1", "v1")
        rt.save("k2", "v2")
        rt.clear()
        assert rt.contains("k1") is False

    def test_clear_tier(self):
        rt = StorageRuntime()
        rt.save("k1", "v1")
        rt.clear_memory()
        assert rt.memory.contains("k1") is False
        assert rt.cache.contains("k1") is True
        rt.clear_cache()
        assert rt.cache.contains("k1") is False
        assert rt.persistent.contains("k1") is True

    def test_warmup(self):
        rt = StorageRuntime()
        rt.save("k1", "v1")
        rt.clear_memory()
        rt.clear_cache()
        n = rt.warmup(["k1"])
        assert n == 1
        assert rt.memory.contains("k1") is True
        assert rt.cache.contains("k1") is True

    def test_cache_promotion(self):
        rt = StorageRuntime()
        rt.save("k1", "v_persist")
        rt.clear_memory()
        rt.clear_cache()
        val = rt.load("k1")
        assert val == "v_persist"
        assert rt.memory.contains("k1") is True

    def test_stats(self):
        rt = StorageRuntime()
        s = rt.stats
        assert isinstance(s.memory_entries, int)
        rt.save("k1", "v1")
        s2 = rt.stats
        assert s2.memory_entries >= 1

    def test_close(self):
        rt = StorageRuntime()
        rt.save("k1", "v1")
        rt.close()
