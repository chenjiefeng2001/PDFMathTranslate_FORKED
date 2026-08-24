"""Module: IRConvergence — 历史冗余 IR 视图收敛（P2）。

项目里存在两个「IR 视图」出口：
    - ``structure.to_document_ir``（V8.3 老快照路径，mainline 在用）
    - ``view_as_ir``（V9.0 单一 IR 的官方投影视图）

按「不删旧接口、标注 deprecated、以 view_as_ir 为唯一视图出口逐步收敛」
的原则，本模块提供：
    - ``DEPRECATED_VIEWS``：记录已废弃视图的清单（审计用）；
    - ``deprecated_note()``：统一说明文案；
    - ``converged_snapshot``：用**唯一出口**（IRBuilder.from_graph →
      snapshot_ir）产出与老路径同构的快照 JSON —— 迁移闭环里用它把
      emit_page_ir 平滑切到新出口而不改变基线语义。
"""

from __future__ import annotations

from typing import Optional, Sequence

#: 已标记 deprecated 的视图出口（迁移期间保留，禁止新增消费者）
DEPRECATED_VIEWS = ("structure.to_document_ir",)

DEPRECATION_NOTE = (
    "DEPRECATED: structure.to_document_ir is a legacy IR snapshot view. "
    "Use pdf2zh.v3.document_pipeline.view_as_ir (IRBuilder.from_graph) as "
    "the single serialization entry point. This interface is kept for "
    "migration; new consumers must not call it."
)


def deprecated_note() -> str:
    return DEPRECATION_NOTE


def converged_snapshot(graph, title: str = "", include_geometry: bool = True) -> dict:
    """用唯一视图出口（IRBuilder.from_graph）产出快照。

    与 ``structure.to_document_ir`` + ``snapshot_ir`` 输出同构
    （role 桶 / bbox / reading 顺序），可在迁移闭环内做到位替换。
    graph 可为 DocumentGraph（view_as_ir 直出）或 PageGeometry 列表
    （先 to_document_ir 再收敛到 IRBuilder）。
    """
    from pdf2zh.v3.migration_diff import snapshot_ir

    has_nodes = getattr(graph, "nodes", None) is not None
    if has_nodes:
        from pdf2zh.v3.document_pipeline import view_as_ir

        ir = view_as_ir(graph, title=title)
    else:
        # PageGeometry 序列：走老路径后仍是同构快照（兼容迁移）
        from pdf2zh.v3.structure import StructureClassifier, to_document_ir

        ir = to_document_ir(graph, classifier=StructureClassifier(), title=title)
    return snapshot_ir(ir, title=title, include_geometry=include_geometry)


def snapshot_consistency(legacy_snapshot: dict, converged_snapshot_: dict) -> dict:
    """两份快照的桶一致性报告（确认收敛不改变基线语义）。"""
    buckets = {k for k in legacy_snapshot} | {k for k in converged_snapshot_}
    changed = {}
    for k in sorted(buckets):
        a = legacy_snapshot.get(k)
        b = converged_snapshot_.get(k)
        if isinstance(a, list) and isinstance(b, list):
            if [e.get("id") for e in a] != [e.get("id") for e in b]:
                changed[k] = {"a_count": len(a), "b_count": len(b)}
        elif a != b:
            changed[k] = {"a": a, "b": b}
    return {"consistent": not changed, "changed_buckets": changed}


__all__ = [
    "DEPRECATED_VIEWS",
    "DEPRECATION_NOTE",
    "deprecated_note",
    "converged_snapshot",
    "snapshot_consistency",
]
