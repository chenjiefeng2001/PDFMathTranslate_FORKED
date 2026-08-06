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
    """V8.3–V9.0 主链路 side-channel 统一入口。

    IR 快照 + 写回门控 + （可选）超链接重定位桥接数据 + Processor
    语义通道 + TOC 结构化记录。所有通道严格 side-channel：失败只写入
    debug 日志，永不干扰主链路渲染。
    """
    if conv.emit_ir:
        emit_page_ir(conv, ltpage)
    if conv.relayout_gate and conv._gate_records:
        run_writeback_gate(conv, ltpage)
    # V9.0: Processor 层挂主链路（RAW/SEMANTIC 语义通道，结果只存报告）
    if getattr(conv, "processor_channels", False):
        run_processor_channels(conv, ltpage)
    # V9.0: 目录条目 → IR 结构化记录（读 gate 记录，不触碰 converter）
    if getattr(conv, "processor_channels", False) and conv._gate_records:
        run_toc_channel(conv, ltpage)
    # V8.3 后半程: gate 判据驱动的渲染路径切换（决策只存 render_plans）
    if getattr(conv, "render_takeover", False) and conv._gate_records \
            and conv.relayout_gate:
        run_render_takeover(conv, ltpage)
    # 阶段六/八: 置信度路由 + Review 复检 QA（读 gate 记录，不触碰渲染）
    if getattr(conv, "translation_qa", False) and conv._gate_records:
        run_translation_qa_channel(conv, ltpage)
    # 可观测层: 逐阶段 dump（Glyph/Line/Block/TOC/Translation/Layout，side-channel）
    if getattr(conv, "pipeline_dump", False):
        run_pipeline_dump(conv, ltpage)
    # V11: 文档统一模型（多页树 + Relations，累积到 conv.document_model）
    if getattr(conv, "document_model_enabled", False):
        run_document_model(conv, ltpage)
    # Phase D: 可观测层（Trace/Snapshot/Decision side-channel，只采集不渲染）
    if getattr(conv, "observability", False):
        run_observability_channel(conv, ltpage)
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


def run_processor_channels(conv, ltpage) -> None:
    """V9.0 P1：把 Processor 层（RAW/SEMANTIC）挂到主链路字符流上。

    同一份 LTChar → Geometry 段落 → DocumentGraph（全 PARAGRAPH 起点，
    交给默认注册表做 TOC/公式/代码/图片/表格/引用/题注语义化），运行
    RAW+SEMANTIC 两阶段。结果只进 ``conv.processor_reports[pageid]``
    （PipelineReport.to_dict）+ 语义类型计数，绝不回写 legacy 渲染。
    """
    try:
        from pdf2zh.v3.geometry import GeometryEngine, chars_from_ltpage
        from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType
        from pdf2zh.v3.document_pipeline import run_semantic_pipeline

        pageid = getattr(ltpage, "pageid", 0)
        chars = chars_from_ltpage(ltpage, page_num=pageid)
        if not chars:
            return
        page = GeometryEngine().build_page(chars, page_num=pageid)
        graph = DocumentGraph()
        for i, para in enumerate(page.reading_order()):
            graph.add_node(DocumentNode(
                id=f"p{pageid}_{i}",
                node_type=NodeType.PARAGRAPH,
                bbox=(para.x0, para.y0, para.x1, para.y1),
                text=para.text,
                page_num=pageid,
                font_size=getattr(para, "avg_char_size", 0.0) or 0.0,
                metadata={"index": i, "source": "geometry"},
            ))
        report = run_semantic_pipeline(graph)
        reports = getattr(conv, "processor_reports", {})
        reports[pageid] = (report.to_dict() if hasattr(report, "to_dict")
                           else {"ok": report.ok(), "errors": list(report.errors)})
        conv.processor_reports = reports
        # 语义类型分布（轻量侧信道，供报告/QA 用）
        type_counts = {}
        for n in graph.nodes:
            key = n.node_type.value if hasattr(n.node_type, "value") \
                else str(n.node_type)
            type_counts[key] = type_counts.get(key, 0) + 1
        counts = getattr(conv, "processor_type_counts", {})
        counts[pageid] = type_counts
        conv.processor_type_counts = counts
    except Exception as e:
        log.debug("V9.0 processor channels failed for page %s: %s",
                  getattr(ltpage, "pageid", 0), e)


def run_toc_channel(conv, ltpage) -> None:
    """V9.0 P1：gate 记录里的目录行 → IR 结构化 TOC 记录。

    复用 toc_semantics 解析 + toc_to_ir_records，把 ``(entry, remainder,
    translated_title)`` 三字段契约存进 ``conv.toc_ir_records[pageid]``。
    目录行的 translated 文本即 TOCPolicy 渲染后的译文行。converter 的
    gate 记录只保留标题余量（号段被剥离），号段在组合译文中 ——
    PLAIN 时回退解析组合译头（"第1节 …"）复原 kind/number。
    """
    try:
        from pdf2zh.v3.toc_semantics import parse_toc_entry, toc_to_ir_records
        pageid = getattr(ltpage, "pageid", 0)
        triples = []
        for rec in getattr(conv, "_gate_records", []):
            if rec.get("node_type") != "toc":
                continue
            text = rec.get("text") or ""
            entry = parse_toc_entry(text)
            if not entry.matched:
                composed = rec.get("translated") or ""
                fallback = parse_toc_entry(composed)
                if fallback.matched:
                    entry = fallback
            triples.append((entry, text, rec.get("translated", "")))
        records = toc_to_ir_records(triples, page_num=pageid)
        if not records:
            return
        stored = getattr(conv, "toc_ir_records", {})
        stored[pageid] = records
        conv.toc_ir_records = stored
    except Exception as e:
        log.debug("V9.0 toc channel failed for page %s: %s",
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


def run_render_takeover(conv, ltpage) -> None:
    """V8.3 后半程: gate 判据驱动的渲染路径切换（side-channel 决策）。

    把 gate 裁决 + 写回块合并为逐块渲染路由（RenderAdvisor.plan），并给出
    应用后的写回块清单（apply_render_plan）。只存 ``conv.render_plans``，
    不触碰 legacy 渲染 —— 消费端（迁移闭环）决定是否应用。
    """
    try:
        from pdf2zh.v3.render_takeover import (
            WritebackBlock, apply_render_plan, plan_writeback_takeover,
        )
        pageid = getattr(ltpage, "pageid", 0)
        blocks = [
            WritebackBlock(
                node_id=f"p{pageid}_{i}",
                text=rec["text"],
                translated=rec.get("translated", rec["text"]),
                x=rec["x"], y=rec["y"],
                width=max(1.0, rec["width"]),
                height=max(1.0, rec["height"]),
                page=pageid, font_size=rec.get("size") or 12.0,
                node_type=rec.get("node_type", "paragraph"),
            )
            for i, rec in enumerate(conv._gate_records)
        ]
        if not blocks:
            return
        verdict = conv.gate_verdicts.get(pageid)
        plan = plan_writeback_takeover(blocks, verdict=verdict)
        applied = apply_render_plan(plan, blocks)
        plans = getattr(conv, "render_plans", {})
        plans[pageid] = {
            "plan": plan,
            "applied_count": len(applied),
            "dropped_count": len(blocks) - len(applied),
        }
        conv.render_plans = plans
    except Exception as e:
        log.debug("V8.3 render takeover failed for page %s: %s",
                  getattr(ltpage, "pageid", 0), e)


def run_translation_qa_channel(conv, ltpage) -> None:
    """阶段六/八 P2: gate 记录逐段「置信度路由 + Review 复检」QA。

    只产出 ``conv.translation_qa_records[pageid]``（逐段 route/confidence/
    issues/action）与 QA 标记（kind=translation-qa），不触碰渲染。
    """
    try:
        from pdf2zh.v3.mainline_qa import run_translation_qa
        pageid = getattr(ltpage, "pageid", 0)
        records = [
            {"node_id": f"p{pageid}_{i}", "text": rec["text"],
             "translated": rec.get("translated", rec["text"])}
            for i, rec in enumerate(conv._gate_records)
        ]
        if not records:
            return
        report = run_translation_qa(records)
        qa = getattr(conv, "translation_qa_records", {})
        qa[pageid] = report.to_dict()
        conv.translation_qa_records = qa
        if report.action_retranslate:
            conv._overflow_flags.append({
                "page": pageid,
                "kind": "translation-qa",
                "issue": f"{report.action_retranslate} segments need retranslate",
                "text": "",
            })
    except Exception as e:
        log.debug("translation QA failed for page %s: %s",
                  getattr(ltpage, "pageid", 0), e)


def run_pipeline_dump(conv, ltpage) -> None:
    """可观测层: 单页全阶段 dump（Glyph/Line/Block/TOC/Translation/Layout）。

    只存 ``conv.pipeline_dumps[pageid]``（JSON 可序列化），供排查字符流
    损坏（� / (cid:N) / 标题丢失）发生在哪一层；不触碰主链路渲染。
    """
    try:
        from pdf2zh.v3.pipeline_dump import dump_page
        pageid = getattr(ltpage, "pageid", 0)
        dumps = getattr(conv, "pipeline_dumps", {})
        dumps[pageid] = dump_page(conv, ltpage)
        conv.pipeline_dumps = dumps
    except Exception as e:
        log.debug("pipeline dump failed for page %s: %s",
                  getattr(ltpage, "pageid", 0), e)


def run_document_model(conv, ltpage) -> None:
    """V11: 文档统一模型累积（多页树 + Relations，side-channel）。

    每页经 ``build_document_model`` 增量追加：结构恢复 + Role/Formula/
    Style/TOC（scan + gate 记录匹配）/Render 标注；gate 目录记录存在时
    用 ``toc_dump`` 条目做精确匹配标注。结果存 ``conv.document_model``
    （DocumentModel 实例），由 high_level 回传 v3_output["document_model"]。
    """
    try:
        from pdf2zh.v3.document_model import DocumentModel, build_document_model
        from pdf2zh.v3.pipeline_dump import toc_dump
        pageid = getattr(ltpage, "pageid", 0)
        model = getattr(conv, "document_model", None)
        if model is None or not isinstance(model, DocumentModel):
            model = build_document_model([])
            conv.document_model = model
        entries = toc_dump(conv, pageid) if getattr(conv, "_gate_records", None) else []
        page = build_document_model([ltpage],
                                    annotate_toc_entries={pageid: entries}).pages[0]
        # 译后文本写回模型（gate 记录 → Block.metadata.translated）
        try:
            from pdf2zh.v3.document_model import annotate_translation_from_records
            annotate_translation_from_records(page, conv._gate_records)
        except Exception as e:  # noqa: BLE001
            log.debug("document model translation annotation failed p%s: %s",
                      pageid, e)
        model.add_page(page)
        # Phase 2：Pass 流水线（Normalize/Semantic/Policy/Typography）
        try:
            from pdf2zh.v3.doc_passes import default_pass_manager
            report = default_pass_manager().run(model)
            model.metadata["pass_report"] = report.to_dict()
        except Exception as e:  # noqa: BLE001
            log.debug("document model pass pipeline failed p%s: %s",
                      pageid, e)
        # Phase 4.1：语义图（sections/belongs_to/mentions）
        try:
            from pdf2zh.v3.semantic_graph import build_semantic_relations
            model.metadata["semantic_graph"] = build_semantic_relations(model)
        except Exception as e:  # noqa: BLE001
            log.debug("semantic graph failed p%s: %s", pageid, e)
        # Phase 5：质量诊断 + 置信度模型（哪里错 / 多可信）
        try:
            from pdf2zh.v3.diagnostics import (
                analyze_document, annotate_confidence,
            )
            model.metadata["diagnostics"] = analyze_document(model).to_dict()
            model.metadata["confidence_stats"] = annotate_confidence(model)
        except Exception as e:  # noqa: BLE001
            log.debug("diagnostics failed p%s: %s", pageid, e)
        conv.document_model = model
    except Exception as e:
        log.debug("document model failed for page %s: %s",
                  getattr(ltpage, "pageid", 0), e)


def run_observability_channel(conv, ltpage) -> None:
    """Phase D: 主链路可观测 side-channel（D0–D9 聚合到 ``conv.obs_session``）。

    每页从同一 LTChar 流捕获节点级快照（NodeID 引用，不写裸字符串），
    并把 gate 段落记录转成决策证据日志；全部只采集，不触碰主链路渲染。
    结果由 high_level 回传 ``v3_output["observability"]``（含 Inspector HTML）。
    """
    try:
        from pdf2zh.v3.canonical_page import build_page_model
        from pdf2zh.v3.observability import ObsSession, capture_snapshot
        pageid = getattr(ltpage, "pageid", 0)
        session = getattr(conv, "obs_session", None)
        if session is None or not isinstance(session, ObsSession):
            session = ObsSession()
            conv.obs_session = session
        session.page_dims[pageid] = (
            float(getattr(ltpage, "width", 0.0) or 0.0),
            float(getattr(ltpage, "height", 0.0) or 0.0))
        page = build_page_model(ltpage, page_num=pageid)
        session.capture(page, "render")
        doc = session.trace.doc_id
        for i, rec in enumerate(getattr(conv, "_gate_records", []) or []):
            session.record(
                f"{doc}::P{pageid}::B{i}",
                "render:paragraph",
                evidence={"size": float(rec.get("size", 0.0) or 0.0),
                          "translated": 1.0 if rec.get("translated") else 0.0,
                          "node_type": 0.9 if rec.get("node_type") == "toc" else 0.1},
                confidence=min(0.99, 0.5 + float(rec.get("size", 0.0) or 0.0) / 100.0),
                source="gate", stage="render",
                message="mainline gate record")
    except Exception as e:  # noqa: BLE001 — 可观测层永不阻断主链路
        log.debug("observability failed for page %s: %s",
                  getattr(ltpage, "pageid", 0), e)