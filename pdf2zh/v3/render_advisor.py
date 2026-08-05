"""Module: RenderAdvisor — V8.3「IR 侧通道 → 渲染接管」的决策机制。

主链路 side-channel 已能产出两类事实（V8.3 IR 快照 / V8.4 写回门控裁决），
但**渲染路径仍然完全由 legacy 排版器决定**。本模块提供"接管"的最小决策
机制：把门控裁决 + IR 角色（+ Processor 策略）合并成逐节点的渲染路由
（``translate_refit / shift_down / preserve_float / overlay / block``），
供迁移闭环内以 gate 判据切换渲染路径的消费端使用。

    RenderAdvisor.plan(gate_verdict, ir_snapshot)
        │
        ├── gate.writeback_allowed == False + overflow issues
        │       → 溢出段落路由为 shift_down / block
        ├── IR 角色 image/table/formula（受保护对象）
        │       → preserve_float（原文原位，不参与重排）
        ├── IR 角色 toc_entry / header / footer
        │       → overlay（不推挤正文流）
        └── 其余角色            → translate_refit（默认接管路径）

纯逻辑、无 I/O；输入是 side-channel 已经序列化的 dict（gate_verdicts /
ir_snapshots 快照节点），输出是确定性的 RenderPlan dict —— 与
image_engine / toc_semantics 同风格（引擎不建 IR，只产出决策）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ── 渲染路由（RenderPath）── 接管机制的离散输出 ────────────────────────────

#: 默认接管路径：译文按约束布局重排（gate 放行时使用）
PATH_TRANSLATE_REFIT = "translate_refit"
#: 译文膨胀后整体下移（gate 检测到溢出时的最小修正）
PATH_SHIFT_DOWN = "shift_down"
#: 原文对象原位保留（图片/表格/公式等保护对象）
PATH_PRESERVE_FLOAT = "preserve_float"
#: 译文叠加在原文上方（目录行/页眉页脚，不推挤正文流）
PATH_OVERLAY = "overlay"
#: 阻断写回（gate 判据：重叠率超限且无法解决）
PATH_BLOCK = "block"

#: 受保护对象角色（IR 快照 ``role`` 字段 → 原位保留）
_PRESERVE_FLOAT_ROLES = frozenset(
    {"figure", "image", "table", "formula", "formula_inline", "code"}
)
#: 叠加角色（不推挤正文流）
_OVERLAY_ROLES = frozenset({"toc_entry", "header", "footer"})


@dataclass
class RoutingDecision:
    """单个节点的渲染路由。"""

    node_id: str
    render_path: str = PATH_TRANSLATE_REFIT
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"node_id": self.node_id,
                "render_path": self.render_path,
                "reasons": list(self.reasons)}


class RenderAdvisor:
    """把 side-channel 产出（gate 裁决 + IR 角色）合并为渲染路由。

    纯函数式设计：``plan`` 无副作用，输入输出均为 dict/DataClass，
    可直接在迁移闭环中作为「gate 判据切换渲染路径」的决策器。
    """

    #: 溢出判定阈值：段落底部距页面下缘（pt），低于即视为溢出
    overflow_margin: float = 50.0

    def __init__(self, overflow_margin: float = 50.0) -> None:
        self.overflow_margin = overflow_margin

    # ── 主入口 ─────────────────────────────────────────────────────

    def plan(self, gate_verdict: Optional[dict] = None,
             snapshot_nodes: Optional[List[dict]] = None) -> dict:
        """为快照中的每个节点计算渲染路由。

        Args:
            gate_verdict: ``gate_verdicts[pageid]``（GatedResult.to_dict 输出）;
                None 表示门控未启用（默认全部 translate_refit）。
            snapshot_nodes: ``ir_snapshots[pageid]["paragraphs"|...]`` 合并后的
                节点列表（每节点含 id/role/bbox），可为 None。

        Returns:
            RenderPlan dict：
                {"admissible": bool, "overlap_rate": float,
                 "issues": [...], "routing": {node_id: {render_path, reasons}}}
        """
        gate = gate_verdict or {}
        allowed = bool(gate.get("writeback_allowed", True))
        issues = list(gate.get("issues") or [])
        rate = float(gate.get("overlap_rate", 0.0))
        page_h = float(gate.get("page_height", 792.0))

        overflow_ids = self._overflow_node_ids(issues, page_h)

        routing: Dict[str, RoutingDecision] = {}
        for node in snapshot_nodes or []:
            nid = str(node.get("id", ""))
            if not nid:
                continue
            decision = self._route_node(node, allowed, overflow_ids)
            routing[nid] = decision

        # 门控拒绝且存在无法路由的溢出块 → 整页不可写回（阻断）
        inadmissible = not allowed and bool(overflow_ids or issues)
        return {
            "admissible": not inadmissible,
            "overlap_rate": rate,
            "issues": issues,
            "routing": {k: v.to_dict() for k, v in routing.items()},
        }

    # ── 内部 ────────────────────────────────────────────────────────

    def _route_node(self, node: dict, allowed: bool,
                    overflow_ids: set) -> RoutingDecision:
        nid = str(node.get("id", ""))
        role = str(node.get("role", "")).lower()
        reasons: List[str] = []
        # 1. 受保护对象：原位保留（即使 gate 放行也不参与重排）
        if role in _PRESERVE_FLOAT_ROLES:
            return RoutingDecision(nid, PATH_PRESERVE_FLOAT,
                                   [f"role:{role}"])
        # 2. 叠加角色：不推挤正文流
        if role in _OVERLAY_ROLES:
            return RoutingDecision(nid, PATH_OVERLAY, [f"role:{role}"])
        # 3. gate 判据：溢出节点禁止原位写回
        if nid in overflow_ids:
            if not allowed:
                return RoutingDecision(nid, PATH_BLOCK,
                                       ["gate:overflow-unresolved"])
            return RoutingDecision(nid, PATH_SHIFT_DOWN,
                                   ["gate:overflow-shift-down"])
        # 4. gate 拒绝整页但本节点无溢出 → 仍按常规接管
        if not allowed:
            reasons.append("gate:writeback-rejected")
        reasons.append("default:translate_refit")
        return RoutingDecision(nid, PATH_TRANSLATE_REFIT, reasons)

    @staticmethod
    def _overflow_node_ids(issues: List[str], page_height: float) -> set:
        """从 gate issues 中提取溢出节点 id（``blocks overflow the page: [...]``）。"""
        overflow: set = set()
        for issue in issues or []:
            if "overflow" not in issue:
                continue
            for tok in issue.replace("[", " ").replace("]", " ").split():
                # gate 节点 id 形如 "p3_0"（page_index）或纯数字
                if re.match(r"^p\d+_\d+$", tok) or tok.isdigit():
                    overflow.add(tok)
        return overflow

    @staticmethod
    def summarize(plan: dict) -> str:
        routing = plan.get("routing", {})
        counts: Dict[str, int] = {}
        for r in routing.values():
            path = r.get("render_path", "?")
            counts[path] = counts.get(path, 0) + 1
        return (f"RenderPlan admissible={plan.get('admissible')} "
                f"overlap={plan.get('overlap_rate', 0.0):.3f} "
                f"{dict(sorted(counts.items()))}")


__all__ = [
    "PATH_TRANSLATE_REFIT", "PATH_SHIFT_DOWN", "PATH_PRESERVE_FLOAT",
    "PATH_OVERLAY", "PATH_BLOCK",
    "RoutingDecision", "RenderAdvisor",
]
