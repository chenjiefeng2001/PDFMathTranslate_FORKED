"""8.3.1 段落级 Batch 翻译（外移至 v3/ 侧通道，满足 converter.py strangulation 约束）。

依据 ``doc/performance_bottleneck_report.md`` §6.6/§8.3.1：单页约 59 个段落
翻译请求，网络 RTT 是长文档的主要成本之一。本模块把同页多段落合并为单次
翻译请求（RTT 减 70–90%），返回后按强分隔符切回：

- 开关 ``PDF2ZH_PARAGRAPH_BATCH=1`` 启用（默认关闭）；未启用时本模块退化
  为逐段并发翻译（与 converter 原 ``executor.map`` 语义一致），
  converter.py 无需感知开关 —— 满足 strangulation 行数死线；
- 聚合规则：跳过空段 / 纯公式占位符（``{vN}``）/ TOC 行；
- 按字符预算（``PDF2ZH_PARAGRAPH_BATCH_CHARS``，默认 2000）分批；
- 严格还原校验：切回段数不符或出现空段（LLM 吞掉分隔符 / 篡改格式）
  → 整批逐段回退 ``safe_worker``，失败场景下与逐段翻译语义完全一致，
  绝不产出错误译文。
"""

from __future__ import annotations

import concurrent.futures
import os
import re
from typing import Callable, List, Optional, Sequence

__all__ = ["PARA_BATCH_SEP", "batch_translate_paragraphs"]

#: 强分隔符（文本中出现概率极低；被 LLM 改写时还原校验失败 → 逐段回退）。
PARA_BATCH_SEP = "\n\n=====PDF2ZH_PARAGRAPH_SEP=====\n\n"


def _translate_threads() -> int:
    """并发翻译线程数（``PDF2ZH_PARAGRAPH_BATCH_THREADS``，默认 4）。"""
    try:
        return max(1, int(os.environ.get("PDF2ZH_PARAGRAPH_BATCH_THREADS") or 4))
    except (TypeError, ValueError):
        return 4


def _batch_chars_budget() -> int:
    """单批字符预算（``PDF2ZH_PARAGRAPH_BATCH_CHARS``，默认 2000）。"""
    try:
        return max(
            200,
            min(int(os.environ.get("PDF2ZH_PARAGRAPH_BATCH_CHARS") or 2000), 16000),
        )
    except (TypeError, ValueError):
        return 2000


def batch_translate_paragraphs(
    texts: List[str],
    font_sigs: List[str],
    toc_specs: Optional[List[Optional[dict]]],
    safe_worker: Callable[[str, str], str],
    thread: int = 0,
    keep: Optional[Sequence[bool]] = None,
) -> List[str]:
    """同页多段合并翻译，返回与 ``texts`` 同序的译文列表。

    ``PDF2ZH_PARAGRAPH_BATCH != "1"`` 时退化为逐段并发翻译（语义与 converter
    原 ``executor.map(_safe_worker, sstk, _font_sigs)`` 一致），调用方无需感知。

    Args:
        texts: 段落文本列表（与 sstk 同序）。
        font_sigs: 每段的字体缓存签名（逐段回退时使用）。
        toc_specs: 每段的 TOC 元数据（None=非目录行；目录行不打包）。
        safe_worker: 逐段翻译回调 ``fn(text, font_sig) -> str``。
        thread: 并发线程数；0/负值回落默认 4。
        keep: 逐段布尔掩码；True 的段落**原样保留、绝不进翻译器**
            （Phase 1 代码保护）。默认全 False（正常翻译）。

    Returns:
        与 ``texts`` 同序的译文列表。
    """
    workers = max(1, thread or _translate_threads())
    if keep is None:
        keep = [False] * len(texts)

    if os.environ.get("PDF2ZH_PARAGRAPH_BATCH") != "1":
        # 开关关闭 → 逐段并发翻译（与原 executor.map 路径一致）；
        # keep=True 的段落走 passthrough（返回原文，不触碰翻译器/缓存）。
        def _fn(i: int) -> str:
            if keep[i]:
                return texts[i]
            return safe_worker(texts[i], font_sigs[i])

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as _ex:
            return list(_ex.map(_fn, range(len(texts))))

    sep = PARA_BATCH_SEP
    n = len(texts)
    news: List[Optional[str]] = [None] * n

    packable = []
    for i, t in enumerate(texts):
        if keep[i]:
            continue  # Phase 1 代码保护：绝不打包进批量翻译请求
        if not t.strip():
            continue
        if re.match(r"^\{\\?v\d+\}$", t.strip()):
            continue  # 纯公式占位符段
        if toc_specs is not None and toc_specs[i] is not None:
            continue  # 目录行单独翻译（compose_toc_title 依赖原样输出）
        packable.append(i)

    if len(packable) < 2:
        def _fn_short(i: int) -> str:
            if keep[i]:
                return texts[i]
            return safe_worker(texts[i], font_sigs[i])

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as _ex:
            return list(_ex.map(_fn_short, range(n)))

    # 按字符预算聚合成批
    budget = _batch_chars_budget()
    batches: List[List[int]] = []
    cur: List[int] = []
    cur_len = 0
    for i in packable:
        seg_len = len(texts[i]) + len(sep)
        if cur and cur_len + seg_len > budget:
            batches.append(cur)
            cur = []
            cur_len = 0
        cur.append(i)
        cur_len += seg_len
    if cur:
        batches.append(cur)

    def _translate_batch(indices: List[int]):
        joined = sep.join(texts[i] for i in indices)
        try:
            result = safe_worker(joined, "")
            parts = result.split(sep)
            if len(parts) == len(indices) and all(p.strip() for p in parts):
                return indices, parts
        except Exception:  # noqa: BLE001 -- 失败统一逐段回退
            pass
        # 还原失败 / 服务异常 → 逐段回退（与逐段语义完全一致）
        return indices, [safe_worker(texts[i], font_sigs[i]) for i in indices]

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_translate_batch, batches))
    for indices, parts in results:
        for i, part in zip(indices, parts):
            news[i] = part

    for i in range(n):
        if keep[i]:
            news[i] = texts[i]  # Phase 1 代码保护：原样保留（兼容非 Keep 集合外漏网）
        elif news[i] is None:
            news[i] = safe_worker(texts[i], font_sigs[i])
    return news
