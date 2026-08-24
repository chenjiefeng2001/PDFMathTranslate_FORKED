# -*- coding: utf-8 -*-
"""V1.24 — Progress Aggregator / Work Graph 工作量模型。

覆盖（并发系统进度统计标准）：
- Work Graph：build_work_graph 按文档节点计数 + Pass 成本模型生成加权任务
  （页数已知时 Translation 按页拆分，100 页 = 100 个 Translate Task）；
- Pass 成本注册：register_pass("X").estimated_cost(fn) / 默认注册表；
- 单元权重：Paragraph=10 / Formula=25 / Table=30 / Image=60；
- Task 生命周期：Created -> Queued -> Running(partial) -> Finished（+Fail/Skip）；
- Partial Progress：Running 期间按局部完成度折算权重；
- 指数平滑：display += (real - display) * alpha（默认 0.08，UI 不跳变）；
- ETA 预测：按已完工权重速率外推剩余时间；
- Executor 桥接：TaskGraph(weight) + progress_cb -> ProgressAggregator；
- RuntimeService 集成：阶段权重映射（检查点 10/30/40/50/70/85/95 不变）、
  批量重置、文档页数重排权重、TaskState.eta 回传；
- GUI：build_progress_bar_html 渲染「预计剩余 m:ss」。
"""

import unittest

from pdf2zh.v3.progress_aggregator import (
    DEFAULT_PIPELINE_PASSES,
    DEFAULT_SMOOTHING_ALPHA,
    UNIT_WEIGHTS,
    PassCostRegistry,
    ProgressAggregator,
    TaskLifecycle,
    WorkGraph,
    bind_taskgraph,
    build_work_graph,
    default_pass_registry,
    estimate_document_weight,
    estimate_pass_weight,
    make_progress_cb,
    register_pass,
)


def _doc_counts(**kwargs) -> dict:
    base = {
        "pages": 100,
        "paragraphs": 1500,
        "formulas": 40,
        "tables": 12,
        "headings": 60,
        "lines": 8000,
        "images": 8,
    }
    base.update(kwargs)
    return base


class TestWorkGraph(unittest.TestCase):
    def test_build_work_graph_stages(self):
        graph = build_work_graph(_doc_counts(pages=100))
        self.assertIsInstance(graph, WorkGraph)
        self.assertGreater(len(graph.tasks), 0)
        stages = graph.stage_weights()
        self.assertEqual(set(stages), set(DEFAULT_PIPELINE_PASSES))

    def test_translation_split_per_page(self):
        # 100 页 -> Translation 100 个 task（每页权重 = 总成本/页数）
        graph = build_work_graph(_doc_counts(pages=100))
        t_tasks = [t for t in graph.tasks if t.stage == "Translation"]
        self.assertEqual(len(t_tasks), 100)
        self.assertAlmostEqual(
            sum(t.weight for t in t_tasks),
            estimate_pass_weight("Translation", _doc_counts(pages=100)),
        )
        self.assertTrue(all(t.task_id.startswith("Translation:page") for t in t_tasks))

    def test_no_pages_means_single_task_per_pass(self):
        graph = build_work_graph({"paragraphs": 10})
        names = [t.task_id for t in graph.tasks]
        self.assertEqual(
            names,
            [
                "Parser:doc",
                "SemanticAnalysis:doc",
                "Translation:doc",
                "Layout:doc",
                "Render:doc",
            ],
        )

    def test_total_weight_is_sum(self):
        graph = build_work_graph(_doc_counts(pages=50))
        self.assertAlmostEqual(graph.total_weight, sum(t.weight for t in graph.tasks))

    def test_dependencies_follow_pipeline(self):
        graph = build_work_graph(_doc_counts(pages=2))
        # Translation:page1 依赖 SemanticAnalysis:page1
        t = next(t for t in graph.tasks if t.task_id == "Translation:page1")
        self.assertEqual(t.dependencies, ["SemanticAnalysis:page1"])


class TestPassRegistry(unittest.TestCase):
    def test_default_estimate_translation_weight(self):
        # 1500 段 * 10 + 40 公式 * 25 + 12 表 * 30 + 60 标题 * 4 + 8 图 * 60
        expected = 1500 * 10 + 40 * 25 + 12 * 30 + 60 * 4 + 8 * 60
        w = estimate_pass_weight("Translation", _doc_counts())
        self.assertAlmostEqual(w, float(expected))

    def test_register_custom_pass(self):
        registry = PassCostRegistry()
        registry.register("OCR").estimated_cost(
            lambda c: c.get("images", 0) * UNIT_WEIGHTS["ocr"]
        )
        self.assertAlmostEqual(registry.estimate("OCR", {"images": 3}), 240.0)
        self.assertAlmostEqual(registry.estimate("OCR", {}), 0.0)

    def test_global_register_pass(self):
        name = "OCRBench"
        register_pass(name).estimated_cost(lambda c: 42.0)
        try:
            self.assertAlmostEqual(estimate_pass_weight(name, {}), 42.0)
        finally:
            default_pass_registry.clear()
            default_pass_registry.restore_defaults()

    def test_estimate_document_weight_units(self):
        w = estimate_document_weight({"paragraphs": 2, "formulas": 1, "images": 1})
        self.assertAlmostEqual(w, 2 * 10 + 25 + 60)


class TestProgressAggregator(unittest.TestCase):
    def test_lifecycle_weights(self):
        agg = ProgressAggregator(alpha=1.0)
        agg.add_task("p1", 10.0, stage="Translation")
        agg.add_task("p2", 10.0, stage="Translation")
        self.assertEqual(agg.total_weight, 20.0)
        self.assertEqual(agg.queued_weight, 20.0)
        agg.mark_running("p1")
        self.assertEqual(agg.running_weight, 10.0)
        agg.finish("p1")
        self.assertEqual(agg.finished_weight, 10.0)
        self.assertEqual(agg.failed_weight, 0.0)
        st = agg.get_state()
        self.assertAlmostEqual(st.percentage, 50.0)

    def test_partial_progress_counts_weight(self):
        # 第五原则：Running 35% 只折算 35% 权重
        agg = ProgressAggregator(alpha=1.0)
        agg.add_task("p1", 10.0, stage="Translation")
        agg.mark_running("p1", partial=35.0)
        st = agg.get_state()
        self.assertAlmostEqual(st.finished_weight, 3.5)
        self.assertAlmostEqual(st.percentage, 35.0)
        agg.update_partial("p1", 80.0)
        self.assertAlmostEqual(agg.get_state().percentage, 80.0)

    def test_running_is_not_jump(self):
        # 原则三/五：任务自己上报进度，而不是只有 Done
        agg = ProgressAggregator(alpha=1.0)
        agg.add_task("llm", 40.0, stage="Translation")
        seen = []
        for p in (0.0, 20.0, 60.0, 100.0):
            agg.mark_running("llm", partial=p)
            seen.append(agg.get_state().percentage)
        agg.finish("llm")
        seen.append(agg.get_state().percentage)
        self.assertEqual(seen, [0.0, 20.0, 60.0, 100.0, 100.0])

    def test_fail_and_skip(self):
        agg = ProgressAggregator(alpha=1.0)
        agg.add_task("a", 5.0, stage="X")
        agg.add_task("b", 5.0, stage="X")
        agg.fail("a")
        agg.skip("b")
        st = agg.get_state()
        self.assertAlmostEqual(st.failed_weight, 5.0)
        self.assertAlmostEqual(st.finished_weight, 10.0)  # fail 计入已消耗
        self.assertAlmostEqual(st.percentage, 100.0)

    def test_smoothing_alpha(self):
        # 第七原则：指数滤波，UI 看到 1,2,3 而不是 1,25,70
        agg = ProgressAggregator(alpha=DEFAULT_SMOOTHING_ALPHA)
        agg.add_task("a", 50.0, stage="X")
        agg.add_task("b", 50.0, stage="X")
        agg.finish("a")
        first = agg.get_state().percentage  # 4.0
        second = agg.get_state().percentage  # 7.68
        self.assertGreater(second, first)
        self.assertLess(second, 100.0)
        self.assertAlmostEqual(first, 4.0, places=3)
        agg.finish("b")
        self.assertEqual(agg.get_state().percentage, 100.0)

    def test_smoothing_time_based_catchup(self):
        # 时间基平滑：稀疏阶段事件（间隔秒级）快速追上 real，避免进度条
        # 长时间停在低数值；高频并行事件（100ms 级）走慢速平滑。
        now = [0.0]
        agg = ProgressAggregator(alpha=DEFAULT_SMOOTHING_ALPHA, now_fn=lambda: now[0])
        for name, w in (("a", 10.0), ("b", 20.0), ("c", 30.0), ("d", 40.0)):
            agg.add_task(name, w, stage="X")
        agg.finish("a")
        now[0] += 0.1
        first = agg.get_state().percentage
        self.assertAlmostEqual(first, 0.8, places=2)  # 10 * 0.08
        agg.finish("b")  # real=30
        now[0] += 1.5
        mid = agg.get_state().percentage
        self.assertGreater(mid, 20.0)  # 稀疏事件快速收敛
        self.assertLess(mid, 30.0)
        now[0] += 1.5
        self.assertGreater(agg.get_state().percentage, mid)
        agg.finish("c")
        agg.finish("d")  # real=100
        self.assertEqual(agg.get_state().percentage, 100.0)

    def test_smoothing_never_backwards(self):
        agg = ProgressAggregator(alpha=DEFAULT_SMOOTHING_ALPHA)
        for i in range(3):
            agg.add_task(f"t{i}", 10.0, stage="X")
        prev = -1.0
        for i in range(3):
            agg.finish(f"t{i}")
            for _ in range(5):
                cur = agg.get_state().percentage
                self.assertGreaterEqual(cur, prev)
                prev = cur

    def test_eta_unknown_without_history(self):
        agg = ProgressAggregator(alpha=1.0)
        agg.add_task("a", 10.0, stage="X")
        self.assertEqual(agg.get_state().eta, 0.0)

    def test_eta_from_finish_rate(self):
        now = [100.0]
        agg = ProgressAggregator(alpha=1.0, now_fn=lambda: now[0])
        agg.add_task("a", 50.0, stage="X")
        agg.add_task("b", 50.0, stage="X")
        agg.finish("a")  # t=100, done=50
        now[0] += 25.0
        agg.finish("b")  # t=125, done=100
        st = agg.get_state()
        self.assertEqual(st.eta, 0.0)  # 全部完成 -> 无剩余

    def test_eta_positive_while_running(self):
        now = [100.0]
        agg = ProgressAggregator(alpha=1.0, now_fn=lambda: now[0])
        agg.add_task("a", 50.0, stage="X")
        agg.add_task("b", 50.0, stage="X")
        agg.finish("a")
        now[0] += 10.0
        agg.update_partial("b", 50.0)  # 50 -> 已完成 75，速率 2.5 weight/s
        st = agg.get_state()
        self.assertAlmostEqual(st.eta, 10.0)  # 剩余 25 / 2.5

    def test_stage_breakdown(self):
        agg = ProgressAggregator(alpha=1.0)
        agg.add_task("p1", 10.0, stage="Translation")
        agg.add_task("l1", 5.0, stage="Layout")
        agg.finish("p1")
        bd = agg.stage_breakdown()
        self.assertEqual(bd["Translation"]["total"], 10.0)
        self.assertEqual(bd["Translation"]["done"], 10.0)
        self.assertEqual(bd["Layout"]["done"], 0.0)
        self.assertAlmostEqual(agg.stage_progress("Translation"), 1.0)
        self.assertAlmostEqual(agg.stage_progress("Layout"), 0.0)

    def test_reset_clears_everything(self):
        agg = ProgressAggregator(alpha=1.0)
        agg.add_task("a", 10.0, stage="X")
        agg.finish("a")
        agg.reset()
        self.assertEqual(agg.total_weight, 0.0)
        self.assertEqual(agg.get_state().percentage, 0.0)

    def test_task_state_snapshot(self):
        agg = ProgressAggregator(alpha=1.0)
        agg.add_task("a", 10.0, stage="Translation")
        agg.mark_running("a", partial=35.0)
        ts = agg.task_state("a")
        self.assertEqual(ts["lifecycle"], TaskLifecycle.RUNNING.value)
        self.assertEqual(ts["weight"], 10.0)
        self.assertEqual(ts["partial"], 35.0)
        self.assertIsNone(agg.task_state("missing"))


class TestExecutorBridge(unittest.TestCase):
    def test_taskgraph_weights_drive_progress(self):
        from pdf2zh.v3.scheduler import Task, TaskGraph, Executor

        graph = TaskGraph()
        graph.add_task(Task("parse", "Parse", weight=40.0))
        graph.add_task(Task("render", "Render", weight=60.0))
        agg = ProgressAggregator(alpha=1.0)
        bind_taskgraph(agg, graph.tasks)
        self.assertAlmostEqual(agg.total_weight, 100.0)
        executor = Executor(graph, progress_cb=make_progress_cb(agg))
        executor.run_all()
        st = agg.get_state()
        self.assertAlmostEqual(st.percentage, 100.0)
        self.assertAlmostEqual(st.finished_weight, 100.0)

    def test_scheduler_create_task_with_weight(self):
        from pdf2zh.v3.scheduler import Scheduler

        sch = Scheduler()
        sch.create_task("t1", "Translate", weight=25.0)
        self.assertEqual(sch.graph.get_task("t1").weight, 25.0)

    def test_partial_via_task_update_partial(self):
        from pdf2zh.v3.scheduler import Task, TaskGraph, Executor

        calls = []

        def handler(task):
            task.update_partial(60.0)
            calls.append(("partial", task.partial))
            return "ok"

        graph = TaskGraph()
        graph.add_task(Task("t", "T", weight=10.0, handler=handler))
        agg = ProgressAggregator(alpha=1.0)
        bind_taskgraph(agg, graph.tasks)
        Executor(graph, progress_cb=make_progress_cb(agg)).run_all()
        self.assertEqual(calls, [("partial", 60.0)])
        self.assertAlmostEqual(agg.get_state().percentage, 100.0)


class TestRuntimeServiceIntegration(unittest.TestCase):
    def _svc(self):
        from pdf2zh.services.runtime_service import (
            RuntimeService,
            TaskStage,
        )

        return RuntimeService(), TaskStage

    def test_stage_mapping_preserves_checkpoints(self):
        svc, TS = self._svc()
        svc._init_aggregator("t1", alpha=1.0)
        expected = {
            TS.PARSING.value: 10.0,
            TS.ANALYZING.value: 30.0,
            TS.PLANNING.value: 40.0,
            TS.TRANSLATING.value: 50.0,
            TS.LAYOUTING.value: 70.0,
            TS.RENDERING.value: 85.0,
            TS.EVALUATING.value: 95.0,
        }
        prev = -1.0
        for stage, checkpoint in expected.items():
            p, eta = svc._map_stage_progress("t1", stage, checkpoint)
            self.assertAlmostEqual(p, checkpoint, places=4)
            self.assertGreaterEqual(p, prev)
            prev = p
        p, _ = svc._map_stage_progress("t1", TS.COMPLETED.value, 100.0)
        self.assertEqual(p, 100.0)

    def test_stage_mapping_order_invariant(self):
        # 第一原则：进度来自 Work Graph，不依赖事件顺序 —— 直接从翻译阶段
        # 开始上报（前序阶段未发事件）进度依然正确累计。
        svc, TS = self._svc()
        svc._init_aggregator("t1", alpha=1.0)
        p, _ = svc._map_stage_progress("t1", TS.TRANSLATING.value, 40.0)
        self.assertAlmostEqual(p, 40.0)
        p, _ = svc._map_stage_progress("t1", TS.LAYOUTING.value, 70.0)
        self.assertAlmostEqual(p, 70.0)

    def test_within_stage_partial_moves_weighted(self):
        svc, TS = self._svc()
        svc._init_aggregator("t1", alpha=1.0)
        p0, _ = svc._map_stage_progress("t1", TS.TRANSLATING.value, 40.0)
        p1, _ = svc._map_stage_progress("t1", TS.TRANSLATING.value, 55.0)
        p2, _ = svc._map_stage_progress("t1", TS.TRANSLATING.value, 70.0)
        self.assertAlmostEqual(p0, 40.0)
        self.assertAlmostEqual(p1, 55.0, places=2)  # 40 + 30*0.5
        self.assertAlmostEqual(p2, 70.0)

    def test_no_aggregator_keeps_legacy_exact(self):
        # 未 submit 的任务（测试直连路径）保持历史行为
        svc, TS = self._svc()
        got = []
        svc.add_event_listener(lambda ev: got.append(ev.progress))
        svc._emit_event("t1", TS.PARSING.value, 10.0, "hi")
        svc._emit_event("t1", TS.PLANNING.value, 20.0)
        svc._emit_event("t1", TS.TRANSLATING.value, 30.0)
        self.assertEqual(got, [10.0, 20.0, 30.0])

    def test_submit_task_initializes_aggregator_and_eta_field(self):
        svc, TS = self._svc()
        from pdf2zh.services.runtime_service import TranslationRequest

        tid = svc.submit_task(TranslationRequest(source_path="x.pdf"))
        state = svc.get_task_state(tid)
        self.assertIsNotNone(state)
        self.assertIn(tid, svc._aggregators)
        # 无文件 -> FAILED，但 eta 字段存在
        self.assertIn("eta", state.to_dict())

    def test_update_weights_by_pages(self):
        svc, TS = self._svc()
        svc._init_aggregator("t1", alpha=1.0)
        svc._update_aggregator_weights("t1", {"pages": 200})
        bounds = svc._stage_bounds("t1")
        # 页数多 -> 翻译窗口变宽（权重占比上调，起点上移）
        self.assertGreater(bounds["translating"][1] - bounds["translating"][0], 30.0)

    def test_emit_event_flow_sets_eta(self):
        svc, TS = self._svc()
        svc._store.create_task("t1")
        svc._init_aggregator("t1", alpha=1.0)
        svc._emit_event("t1", TS.PARSING.value, 10.0, "Parsing")
        svc._emit_event("t1", TS.TRANSLATING.value, 50.0, "Translating")
        state = svc.get_task_state("t1")
        self.assertAlmostEqual(state.progress, 50.0, places=4)
        self.assertGreaterEqual(state.eta, 0.0)
        svc._emit_event("t1", TS.COMPLETED.value, 100.0, "Done")
        self.assertEqual(svc.get_task_state("t1").progress, 100.0)


class TestGuiEtaDisplay(unittest.TestCase):
    def test_eta_rendered(self):
        from pdf2zh.gui.components.progress_panel import build_progress_bar_html

        html = build_progress_bar_html("translating", 55.0, "Working", eta=83.0)
        self.assertIn("预计剩余", html)
        self.assertIn("1:23", html)

    def test_eta_hidden_when_zero(self):
        from pdf2zh.gui.components.progress_panel import build_progress_bar_html

        html = build_progress_bar_html("translating", 55.0, "Working")
        self.assertNotIn("预计剩余", html)

    def test_eta_hidden_terminal(self):
        from pdf2zh.gui.components.progress_panel import build_progress_bar_html

        html = build_progress_bar_html("completed", 100.0, "Done", eta=5.0)
        self.assertNotIn("预计剩余", html)

    def test_format_eta(self):
        from pdf2zh.gui.components.progress_panel import _format_eta

        self.assertEqual(_format_eta(0), "0:00")
        self.assertEqual(_format_eta(83), "1:23")
        self.assertEqual(_format_eta(3661), "1:01:01")


class TestUploadLimit(unittest.TestCase):
    def test_resolve_max_file_size_priority(self):
        import os
        from pdf2zh.gui.entry import DEFAULT_MAX_FILE_SIZE, resolve_max_file_size

        self.assertEqual(resolve_max_file_size(None), DEFAULT_MAX_FILE_SIZE)
        self.assertEqual(resolve_max_file_size("200mb"), "200mb")
        self.assertEqual(resolve_max_file_size(100), "100mb")
        os.environ["PDF2ZH_MAX_FILE_SIZE"] = "500mb"
        try:
            self.assertEqual(resolve_max_file_size(None), "500mb")
            self.assertEqual(resolve_max_file_size("50mb"), "50mb")
        finally:
            os.environ.pop("PDF2ZH_MAX_FILE_SIZE", None)
        self.assertEqual(resolve_max_file_size(""), DEFAULT_MAX_FILE_SIZE)

    def test_cli_has_max_file_size_flag(self):
        from pdf2zh.pdf2zh import create_parser

        ns = create_parser().parse_args(["--interactive", "--max-file-size", "256"])
        self.assertEqual(ns.max_file_size, 256)


if __name__ == "__main__":
    unittest.main()
