"""
Tests for TranslationCache (pdf2zh 2.0 L3 - translation cache).
"""
import os
import tempfile
import pytest
from pdf2zh.translation_cache import TranslationCache


@pytest.fixture
def cache():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    tc = TranslationCache(db_path=db_path)
    yield tc
    tc.close()
    if os.path.exists(db_path):
        os.unlink(db_path)


class TestTranslationCache:
    def test_get_returns_none_for_uncached(self, cache):
        assert cache.get("hello", "en", "zh") is None

    def test_set_and_get(self, cache):
        cache.set("hello", "en", "zh", "你好")
        assert cache.get("hello", "en", "zh") == "你好"

    def test_update_replaces_existing(self, cache):
        cache.set("hello", "en", "zh", "hi")
        cache.set("hello", "en", "zh", "hello again")
        assert cache.get("hello", "en", "zh") == "hello again"

    def test_multiple_entries(self, cache):
        for text, li, lo, tr in [("a", "en", "zh", "1"), ("b", "en", "zh", "2")]:
            cache.set(text, li, lo, tr)
        assert cache.get("a", "en", "zh") == "1"
        assert cache.get("b", "en", "zh") == "2"

    def test_clear_removes_all(self, cache):
        cache.set("hello", "en", "zh", "hi")
        cache.clear()
        assert cache.get("hello", "en", "zh") is None

    def test_stats(self, cache):
        assert cache.stats()["entries"] == 0
        cache.set("hello", "en", "zh", "hi")
        assert cache.stats()["entries"] == 1

    def test_empty_text(self, cache):
        cache.set("", "en", "zh", "")
        assert cache.get("", "en", "zh") == ""

    def test_special_characters(self, cache):
        s = "héllo wörld !@#$"
        cache.set(s, "en", "zh", s)
        assert cache.get(s, "en", "zh") == s

    def test_unicode_roundtrip(self, cache):
        cache.set("你好", "zh", "en", "Hello")
        assert cache.get("你好", "zh", "en") == "Hello"

    def test_hash_consistency(self, cache):
        assert cache._hash("hello") == cache._hash("hello")

    def test_close_then_reopen(self, cache):
        db_path = cache.db_path
        cache.set("hello", "en", "zh", "hi")
        cache.close()
        tc2 = TranslationCache(db_path=db_path)
        assert tc2.get("hello", "en", "zh") == "hi"
        tc2.close()
