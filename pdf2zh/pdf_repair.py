"""损坏 PDF 的分级检测与**自动修复链**。

为什么需要这条链
----------------
源文件损坏时，工具原本只有两个动作：能读就继续（``ensure_source_usable``
发一条 ``warn``），读不了就 ``UnusableSourceError`` 直接失败。前者的问题
是「warn 说能继续」与「引擎根本打不开」互相矛盾 —— 实测那份 325 页混合引用
文件上，MuPDF/pikepdf 放行、入口闸门只告警，而 MinerU 的 PDFium 后端直接崩在
``open_pdfium_document``。后者的问题则是过早放弃：很多损坏 pikepdf 一次
load+save 就能救回来。

所以这里补的是中间那一档：**探测 → 逐级修复 → 验证 → 采用**，全程不动用户
的原文件。

修复链（由廉价到激进）
--------------------
1. :func:`_normalize` —— 清洗 Catalog 键类型 + 一次 load+save。重新序列化会写出
   单一完整的 xref，顺带修掉「只挂在 XRef 流上的 ``/Encrypt``」「残留
   ``/XRefStm``」「错位的 ``startxref``」三类结构缺陷。实测命中率最高。
2. :func:`_rebuild` —— ``attempt_recovery=True`` + 重新生成对象流 + 压缩 +
   规范化内容。libqpdf 扫描重建 xref，并把每个对象/流重新序列化。
3. :func:`_ignore_xref_streams` —— 恢复模式下**不信** xref 流。给「xref 流本身
   损坏」的文件用（普通恢复也会先试 xref 流）。
4. :func:`_salvage_pages` —— 只救能读的页，新建干净文档装进去。**会丢页**，
   所以页数变化被记进 :class:`RepairAttempt` 并写进日志，绝不静默。

关于「逐级」这件事的诚实说明
----------------------------
实测（合成损坏样本，见 ``tests/test_pdf_repair_chain.py``）里第 1/2/3 级常常
**结果相同** —— 因为 libqpdf 在 ``open`` 阶段就会自动恢复，大多数结构缺陷在
save 的重新序列化时就消失了。分级不是为了凑数：它保证 (a) 先试代价最小的改动，
(b) 每一级的产物都被**同一个闸门**复检，只有真的修好才采用，(c) 修不好时日志
能说出试过什么、每一步怎么失败的 —— 而不是一句「翻译失败」。

（早先这里有一级独立的「只清洗 Catalog」，实测发现它同样要 ``pdf.save()``，
与 ``normalize`` 完全重复，已合并。）

刻意**不**做的事
----------------
- 不把「带加密字典」当损坏。实测空用户密码的加密 PDF 严格阅读器能正常打开，
  当成损坏会导致每次白跑修复链，还会顺手把加密剥掉 —— 那是未经用户要求改变
  文档安全语义。
- 不做「光栅化抢救」（ghostscript/mutool draw）。那会把文档变成图片，对翻译
  工具来说等于静默交付一个译不出字的结果，比明确失败更糟。
- 不引入外部二进制依赖：Tauri 打包产物里多带一个可执行文件会带来它自己的一整
  套打包问题，而 pikepdf（libqpdf）已经覆盖了绝大部分可救回的损坏。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: 修复副本目录前缀。``release_repair`` 只回收自己建的目录。
_REPAIR_DIR_PREFIX = "pdf2zh_repair_"


@dataclass(frozen=True)
class DamageSignal:
    """一处可命名的损坏。

    ``code`` 取值刻意少而稳定，便于日志聚合与前端展示：

    - ``unreadable``   —— pikepdf 都打不开（结构性死亡，后面的链多半白跑）
    - ``strict``       —— 宽松读取器能开、严格阅读器（PDFium/浏览器）不能
    - ``structural``   —— Catalog 键类型违反规范（``/AcroForm`` 指向数组…）
    - ``truncated``    —— 文件尾部缺失（没有 %%EOF 或 xref 被截断）
    - ``page_loss``    —— 打开后页数少于文件里实际存在的页对象数

    刻意**没有** ``encrypted``：带空用户密码的加密 PDF 是正常文档，严格阅读器
    能打开（实测），把它当损坏会导致每次都白跑一遍修复链，还会顺手把加密剥掉
    —— 那是未经用户要求改变文档安全语义。真的读不了（缺密码）时归入
    ``unreadable``，由 :func:`repair_copy` 明确报出。
    """

    code: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class RepairAttempt:
    """一次策略尝试的结果（含失败原因）。

    失败原因必须留痕：修不好时，能回答「试过什么、为什么不行」比只说
    「修复失败」有用得多。
    """

    strategy: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0
    pages_in: int = 0
    pages_out: int = 0

    @property
    def pages_lost(self) -> int:
        return max(0, self.pages_in - self.pages_out)


@dataclass(frozen=True)
class RepairOutcome:
    """整条链的结论。

    ``path`` 为 ``None`` 表示**没有**产出可用的修复副本 —— 要么原件本来就
    健康（``signals`` 为空），要么所有策略都失败。调用方必须区分这两种情况：
    前者照常处理原件，后者才需要报错。
    """

    source: str
    path: Optional[str] = None
    strategy: Optional[str] = None
    signals: Tuple[DamageSignal, ...] = ()
    attempts: Tuple[RepairAttempt, ...] = ()
    healthy: bool = False

    @property
    def repaired(self) -> bool:
        return self.path is not None

    @property
    def pages_lost(self) -> int:
        return sum(a.pages_lost for a in self.attempts if a.ok)

    def summary(self) -> str:
        """一行结论，给日志用。"""
        if self.healthy:
            return f"{os.path.basename(self.source)}: no damage detected"
        if not self.repaired:
            tried = (
                ", ".join(f"{a.strategy}({a.detail[:40]})" for a in self.attempts)
                or "no strategy ran"
            )
            return (
                f"{os.path.basename(self.source)}: UNREPAIRABLE after "
                f"{len(self.attempts)} strateg(y|ies) [{tried}]"
            )
        lost = f", {self.pages_lost} page(s) dropped" if self.pages_lost else ""
        return (
            f"{os.path.basename(self.source)}: repaired via {self.strategy} "
            f"({len(self.signals)} signal(s){lost})"
        )


# --------------------------------------------------------------------------
# 检测
# --------------------------------------------------------------------------
def detect_damage(path: str) -> List[DamageSignal]:
    """列出 ``path`` 上可命名的损坏（不修改文件）。

    复用 :mod:`pdf2zh.pdf_validity` 的既有判据，不另立一套：闸门用哪个标准
    判「读不了」，修复链就用同一个标准复检，否则会出现「按 A 判坏、按 B 判好」
    的自欺。

    「严格阅读器打不开」只在**不是**结构缺陷导致时才叫 ``strict``：pikepdf 的
    ``inspect_pdf`` 把结构问题也折进 ``ok``，照抄它会把一个 Catalog 键类型错误
    误报成「浏览器打不开」。
    """
    from pdf2zh.pdf_validity import _pikepdf, catalog_structure_problems, inspect_pdf

    signals: List[DamageSignal] = []
    if not path or not os.path.isfile(path):
        return [DamageSignal("missing", f"not a file: {path}")]

    pikepdf = _pikepdf()
    if pikepdf is None:
        return [DamageSignal("no_tool", "pikepdf unavailable; cannot assess")]

    try:
        with pikepdf.open(path) as pdf:
            pages = len(pdf.pages)
    except Exception as exc:  # noqa: BLE001 -- 损坏形态千奇百怪
        return [DamageSignal("unreadable", f"{type(exc).__name__}: {str(exc)[:120]}")]

    structural = list(catalog_structure_problems(path))
    for problem in structural:
        signals.append(DamageSignal("structural", problem))

    verdict = inspect_pdf(path)
    if not verdict.ok and not structural:
        # 没有结构问题却仍判失败 → 真的是严格阅读器这一侧过不去
        signals.append(
            DamageSignal("strict" if pages else "unreadable", verdict.reason[:160])
        )

    if _looks_truncated(path):
        signals.append(DamageSignal("truncated", "missing %%EOF / short tail"))

    # 丢页：pikepdf 打开时就可能已经扔掉了几页（实测截断样本 3 页只剩 2 页）。
    # 拿它自己报的页数当基准是看不出损失的，所以另取一个下界。
    floor = _raw_page_object_count(path)
    if floor and pages < floor:
        signals.append(
            DamageSignal(
                "page_loss",
                f"{floor} page objects in the file but only {pages} survive open()",
            )
        )
    return signals


#: ``/Type /Page`` 但**不是** ``/Type /Pages``（页树节点）。
#: 早先这里用 ``bytes.count(b"/Type /Page")``，结果把每个页树根也算成一页 ——
#: 于是任何正常文档都被误报成「丢了一页」，健康文件甚至被当成损坏送去修复。
_RE_PAGE_OBJ = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def _raw_page_object_count(path: str, cap: int = 20000) -> int:
    """文件里页对象（``/Type /Page``）的出现次数，作为「实际存在多少页」的下界。

    刻意粗糙：它数的是字节序列，不是对象图，所以对压缩对象流无效（返回 0 时
    调用方直接跳过）。但它足够抓住「尾部被截断导致 pikepdf 丢页」这类损失 ——
    那正是静默丢内容最危险、也最难发现的形态。
    """
    try:
        if os.path.getsize(path) > 80 * 1024 * 1024:
            return 0
        data = open(path, "rb").read()
    except OSError:
        return 0
    return min(len(_RE_PAGE_OBJ.findall(data)), cap)


def _looks_truncated(path: str) -> bool:
    """尾部是否有 ``%%EOF``（允许少量尾随空白）。"""
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(max(0, os.path.getsize(path) - 2048))
            except OSError:
                fh.seek(0)
            tail = fh.read()
    except OSError:
        return False
    return b"%%EOF" not in tail


# --------------------------------------------------------------------------
# 修复策略
# --------------------------------------------------------------------------
def _normalize(src: str, dst: str) -> Tuple[int, int]:
    """清洗 Catalog 键类型 + 一次 load+save。

    这是链的默认一级，也是最常命中的一级：重新序列化会写出单一完整的 xref，
    于是「只挂在 XRef 流上的 ``/Encrypt``」「残留 ``/XRefStm``」「错位的
    ``startxref``」三类结构缺陷同时消失。

    注意它**确实**会重新序列化整个文件 —— 早先这里有个只做 Catalog 清洗的独立
    一级，实测发现它同样要 ``pdf.save()``，于是与本级完全重复、只是名字更好听。
    已合并。
    """
    from pdf2zh.pdf_validity import _sanitize_catalog

    with pikepdf_open(src) as pdf:
        pages = len(pdf.pages)
        _sanitize_catalog(pdf)
        pdf.save(dst)
    return pages, _page_count(dst)


def _rebuild(src: str, dst: str) -> Tuple[int, int]:
    """恢复模式 + 重新生成对象流 / 压缩 / 规范化内容。"""
    with pikepdf_open(src, attempt_recovery=True) as pdf:
        pages = len(pdf.pages)
        pdf.save(
            dst,
            object_stream_mode=_object_stream_generate(),
            compress_streams=True,
            normalize_content=True,
        )
    return pages, _page_count(dst)


def _ignore_xref_streams(src: str, dst: str) -> Tuple[int, int]:
    """恢复模式下不信 xref 流（给 xref 流本身损坏的文件）。"""
    with pikepdf_open(src, attempt_recovery=True, ignore_xref_streams=True) as pdf:
        pages = len(pdf.pages)
        pdf.save(dst, object_stream_mode=_object_stream_generate())
    return pages, _page_count(dst)


def _salvage_pages(src: str, dst: str) -> Tuple[int, int]:
    """只救能读的页。会丢页 —— 调用方必须把页数变化报出来。"""
    with pikepdf_open(src, attempt_recovery=True) as pdf:
        pages = len(pdf.pages)
        out = pikepdf_Pdf_new()
        for i in range(pages):
            try:
                out.pages.append(pdf.pages[i])
            except Exception:  # noqa: BLE001 -- 单页坏掉不该拖垮整份
                continue
        out.save(dst)
        out.close()
    return pages, _page_count(dst)


def _strategy_catalog() -> List[Tuple[str, Callable[[str, str], Tuple[int, int]]]]:
    """修复链定义。顺序即代价：从最轻到最重。"""
    return [
        ("normalize", _normalize),
        ("rebuild", _rebuild),
        ("ignore_xref_streams", _ignore_xref_streams),
        ("salvage_pages", _salvage_pages),
    ]


# --------------------------------------------------------------------------
# pikepdf 访问器（延迟导入 + 薄封装，便于测试打桩）
# --------------------------------------------------------------------------
def _pikepdf():
    try:
        import pikepdf  # noqa: PLC0415 -- 惰性导入
    except Exception:  # noqa: BLE001
        return None
    return pikepdf


def pikepdf_open(path: str, **kwargs):
    """``pikepdf.open`` 的薄封装（关键字参数按支持度过滤）。"""
    import inspect

    mod = _pikepdf()
    if mod is None:  # pragma: no cover - 依赖缺失
        raise RuntimeError("pikepdf unavailable")
    supported = set(inspect.signature(mod.open).parameters)
    filtered = {k: v for k, v in kwargs.items() if k in supported}
    return mod.open(path, **filtered)


def pikepdf_Pdf_new():
    mod = _pikepdf()
    if mod is None:  # pragma: no cover
        raise RuntimeError("pikepdf unavailable")
    return mod.Pdf.new()


def _object_stream_generate():
    mod = _pikepdf()
    mode = getattr(mod, "ObjectStreamMode", None)
    return mode.generate if mode is not None else None


def _page_count(path: str) -> int:
    try:
        with pikepdf_open(path) as pdf:
            return len(pdf.pages)
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------
# 链驱动
# --------------------------------------------------------------------------
def repair_copy(
    path: str,
    *,
    enabled: bool = True,
    dest_dir: Optional[str] = None,
    strategies: Optional[Sequence[str]] = None,
) -> RepairOutcome:
    """探测 ``path``，必要时产出一份**已验证**的修复副本。

    原文件**永远不被修改** —— 它属于用户，损坏与否都是他们的原始数据。

    Args:
        path: 源 PDF。
        enabled: 关闭时只探测不修复（对应 ``TranslationRequest.enable_repair``）。
        dest_dir: 副本落盘目录；缺省用一次性临时目录。
        strategies: 只跑这些策略（按给定顺序），缺省跑整条链。

    Returns:
        :class:`RepairOutcome`。``healthy=True`` 表示原件本来就没问题；
        ``path is None`` 且 ``healthy=False`` 表示修不好，调用方应明确报错。
    """
    signals = tuple(detect_damage(path))

    # 缺文件 / 缺 pikepdf：链跑不了，也不该假装「修好了」。
    if any(s.code in ("missing", "no_tool") for s in signals):
        return RepairOutcome(source=path, signals=signals, healthy=False, attempts=())

    # 没检测到损坏：原件直接可用，**不要**为了「统一流程」去重写它。
    if not signals:
        return RepairOutcome(source=path, signals=(), healthy=True, attempts=())

    if not enabled:
        logger.info(
            "repair[%s] %d damage signal(s) but repair disabled by caller",
            os.path.basename(path),
            len(signals),
        )
        return RepairOutcome(source=path, signals=signals, healthy=False, attempts=())

    catalog = _strategy_catalog()
    if strategies is not None:
        wanted = list(strategies)
        catalog = [c for c in catalog if c[0] in wanted]

    owner = dest_dir or tempfile.mkdtemp(prefix=_REPAIR_DIR_PREFIX)
    accepted = False
    attempts: List[RepairAttempt] = []
    # 基准页数取「文件里实际有多少页对象」的上界，而不是 pikepdf 打开后报的数 ——
    # 后者已经在 open() 阶段就丢过页了（实测截断样本 3 页只剩 2 页），拿它当
    # pages_in 就永远看不出损失。
    baseline = _raw_page_object_count(path)
    try:
        for name, fn in catalog:
            # 副本沿用**原文件名**：结果文件名由源 stem 派生
            # （``runtime_service._shorten_result_entries`` 用结果文件自己的路径
            # 算稳定哈希），换成策略名会把产物叫成 ``normalize-mono.pdf``，
            # 也会让同一份输入每次得到不同的下载名。
            dst = os.path.join(owner, os.path.basename(path) or f"{name}.pdf")
            t0 = time.perf_counter()
            try:
                pages_seen, pages_out = fn(path, dst)
                elapsed = time.perf_counter() - t0
            except Exception as exc:  # noqa: BLE001 -- 这一级不行就换下一级
                attempts.append(
                    RepairAttempt(
                        strategy=name,
                        ok=False,
                        detail=f"{type(exc).__name__}: {str(exc)[:140]}",
                        seconds=time.perf_counter() - t0,
                    )
                )
                logger.debug(
                    "repair[%s] %s raised: %s", os.path.basename(path), name, exc
                )
                _silent_remove(dst)
                continue

            ok, reason = _verify_repaired(dst)
            pages_in = max(pages_seen, baseline) if baseline else pages_seen
            attempts.append(
                RepairAttempt(
                    strategy=name,
                    ok=ok,
                    detail="" if ok else reason[:140],
                    seconds=elapsed,
                    pages_in=pages_in,
                    pages_out=pages_out,
                )
            )
            if not ok:
                logger.debug(
                    "repair[%s] %s produced a file the gate still refuses: %s",
                    os.path.basename(path),
                    name,
                    reason[:140],
                )
                _silent_remove(dst)
                continue

            accepted = True
            lost = max(0, pages_in - pages_out)
            if lost:
                logger.warning(
                    "repair[%s] accepted via %s but %d page(s) could not be "
                    "recovered (raw page objects suggest %d, output has %d)",
                    os.path.basename(path),
                    name,
                    lost,
                    pages_in,
                    pages_out,
                )
            else:
                logger.info(
                    "repair[%s] accepted via %s in %.2fs (%d signal(s))",
                    os.path.basename(path),
                    name,
                    elapsed,
                    len(signals),
                )
            return RepairOutcome(
                source=path,
                path=dst,
                strategy=name,
                signals=signals,
                attempts=tuple(attempts),
                healthy=False,
            )

        logger.warning(
            "repair[%s] unrepairable after %d strateg(y|ies): %s",
            os.path.basename(path),
            len(attempts),
            "; ".join(f"{a.strategy}={a.detail[:60]}" for a in attempts) or "-",
        )
        return RepairOutcome(
            source=path, signals=signals, attempts=tuple(attempts), healthy=False
        )
    finally:
        # 失败路径不留空目录；成功路径的副本归调用方（release_repair 回收）。
        if not accepted and dest_dir is None:
            shutil.rmtree(owner, ignore_errors=True)


def _verify_repaired(path: str) -> Tuple[bool, str]:
    """复检修复产物。

    用**与判坏同一个闸门**（:func:`pdf_validity.strict_reader_check`）。按别的
    标准验收就会出现「按 A 判坏、按 B 判好」：产物在闸门这里仍然打不开，却已经
    交给了只吃字节的引擎。

    Catalog 结构问题**不必**在这里再查一遍：闸门底下的 ``inspect_pdf`` 已经把
    ``catalog_structure_problems`` 折进 ``ok``（实测 ``/AcroForm`` 指向数组的文件
    严格检查就是 ``False``）。早先在这里多加一次复查纯属重复，已去掉。
    """
    from pdf2zh.pdf_validity import strict_reader_check

    ok, reason = strict_reader_check(path)
    if not ok:
        return False, reason or "strict reader refused the repaired file"
    return True, ""


def release_repair(path: Optional[str]) -> None:
    """回收 :func:`repair_copy` 产出的副本及其私有目录。

    只认自己建的布局（父目录以 ``pdf2zh_repair_`` 开头），传别的路径进来什么
    也不做 —— 清理代码不该有能力删掉调用方自己的文件。
    """
    if not path:
        return
    parent = os.path.dirname(path)
    if not os.path.basename(parent).startswith(_REPAIR_DIR_PREFIX):
        return
    try:
        os.unlink(path)
    except OSError:
        pass
    try:
        os.rmdir(parent)
    except OSError:
        pass


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


__all__ = [
    "DamageSignal",
    "RepairAttempt",
    "RepairOutcome",
    "detect_damage",
    "release_repair",
    "repair_copy",
]
