import logging
import warnings

# 第三方依赖（babeldoc / magic-pdf）内部仍 `import fitz`——pymupdf 1.26+ 会对
# 顶层 fitz 别名发 DeprecationWarning，每次翻译都刷屏且无法由用户操作消除。
# 我们自身的运行时代码已全部迁移到 `import pymupdf`；这里在包入口一次性
# 压制该特定告警（不影响其它 DeprecationWarning 的可见性）。
warnings.filterwarnings(
    "ignore",
    message=r".*\bfitz\b API is deprecated.*",
    category=DeprecationWarning,
)

log = logging.getLogger(__name__)

__version__ = "1.9.12"
__author__ = "Byaidu"
__all__ = ["translate", "translate_stream"]


def __getattr__(name):
    if name in {"translate", "translate_stream"}:
        from pdf2zh.high_level import translate, translate_stream

        return {"translate": translate, "translate_stream": translate_stream}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
