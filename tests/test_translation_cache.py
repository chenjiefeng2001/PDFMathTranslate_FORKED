"""
Tests for TranslationCache (pdf2zh 2.0 L3 - translation cache).
"""

import os
import tempfile
from pathlib import Path

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

    def test_variant_isolates_entries(self, cache):
        # V1.19: 字体 variant 参与键——同文本、不同字体形态的段落不得互相命中
        cache.set("hello", "en", "zh", "你好", variant="|fonts:A|B")
        assert cache.get("hello", "en", "zh") is None
        assert cache.get("hello", "en", "zh", variant="|fonts:A|B") == "你好"
        cache.set("hello", "en", "zh", "您好", variant="|fonts:C")
        assert cache.get("hello", "en", "zh", variant="|fonts:C") == "您好"
        assert cache.get("hello", "en", "zh", variant="|fonts:A|B") == "你好"

    def test_variant_default_equals_no_variant(self, cache):
        # 空 variant 与缺省一致：不影响存量键
        cache.set("hello", "en", "zh", "hi", variant="")
        cache.set("world", "en", "zh", "世界")
        assert cache.get("hello", "en", "zh") == "hi"
        assert cache.get("world", "en", "zh") == "世界"
        assert cache._hash("hello", "") == cache._hash("hello")

    def test_close_then_reopen(self, cache):
        db_path = cache.db_path
        cache.set("hello", "en", "zh", "hi")
        cache.close()
        tc2 = TranslationCache(db_path=db_path)
        assert tc2.get("hello", "en", "zh") == "hi"
        tc2.close()

    def test_default_dir_is_canonical_cache_dir(self, tmp_path, monkeypatch):
        # 默认库必须与 legacy cache.py 同目录：~/.cache/pdf2zh
        monkeypatch.setattr(
            TranslationCache, "DEFAULT_DB_DIR", tmp_path / ".cache" / "pdf2zh"
        )
        monkeypatch.setattr(TranslationCache, "LEGACY_DB_DIR", tmp_path / "nope")
        monkeypatch.delenv("PDF2ZH_CACHE_DIR", raising=False)
        assert TranslationCache.resolve_default_db_path() == str(
            tmp_path / ".cache" / "pdf2zh" / "translation_cache.db"
        )
        tc = TranslationCache()
        try:
            assert Path(tc.db_path).parent == tmp_path / ".cache" / "pdf2zh"
        finally:
            tc.close()

    def test_env_override_cache_dir(self, tmp_path, monkeypatch):
        # 环境变量是绝对权威：指定位置即为库位置，且不触发旧库迁移
        legacy_dir = tmp_path / "legacy_pdf2zh"
        legacy_dir.mkdir()
        legacy_db = legacy_dir / "translation_cache.db"
        legacy_db.write_bytes(b"legacy")
        monkeypatch.setattr(TranslationCache, "LEGACY_DB_DIR", legacy_dir)
        custom = tmp_path / "custom-cache"
        monkeypatch.setenv("PDF2ZH_CACHE_DIR", str(custom))
        path = TranslationCache.resolve_default_db_path()
        assert path == str(custom / "translation_cache.db")
        assert legacy_db.exists()  # 旧库未被迁移/移动
        assert custom.exists()  # 新目录已被创建

    def test_auto_migration_from_legacy_dir(self, tmp_path):
        # 旧位置 ~/.pdf2zh/translation_cache.db 存在时自动迁移到新默认位置
        legacy_dir = tmp_path / "legacy_pdf2zh"
        legacy_dir.mkdir()
        legacy_db = legacy_dir / "translation_cache.db"
        legacy_db.write_bytes(b"legacy")
        new_dir = tmp_path / ".cache" / "pdf2zh"
        TranslationCache.DEFAULT_DB_DIR = new_dir
        TranslationCache.LEGACY_DB_DIR = legacy_dir
        try:
            target = TranslationCache.resolve_default_db_path()
            assert not legacy_db.exists()  # 已迁移
            assert Path(target) == new_dir / "translation_cache.db"
            assert Path(target).read_bytes() == b"legacy"
        finally:
            TranslationCache.DEFAULT_DB_DIR = Path.home() / ".cache" / "pdf2zh"
            TranslationCache.LEGACY_DB_DIR = Path.home() / ".pdf2zh"
