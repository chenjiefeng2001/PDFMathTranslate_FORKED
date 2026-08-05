"""V8.3/V8.4 mainline side-channels (extracted from converter.py).

Keeps the legacy 2.x converter lean (strangulation guard) while letting the
Geometry Engine and write-back gate consume the *same* LTChar/LTPage stream
that ``TranslateConverter.receive_layout`` walks. All channels are strictly
non-blocking: failures become debug logs and never affect the mainline.

  * emit_page_ir      — build a DocumentIR snapshot per page (V8.3).
  * run_writeback_gate — Constraint-Layout pre-writeback reflow gate (V8.4).
"""
import logging

log = logging.getLogger(__name__)


def _new_gate_record(x0, y, x1, size, text, translated, toc_mode,
                     lidx=0, line_height=1.2, src_y0=None, src_y1=None):
    """创建 V8.4/V8.5 段落几何记录（已回填 final 几何 + 源几何）。

    V8.5 超链接重定位：同时记录段落的源 bbox（pdfminer LTChar 包围盒，
    即原文链接 /Rect 所在的坐标系）与目标 bbox（译文实际渲染落点）。
    """
    height = max(0.0, (lidx + 1) * size * line_height)
    width = max(0.0, x1 - x0)
    y_top, y_bottom = y, y - height
    if src_y0 is None or src_y1 is None:
        src_y0, src_y1 = y_bottom, y_top
    return {
        "x": x0, "y": y, "width": width,
        "height": height,
        "size": size, "text": text, "translated": translated,
        "node_type": "toc" if toc_mode else "paragraph",
        # V8.5 link_remap bridge data（坐标系与 gate 其它字段一致，y 上为正）
        "src_box": (x0, src_y0, x1, src_y1),
        "dst_box": (x0, y_bottom, x1, y_top),
    }


def run_mainline_channels(conv, ltpage) -> None:
    """V8.3/V8.4/V8.5 主链路 side-channel 统一入口。

    IR 快照 + 写回门控 + （可选）超链接重定位桥接数据。所有通道严格
    side-channel：失败只写入 debug 日志，永不干扰主链路渲染。
    """
    if conv.emit_ir:
        emit_page_ir(conv, ltpage)
    if conv.relayout_gate and conv._gate_records:
        run_writeback_gate(conv, ltpage)
    # V8.5: 超链接重定位需要逐段落的源/目标几何 —— 按页存档（页面级重置为
    # 空列表，_gate_records 在本页耗尽，存档需在 run_writeback_gate 之后、
    # _gate_records 尚未被下一页覆盖时立即做）。
    if getattr(conv, "link_remap", False):
        pageid = getattr(ltpage, "pageid", 0)
        gate_recs = getattr(conv, "gate_records_by_page", {})
        gate_recs[pageid] = [dict(r) for r in conv._gate_records]
        conv.gate_records_by_page = gate_recs


def emit_page_ir(conv, ltpage) -> None:
    """从 legacy 解析器正在遍历的同一 LTChar 流构建 DocumentIR 快照。

    输入是 pdfminer 的页面对象（V8.3 收敛点：Geometry Engine 与 legacy
    receive_layout 消费同一份字符流），输出 ``snapshot_ir`` 字典存入
    ``conv.ir_snapshots[pageid]``。失败不影响翻译主链路（side-channel）。
    """
    try:
        from pdf2zh.v3.geometry import GeometryEngine, chars_from_ltpage
        from pdf2zh.v3.migration_diff import snapshot_ir
        from pdf2zh.v3.structure import StructureClassifier, to_document_ir

        pageid = getattr(ltpage, "pageid", 0)
        chars = chars_from_ltpage(ltpage, page_num=pageid)
        if not chars:
            return
        engine = GeometryEngine()
        page = engine.build_page(chars, page_num=pageid)
        StructureClassifier().estimate_body_font_size([page])
        ir = to_document_ir(
            [page],
            classifier=StructureClassifier(),
            title=f"page_{pageid}",
            source_lang=getattr(conv.translator, "lang_in", "") or "en",
            target_lang=getattr(conv.translator, "lang_out", "") or "zh-cn",
        )
        conv.ir_snapshots[pageid] = snapshot_ir(ir, title=f"page_{pageid}")
    except Exception as e:
        log.debug("V8.3 IR emission failed for page %s: %s",
                  getattr(ltpage, "pageid", 0), e)


def run_writeback_gate(conv, ltpage) -> None:
    """在 legacy 内容流写回 PDF 对象之前，用 Constraint Layout + 碰撞检测
    管线复验本页布局；重叠率超标或溢出页边界时记录 QA 标记与门控裁决。

    ``conv.relayout_gate`` 可以是 ``MainlineRelayoutGate`` 实例或
    ``(page_width, page_height) -> MainlineRelayoutGate`` 工厂。
    """
    try:
        from pdf2zh.v3.mainline_gate import GateBlock, MainlineRelayoutGate
        pageid = getattr(ltpage, "pageid", 0)
        page_w = float(getattr(ltpage, "width", 612.0) or 612.0)
        page_h = float(getattr(ltpage, "height", 792.0) or 792.0)
        gate = conv.relayout_gate
        if callable(gate):
            gate = gate(page_w, page_h)
        elif not isinstance(gate, MainlineRelayoutGate):
            gate = MainlineRelayoutGate(page_width=page_w, page_height=page_h)
        blocks = [
            GateBlock(
                node_id=f"p{pageid}_{i}",
                text=rec["text"],
                translated=rec.get("translated", rec["text"]),
                x=rec["x"],
                y=rec["y"],
                width=max(1.0, rec["width"]),
                height=max(1.0, rec["height"]),
                page=pageid,
                font_size=rec.get("size") or 12.0,
                node_type=rec.get("node_type", "paragraph"),
            )
            for i, rec in enumerate(conv._gate_records)
        ]
        if not blocks:
            return
        result = gate.run(blocks)
        conv.gate_verdicts[pageid] = result.to_dict()
        if not result.writeback_allowed:
            conv._overflow_flags.append(
                {
                    "page": pageid,
                    "kind": "gate-blocked",
                    "issue": " | ".join(result.issues) if result.issues
                    else "writeback blocked",
                    "text": "",
                }
            )
    except Exception as e:
        log.debug("V8.4 gate failed for page %s: %s",
                  getattr(ltpage, "pageid", 0), e)