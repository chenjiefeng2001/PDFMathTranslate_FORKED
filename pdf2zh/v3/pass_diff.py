"""Module: PassDiff — Phase D2 逐 Pass 快照差分（节点级 Before→After）。

编译器式 Debug：两次快照之间，按 NodeID 对齐，输出每个节点 / 字段的
变动（``node added / removed / changed``）。与 ``SnapshotStore`` 配对：
Diff 输入是两个快照 dict，输出结构化的 PassDiffReport —— 可直接回喂
Overlay / Inspector / Replay。

    from pdf2zh.v3.pass_diff import diff_snapshots, PassDiffReport
    report = diff_snapshots(before, after)
    print(report.summary())
    for e in report.entries: print(e.render())

纯逻辑、无 I/O、无外部依赖；节点 id 直接用快照里的 NodeID 字符串。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


@dataclass
class FieldDiff:
    """单字段差异记录（节点级）。"""

    node_id: str = ""
    field: str = ""
    kind: str = "changed"  # added | removed | changed
    before: Any = None
    after: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "field": self.field,
            "kind": self.kind,
            "before": self.before,
            "after": self.after,
        }

    def render(self) -> str:
        if self.kind == "added":
            return f"+ {self.node_id}  (node added)"
        if self.kind == "removed":
            return f"- {self.node_id}  (node removed)"
        fmt = lambda v: (jsonify(v)[:48] if not isinstance(v, str) else v[:48])
        return (
            f"~ {self.node_id}.{self.field}: {fmt(self.before)!r} → {fmt(self.after)!r}"
        )


def jsonify(v: Any) -> str:
    import json

    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(v)


@dataclass
class PassDiffReport:
    """两快照之间的完整差分：节点增删 + 每节点字段级变化。"""

    entries: List[FieldDiff] = field(default_factory=list)

    @property
    def added_nodes(self) -> List[str]:
        return [e.node_id for e in self.entries if e.kind == "added"]

    @property
    def removed_nodes(self) -> List[str]:
        return [e.node_id for e in self.entries if e.kind == "removed"]

    @property
    def changed_node_ids(self) -> List[str]:
        seen: List[str] = []
        for e in self.entries:
            if e.kind == "changed" and e.node_id not in seen:
                seen.append(e.node_id)
        return seen

    @property
    def empty(self) -> bool:
        return not self.entries

    def for_node(self, node_id: str) -> List[FieldDiff]:
        return [e for e in self.entries if e.node_id == node_id]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "added": len(self.added_nodes),
            "removed": len(self.removed_nodes),
            "changed_nodes": len(self.change_event_count()),
            "entries": [e.to_dict() for e in self.entries],
        }

    def change_event_count(self) -> int:
        return len(self.entries) - len(self.added_nodes) - len(self.removed_nodes)

    def summary(self) -> str:
        return (
            f"PassDiff changed={self.change_event_count()} "
            f"added={len(self.added_nodes)} removed={len(self.removed_nodes)}"
        )

    def render_lines(self) -> List[str]:
        return [e.render() for e in self.entries]


def _diff_objects(
    node_id: str,
    before: Any,
    after: Any,
    prefix: str,
    depth: int,
    max_depth: int,
    out: list,
    max_entries: int,
) -> None:
    """递归对比两个值，遇叶子不等产出 FieldDiff（深度护栏，防爆炸）。"""
    if len(out) >= max_entries:
        return
    if isinstance(before, dict) and isinstance(after, dict) and depth < max_depth:
        keys = sorted(set(before) | set(after))
        for k in keys:
            b, a = before.get(k), after.get(k)
            nxt = f"{prefix}.{k}" if prefix else k
            _diff_objects(node_id, b, a, nxt, depth + 1, max_depth, out, max_entries)
        return
    if isinstance(before, dict) or isinstance(after, dict):
        if _normalize(before) != _normalize(after):
            out.append(FieldDiff(node_id, prefix or "<self>", "changed", before, after))
        return
    if _normalize(before) != _normalize(after):
        out.append(FieldDiff(node_id, prefix or "<self>", "changed", before, after))


def _normalize(v: Any) -> Any:
    if isinstance(v, list):
        return [_normalize(x) for x in v]
    if isinstance(v, dict):
        return {k: _normalize(x) for k, x in v.items()}
    if isinstance(v, float):
        return round(v, 3)
    return v


def diff_snapshots(
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
    max_depth: int = 4,
    max_entries: int = 200,
) -> PassDiffReport:
    """按 NodeID 对齐两段快照：字段级差分（JSON 可序列化、供 View 渲染）。

    ``before/after`` 为 ``capture_snapshot``（或任意 {node_id: payload} dict）。
    节点级断言：快照统计差异只当 node 有字段变化才算变化，避免空差异噪音。
    """
    before_nodes = (before or {}).get("nodes", before or {})
    after_nodes = (after or {}).get("nodes", after or {})
    entries: List[FieldDiff] = []
    all_ids = sorted(set(before_nodes) | set(after_nodes))
    for nid in all_ids:
        b, a = before_nodes.get(nid), after_nodes.get(nid)
        if b is None and a is not None:
            entries.append(FieldDiff(nid, "", "added", None, a))
            continue
        if a is None and b is not None:
            entries.append(FieldDiff(nid, "", "removed", b, None))
            continue
        _diff_objects(nid, b, a, "", 0, max_depth, entries, max_entries)
    return PassDiffReport(entries)


def diff_json(before_path: str, after_path: str) -> PassDiffReport:
    """读取两份快照 JSON 文件后差分（供 CLI / 回归脚本）。"""
    import json

    with open(before_path, "r", encoding="utf-8") as f:
        b = json.load(f)
    with open(after_path, "r", encoding="utf-8") as f:
        a = json.load(f)
    return diff_snapshots(b, a)


def render_diff_report(report: PassDiffReport) -> str:
    if report.empty:
        return report.summary() + " (clean)"
    lines = [report.summary()] + report.render_lines()
    return "\n".join(lines)


__all__ = [
    "FieldDiff",
    "PassDiffReport",
    "diff_snapshots",
    "diff_json",
    "render_diff_report",
]
