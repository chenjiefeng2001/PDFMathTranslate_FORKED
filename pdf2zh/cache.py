import logging
import os
import json
import hashlib
from peewee import Model, SqliteDatabase, AutoField, CharField, TextField, SQL
from typing import Optional

# we don't init the database here
db = SqliteDatabase(None)
logger = logging.getLogger(__name__)


class _TranslationCache(Model):
    id = AutoField()
    translate_engine = CharField(max_length=20)
    translate_engine_params = TextField()
    original_text = TextField()
    translation = TextField()

    class Meta:
        database = db
        constraints = [SQL("""
            UNIQUE (
                translate_engine,
                translate_engine_params,
                original_text
                )
            ON CONFLICT REPLACE
            """)]


class _FileCache(Model):
    """文件级别的翻译缓存：记录已翻译完成的文件的 hash -> 输出文件路径映射"""
    id = AutoField()
    file_hash = CharField(max_length=64, unique=True)
    file_name = CharField(max_length=512)
    lang_in = CharField(max_length=10)
    lang_out = CharField(max_length=10)
    service = CharField(max_length=50)
    mono_path = TextField()
    dual_path = TextField()
    page_range = TextField(default="")
    created_at = CharField(max_length=32)

    class Meta:
        database = db


class TranslationCache:
    @staticmethod
    def _sort_dict_recursively(obj):
        if isinstance(obj, dict):
            return {
                k: TranslationCache._sort_dict_recursively(v)
                for k in sorted(obj.keys())
                for v in [obj[k]]
            }
        elif isinstance(obj, list):
            return [TranslationCache._sort_dict_recursively(item) for item in obj]
        return obj

    def __init__(self, translate_engine: str, translate_engine_params: dict = None):
        assert (
            len(translate_engine) < 20
        ), "current cache require translate engine name less than 20 characters"
        self.translate_engine = translate_engine
        self.replace_params(translate_engine_params)

    # The program typically starts multi-threaded translation
    # only after cache parameters are fully configured,
    # so thread safety doesn't need to be considered here.
    def replace_params(self, params: dict = None):
        if params is None:
            params = {}
        self.params = params
        params = self._sort_dict_recursively(params)
        self.translate_engine_params = json.dumps(params)

    def update_params(self, params: dict = None):
        if params is None:
            params = {}
        self.params.update(params)
        self.replace_params(self.params)

    def add_params(self, k: str, v):
        self.params[k] = v
        self.replace_params(self.params)

    # Since peewee and the underlying sqlite are thread-safe,
    # get and set operations don't need locks.
    def get(self, original_text: str) -> Optional[str]:
        result = _TranslationCache.get_or_none(
            translate_engine=self.translate_engine,
            translate_engine_params=self.translate_engine_params,
            original_text=original_text,
        )
        return result.translation if result else None

    def set(self, original_text: str, translation: str):
        try:
            _TranslationCache.create(
                translate_engine=self.translate_engine,
                translate_engine_params=self.translate_engine_params,
                original_text=original_text,
                translation=translation,
            )
        except Exception as e:
            logger.debug(f"Error setting cache: {e}")


# ========== 文件级 Hash 缓存操作 ==========

def compute_file_hash(file_path: str) -> str:
    """计算文件的 SHA256 哈希值"""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def get_file_cache(file_hash: str) -> Optional[dict]:
    """根据文件 hash 查找是否已有翻译缓存"""
    try:
        result = _FileCache.get_or_none(file_hash=file_hash)
        if result is not None and os.path.exists(result.mono_path) and os.path.exists(result.dual_path):
            return {
                "file_hash": result.file_hash,
                "file_name": result.file_name,
                "lang_in": result.lang_in,
                "lang_out": result.lang_out,
                "service": result.service,
                "mono_path": result.mono_path,
                "dual_path": result.dual_path,
                "page_range": result.page_range,
                "created_at": result.created_at,
            }
        # 文件已被删除，清除缓存记录
        if result is not None:
            result.delete_instance()
            logger.info(f"缓存文件已不存在，清除 hash 记录: {file_hash[:12]}...")
    except Exception as e:
        logger.debug(f"Error getting file cache: {e}")
    return None


def set_file_cache(
    file_hash: str,
    file_name: str,
    lang_in: str,
    lang_out: str,
    service: str,
    mono_path: str,
    dual_path: str,
    page_range: str = "",
) -> None:
    """记录已翻译完成的文件缓存"""
    from datetime import datetime
    try:
        _FileCache.create(
            file_hash=file_hash,
            file_name=file_name,
            lang_in=lang_in,
            lang_out=lang_out,
            service=service,
            mono_path=mono_path,
            dual_path=dual_path,
            page_range=page_range,
            created_at=datetime.now().isoformat(),
        )
        logger.info(f"文件翻译缓存已记录: {file_name} ({file_hash[:12]}...)")
    except Exception as e:
        logger.debug(f"Error setting file cache: {e}")


def check_file_cache(file_path: str, lang_in: str, lang_out: str, service: str) -> Optional[dict]:
    """综合检查：计算文件 hash 并查看是否已有缓存"""
    try:
        file_hash = compute_file_hash(file_path)
        cache = get_file_cache(file_hash)
        if cache:
            # 验证翻译参数是否匹配
            if (cache["lang_in"] == lang_in and
                cache["lang_out"] == lang_out and
                cache["service"] == service):
                return cache
    except Exception as e:
        logger.debug(f"Error checking file cache: {e}")
    return None


def get_all_cached_hashes() -> set:
    """获取所有已缓存的文件的 hash 集合，用于前端快速展示"""
    try:
        results = _FileCache.select()
        return {r.file_hash for r in results}
    except Exception as e:
        logger.debug(f"Error getting all cached hashes: {e}")
        return set()


def init_db(remove_exists=False):
    cache_folder = os.path.join(os.path.expanduser("~"), ".cache", "pdf2zh")
    os.makedirs(cache_folder, exist_ok=True)
    # The current version does not support database migration, so add the version number to the file name.
    cache_db_path = os.path.join(cache_folder, "cache.v1.db")
    if remove_exists and os.path.exists(cache_db_path):
        os.remove(cache_db_path)
    db.init(
        cache_db_path,
        pragmas={
            "journal_mode": "wal",
            "busy_timeout": 1000,
        },
    )
    db.create_tables([_TranslationCache, _FileCache], safe=True)


def init_test_db():
    import tempfile

    cache_db_path = tempfile.mktemp(suffix=".db")
    test_db = SqliteDatabase(
        cache_db_path,
        pragmas={
            "journal_mode": "wal",
            "busy_timeout": 1000,
        },
    )
    test_db.bind([_TranslationCache, _FileCache], bind_refs=False, bind_backrefs=False)
    test_db.connect()
    test_db.create_tables([_TranslationCache, _FileCache], safe=True)
    return test_db


def clean_test_db(test_db):
    test_db.drop_tables([_TranslationCache, _FileCache])
    test_db.close()
    db_path = test_db.database
    if os.path.exists(db_path):
        os.remove(test_db.database)
    wal_path = db_path + "-wal"
    if os.path.exists(wal_path):
        os.remove(wal_path)
    shm_path = db_path + "-shm"
    if os.path.exists(shm_path):
        os.remove(shm_path)


init_db()