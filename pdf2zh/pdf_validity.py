"""产物 PDF 的「严格阅读器可读性」闸门。

为什么需要
----------
产物落盘后，各链路原本只校验 ``isfile`` + ``st_size > 0``
（``babeldoc_adapter._collect_result_files`` /
``babeldoc_next_adapter._collect_result_files`` /
``runtime_service._magicpdf_result_entries`` /
``runtime_service._execute_v4``）。字节数大于 0 与「用户能打开」是两件事：
Chrome / Edge / 多数浏览器内置阅读器、以及 PDFium 系的校验器对同一份字节的判定
比 MuPDF、pikepdf 严格得多。实测同一个文件 MuPDF 报
``is_encrypted=False``、pikepdf ``check_pdf_syntax`` 无问题，PDFium 却直接
``FPDF_ERR_SECURITY`` 拒绝加载。

已实证的失效形态（325 页扫描书源文件，z-library 混合引用文件）：
``/Encrypt`` 只挂在 **XRef 流**上（object 1027 有 ``/Encrypt 1026 0 R``），
而传统 ``trailer`` 里没有 ``/Encrypt``。按规范这是无效的混合引用结构 ——
严格阅读器读到 ``/Encrypt`` 却建不起 security handler，于是在
``FPDF_LoadDocument`` 阶段就失败；宽松的 MuPDF/pikepdf 把它当未加密文档直接
放行。``pikepdf`` 一次 load+save（0.2s、体积不变）即可修复：``/Encrypt`` 与
``/XRefStm`` 同时消失。

本模块做三件事
--------------
1. :func:`catalog_structure_problems` —— **不依赖 PDFium** 的结构校验。检查
   Catalog 里那些「值类型由规范钉死」的键（``/AcroForm`` 必须是字典…）。
2. :func:`strict_reader_check` —— 结构校验 + PDFium 试开，把「严格阅读器打不开」
   变成一个可判断的布尔值，而不是用户拿到手才发现。
3. :func:`repair_pdf` / :func:`ensure_readable` —— 产物若过不了闸门，先尝试
   用 pikepdf 规范化重写（原地），成功即交付修复后的文件；仍不通过才告警。
   **绝不因为校验失败就丢弃产物** —— 那会让用户什么都拿不到。

为什么必须有第 1 项（这是实测踩出来的）
--------------------------------------
``strict_reader_check`` 原本只靠 PDFium，而 PDFium **有版本差**：项目里
``pypdfium2`` 绑定的是 PDFium 151.0.7891.0，Edge 155 内核是 PDFium 13x 系列。
实测 325 页扫描书（``Structural Holes`` 源文件）::

    /Root/AcroForm -> 1 0 R，而对象 1 是  1 0 obj [] endobj   # 空「数组」

PDF 规范（ISO 32000-1§12.7.3）要求 ``/AcroForm`` 是**字典**。PDFium 151 容忍，
放行；Edge 155 直接拒绝加载整个文档，界面报「We can't open this file /
Something went wrong」、页码 ``0 of 0``。因为旧 PDFium 放行，纯 PDFium 闸门对此
**系统性假阴性** —— 实测该文件 ``strict_reader_check`` 返回 ``True``，却在
Edge 里打不开。

同一文件只删掉 ``Root/AcroForm``（或换成字典）即可打开，已用 Edge 逐项验证；
而 pikepdf 的 load+save **不会**修好它（原样保留），所以 :func:`repair_pdf`
必须显式做 Catalog 类型清洗，否则「修复」永远失败。

``pypdfium2`` 是既有传递依赖（``uv.lock`` 内已有，随 marker-pdf 一同安装），
因此这里不新增依赖。万一缺失：结构校验照跑（只依赖 pikepdf），PDFium 试开
降级为 **warning**（不再是静默 info）—— 明确告知「产物未经真实阅读器验证」。
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: ``pypdfium2`` 缺失时只提醒一次的开关。
_warned_missing = False


class UnusableSourceError(RuntimeError):
    """源 PDF 结构非法且无法规范化 —— 应当立刻报错，而不是继续翻译。"""


#: Catalog 中「值类型由规范钉死」的键（ISO 32000-1 §7.7.2 / §12.7.3 等）。
#:
#: 只收类型无歧义的条目：``/Threads`` 是数组、``/Metadata`` 是流，其余
#: （``/PageMode``、``/OpenAction``…）类型随内容变化，一律不查 —— 结构闸门
#: 宁可漏报也绝不能误报，否则会把好文件判死。
#:
#: 这里**不包括** ``/Pages``/``/Type``：它们缺失时 pikepdf 根本打不开文件，
#: 由 PDFium 那一路覆盖。
CATALOG_KEY_TYPES: Dict[str, str] = {
    "/AcroForm": "dict",
    "/Names": "dict",
    "/DSS": "dict",
    "/OCProperties": "dict",
    "/StructTreeRoot": "dict",
    "/Threads": "array",
    "/Metadata": "stream",
}

_TYPE_LABEL = {
    "dict": "字典",
    "array": "数组",
    "stream": "流",
    "name": "名字",
    "string": "字符串",
    "integer": "整数",
    "real": "实数",
    "boolean": "布尔值",
    "null": "null",
    "unknown": "未知类型",
}


@dataclass(frozen=True)
class ReaderVerdict:
    """:func:`inspect_pdf` 的结论。"""

    ok: bool
    reason: str = ""
    pdfium_available: bool = True
    structural: Tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        """PDFium 不可用 —— 只做了结构校验，未经真实阅读器验证。"""
        return not self.pdfium_available


@dataclass(frozen=True)
class SourceVerdict:
    """源 PDF 的可用性结论。"""

    severity: str  # "ok" | "warn" | "fatal"
    message: str = ""
    problems: Tuple[str, ...] = field(default=())


def _pikepdf():
    try:
        import pikepdf  # noqa: PLC0415 -- 惰性导入

        return pikepdf
    except Exception:  # noqa: BLE001 -- 缺依赖不得阻断翻译
        return None


def _pdfium():
    """惰性返回 ``pypdfium2`` 模块；不可用时返回 None 并**告警**一次。"""
    global _warned_missing
    try:
        import pypdfium2  # noqa: PLC0415 -- 惰性导入，缺失时降级

        return pypdfium2
    except Exception:  # noqa: BLE001 -- 缺依赖不得阻断翻译
        if not _warned_missing:
            _warned_missing = True
            logger.warning(
                "pypdfium2 unavailable: the strict-reader (Chrome/Edge) check "
                "is DEGRADED. Only the pikepdf-based structural check will run, "
                "so outputs are NOT verified against a real browser PDF engine."
            )
        return None


def _pdf_type_name(obj) -> str:
    """把 pikepdf 对象映射成 ``dict`` / ``array`` / ``stream``… 之类的短名。"""
    pikepdf = _pikepdf()
    if pikepdf is None:  # pragma: no cover -- 仅在缺依赖时可达
        return "unknown"
    # 顺序要紧：Stream 与 Dictionary 不是继承关系，但显式先判 Stream 更直观。
    if isinstance(obj, pikepdf.Stream):
        return "stream"
    if isinstance(obj, pikepdf.Dictionary):
        return "dict"
    if isinstance(obj, pikepdf.Array):
        return "array"
    if isinstance(obj, pikepdf.Name):
        return "name"
    if isinstance(obj, pikepdf.String):
        return "string"
    name = type(obj).__name__
    return {
        "Integer": "integer",
        "Real": "real",
        "Boolean": "boolean",
        "Null": "null",
    }.get(name, "unknown")


def _describe_ref(obj) -> str:
    """``1 0 R`` 这样的引用描述，便于用户定位。"""
    try:
        og = obj.objgen
    except Exception:  # noqa: BLE001 -- 直接对象没有 objgen
        return "直接对象"
    if not og or og[0] <= 0:
        return "直接对象"
    return f"对象 {og[0]} {og[1]} R"


def _resolve(pdf, obj):
    """解引用；返回 ``(值, 错误信息)``。"""
    try:
        if getattr(obj, "is_indirect", False):
            og = obj.objgen
            return pdf.get_object((og[0], og[1])), None
    except Exception as exc:  # noqa: BLE001 -- 悬空引用
        return None, f"{type(exc).__name__}: {exc}"
    return obj, None


def catalog_structure_problems(path: str) -> List[str]:
    """检查 Catalog 里类型被规范钉死的键，返回问题描述列表（可读则为空）。

    只依赖 pikepdf，不依赖 pypdfium2 —— 这样即使 PDFium 缺失，结构闸门照跑。
    """
    pikepdf = _pikepdf()
    if pikepdf is None or not path or not os.path.isfile(path):
        return []
    problems: List[str] = []
    try:
        with pikepdf.open(path) as pdf:
            root = pdf.Root
            for key, want in CATALOG_KEY_TYPES.items():
                if key not in root:
                    continue
                raw = root[key]
                value, err = _resolve(pdf, raw)
                if err is not None:
                    problems.append(
                        f"{key} 指向的对象无法解析（{_describe_ref(raw)}）：{err}"
                    )
                    continue
                got = _pdf_type_name(value)
                if got != want:
                    problems.append(
                        f"{key} 应为{_TYPE_LABEL[want]}，实际为"
                        f"{_TYPE_LABEL.get(got, got)}"
                        f"（{_describe_ref(raw)}）；规范见 ISO 32000-1"
                    )
    except Exception as exc:  # noqa: BLE001 -- 打不开交给 PDFium 那一路
        logger.debug("structural check skipped for %s: %s", path, exc)
    return problems


def _sanitize_catalog(pdf) -> List[str]:
    """就地删掉类型非法的 Catalog 键，返回被删项的描述。

    pikepdf 的 load+save **不会**自动纠正这类错误（实测 ``/AcroForm -> []``
    原样保留），所以修复必须显式做。删键是安全的：这些键都是可选的，
    删掉只损失「表单/批注/结构树」这类附加功能，不影响页面内容。
    """
    removed: List[str] = []
    root = pdf.Root
    for key, want in CATALOG_KEY_TYPES.items():
        if key not in root:
            continue
        raw = root[key]
        value, err = _resolve(pdf, raw)
        if err is not None:
            del root[key]
            removed.append(f"{key}（悬空引用）")
            continue
        got = _pdf_type_name(value)
        if got != want:
            del root[key]
            removed.append(
                f"{key}（应为{_TYPE_LABEL[want]}，实为" f"{_TYPE_LABEL.get(got, got)}）"
            )
    return removed


def _pdfium_check(path: str, deep: bool) -> Tuple[bool, str]:
    pdfium = _pdfium()
    if pdfium is None:
        return True, ""
    doc = None
    try:
        doc = pdfium.PdfDocument(path)
        n = len(doc)
        if deep:
            for i in range(n):
                page = doc[i]
                try:
                    page.get_size()
                finally:
                    page.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001 -- PDFium 的错误类型随版本变化
        return False, f"PDFium {type(exc).__name__}: {exc}"
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001 -- 关闭失败不影响结论
                pass


def inspect_pdf(path: str, deep: bool = False) -> ReaderVerdict:
    """结构校验 + PDFium 试开，给出完整结论。

    与 :func:`strict_reader_check` 的区别：本函数额外返回 PDFium 是否可用
    （:attr:`ReaderVerdict.degraded`）以及具体结构问题，便于调用方决定
    「只是告警」还是「必须拦截」。
    """
    if not path or not os.path.exists(path):
        return ReaderVerdict(False, f"file not found: {path}")

    structural = tuple(catalog_structure_problems(path))
    pdfium_ok, pdfium_reason = _pdfium_check(path, deep)

    reasons: List[str] = list(structural)
    if not pdfium_ok:
        reasons.append(pdfium_reason)
    return ReaderVerdict(
        ok=(pdfium_ok and not structural),
        reason="; ".join(reasons),
        pdfium_available=_pdfium() is not None,
        structural=structural,
    )


def strict_reader_check(path: str, deep: bool = False) -> Tuple[bool, str]:
    """用结构校验 + PDFium（Chrome/Edge 内置阅读器内核）检查 ``path``。

    Args:
        path: 待检查的 PDF。
        deep: 为 True 时额外逐页加载。PDFium 是惰性解析，整档 650 页约 4.9s，
            所以默认只做载入期检查 —— 本模块要拦的那类缺陷（加密方案、格式、
            xref、文件头、Catalog 键类型）恰恰都在载入期暴露。

    Returns:
        ``(ok, reason)``。``ok`` 为 True 时 ``reason`` 为空串。
        PDFium 缺失时结构校验仍然生效，并会打一条 warning 说明降级。
    """
    verdict = inspect_pdf(path, deep=deep)
    return verdict.ok, verdict.reason


def repair_pdf(path: str) -> bool:
    """把 ``path`` 原地规范化重写 + 清洗 Catalog 键类型。

    做两件事：
    1. **Catalog 类型清洗**（:func:`_sanitize_catalog`）—— 修掉 PDFium 13x 会
       拒绝、而 pikepdf 忠实保留的类型错误（实测 ``/AcroForm`` 指向数组）。
    2. **load + save 重新序列化** —— pikepdf/qpdf 会写出单一、完整的 xref，
       畸形结构（只挂在 XRef 流上的 ``/Encrypt``、残留 ``/XRefStm``、错位的
       ``startxref``）随之消失。

    刻意**不做**的事：不改页面内容、不重新压缩图像、不删对象，因此不会像
    ``garbage=4`` 那样顺手丢掉不可达内容。

    PDFium 不可用时**直接放弃**：那样就无从验证重写结果是否真的可读，属于
    「无法验证的原地改写」，比不改更危险。结构校验只能证明 Catalog 键类型
    修好了，不能证明阅读器仍能打开整份文件 —— 两者不可互相替代。

    Returns:
        重写成功且复检通过时为 True。
    """
    pikepdf = _pikepdf()
    if pikepdf is None:
        return False
    if _pdfium() is None:
        logger.warning(
            "refusing to rewrite %s: pypdfium2 is unavailable, so the result "
            "cannot be verified against a real browser PDF engine",
            os.path.basename(path),
        )
        return False
    if not os.path.exists(path):
        return False

    try:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(
            prefix=".pdf2zh_repair_", suffix=".pdf", dir=directory
        )
        os.close(fd)
        try:
            with pikepdf.open(path) as pdf:
                removed = _sanitize_catalog(pdf)
                if removed:
                    logger.info(
                        "dropping malformed catalog keys in %s: %s",
                        os.path.basename(path),
                        ", ".join(removed),
                    )
                pdf.save(tmp)
            verdict = inspect_pdf(tmp)
            if not verdict.ok:
                os.unlink(tmp)
                logger.debug(
                    "repair of %s still fails: %s",
                    os.path.basename(path),
                    verdict.reason,
                )
                return False
            # 校验通过才替换，避免把原件换成另一个坏文件
            os.replace(tmp, path)
            return True
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001 -- 修复是尽力而为，永不抛给调用方
        logger.debug("pdf repair failed for %s: %s", path, exc)
        return False


def ensure_readable(path: str, label: str = "") -> bool:
    """产物交付前的闸门：能开就过；开不了先尝试修复，再判定。

    永远不会因为校验失败而让调用方丢弃产物 —— 那比交付一个打不开的文件更糟。
    修复成功会在日志里明确说明产物被规范化过。

    Returns:
        严格阅读器能否打开 ``path``（修复后复检的结果）。
    """
    tag = label or os.path.basename(path)
    verdict = inspect_pdf(path)
    if verdict.degraded:
        logger.warning(
            "%s could not be verified against a real browser PDF engine "
            "(pypdfium2 unavailable); structural check only",
            tag,
        )
    if verdict.ok:
        return True

    logger.warning(
        "%s cannot be loaded by a strict reader (Chrome/Edge/PDFium): %s",
        tag,
        verdict.reason,
    )
    if repair_pdf(path):
        after = inspect_pdf(path)
        if after.ok:
            logger.info(
                "%s was normalised (catalog sanitised + pikepdf re-serialised) "
                "and now loads in a strict reader",
                tag,
            )
            return True
        logger.warning(
            "%s still cannot be loaded after normalisation: %s", tag, after.reason
        )
        return False

    logger.error(
        "%s could not be repaired and a strict reader will reject it: %s "
        "(the file is still delivered so the result is not lost)",
        tag,
        verdict.reason,
    )
    return False


def check_source(path: str) -> SourceVerdict:
    """判定源 PDF 是否可用，给出 ``ok`` / ``warn`` / ``fatal`` 三档结论。

    分档依据是「我们能不能处理」，而不是「严格阅读器喜不喜欢」：

    ``fatal``
        pikepdf 打不开（含需要密码）、或 Catalog 缺 ``/Pages`` —— 这类输入我们
        **根本没法翻译**，继续跑只会在更深处报一个更难懂的错。应当立刻报错。
    ``warn``
        严格阅读器会拒绝、但我们读得了（如 ``/AcroForm`` 指向数组、只挂在 XRef
        流上的 ``/Encrypt``）。**不阻断**：实测这类源文件能被正常翻译成可打开的
        译文（产物经重写已丢掉畸形键），阻断只会白白断掉一条可用链路。但必须把
        缺陷说清楚，让「Chrome 打不开」有归因。
    ``ok``
        没发现问题。

    关于加密：**只有真的打不开才算 fatal**。实测存在大量「空用户密码 + 仅 owner
    密码限制权限」的文件（``pikepdf``/``MuPDF``/``PDFium`` 均可正常打开，
    ``is_encrypted=True``），把它们判成 fatal 会直接阻断本来能翻译的任务 ——
    那是闸门制造问题，不是发现���题。

    文件不存在 / 路径不可读返回 ``ok`` —— 那不是结构缺陷，调用方另有存在性校验。
    """
    if not path or not os.path.isfile(path):
        return SourceVerdict("ok")

    pikepdf = _pikepdf()
    if pikepdf is None:
        return SourceVerdict("ok")

    fatal: List[str] = []
    encrypted_but_readable = False
    try:
        with pikepdf.open(path) as pdf:
            encrypted_but_readable = bool(pdf.is_encrypted)
            root = pdf.Root
            if "/Pages" not in root:
                fatal.append("Catalog 缺少 /Pages")
            else:
                pages, err = _resolve(pdf, root["/Pages"])
                if err is not None:
                    fatal.append(f"Catalog 的 /Pages 无法解析（{err}）")
                elif _pdf_type_name(pages) != "dict":
                    fatal.append("Catalog 的 /Pages 不是字典")
    except pikepdf.PasswordError:
        fatal.append("文档已加密且需要密码，无法处理")
    except pikepdf.PdfError as exc:
        fatal.append(f"无法解析（{type(exc).__name__}: {exc}）")
    except Exception as exc:  # noqa: BLE001
        fatal.append(f"无法解析（{type(exc).__name__}: {exc}）")

    if fatal:
        return SourceVerdict(
            "fatal",
            "源 PDF 无法处理：" + "；".join(fatal),
            tuple(fatal),
        )

    verdict = inspect_pdf(path)
    if verdict.ok:
        return SourceVerdict("ok")

    problems = list(verdict.structural)
    if not problems and verdict.reason:
        problems.append(verdict.reason)
    if encrypted_but_readable:
        problems.insert(
            0,
            "文档带权限加密（空用户密码，可正常打开）",
        )
    detail = "；".join(problems)
    repairable = bool(verdict.structural)
    hint = (
        "该缺陷属于 Catalog 键类型错误，本工具输出时会自动清洗；但浏览器会"
        "直接拒绝加载原始文件。"
        if repairable
        else "已知形态是无效的混合引用结构（/Encrypt 只挂在 XRef 流上、传统 "
        "trailer 缺少 /Encrypt）：MuPDF/pikepdf 更宽松所以本工具能读，浏览器"
        "会拒绝。修复方式：用 pikepdf/qpdf 做一次 load+save 规范化。"
    )
    return SourceVerdict(
        "warn",
        f"源 PDF 无法被严格阅读器（Chrome/Edge/PDFium）加载：{detail}。"
        f"{hint}翻译可继续。",
        tuple(problems),
    )


def ensure_source_usable(path: str, label: str = "") -> SourceVerdict:
    """源 PDF 的入口闸门：``fatal`` 直接抛 :class:`UnusableSourceError`。

    这样「坏源文件」在任务一开始就有明确、可执行的报错，而不是跑到一半才在
    某个深层模块里抛一个语焉不详的异常。
    """
    verdict = check_source(path)
    if verdict.severity == "fatal":
        raise UnusableSourceError(
            f"{label or os.path.basename(path)}: {verdict.message}"
        )
    return verdict


def source_readability_warning(path: str) -> Optional[str]:
    """检查**源** PDF 是否严格阅读器打不开，返回告警文案（可读则 None）。

    只告警、不改写用户输入：源文件属于用户。存在的意义是让「Chrome 打不开」
    这件事有明确归因。
    """
    verdict = check_source(path)
    if verdict.severity == "ok":
        return None
    return verdict.message


__all__ = [
    "CATALOG_KEY_TYPES",
    "ReaderVerdict",
    "SourceVerdict",
    "UnusableSourceError",
    "catalog_structure_problems",
    "check_source",
    "ensure_readable",
    "ensure_source_usable",
    "inspect_pdf",
    "repair_pdf",
    "source_readability_warning",
    "strict_reader_check",
]
