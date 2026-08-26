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

    冷启动注记（doc/perf/coldstart-trace/report.md）：pymupdf 的导入含原生
    DLL 加载，实测占 API sidecar 关键路径 ~0.38s。这里**不再急切 import**，
    而是挂一个 meta_path 钩子——日志过滤器先于任何 ``import fitz/pymupdf``
    就位（保住既有语义），真正的导入+消息路由推迟到首次实际使用时发生。
    """
    _pymupdf_messages = logging.getLogger("pymupdf.message")

    class _FitzDeprecationOnly(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "API is deprecated" not in record.getMessage()

    # 过滤器必须先于首次 `import fitz` 挂上：fitz 导入的瞬间即打印提示。
    _pymupdf_messages.addFilter(_FitzDeprecationOnly())

    def _route_pymupdf_messages() -> None:
        try:
            import pymupdf

            pymupdf.set_messages(pylogging=True, pylogging_name="pymupdf.message")
        except Exception:  # noqa: BLE001 -- pymupdf 缺失时不影响调用方
            pass

    class _FitzRoutingFinder:
        """拦截首次 ``import pymupdf``/``import fitz``，加载后接管消息路由。

        只包一层 loader：真实 spec/加载逻辑完全委托原 finder，行为与未拦截
        一致；两个名字各自独立拦截（先到者路由完成后，另一者的弃用提示
        已可被过滤器接住），全部处理完即自摘。
        """

        _pending = {"pymupdf", "fitz"}

        def find_spec(self, fullname, path=None, target=None):
            if fullname not in self._pending:
                return None
            self._pending.discard(fullname)
            for finder in _sys.meta_path:
                if finder is self:
                    continue
                try:
                    spec = finder.find_spec(fullname, path, target)
                except Exception:  # noqa: BLE001 -- 让位给下一个 finder
                    continue
                if spec is not None and spec.loader is not None:
                    inner = spec.loader

                    class _Loader:
                        def create_module(self, spec_):
                            return inner.create_module(spec_)

                        def exec_module(self, module):
                            inner.exec_module(module)
                            _route_pymupdf_messages()

                        def __getattr__(self, name):
                            return getattr(inner, name)

                    spec.loader = _Loader()
                    return spec
            return None

    import sys as _sys

    if not any(isinstance(f, _FitzRoutingFinder) for f in _sys.meta_path):
        _sys.meta_path.insert(0, _FitzRoutingFinder())
    # 包已在本解释器内加载过时钩子不会触发，直接补路由。
    if "pymupdf" in _sys.modules or "fitz" in _sys.modules:
        _route_pymupdf_messages()

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
