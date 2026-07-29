"""Phase 2 P5: Storage Runtime tests."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
_HAS_V3 = False
try:
    from pdf2zh.v3.storage import StorageTier, StorageStats, MemoryGraph, CacheGraph, PersistentGraph, StorageRuntime
    _HAS_V3 = True
except ImportError as e:
    print(f"Storage import error: {e}")

@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestMemoryGraph(unittest.TestCase):
    def test_put_get(self):
        mg = MemoryGraph(); mg.put("k1", "v1")
        self.assertEqual(mg.get("k1"), "v1")
    def test_get_missing(self):
        self.assertIsNone(MemoryGraph().get("x"))
    def test_contains(self):
        mg = MemoryGraph(); mg.put("k", "v")
        self.assertTrue(mg.contains("k")); self.assertFalse(mg.contains("x"))
    def test_remove(self):
        mg = MemoryGraph(); mg.put("k", "v")
        self.assertTrue(mg.remove("k")); self.assertFalse(mg.contains("k"))
    def test_remove_missing(self):
        self.assertFalse(MemoryGraph().remove("x"))
    def test_clear(self):
        mg = MemoryGraph(); mg.put("a", 1); mg.put("b", 2)
        mg.clear(); self.assertEqual(mg.size, 0)
    def test_size(self):
        mg = MemoryGraph()
        self.assertEqual(mg.size, 0); mg.put("a", 1)
        self.assertEqual(mg.size, 1)
    def test_hits(self):
        mg = MemoryGraph(); mg.put("k", "v")
        mg.get("k"); mg.get("k"); self.assertEqual(mg.hits, 2)
    def test_keys(self):
        mg = MemoryGraph(); mg.put("a", 1); mg.put("b", 2)
        self.assertEqual(set(mg.keys()), {"a", "b"})
@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestCacheGraph(unittest.TestCase):
    def test_put_get(self):
        cg = CacheGraph(); cg.put("k", "v")
        self.assertEqual(cg.get("k"), "v")
    def test_get_missing(self):
        self.assertIsNone(CacheGraph().get("x"))
    def test_contains(self):
        cg = CacheGraph(); cg.put("k", "v")
        self.assertTrue(cg.contains("k")); self.assertFalse(cg.contains("x"))
    def test_remove(self):
        cg = CacheGraph(); cg.put("k", "v")
        self.assertTrue(cg.remove("k")); self.assertFalse(cg.contains("k"))
    def test_clear(self):
        cg = CacheGraph(); cg.put("a", 1); cg.put("b", 2)
        cg.clear(); self.assertEqual(cg.size, 0)
    def test_eviction(self):
        cg = CacheGraph(max_size=2)
        cg.put("a", 1); cg.put("b", 2); cg.put("c", 3)
        self.assertFalse(cg.contains("a")); self.assertTrue(cg.contains("c"))
    def test_ttl_expiry(self):
        cg = CacheGraph(default_ttl=0.01)
        cg.put("k", "v")
        import time
        time.sleep(0.02)
        self.assertIsNone(cg.get("k"))
    def test_hits(self):
        cg = CacheGraph(); cg.put("k", "v")
        cg.get("k"); cg.get("k"); self.assertEqual(cg.hits, 2)
    def test_size_property(self):
        cg = CacheGraph()
        self.assertEqual(cg.size, 0); cg.put("a", 1); self.assertEqual(cg.size, 1)
    def test_custom_ttl(self):
        cg = CacheGraph(); cg.put("k", "v", ttl=9999)
        self.assertEqual(cg.get("k"), "v")
@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestPersistentGraph(unittest.TestCase):
    def setUp(self): self.pg = PersistentGraph()
    def tearDown(self): self.pg.close()
    def test_put_get(self):
        self.pg.put("k", {"data": "hello"})
        self.assertEqual(self.pg.get("k"), {"data": "hello"})
    def test_get_missing(self): self.assertIsNone(self.pg.get("x"))
    def test_contains(self):
        self.pg.put("k", "v")
        self.assertTrue(self.pg.contains("k")); self.assertFalse(self.pg.contains("x"))
    def test_remove(self):
        self.pg.put("k", "v"); self.assertTrue(self.pg.remove("k"))
        self.assertFalse(self.pg.contains("k"))
    def test_clear(self):
        self.pg.put("a", 1); self.pg.put("b", 2)
        self.pg.clear(); self.assertEqual(self.pg.size, 0)
    def test_size(self):
        self.assertEqual(self.pg.size, 0); self.pg.put("a", 1); self.assertEqual(self.pg.size, 1)
    def test_meta(self):
        self.pg.put_meta("doc1", "lang", "en")
        self.assertEqual(self.pg.get_meta("doc1", "lang"), "en")
    def test_meta_missing(self): self.assertIsNone(self.pg.get_meta("x", "y"))
    def test_list_keys(self):
        self.pg.put("a", 1); self.pg.put("b", 2)
        self.assertEqual(set(self.pg.list_keys()), {"a", "b"})
    def test_hits(self):
        self.pg.put("k", "v"); self.pg.get("k"); self.assertEqual(self.pg.hits, 1)
@unittest.skipIf(not _HAS_V3, "V3 not importable")
class TestStorageRuntime(unittest.TestCase):
    def setUp(self): self.sr = StorageRuntime()
    def tearDown(self): self.sr.close()
    def test_save_load(self):
        self.sr.save("k", {"data": "test"})
        self.assertEqual(self.sr.load("k"), {"data": "test"})
    def test_load_missing(self): self.assertIsNone(self.sr.load("x"))
    def test_contains(self):
        self.sr.save("k", "v"); self.assertTrue(self.sr.contains("k"))
        self.assertFalse(self.sr.contains("x"))
    def test_remove(self):
        self.sr.save("k", "v"); self.assertTrue(self.sr.remove("k"))
        self.assertFalse(self.sr.contains("k"))
    def test_clear(self):
        self.sr.save("a", 1); self.sr.save("b", 2)
        self.sr.clear(); self.assertFalse(self.sr.contains("a"))
    def test_clear_memory(self):
        self.sr.save("k", "v"); self.sr.clear_memory()
        self.assertEqual(self.sr.load("k"), "v")
    def test_clear_cache(self):
        self.sr.save("k", "v"); self.sr.clear_cache()
        self.assertEqual(self.sr.load("k"), "v")
    def test_warmup(self):
        self.sr.save("a", 1); self.sr.save("b", 2)
        self.sr.clear_memory(); self.sr.clear_cache()
        self.assertEqual(self.sr.warmup(["a", "b"]), 2)
        self.assertTrue(self.sr.memory.contains("a"))
    def test_stats(self):
        self.sr.save("k", "v"); self.sr.load("k")
        self.assertGreater(self.sr.stats.total_latency_ms, 0)
    def test_stats_to_dict(self):
        d = StorageStats().to_dict(); self.assertIn("memory_entries", d)
    def test_storage_tier_enum(self):
        self.assertEqual(StorageTier.MEMORY.value, "memory")
    def test_load_cascade(self):
        self.sr.save("k", "v")
        self.sr.clear_memory(); self.assertEqual(self.sr.load("k"), "v")
        self.sr.clear_cache(); self.assertEqual(self.sr.load("k"), "v")

if __name__ == "__main__":
    unittest.main(verbosity=2)