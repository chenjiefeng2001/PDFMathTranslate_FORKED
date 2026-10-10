"""8.3.1 段落级 Batch 翻译（外移至 v3/ 侧通道，满足 converter.py strangulation 约束）。

依据 ``doc/performance_bottleneck_report.md`` §6.6/§8.3.1：单页约 59 个段落
翻译请求，网络 RTT 是长文档的主要成本之一。本模块把同页多段落合并为单次
翻译请求（RTT 减 70–90%），返回后按强分隔符切回：

- 开关 ``PDF2ZH_PARAGRAPH_BATCH=0`` 关闭（默认**开启**）；关闭时本模块退化
  为逐段并发翻译（与 converter 原 ``executor.map`` 语义一致），
  converter.py 无需感知开关 —— 满足 strangulation 行数死线；
- 聚合规则：跳过空段 / 纯公式占位符（``{vN}``）/ TOC 行；
- 按字符预算（``PDF2ZH_PARAGRAPH_BATCH_CHARS``，默认 2000）分批；
- 严格还原校验：切回段数不符或出现空段（LLM 吞掉分隔符 / 篡改格式）
  → 整批逐段回退 ``safe_worker``，失败场景下与逐段翻译语义完全一致，
  绝不产出错误译文。

为什么默认开启
--------------
默认关闭时每个段落一次请求。**请求次数就是 RPM**：325 页 / 1787 块、
``--thread 4`` 实测 90 秒发出 1367 个请求（约 15 次/秒），匿名端点
（google /m）直接从限流变成回 ``/sorry/index`` 验证码页，整书翻成原文。
打包把 N 个段落合成 1 次请求，是这里唯一能真正把 RPM 降下来的手段
（限流只是把速率摊平，请求次数一个没少）。

代价：单个请求体变大。对匿名 endpoints 而言换来的是不被封；对 LLM
（opencode/openai…）而言本来一次请求就要秒级，合并反而更划算。还原校验
失败会整批回落逐段，正确性不受影响。
"""

from __future__ import annotations

import concurrent.futures
import os
import queue
import re
import threading
import time
from typing import Callable, List, Optional, Sequence

__all__ = ["PARA_BATCH_SEP", "batch_translate_paragraphs", "make_batching_translate_fn"]

#: 强分隔符（文本中出现概率极低；被 LLM 改写时还原校验失败 → 逐段回退）。
PARA_BATCH_SEP = "\n\n=====PDF2ZH_PARAGRAPH_SEP=====\n\n"


def _batch_enabled() -> bool:
    """段落打包开关：``PDF2ZH_PARAGRAPH_BATCH=0`` 关闭，其余视为开启。

    默认开启的理由见模块 docstring：请求次数就是 RPM，不打包时长文档会把
    匿名翻译端点的 IP 限额打爆（实测 90 秒 1367 次请求 → 整书 429 回落原文）。
    """
    return os.environ.get("PDF2ZH_PARAGRAPH_BATCH", "1") != "0"


def _translate_threads() -> int:
    """并发翻译线程数（``PDF2ZH_PARAGRAPH_BATCH_THREADS``，默认 4）。"""
    try:
        return max(1, int(os.environ.get("PDF2ZH_PARAGRAPH_BATCH_THREADS") or 4))
    except (TypeError, ValueError):
        return 4


def _batch_chars_budget() -> int:
    """单批字符预算（``PDF2ZH_PARAGRAPH_BATCH_CHARS``，默认 4000）。

    默认 2000 时一页约 59 段仍要分 3~4 批。4000 是"请求体别大到被网关拒"
    和"请求次数尽量少"之间的折中：**请求次数就是 RPM**，而匿名端点封的是
    IP 而不是单请求大小。上限 16000 不变。
    """
    try:
        return max(
            200,
            min(int(os.environ.get("PDF2ZH_PARAGRAPH_BATCH_CHARS") or 4000), 16000),
        )
    except (TypeError, ValueError):
        return 4000


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
    # 防御性对齐：侧通道钩子改写 texts 后 font_sigs 必须同步（历史 bug：
    # toc_split 重切 sstk 漏改 pfkstk → font_sigs[i] IndexError 崩页）。
    # 失同步时兜底补齐/截断，保证任何 hook 失步不再炸翻译。
    if len(font_sigs) != len(texts):
        font_sigs = (
            list(font_sigs) + [""] * (len(texts) - len(font_sigs))
            if len(font_sigs) < len(texts)
            else font_sigs[: len(texts)]
        )
    workers = max(1, thread or _translate_threads())
    if keep is None:
        keep = [False] * len(texts)

    if not _batch_enabled():
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


# ── 动态批处理：并发到达的 translate_fn 攒成一撮 ──────────────────────────
#: 攒批窗口（秒）。只影响"并发请求正在到达"的场景；串行翻译不等待。
_BATCH_WAIT = 0.025


def _flush_batch(inner, batch, sep):
    """把一批文本合成一次请求，再按分隔符切回；任何不一致都整批逐段回落。"""
    if len(batch) == 1:
        text, ev, box = batch[0]
        try:
            box["ok"] = True
            box["result"] = inner(text)
        except BaseException as exc:  # noqa: BLE001
            box["ok"] = False
            box["error"] = exc
        finally:
            ev.set()
        return

    joined = sep.join(t for t, _ev, _box in batch)
    try:
        parts = inner(joined).split(sep)
        if len(parts) != len(batch) or not all(p.strip() for p in parts):
            raise ValueError("reassembly failed")
    except BaseException:  # noqa: BLE001 -- 整批逐段回落，语义与不打包一致
        for text, ev, box in batch:
            try:
                box["ok"] = True
                box["result"] = inner(text)
            except BaseException as exc:  # noqa: BLE001
                box["ok"] = False
                box["error"] = exc
            finally:
                ev.set()
        return

    for (text, ev, box), part in zip(batch, parts):
        box["ok"] = True
        box["result"] = part
        ev.set()


def make_batching_translate_fn(
    inner,
    max_chars: int = 4000,
    wait: float = _BATCH_WAIT,
    enabled: bool = True,
):
    """给 ``translate_fn`` 包一层动态批处理：并发到达的请求合并成一次调用。

    为什么需要它：``document_model.translate_document`` 按块并发调 ``translate_fn``
    （magicpdf 链路，GUI 也在上面）。**请求次数就是 RPM** —— 实测 325 页 / 1787 块、
    ``--thread 4`` 在 90 秒内发出 1367 个请求，匿名 Google 端点直接回
    ``/sorry/index`` 验证码页，整书回落原文。上面的静态打包
    （``batch_translate_paragraphs``）只服务于 legacy ``converter.py``，magicpdf
    这条链路一直是"一块一请求"。

    做法：调用方无感知。请求先进队列，一个后台 flush 线程把当前积压攒成一批，
    用强分隔符合并成**一次**调用，返回后按分隔符切回各处。

    语义保证（与不打包逐字一致）：
    - 分隔符被吞 / 段数不符 / 出现空段 / 任何异常 → 整批**逐段回落**，
      绝不产出错位译文；
    - 空白文本直接返回原文，**不触碰翻译器** —— 与静态打包一致：
      白占一次请求就是白占一份 RPM。

    ``wait`` 是攒批窗口。串行场景下（``workers<=1``）调用方根本不会包装本函数，
    所以这里不必区分"独孤请求"，只管把积压攒大。
    """
    sep = PARA_BATCH_SEP
    q: "queue.Queue" = queue.Queue()

    def _flusher():
        while True:
            first = q.get()
            if first is None:
                return
            batch = [first]
            total = len(first[0])
            deadline = time.monotonic() + wait
            while total < max_chars:
                try:
                    nxt = q.get_nowait()
                    batch.append(nxt)
                    total += len(nxt[0])
                except queue.Empty:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.002)
            try:
                _flush_batch(inner, batch, sep)
            except BaseException:  # noqa: BLE001
                # 兜底：任何漏网异常都不许把等结果的调用方永久挂住。挂住比报错糟得
                # 多 —— 整篇翻译静默卡死，连"回落原文"的机会都没有。
                for _text, ev, box in batch:
                    if not box.get("ok"):
                        box["ok"] = False
                        box.setdefault("error", RuntimeError("packing flusher failed"))
                    ev.set()

    threading.Thread(target=_flusher, name="para-batch-flush", daemon=True).start()

    def fn(text):
        if not (text or "").strip():
            return text  # 空白段原样保留：不发请求
        ev = threading.Event()
        box: dict = {}
        q.put((text, ev, box))
        ev.wait()
        if not box.get("ok"):
            raise box["error"]
        return box["result"]

    return fn
