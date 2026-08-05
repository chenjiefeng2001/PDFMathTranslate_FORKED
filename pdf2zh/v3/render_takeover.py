"""Module: RenderTakeover — V8.3 后半程：gate 判据驱动的渲染路径切换（P1）。

IR 侧通道（V8.3 快照）+ 写回门控（V8.4 裁决）已就位，但**渲染路径仍完全
由 legacy 排版器决定**。本模块提供迁移闭环内"以 gate 判据切换渲染路径"
的纯逻辑消费端：

    writeback 块（_gate_records 派生）+ gate 裁决（gate_verdicts）
        │
        ▼
    plan_writeback_takeover   →  RenderAdvisor.plan（逐块渲染路由）
        │
        ▼
    apply_render_plan   →  调整/过滤后的写回块（shift_down / block / 保持）

纯数据进出：输入/输出均为 dict，单元测试驱动；物理写回与否由消费端
（迁移闭环）决定，本模块不触碰 converter/fitz。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from pdf2zh.v3.render_advisor import (
    PATH_BLOCK,
    PATH_PRESERVE_FLOAT,
    PATH_SHIFT_DOWN,
    PATH_TRANSLATE_REFIT,
    RenderAdvisor,
)


@dataclass
class WritebackBlock:
    """参与接管计划的写回块（gate_verdicts 的 blocks 派生）。"""

    node_id: str
    x: float = 0.0
    y: float = 0.0
    width: float = 400.0
    height: float = 20.0
    page: int = 0
    font_size: float = 12.0
    node_type: str = "paragraph"
    text: str = ""
    translated: str = ""

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id, "page": self.page,
            "text": self.text, "translated": self.translated,
            "x": self.x, "y": self.y,
            "width": self.width, "height": self.height,
            "font_size": self.font_size, "node_type": self.node_type,
        }


def _verdict_block_dict(b) -> dict:
    """GateBlock/GatedResult block → 节点 dict（RenderAdvisor 输入）。"""
    data = getattr(b, "node_id", None)
    if data is None and isinstance(b, dict):
        return {
            "id": b.get("node_id", ""),
            "role": b.get("node_type", "paragraph"),
            "y": b.get("y", 0.0),
            "height": b.get("height", 1.0),
        }
    return {
        "id": getattr(b, "node_id", ""),
        "role": getattr(b, "node_type", "paragraph"),
        "y": getattr(b, "y", 0.0),
        "height": getattr(b, "height", 1.0),
    }


def plan_writeback_takeover(blocks, verdict: Optional[dict] = None,
                            advisor: Optional[RenderAdvisor] = None) -> dict:
    """按 gate 判据为每个写回块计算渲染路由。

    ``verdict`` 为 ``gate_verdicts[pageid]``（GatedResult.to_dict 输出）；
    None 表示门控未启用（默认全部 translate_refit）。
    返回 RenderRenderPlan dict（``admissible`` / ``routing`` …）。
    """
    gate = dict(verdict or {"writeback_allowed": True, "issues": [],
                            "overlap_rate": 0.0, "page_height": 792.0})
    nodes = [_verdict_block_dict(b) for b in (blocks or [])]
    plan = (advisor or RenderAdvisor()).plan(gate_verdict=gate,
                                             snapshot_nodes=nodes)
    return plan


def apply_render_plan(plan: Optional[dict],
                      blocks: Sequence[WritebackBlock],
                      shift_amount: Optional[float] = None) -> List[dict]:
    """把 RenderPlan 应用到写回块列表。

    - ``block`` 路径 → 块从写回列表剔除（gate 判据：溢出未解决）；
    - ``shift_down`` 路径 → y 下移 ``shift_amount``（缺失时按该块高度）；
    - 其余路径原样保留（translate_refit / preserve_float / overlay）。
    返回调整后的块 dict 列表（纯数据，供接管方消费）。
    """
    routing: Dict[str, dict] = {}
    if plan:
        routing = plan.get("routing", {}) or {}
    out: List[dict] = []
    for block in blocks:
        if isinstance(block, WritebackBlock):
            node_id = block.node_id
        else:
            node_id = getattr(block, "node_id", "") or (block.get("node_id", "") if isinstance(block, dict) else "")
        r = routing.get(node_id, {})
        path = r.get("render_path", PATH_TRANSLATE_REFIT)
        d = block.to_dict() if hasattr(block, "to_dict") else dict(block)
        if path == PATH_BLOCK:
            continue
        if path == PATH_SHIFT_DOWN:
            amount = shift_amount if shift_amount is not None \
                else float(d.get("height", 1.0))
            d["y"] = float(d.get("y", 0.0)) + amount
            d["render_path"] = path
        else:
            d["render_path"] = path
        out.append(d)
    return out


__all__ = [
    "WritebackBlock", "plan_writeback_takeover",
    "apply_render_plan",
]