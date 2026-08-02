"""
Persistent translation cache for pdf2zh 2.0.

Caches translation results in SQLite to avoid re-translating
identical text segments across sessions and documents.
Supports configurable max size, TTL, and manual clearing.
"""
import hashlib
import logging
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

    DEFAULT_DB_DIR = Path.home() / ".pdf2zh"
    DEFAULT_DB_NAME = "translation_cache.db"
    MAX_ENTRIES = 50000
    MAX_AGE_DAYS = 30

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_dir = self.DEFAULT_DB_DIR
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(db_dir / self.DEFAULT_DB_NAME)
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

    def get(self, text: str, lang_in: str, lang_out: str) -> Optional[str]:
        """Retrieve cached translation if available and fresh."""
        text_hash = self._hash(text)
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

    def set(self, text: str, lang_in: str, lang_out: str, translation: str):
        """Store a translation result in the cache."""
        text_hash = self._hash(text)
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
        return {"entries": count, "db_path": self.db_path, "max_entries": self.MAX_ENTRIES}

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
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def close(self):
        """Close the database connection."""
        with self._lock:
            self.conn.close()
