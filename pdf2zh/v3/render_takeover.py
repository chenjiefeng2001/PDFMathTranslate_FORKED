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
import copy
import math
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
            "node_id": self.node_id,
            "page": self.page,
            "text": self.text,
            "translated": self.translated,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "font_size": self.font_size,
            "node_type": self.node_type,
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


def plan_writeback_takeover(
    blocks, verdict: Optional[dict] = None, advisor: Optional[RenderAdvisor] = None
) -> dict:
    """按 gate 判据为每个写回块计算渲染路由。

    ``verdict`` 为 ``gate_verdicts[pageid]``（GatedResult.to_dict 输出）；
    None 表示门控未启用（默认全部 translate_refit）。
    返回 RenderRenderPlan dict（``admissible`` / ``routing`` …）。
    """
    gate = dict(
        verdict
        or {
            "writeback_allowed": True,
            "issues": [],
            "overlap_rate": 0.0,
            "page_height": 792.0,
        }
    )
    nodes = [_verdict_block_dict(b) for b in (blocks or [])]
    plan = (advisor or RenderAdvisor()).plan(gate_verdict=gate, snapshot_nodes=nodes)
    return plan


def apply_render_plan(
    plan: Optional[dict],
    blocks: Sequence[WritebackBlock],
    shift_amount: Optional[float] = None,
) -> List[dict]:
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
            node_id = getattr(block, "node_id", "") or (
                block.get("node_id", "") if isinstance(block, dict) else ""
            )
        r = routing.get(node_id, {})
        path = r.get("render_path", PATH_TRANSLATE_REFIT)
        d = block.to_dict() if hasattr(block, "to_dict") else dict(block)
        if path == PATH_BLOCK:
            continue
        if path == PATH_SHIFT_DOWN:
            amount = (
                shift_amount
                if shift_amount is not None
                else float(d.get("height", 1.0))
            )
            d["y"] = float(d.get("y", 0.0)) + amount
            d["render_path"] = path
        else:
            d["render_path"] = path
        out.append(d)
    return out


def fixup_render_plan(
    plan: Optional[Sequence[dict]],
    page_height: Optional[Dict[int, float]] = None,
    trace=None,
) -> Tuple[List[dict], dict]:
    """RenderTakeover 渲染计划修正（Step 3.x / magicpdf RenderTakeover）。

    对 :func:`pdf2zh.v3.document_model.render_plan_from_model` 产出的逐块渲染
    计划做两件事：

    - **preserve_float 保持**：``code``/``formula``/``figure``/``table`` 等
      保留块 ``dst_box`` 强制等于 ``src_box``（原样原位，不参与重排）；
    - **溢出下移**：翻译文本显著变长导致估算行数超出 ``dst_box`` 高度时，
      在页面剩余空间内整体下移（``shift_down``），空间不足则原地保持并打
      ``overflowed`` 标记（交给后续渲染器决定截断/换页）。

    7N-FIX-3（方向修正）：v3 为 y-up（左下原点、y 向上），页面下方是 −Δy，
    因此 shift_down 对 ``dst_box`` 与 commands 施加 **−shift**（原实现 +
    shift 把盒子移向页顶，渲染后译文上浮压住上一行，MECH-4 全书 153/153
    复现）。``page_height`` 参数保留仅为 API 兼容。

    ``trace``（可选）：FlightRecorder —— 每块发射 ``plan.shift_down`` /
    ``plan.keep`` / ``plan.preserve`` / ``plan.overflowed`` 决策事件，携带
    src/dst box 与 Δy（v3 y-up：向下为负，供 SHIFT_DIRECTION 规则消费）。

    纯数据进出，无 I/O；输入/输出 dict 结构不变（仅修正 ``dst_box`` 与附加
    ``render_fixup`` 键），单元测试可直接驱动。

    Returns:
        ``(fixed_plan, stats)``，``stats`` 含 ``preserved`` / ``shifted`` /
        ``overflowed`` 计数与明细。
    """
    fixed: List[dict] = []
    stats: Dict[str, Any] = {
        "preserved": 0,
        "shifted": 0,
        "overflowed": 0,
        "fixed": [],
    }
    for item in list(plan or []):
        item = dict(item)
        src = list(item.get("src_box") or [0, 0, 0, 0])
        dst = list(item.get("dst_box") or list(src))
        path = item.get("render_path", "translate_refit")
        kind = item.get("kind", "")

        if path == "preserve_float" or kind in _PRESERVE_KINDS:
            # 保留块：dst 恒等于 src，不做任何位移。
            item["dst_box"] = [round(v, 2) for v in src]
            item["render_fixup"] = "preserve"
            item["render_path"] = path
            fixed.append(item)
            stats["preserved"] += 1
            _emit_fixup_trace(trace, item, "preserve")
            continue

        # 7N-FIX-2（Invariant 4）：后续 shift_down 需要同时改写嵌套的
        # render_payload.commands（MECH-2 坐标脱钩修复），此处必须深拷贝 ——
        # 上面的 ``dict(item)`` 是浅拷贝，嵌套 commands 仍是调用方原计划的
        # 同一批 dict，就地改 y 会污染输入。
        item = copy.deepcopy(item)
        translated = item.get("translated") or item.get("text") or ""
        font_size = float(item.get("font_size") or 12.0)
        box_w = max(0.1, float(dst[2] - dst[0]))
        box_h = max(0.1, float(dst[3] - dst[1]))
        line_h = max(2.0, font_size * 1.4)
        # 行数估算：全角（CJK/宽字符）按 1.0em、半角按 0.5em 计宽，
        # 避免把大量全角文本低估成一行。
        est_width = sum(
            font_size * (1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.5)
            for ch in translated
        )
        est_lines = max(1.0, math.ceil(est_width / box_w))
        # 7N-FIX-3（amount）：行高模型 —— 只有**换行行**才消耗完整 line_h，
        # 最后一行只需 1em（与 adaptive_layout 的高度判据一致）。旧模型
        # ``est_lines * line_h`` 会让 box_h < 1.4*fs 的 heading/单行盒
        # （p26_6 的 11pt 盒、章号等）永远"溢出"并整块误移，译文压住下一行。
        est_height = max(font_size, (est_lines - 1) * line_h + font_size)
        # 7N-FIX-3（amount）：已定版 flow 布局（layout_ok=True 的 WRAP/SHRINK
        # 结果）是权威 —— overflow 裁决已经过 adaptive_layout 的
        # WRAP→SHRINK→CLIP 全流程与字号协商。fixup 若再用**名义字号**重估，
        # 会把 SHRINK 已解决的块（p3_4 尼尔·沙维特 13.01pt 单行、p263_1
        # 章号 57.8pt）误判为溢出并大幅下移（p3_4 压住下一行、p263_1 产生
        # large_shift）。因此 layout_ok=True 且无 overflow → keep；CLIP /
        # 布局失败 / 无 settled payload（heading 等）沿用文本估算。
        rp = item.get("render_payload")
        if (
            isinstance(rp, dict)
            and rp.get("layout_ok") is True
            and not rp.get("overflow")
        ):
            # 7N-FIX-3（amount）：已定版 flow 布局（layout_ok=True 的
            # WRAP/SHRINK 结果）是权威 —— overflow 裁决已经过 adaptive_layout
            # 的 WRAP→SHRINK→CLIP 全流程与字号协商。fixup 若再用**名义字号**
            # 重估，会把 SHRINK 已解决的块（p3_4 尼尔·沙维特 13.01pt 单行、
            # p263_1 章号 57.8pt）误判为溢出并大幅下移（p3_4 压住下一行、
            # p263_1 产生 large_shift）。因此 layout_ok=True 且无 overflow →
            # keep；CLIP / 布局失败 / 无 settled payload（heading 等）继续
            # 走文本估算。
            item["render_fixup"] = "keep"
            fixed.append(item)
            _emit_fixup_trace(trace, item, "keep")
            continue
        if est_height <= box_h * 1.25:
            item["render_fixup"] = "keep"
            fixed.append(item)
            _emit_fixup_trace(trace, item, "keep")
            continue

        # 7N-FIX-3（方向修正）：v3 是 y-up（左下原点、y 向上）——「页面下方」
        # 是 **−Δy**。原先写 ``+shift`` 把盒子移向页顶，渲染后译文整体上移
        # 到上一行（MECH-4 全书实测：153/153 shift 块 dst 都落在源盒上方，
        # p442_4 dst 直接压住 "such" 行）。向下位移的边界是页面下边缘
        # （v3 y=0）：盒底不得越界。``page_height`` 参数保留仅为 API 兼容，
        # 向下位移不再需要页顶约束。
        overflow_lines = (est_height - box_h) / line_h
        shift = max(1.0, overflow_lines) * line_h
        if float(dst[1]) - shift >= -1.0:
            item["dst_box"] = [
                round(dst[0], 2),
                round(dst[1] - shift, 2),
                round(dst[2], 2),
                round(dst[3] - shift, 2),
            ]
            item["render_fixup"] = "shift_down"
            item["render_path"] = PATH_SHIFT_DOWN
            # 7N-FIX-2（Invariant 1/2/3）：带已定版 flow commands 的块，其
            # 文字位置由 commands[*].{x, y} 决定（renderer 只读 command 坐标，
            # 不读 dst_box）。shift_down 是**整体几何变换**：dst_box 平移
            # Δy 的同一时刻 commands[*].y 必须同步平移，否则白底矩形与文字
            # 几何脱钩（MP2e 真实复现：p5_1/p5_3/p5_7，decoupled=3）。锚定
            # 关系 first_cmd_y == dst_box.y1 在 shift 后必须继续成立；
            # x / font_size / overflow / 文本内容不动。keep / preserve /
            # keep_overflow / 无 commands（heading 等）分支不做任何 co-shift。
            # 平移符号与 dst_box 相同（7N-FIX-3 方向修正后为 −Δy）。
            _shift_payload_commands_y(item, -shift)
            stats["shifted"] += 1
            _emit_fixup_trace(trace, item, "shift_down")
        else:
            item["overflowed"] = True
            item["render_fixup"] = "keep_overflow"
            stats["overflowed"] += 1
            _emit_fixup_trace(trace, item, "keep_overflow")
        fixed.append(item)
    return fixed, stats


def _emit_fixup_trace(trace, item: dict, fixup: str) -> None:
    """FlightRecorder：fixup 决策事件（语义化坐标 + Δy）。"""
    if trace is None or not getattr(trace, "enabled", False):
        return
    src = list(item.get("src_box") or [0, 0, 0, 0])
    dst = list(item.get("dst_box") or src)
    delta_y = (
        round(float(dst[3]) - float(src[3]), 2)
        if len(src) == 4 and len(dst) == 4
        else None
    )
    # fixup 在此刻已 co-shift 过 commands（shift_down 分支在调用前执行了
    # _shift_payload_commands_y），因此这里读出的是**平移后**的首命令 y ——
    # DECOUPLED 规则用它与 dst_box.y1 比对，验证 FIX-2 锚定不变量。
    first_cmd_y = None
    rp = item.get("render_payload") or {}
    for key in ("commands", "list_items", "toc_commands"):
        cmds = rp.get(key) or []
        if cmds and isinstance(cmds[0], dict) and cmds[0].get("y") is not None:
            first_cmd_y = round(float(cmds[0]["y"]), 2)
            break
    trace.emit(
        f"plan.{fixup}",
        trace.ctx(int(item.get("page") or 0), item.get("block_id") or "?", "plan"),
        {
            "kind": item.get("kind"),
            "fixup": fixup,
            "src_box": src,
            "dst_box": dst,
            "delta_y": delta_y,
            "delta_y_meaning": "v3_y_up_shift",
            "first_cmd_y": first_cmd_y,
            "cmd_count": (
                None
                if first_cmd_y is None
                else (
                    len(
                        rp.get("commands")
                        or rp.get("list_items")
                        or rp.get("toc_commands")
                        or []
                    )
                )
            ),
            "overflowed": bool(item.get("overflowed")),
        },
    )


def _shift_payload_commands_y(entry: dict, delta: float) -> None:
    """把条目内全部命令载荷的 ``y`` 平移 ``delta``（v3 y-up，与 dst_box 同号）。

    覆盖 ``render_payload.commands`` 与宿主渲染器可能回退到的旧字段
    ``list_items`` / ``toc_commands`` 的 commands。三处常是**同一批 command
    dict 的别名**（build_render_payload 从同一 TranslationUnit payload 复制
    列表、共享 dict），去重按 dict 身份（``id``）而非列表身份 —— 否则同一
    命令会被平移多次（7N-FIX-2 对 page_shift/packer 既有先例的身份去重收紧：
    列表身份在这些别名间不保证一致，dict 身份才稳定）。只改 y；x / width /
    font_size / text / overflow 一律不动。
    """
    shifted: set[int] = set()
    payload = entry.get("render_payload")
    if isinstance(payload, dict):
        for c in payload.get("commands") or []:
            if isinstance(c, dict) and isinstance(c.get("y"), (int, float)):
                c["y"] = round(float(c["y"]) + delta, 2)
                shifted.add(id(c))
    for key in ("list_items", "toc_commands"):
        obj = entry.get(key)
        if isinstance(obj, dict):
            for c in obj.get("commands") or []:
                if isinstance(c, dict) and isinstance(c.get("y"), (int, float)):
                    if id(c) in shifted:
                        continue
                    c["y"] = round(float(c["y"]) + delta, 2)
                    shifted.add(id(c))


#: 渲染时原样保留的 kind（与 document_model.annotate_render 的 preserve 集合
#: 对齐，此处独立成集以免循环导入）。
_PRESERVE_KINDS = frozenset(
    {"figure", "image", "table", "formula", "formula_inline", "code"}
)


__all__ = [
    "WritebackBlock",
    "plan_writeback_takeover",
    "apply_render_plan",
    "fixup_render_plan",
]
