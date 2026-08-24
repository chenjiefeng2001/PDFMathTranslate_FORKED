"""
Persistent translation cache for pdf2zh 2.0.

Caches translation results in SQLite to avoid re-translating
identical text segments across sessions and documents.
Supports configurable max size, TTL, and manual clearing.

Default location: ``~/.cache/pdf2zh/translation_cache.db`` —— 与
``pdf2zh/cache.py``（legacy peewee 缓存）及 pdf2zh_next 的
``~/.cache/pdf2zh_next`` 同根目录，保证 CLI / GUI 后台 / 服务
在任何部署形态下都能找到同一个缓存库。早期版本存放在
``~/.pdf2zh/translation_cache.db``，首次使用时会自动迁移。
可用环境变量 ``PDF2ZH_CACHE_DIR`` 覆盖基础目录。
"""

import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TranslationCache:
    """Persistent SQLite-backed translation cache.

    Usage:
        cache = TranslationCache()
        cached = cache.get("Hello", "en", "zh")
        if cached is None:
            result = translate("Hello")
            cache.set("Hello", "en", "zh", result)
    """

    DEFAULT_DB_DIR = Path.home() / ".cache" / "pdf2zh"
    DEFAULT_DB_NAME = "translation_cache.db"
    LEGACY_DB_DIR = Path.home() / ".pdf2zh"
    MAX_ENTRIES = 50000
    MAX_AGE_DAYS = 30

    @classmethod
    def resolve_default_db_path(cls) -> str:
        """Resolve（并准备）默认缓存库路径，含旧位置自动迁移。

        优先级：``PDF2ZH_CACHE_DIR`` 环境变量 > ``~/.cache/pdf2zh``。
        环境变量是绝对权威（不做迁移）；默认路径下若旧版库
        ``~/.pdf2zh/translation_cache.db`` 存在而新位置不存在，
        则先迁移再使用 —— 保证升级后后台（服务/GUI）仍能找到历史缓存。
        """
        target_full = None
        env_dir = (os.environ.get("PDF2ZH_CACHE_DIR") or "").strip()
        if env_dir:
            db_dir = Path(env_dir)
            try:
                db_dir.mkdir(parents=True, exist_ok=True)
            except OSError as mk_err:
                logger.warning(
                    "PDF2ZH_CACHE_DIR %s not usable (%s); falling back to default",
                    db_dir,
                    str(mk_err)[:120],
                )
                db_dir = cls.DEFAULT_DB_DIR
                target_full = db_dir / cls.DEFAULT_DB_NAME
            else:
                return str(db_dir / cls.DEFAULT_DB_NAME)
        else:
            db_dir = cls.DEFAULT_DB_DIR
            try:
                db_dir.mkdir(parents=True, exist_ok=True)
            except OSError as mk_err:
                logger.warning(
                    "TranslationCache dir %s not writable (%s); skipping migration",
                    db_dir,
                    mk_err,
                )
            target_full = db_dir / cls.DEFAULT_DB_NAME
            legacy = cls.LEGACY_DB_DIR / cls.DEFAULT_DB_NAME
            if legacy != target_full and legacy.exists() and not target_full.exists():
                try:
                    shutil.move(str(legacy), str(target_full))
                    logger.info(
                        "TranslationCache migrated %s -> %s",
                        legacy,
                        target_full,
                    )
                except OSError as mv_err:
                    # 迁移失败（例如旧实例仍占用库文件）时回退到旧路径，
                    # 保证同代进程看到的是同一个库，而不是新旧两处各一份。
                    logger.warning(
                        "TranslationCache migration failed (%s); "
                        "falling back to legacy path %s",
                        str(mv_err)[:120],
                        legacy,
                    )
                    return str(legacy)
        return str(target_full)

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = self.resolve_default_db_path()
        self.db_path = db_path
        # TranslateConverter invokes get()/set() from a ThreadPoolExecutor, so
        # every public method must serialize access to the single connection.
        # Sharing one sqlite3.Connection across threads concurrently raises
        # sqlite3.InterfaceError: "bad parameter or other API misuse".
        self._lock = threading.Lock()
        # timeout + busy_timeout make concurrent writes from multiple processes
        # (parallel page workers) wait for the SQLite file lock instead of
        # failing immediately with "database is locked".
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self._init_db()
            self._enforce_limits()
        logger.debug("TranslationCache initialized at %s", db_path)

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS translations (
                text_hash TEXT PRIMARY KEY,
                source_text TEXT,
                lang_in TEXT,
                lang_out TEXT,
                translated_text TEXT,
                created_at REAL
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_translations_lookup
            ON translations (text_hash, lang_in, lang_out)
        """)
        self.conn.commit()

    def get(
        self, text: str, lang_in: str, lang_out: str, variant: str = ""
    ) -> Optional[str]:
        """Retrieve cached translation if available and fresh.

        ``variant``（如段落字体指纹）参与键计算：同文本、不同字体形态的段落
        分离缓存，避免复用错字体宽度假设下的译文（多字体段落错位优化，V1.19）。
        """
        text_hash = self._hash(text, variant)
        with self._lock:
            cursor = self.conn.execute(
                "SELECT translated_text, created_at FROM translations "
                "WHERE text_hash=? AND lang_in=? AND lang_out=?",
                (text_hash, lang_in, lang_out),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            translated, created_at = row
            # Check TTL
            if time.time() - created_at > self.MAX_AGE_DAYS * 86400:
                self.conn.execute(
                    "DELETE FROM translations WHERE text_hash=? AND lang_in=? AND lang_out=?",
                    (text_hash, lang_in, lang_out),
                )
                self.conn.commit()
                return None
            return translated

    def set(
        self,
        text: str,
        lang_in: str,
        lang_out: str,
        translation: str,
        variant: str = "",
    ):
        """Store a translation result in the cache.

        ``variant`` 与 :meth:`get` 的语义一致（默认空串 = 无区分）。
        """
        text_hash = self._hash(text, variant)
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO translations "
                "(text_hash, source_text, lang_in, lang_out, translated_text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (text_hash, text[:500], lang_in, lang_out, translation, time.time()),
            )
            self.conn.commit()

    def clear(self):
        """Clear all cached translations."""
        with self._lock:
            self.conn.execute("DELETE FROM translations")
            self.conn.commit()
        logger.info("Translation cache cleared")

    def stats(self) -> dict:
        """Return cache statistics."""
        with self._lock:
            cursor = self.conn.execute("SELECT COUNT(*) FROM translations")
            count = cursor.fetchone()[0]
        return {
            "entries": count,
            "db_path": self.db_path,
            "max_entries": self.MAX_ENTRIES,
        }

    def _enforce_limits(self):
        """Remove oldest entries when over capacity."""
        cursor = self.conn.execute("SELECT COUNT(*) FROM translations")
        count = cursor.fetchone()[0]
        if count > self.MAX_ENTRIES:
            excess = count - self.MAX_ENTRIES
            self.conn.execute(
                "DELETE FROM translations WHERE rowid IN ("
                "SELECT rowid FROM translations ORDER BY created_at ASC LIMIT ?"
                ")",
                (excess,),
            )
            self.conn.commit()
            logger.info("Trimmed %d oldest entries from translation cache", excess)

    @staticmethod
    def _hash(text: str, variant: str = "") -> str:
        return hashlib.sha256(f"{variant}\x00{text}".encode("utf-8")).hexdigest()

    def close(self):
        """Close the database connection."""
        with self._lock:
            self.conn.close()
