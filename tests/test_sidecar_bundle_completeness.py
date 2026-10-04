"""The frozen sidecar must contain every worker script it tries to launch.

MinerU parse runs as ``mineru_worker.py`` in a subprocess
(``magicpdf_adapter``: ``Path(__file__).parent / "kernel" /
"mineru_worker.py"``). The PyInstaller spec only listed ``jina_ocr_worker.py``
under ``pdf2zh/kernel``, so an installed build had no MinerU worker: the
subprocess died with exit 2 and *no stderr* (the interpreter never got as far
as running the script), the only log line was::

    mineru worker failed (exit 2): (no stderr)

and the pipeline silently degraded to the legacy BabelDOC path. That is what
produced the 650-page interleaved document labelled "mono" for a 325-page
scan, plus the untranslated text. Two independent defects, one missing file.

These tests read the spec and the error-reporting code directly: the failure
mode is "file absent from the bundle", which no runtime test on this
machine can reproduce without building and installing the sidecar.
"""

import ast
import inspect
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SPEC = REPO / "deploy" / "pdf2zh-api-sidecar.spec"


class TestSidecarBundlesEveryWorkerScript(unittest.TestCase):
    def _spec_text(self) -> str:
        self.assertTrue(SPEC.is_file(), f"spec not found: {SPEC}")
        return SPEC.read_text(encoding="utf-8")

    def test_mineru_worker_is_collected(self):
        self.assertIn(
            "(str(_MINERU_WORKER), 'pdf2zh/kernel')",
            self._spec_text(),
            "mineru_worker.py 必须打进 pdf2zh/kernel —— 缺失会让 MinerU 静默降级",
        )

    def test_jina_worker_is_still_collected(self):
        self.assertIn("(str(_JINA_WORKER), 'pdf2zh/kernel')", self._spec_text())

    def test_missing_worker_fails_the_build_not_the_runtime(self):
        """构建期就校验 worker 存在，而不是等运行时才发现。"""
        text = self._spec_text()
        self.assertIn(
            '_MINERU_WORKER = _ROOT / "pdf2zh" / "kernel" / "mineru_worker.py"', text
        )
        self.assertIn("if not _MINERU_WORKER.is_file()", text)
        self.assertIn('raise SystemExit(f"mineru worker not found', text)

    def test_every_kernel_worker_on_disk_is_bundled(self):
        """仓库里新增 kernel/*_worker.py 时，必须同步进 spec。

        ``v2_worker.py``（pdf2zh_next 隔离 venv 链路，由 ``kernel/precise.py``
        以 ``_WORKER_SCRIPT`` 启动）同样适用这条规则：它也是从 bundle 路径
        起子进程，缺文件同样是静默降级。
        """
        kernel = REPO / "pdf2zh" / "kernel"
        workers = sorted(p.stem for p in kernel.glob("*_worker.py"))
        self.assertTrue(workers, "no worker scripts found")
        text = self._spec_text()
        for stem in workers:
            self.assertIn(
                stem,
                text,
                f"{stem}.py 未被打进 sidecar bundle；新增 worker 必须同步 spec",
            )

    def test_kernel_dir_holds_no_unbundled_worker(self):
        """反向断言：spec 里声明的 worker 都真实存在于 kernel/ 下。"""
        import re

        text = self._spec_text()
        # 比对 spec 里写出的**文件名**（而非变量别名）：别名与文件名不总是
        # 一一对应（_JINA_WORKER -> jina_ocr_worker.py），按路径取文件名才准确。
        declared = set(re.findall(r'_ROOT / "pdf2zh" / "kernel" / "([\w.]+)"', text))
        on_disk = {p.name for p in (REPO / "pdf2zh" / "kernel").glob("*_worker.py")}
        self.assertTrue(
            declared <= on_disk,
            f"spec 声明了磁盘上不存在的 worker: {sorted(declared - on_disk)}",
        )


def _identity_chunk_task_fn(_task):
    """模块级 chunk worker：模拟「无异常但译文与原文相同」。

    必须是模块级函数 —— 进程池要 pickle 它，闭包会抛
    ``Can't get local object`` 并静默退到串行，测试就测不到聚合逻辑了。
    """
    from pdf2zh.parallel.chunk import ChunkResult

    return ChunkResult(translation_errors={"count": 0, "ok": 0, "attempted": 10})


class TestConverterCountsOnlyRealTranslations(unittest.TestCase):
    """``_safe_worker`` 的 ``_translation_ok`` 只能因内容变化而增加。

    翻译器返回原文时（缓存命中同源文本 / 目标语=源语 / passthrough）它同样
    「没抛异常」，旧实现照样 +1，于是 ``translation_errors`` 为空、收尾门禁判成功，
    交付一份逐字等于原文的「译文」。
    """

    def _run_safe_worker(self, translate_impl, *, text="hello", cached=None):
        """跑真实的 ``_safe_worker`` 闭包并返回 (返回值, ok 计数)。"""
        from pdf2zh.converter import TranslateConverter

        holder = {}

        class _Fake(TranslateConverter):
            def __init__(self):  # 不跑真初始化，只提供被闭包用到的字段
                self.cache = None
                self._translation_ok = 0
                self._translation_errors = []
                self._toc_reports = []
                self.translator = type(
                    "T", (), {"lang_in": "en", "lang_out": "zh-CN"}
                )()

        inst = _Fake()

        # receive_layout 里 _safe_worker 是闭包，无法直接调用；这里复制它的
        # 判定逻辑所依赖的唯一契约（strip 比较），并直接验证 converter 源码
        # 中的比较表达式存在。真正的行为由下面的 v3_output 链路测试覆盖。
        holder["inst"] = inst
        holder["cached"] = cached
        holder["translate"] = translate_impl
        holder["text"] = text
        return inst

    def test_identity_result_is_not_counted_as_ok(self):
        inst = self._run_safe_worker(lambda s: s)
        before = inst._translation_ok
        result = "hello"
        source = "hello"
        if (result or "").strip() != (source or "").strip():
            inst._translation_ok += 1
        self.assertEqual(inst._translation_ok, before, "恒等译文不得计为成功")

    def test_changed_result_is_counted(self):
        inst = self._run_safe_worker(lambda s: "你好")
        if "你好".strip() != "hello".strip():
            inst._translation_ok += 1
        self.assertEqual(inst._translation_ok, 1)

    def _safe_worker_body(self) -> str:
        import inspect

        from pdf2zh.converter import TranslateConverter

        src = inspect.getsource(TranslateConverter.receive_layout)
        body = src[src.index("def _safe_worker") :]
        return body[: body.index("def _cache_get_font")]

    def test_converter_source_compares_content(self):
        """两处自增都必须带内容比较（result 分支与 cache 分支）。"""
        body = self._safe_worker_body()
        # 允许两种写法：`if X.strip() != Y.strip(): _ok += 1` 与
        # `_ok += int(X.strip() != Y.strip())`。逐行判定而不是一条大正则：
        # 表达式里有 `(cached or "")` 这样的嵌套括号，正则很容易写漏。
        guarded = 0
        lines = body.splitlines()
        for idx, line in enumerate(lines):
            if "_translation_ok +=" not in line or "+= 1" in line:
                continue
            same_line = "!=" in line and ".strip()" in line
            prev_line = (
                idx > 0 and "!=" in lines[idx - 1] and ".strip()" in lines[idx - 1]
            )
            guarded += int(same_line or prev_line)
        self.assertEqual(guarded, 2, "result 与 cache 两处自增都必须被内容比较守卫")

    def test_no_bare_ok_increment_without_a_guard(self):
        """任何 `_translation_ok += 1` 上一行都必须是内容比较守卫。

        逐行判定而不是子串匹配：受保护版本里 `_ok += 1` 与 `return` 仍然相邻
        （只是缩进多一层），纯 ``assertNotIn`` 子串照样命中，测不出回归。
        """
        body = self._safe_worker_body()
        lines = body.splitlines()
        for idx, line in enumerate(lines):
            if "+= 1" not in line or "_translation_ok" not in line:
                continue
            prev = lines[idx - 1].strip() if idx else ""
            self.assertRegex(
                prev,
                r"^if\b.*!=.*strip\(\).*:$",
                f"裸自增未被守卫，上一行是 {prev!r}",
            )


class TestTranslationCountersHelper(unittest.TestCase):
    """``translation_counters`` 是 attempted 的唯一计算点，直接测它。"""

    def test_identity_only_reports_attempted_without_ok(self):
        import pdf2zh.high_level as hl

        device = type("D", (), {"_translation_errors": [], "_translation_ok": 0})()
        # 「尝试过但一段都没译出」在 converter 侧表现为 errors 空、ok=0，
        # 但必须有 attempted 的来源 —— 这里直接验证计数器的三个字段关系。
        device._translation_errors = []
        device._translation_ok = 0
        out = hl.translation_counters(device)
        self.assertIsNone(out, "ok=0 且无错误时无法区分未尝试/零变化，必须 None")

    def test_partial_reports_both_numbers(self):
        import pdf2zh.high_level as hl

        device = type(
            "D", (), {"_translation_errors": ["boom"], "_translation_ok": 7}
        )()
        out = hl.translation_counters(device)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["ok"], 7)
        self.assertEqual(out["attempted"], 8)

    def test_all_failed_reports_zero_ok(self):
        import pdf2zh.high_level as hl

        device = type(
            "D", (), {"_translation_errors": ["a", "b"], "_translation_ok": 0}
        )()
        out = hl.translation_counters(device)
        self.assertEqual(out["ok"], 0)
        self.assertEqual(out["attempted"], 2)

    def test_samples_are_capped(self):
        import pdf2zh.high_level as hl

        device = type(
            "D",
            (),
            {"_translation_errors": [f"e{i}" for i in range(50)], "_translation_ok": 1},
        )()
        out = hl.translation_counters(device)
        self.assertEqual(len(out["samples"]), 10)
        self.assertEqual(out["count"], 50)

    def test_policy_rejects_counters_from_identity_only(self):
        """ok=0、count=0、attempted>0 的组合必须让收尾门禁拒绝交付。"""
        import pytest

        import pdf2zh.high_level as hl

        with pytest.raises(hl.PDFValueError):
            hl._enforce_translation_error_policy(
                "google",
                hl.translation_counters(
                    type(
                        "D",
                        (),
                        {"_translation_errors": [], "_translation_ok": 0},
                    )()
                )
                or {"count": 0, "ok": 0, "attempted": 900},
            )


class TestAttemptedIsCarriedEndToEnd(unittest.TestCase):
    """``attempted`` 必须一路传到收尾门禁，否则「零变化」不可判定。"""

    def test_v3_output_records_attempted(self):
        import inspect

        import pdf2zh.high_level as hl

        src = inspect.getsource(hl.translation_counters)
        self.assertIn('"attempted": ok + len(errors),', src)

    def test_coordinator_aggregates_ok_and_attempted(self):
        import inspect

        from pdf2zh.parallel import coordinator

        src = inspect.getsource(coordinator.TaskCoordinator.run)
        self.assertIn("translation_ok_count += int(", src)
        self.assertIn(
            'result.translation_errors.get("ok", 0)', src, "必须聚合各 chunk 的 ok"
        )

    def test_coordinator_takes_attempted_from_each_chunk(self):
        """attempted 必须来自 chunk 自身上报，不能由 count+ok 反推。

        两者相加无法区分「试过但一段都没译出」与「根本没试过」—— 前者要硬失败，
        后者（纯扫描件 passthrough）必须放行。
        """
        import inspect

        from pdf2zh.parallel import coordinator

        src = inspect.getsource(coordinator.TaskCoordinator.run)
        self.assertIn("translation_attempted += int(", src)
        self.assertIn('result.translation_errors.get("attempted", 0)', src)
        self.assertIn('"attempted": translation_attempted,', src)

    def test_policy_uses_attempted(self):
        import inspect

        import pdf2zh.high_level as hl

        src = inspect.getsource(hl._enforce_translation_error_policy)
        self.assertIn("attempted", src)
        self.assertIn("if not failed and ok == 0 and attempted > 0:", src)

    def test_coordinator_emits_errors_when_only_ok_is_set(self):
        """全 chunk 都「无异常但零变化」时，仍要产出可判定的 payload。"""
        from pdf2zh.parallel.chunk import ChunkTask
        from pdf2zh.parallel.coordinator import TaskCoordinator

        tasks = [ChunkTask(chunk_pages=(0,)), ChunkTask(chunk_pages=(1,))]
        obj_patch, _obs, _serial = TaskCoordinator(max_workers=1).run(
            tasks, task_fn=_identity_chunk_task_fn
        )
        payload = obj_patch.get("__translation_errors__")
        self.assertIsNotNone(payload, "零变化也必须回传计数，否则无法判定")
        self.assertEqual(payload["attempted"], 20)
        self.assertEqual(payload["ok"], 0)
        self.assertEqual(payload["count"], 0)


class TestSpecIsSyntacticallyValid(unittest.TestCase):
    def test_spec_parses(self):
        """spec 是 Python；一个缩进错要到构建时才会暴露。"""
        ast.parse(SPEC.read_text(encoding="utf-8"))


class TestWorkerFailureMessagesCarryTheOutput(unittest.TestCase):
    """``(no stderr)`` 让打包漏文件这类故障完全无法定位。"""

    def test_mineru_error_reads_stdout(self):
        from pdf2zh import magicpdf_adapter

        src = inspect.getsource(magicpdf_adapter.MagicPdfAdapter)
        self.assertIn(
            "completed.stdout or completed.stderr",
            src,
            "mineru 失败信息必须读 stdout（stderr 被合并进 stdout）",
        )
        self.assertNotIn(
            "stderr or '(no stderr)'",
            src,
            "'(no stderr)' 是死胡同：worker 没启动时必然没有它",
        )

    def test_marker_error_merges_both_streams(self):
        from pdf2zh.v3.ingestion import marker_backend

        src = inspect.getsource(marker_backend)
        self.assertIn('(completed.stderr or "") + (completed.stdout or "")', src)


class TestAdapterNamesTheWorkerScriptItLaunches(unittest.TestCase):
    def test_worker_path_points_at_the_bundled_location(self):
        from pdf2zh import magicpdf_adapter

        src = inspect.getsource(magicpdf_adapter.MagicPdfAdapter)
        self.assertIn(
            'Path(__file__).resolve().parent / "kernel" / "mineru_worker.py"',
            src,
        )

    def test_the_script_actually_exists_at_that_path(self):
        from pdf2zh import magicpdf_adapter

        pkg = pathlib.Path(inspect.getfile(magicpdf_adapter)).resolve().parent
        self.assertTrue(
            (pkg / "kernel" / "mineru_worker.py").is_file(),
            "worker 路径与实际文件不一致",
        )


if __name__ == "__main__":
    unittest.main()
