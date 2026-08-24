import logging
import warnings


def _quiet_third_party_noise() -> None:
    """压制第三方依赖在翻译期间打印、但用户无法操作的告警噪音。

    1) fitz 弃用提示：babeldoc / magic-pdf 内部仍 ``import fitz``。pymupdf
       的提示经它**自带的消息系统**输出（``message()`` 直接 print 到
       ``_g_out_message``），根本不经过 ``warnings`` 模块——旧的
       DeprecationWarning 过滤对它无效（实测 sidecar 日志仍刷屏）。这里改用
       官方出口 :func:`pymupdf.set_messages` 把消息路由进 Python logging，
       再只丢弃这一条文本；其余 pymupdf 消息保持 logging 可见。
    2) sklearn 并行配置告警：babeldoc 调用链触发
       ``sklearn.utils.parallel.delayed should be used with ...Parallel``，
       纯上游内部实现细节，静音。
    """
    _pymupdf_messages = logging.getLogger("pymupdf.message")

    class _FitzDeprecationOnly(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "API is deprecated" not in record.getMessage()

    # 过滤器必须先于首次 `import fitz` 挂上：fitz 导入的瞬间即打印提示。
    _pymupdf_messages.addFilter(_FitzDeprecationOnly())

    try:
        import pymupdf

        pymupdf.set_messages(pylogging=True, pylogging_name="pymupdf.message")
        import fitz  # noqa: F401  -- 提前触发一次性的弃用提示（已被上方过滤）
    except Exception:  # noqa: BLE001 -- pymupdf/fitz 缺失时不阻塞包导入
        pass

    warnings.filterwarnings(
        "ignore",
        message=r".*parallel\.delayed.*should be used with.*",
        category=UserWarning,
    )


_quiet_third_party_noise()

log = logging.getLogger(__name__)

__version__ = "1.9.12"
__author__ = "Byaidu"
__all__ = ["translate", "translate_stream"]


def __getattr__(name):
    if name in {"translate", "translate_stream"}:
        from pdf2zh.high_level import translate, translate_stream

        return {"translate": translate, "translate_stream": translate_stream}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
