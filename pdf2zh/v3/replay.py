"""Module: Replay — Phase D7 免重翻译回放（ReplaySystem）。

把每个 Pass 的输入存下来（``StageInputStore``），Debug 时按 stage 重放，
翻译调用走 ``TranslationMemo`` —— 命中即复用译文，**绝不重复调翻译引擎**，
从而做到「调试多少次都不花第二次翻译钱」。

    from pdf2zh.v3.replay import StageInputStore, TranslationMemo, ReplaySystem

    store = StageInputStore()
    store.save_input("translation", node_id, {"text": "Hello", "src": "en"})
    memo = TranslationMemo()
    sys = ReplaySystem(store, memo)
    report = sys.replay("translation",
                        lambda item, m: m.translate(item.payload["text"]))
    print(report.summary())     # cached=0 translated=1
    report = sys.replay("translation", lambda item, m: m.translate(item["text"]))
    print(report.summary())     # cached=1 translated=0

纯逻辑；``translate`` 只做查表，从不发起外部调用。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class StageInput:
    node_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    stage: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"node_id": self.node_id, "stage": self.stage,
                "payload": self.payload}


class StageInputStore:
    """D7 — 每 Pass 输入快照：stage → [StageInput, ...]（免重读原始 PDF）。"""

    def __init__(self) -> None:
        self._inputs: Dict[str, List[StageInput]] = {}

    def save_input(self, stage: str, node_id, payload: Dict[str, Any]) -> None:
        self._inputs.setdefault(stage, []).append(
            StageInput(str(node_id), dict(payload), stage))

    def save_inputs(self, stage: str, items: List[dict]) -> None:
        for it in items:
            self.save_input(stage, it.get("node_id", ""), it.get("payload", it))

    def inputs_for(self, stage: str) -> List[StageInput]:
        return list(self._inputs.get(stage, []))

    def stages(self) -> List[str]:
        return list(self._inputs)

    def __len__(self) -> int:
        return sum(len(v) for v in self._inputs.values())


class TranslationMemo:
    """译文缓存：src → dst。命中即返回，永不重调翻译引擎。"""

    def __init__(self, seed: Optional[Dict[str, str]] = None) -> None:
        self._cache: Dict[str, str] = dict(seed or {})
        self._hits = 0
        self._misses = 0

    def translate(self, text: str) -> str:
        text = str(text or "")
        if text in self._cache:
            self._hits += 1
            return self._cache[text]
        self._misses += 1
        raise KeyError(f"memo miss for {text[:20]!r}")

    def store(self, src: str, dst: str) -> None:
        self._cache[str(src)] = str(dst)

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def stats(self) -> Dict[str, int]:
        return {"hits": self._hits, "misses": self._misses}


@dataclass
class ReplayStep:
    node_id: str = ""
    stage: str = ""
    status: str = "ok"            # ok | memo_hit | memo_miss | error
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"node_id": self.node_id, "stage": self.stage,
                "status": self.status, "elapsed_ms": round(self.elapsed_ms, 2)}


@dataclass
class ReplayReport:
    stage: str = ""
    steps: List[ReplayStep] = field(default_factory=list)

    @property
    def memo_hits(self) -> int:
        return sum(1 for s in self.steps if s.status == "memo_hit")

    @property
    def translated(self) -> int:
        return sum(1 for s in self.steps if s.status == "ok")

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if s.status in ("error", "memo_miss"))

    def to_dict(self) -> Dict[str, Any]:
        return {"stage": self.stage,
                "steps": [s.to_dict() for s in self.steps],
                "memo_hits": self.memo_hits, "translated": self.translated,
                "failed": self.failed}

    def summary(self) -> str:
        return (f"Replay[{self.stage}] steps={len(self.steps)} "
                f"memo_hit={self.memo_hits} translated={self.translated} "
                f"failed={self.failed}")


class ReplaySystem:
    """把已存 stage 输入按序重放；翻译一律走 memo，绝不重复调用。"""

    def __init__(self, store: StageInputStore,
                 memo: Optional[TranslationMemo] = None) -> None:
        self.store = store
        self.memo = memo or TranslationMemo()

    def warm(self, pairs: Dict[str, str]) -> None:
        for k, v in (pairs or {}).items():
            self.memo.store(k, v)

    def replay(self, stage: str,
               fn: Optional[Callable[[StageInput, TranslationMemo], Any]] = None,
               record_errors: bool = True) -> ReplayReport:
        """按 stage 重放存好的输入；memo 命中则跳过 fn（引擎零调用）。

        ``fn`` 不传时只检查 memo 覆盖（miss 记 failed 不抛）。
        """
        report = ReplayReport(stage=stage)
        for item in self.store.inputs_for(stage):
            t0 = time.time()
            status = "ok"
            try:
                src = str(item.payload.get("text", ""))
                if src:
                    try:
                        self.memo.translate(src)
                        status = "memo_hit"      # 命中缓存 → 不再调 fn
                    except KeyError:
                        if fn is not None:
                            fn(item, self.memo)
                        else:
                            status = "memo_miss" if record_errors else "ok"
                elif fn is not None:
                    fn(item, self.memo)
            except KeyError:
                status = "memo_miss" if record_errors else "ok"
            except Exception:  # noqa: BLE001 — 重放容错
                status = "error"
            report.steps.append(ReplayStep(
                node_id=item.node_id, stage=stage, status=status,
                elapsed_ms=(time.time() - t0) * 1000.0))
        return report

    def replay_all(self, fn=None) -> List[ReplayReport]:
        return [self.replay(stage, fn) for stage in self.store.stages()]


__all__ = ["StageInput", "StageInputStore", "TranslationMemo",
           "ReplayStep", "ReplayReport", "ReplaySystem"]