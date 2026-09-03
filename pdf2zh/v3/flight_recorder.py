"""Flight Recorder — production runtime trace for the magicpdf render path.

MECH-4 lesson (7N-8): span/bbox numbers alone could not tell an auditor
whether ``y=366.58`` meant box-top, baseline or bottom.  This module turns
the *real* plan → fixup → renderer → raster run into a structured,
correlated, replayable event stream (JSONL) where every coordinate carries
its **semantic type** (value + space + origin + meaning), and every block
keeps one eternal identity: ``trace_id = "<page>/<block_id>"``.

Levels (cheap by default, expensive on demand):

- Level 0 — metadata: run/page/block/command/bbox/font/semantics/timing.
- Level 1 — geometry: source/destination/erase bbox, baseline, expected
  baseline, line boxes, ink-expected region, plan-level overlaps.
- Level 2 — raster evidence: src/mono crops + pixel diff + ink accounting,
  generated only for blocks a rule already flagged (see ``trace_audit``).

The consumer is ``trace_rules`` (invariant engine) + ``trace_audit`` (CLI):
rules never guess what a number means — the recorder already declared it.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence

#: v3 规范树坐标系（左下原点、y 向上，pdfminer 惯例）—— plan 层。
SPACE_V3 = "v3"
#: fitz/PDF 坐标系（左上原点、y 向下）—— renderer / raster 层。
SPACE_FITZ = "fitz"

#: 坐标语义（meaning）。同一数值在不同阶段必须声明不同 meaning，
#: 审计器据此直接判定 semantic mismatch（MECH-4 的运行时版本）。
MEANING_BOX_TOP = "box_top"
MEANING_BOX_BOTTOM = "box_bottom"
MEANING_BOX_LEFT = "box_left"
MEANING_BASELINE = "baseline"
MEANING_X = "x"

_ORIGIN_V3 = "lower-left"
_ORIGIN_FITZ = "top-left"


@dataclass(frozen=True)
class Coord:
    """A number is not a coordinate — a number + a declared semantics is.

    ``value`` is always the raw number as the emitting stage saw it;
    ``space``/``origin`` pin the frame (v3 y-up vs fitz y-down); ``meaning``
    states what the number *is* (box_top / baseline / ...).
    """

    value: float
    space: str = SPACE_V3
    origin: str = _ORIGIN_V3
    meaning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": round(float(self.value), 2),
            "space": self.space,
            "origin": self.origin,
            "meaning": self.meaning,
        }


@dataclass
class TraceContext:
    """One block's identity, carried unchanged across every stage.

    ``trace_id = f"{page_no}/{block_id}"`` is the eternal key linking
    translation → plan → fixup → renderer → raster for one block.
    """

    run_id: str
    book_id: str
    page_no: int
    block_id: str
    stage: str

    @property
    def trace_id(self) -> str:
        return f"{self.page_no}/{self.block_id}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "book_id": self.book_id,
            "page": self.page_no,
            "block_id": self.block_id,
            "trace_id": self.trace_id,
            "stage": self.stage,
        }


def make_run_id(book_id: str, stamp: Optional[str] = None) -> str:
    """``<book>-<YYYYMMDD>-<seq>`` style run id (unique per process)."""
    day = stamp or time.strftime("%Y%m%d")
    return f"{book_id}-{day}-{uuid.uuid4().hex[:6]}"


class FlightRecorder:
    """JSONL trace sink — streaming, crash-safe, grep-able.

    One line per event; events for a 562-page book stream to disk instead
    of a single giant JSON (a crash never destroys the whole trace, and the
    auditor can read only ``page=442``).

    All methods are no-ops when ``enabled=False`` (recorder constructed
    without a path), so instrumentation sites stay cheap and opt-in.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        run_id: Optional[str] = None,
        book_id: str = "book",
        level: int = 1,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.path = path
        self.enabled = bool(path)
        self.run_id = run_id or (make_run_id(book_id) if path else "no-trace")
        self.book_id = book_id
        self.level = int(level or 0)
        self.extra = dict(extra or {})
        self._fh = None
        self._count = 0
        self._started = time.time()
        if self.enabled:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            self._fh = open(path, "w", encoding="utf-8")
            self.emit(
                "run.begin",
                TraceContext(self.run_id, book_id, -1, "*", "run"),
                {
                    "level": self.level,
                    "started_ts": self._started,
                    "extra": self.extra,
                },
            )

    # ── emission ────────────────────────────────────────────────────────

    def emit(
        self,
        event: str,
        ctx: TraceContext,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write one event line (no-op when disabled).  Never raises on I/O
        failure — a recorder must not break the translation pipeline."""
        if not self.enabled or self._fh is None:
            return
        try:
            rec: Dict[str, Any] = {
                "event": event,
                "ts": round(time.time(), 4),
            }
            rec.update(ctx.to_dict())
            if payload:
                rec["payload"] = _json_safe(payload)
            self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._count += 1
        except Exception:  # noqa: BLE001 -- recorder failure is never fatal
            pass

    def ctx(self, page_no: int, block_id: str, stage: str) -> TraceContext:
        return TraceContext(self.run_id, self.book_id, page_no, block_id, stage)

    def close(self) -> None:
        if not self.enabled or self._fh is None:
            return
        try:
            self.emit(
                "run.end",
                TraceContext(self.run_id, self.book_id, -1, "*", "run"),
                {
                    "events": self._count,
                    "duration": round(time.time() - self._started, 3),
                },
            )
            self._fh.flush()
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._fh = None

    def __enter__(self) -> "FlightRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def count(self) -> int:
        return self._count


# ── reading / indexing ──────────────────────────────────────────────────


def read_events(path: str) -> Iterator[Dict[str, Any]]:
    """Stream a JSONL trace file (skips corrupt lines defensively)."""
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except (ValueError, TypeError):  # noqa: BLE001 -- one bad line ≠ dead trace
                continue


def build_trace_index(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """``trace-index.json``: quick per-page / per-trace_id lookup.

    For every trace_id records which stages emitted, how many events, and
    the first/last event name — the auditor uses it to answer "did this
    block traverse plan → render → raster?" in one lookup.
    """
    pages: Dict[int, int] = {}
    per_trace: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        pno = int(ev.get("page") or -1)
        pages[pno] = pages.get(pno, 0) + 1
        tid = ev.get("trace_id") or f"{pno}/{ev.get('block_id') or '?'}"
        rec = per_trace.setdefault(
            tid, {"trace_id": tid, "page": pno, "stages": [], "events": 0, "kinds": []}
        )
        rec["events"] += 1
        stage = ev.get("stage")
        if stage and stage not in rec["stages"]:
            rec["stages"].append(stage)
        kind = (ev.get("payload") or {}).get("kind")
        if kind and kind not in rec["kinds"]:
            rec["kinds"].append(kind)
    return {
        "run_ids": sorted({str(ev.get("run_id")) for ev in events}),
        "books": sorted({str(ev.get("book_id")) for ev in events}),
        "total_events": len(events),
        "page_events": {str(k): v for k, v in sorted(pages.items())},
        "traces": sorted(per_trace.values(), key=lambda t: t["trace_id"]),
    }


def write_trace_index(
    events: Sequence[Dict[str, Any]], out_path: str
) -> Dict[str, Any]:
    idx = build_trace_index(events)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, ensure_ascii=False, indent=1)
    return idx


def _json_safe(obj: Any) -> Any:
    """Drop non-JSON values defensively (never raise inside emit)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Coord):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    if hasattr(obj, "to_dict"):
        try:
            return _json_safe(obj.to_dict())
        except Exception:  # noqa: BLE001
            pass
    return str(obj)


__all__ = [
    "Coord",
    "TraceContext",
    "FlightRecorder",
    "read_events",
    "build_trace_index",
    "write_trace_index",
    "make_run_id",
    "SPACE_V3",
    "SPACE_FITZ",
    "MEANING_BOX_TOP",
    "MEANING_BOX_BOTTOM",
    "MEANING_BOX_LEFT",
    "MEANING_BASELINE",
    "MEANING_X",
]
