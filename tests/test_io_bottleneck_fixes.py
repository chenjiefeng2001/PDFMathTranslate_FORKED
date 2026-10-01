"""I/O 瓶颈修复回归测试(报告 P0×2 / P1×2 / P2×3)。

- P0-1: TaskState.to_dict(summary=True) 剔除四个巨字段;list_tasks 端点使用摘要。
- P0-2: cache.py busy_timeout 对齐 30s;get/set 重试一次并显式 warning。
- P1-1: magicpdf JSON dump 紧凑化(indent=None);_collect_magicpdf_results
        不再把 JSON 转储列入 result_files。
- P1-2: 事件历史环形缓冲(_EVENT_RING_SIZE),get_events 游标校正。
- P2-1: GUI 路径 raw 指向源文件(不再复制)。
- P2-2: 上传时 hash 后台计算(_spawn_file_hash)。
- P2-3: _TaskStore 事件驱动唤醒(wait_signal),SSE 泵用 wait_task_signal。
"""

import io
import json
import os
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from pdf2zh.cache import TranslationCache as LegacyCache, _TranslationCache
from pdf2zh.services.api import create_api_app
from pdf2zh.services.runtime_service import (
    _EVENT_RING_SIZE,
    RuntimeService,
    TaskProgressEvent,
    TaskStage,
)


# ── P0-1 ────────────────────────────────────────────────────────────────────
class TestSummaryDict:
    MEGA = ("ir_snapshots", "processor_reports", "gate_verdicts", "toc_ir_records")

    def _mk(self, svc, tid, status):
        svc._store.create_task(tid)
        svc._store.update_task(
            tid,
            status=status,
            result_files=[{"name": "x-mono.pdf", "path": "x-mono.pdf"}],
            ir_snapshots={"0": {"k": "v" * 1000}},
            processor_reports={"0": {"a": 1}},
            gate_verdicts={"0": {"ok": True}},
            toc_ir_records={"0": [1]},
        )
        return svc.get_task_state(tid)

    def test_summary_excludes_mega_fields(self):
        svc = RuntimeService()
        try:
            st = self._mk(svc, "t_s", TaskStage.COMPLETED.value)
            full = st.to_dict()
            summary = st.to_dict(summary=True)
            for key in self.MEGA:
                assert key in full
                assert summary[key] is None
            assert summary["result_files"] == [
                {"name": "x-mono.pdf", "path": "x-mono.pdf"}
            ]
            assert summary["status"] == TaskStage.COMPLETED.value
        finally:
            svc.shutdown()

    def test_list_tasks_uses_summary(self):
        svc = RuntimeService()
        try:
            self._mk(svc, "t_l", TaskStage.COMPLETED.value)
            client = TestClient(
                create_api_app(service=svc), base_url="http://127.0.0.1:11009"
            )
            body = client.get("/api/tasks").json()
            assert len(body) == 1
            row = body[0]
            assert row["result_zip_name"] is None
            for key in self.MEGA:
                assert row[key] is None
            detail = client.get("/api/tasks/t_l").json()
            for key in self.MEGA:
                assert detail[key] is not None
        finally:
            svc.shutdown()


# ── P0-2 ────────────────────────────────────────────────────────────────────
class TestLegacyCacheRetry:
    def test_init_db_has_long_busy_timeout(self):
        src = io.open("pdf2zh/cache.py", encoding="utf-8").read()
        assert '"busy_timeout": 30000' in src
        assert '"synchronous": "normal"' in src
        assert "time.sleep(0.2)" in src

    def test_set_retries_then_warns(self):
        cache = LegacyCache.__new__(LegacyCache)
        cache.params = {}
        cache.translate_engine = "test"
        cache.translate_engine_params = "{}"
        calls = {"n": 0, "ok": False}

        def fake_create(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
            calls["ok"] = True
            return None

        orig = _TranslationCache.create
        _TranslationCache.create = staticmethod(fake_create)
        try:
            cache.set("hello", "世界")
            assert calls["ok"] is True
            assert calls["n"] == 2
        finally:
            _TranslationCache.create = orig

    def test_get_retries_then_returns_none(self):
        cache = LegacyCache.__new__(LegacyCache)
        cache.params = {}
        cache.translate_engine = "test"
        cache.translate_engine_params = "{}"
        calls = {"n": 0}

        def fake_get_or_none(**kwargs):
            calls["n"] += 1
            raise RuntimeError("database is locked")

        orig = _TranslationCache.get_or_none
        _TranslationCache.get_or_none = staticmethod(fake_get_or_none)
        try:
            assert cache.get("missing") is None
            assert calls["n"] == 2
        finally:
            _TranslationCache.get_or_none = orig


# ── P1-1 ────────────────────────────────────────────────────────────────────
class TestMagicpdfCompactDumps:
    def test_dumps_use_compact_json(self):
        src = io.open("pdf2zh/magicpdf_cli.py", encoding="utf-8").read()
        # 函数调用不含 indent=2(允许 docstring/注释提及该名字)
        assert "json.dump(" in src
        for line in src.splitlines():
            if line.strip().startswith("json.dump("):
                assert "indent=2" not in line
        assert "to_json(indent=None)" in src
        assert "indent=None" in src  # render_plan/ingest 也紧凑化

    def test_collect_magicpdf_results_skips_json(self, tmp_path):
        svc = RuntimeService()
        try:
            tid = "t_mg"
            svc._store.create_task(tid)
            out = tmp_path / "out"
            magic = out / "magicpdf"
            magic.mkdir(parents=True)
            (magic / "in_document.json").write_text("{}", encoding="utf-8")
            (magic / "in_magicpdf.json").write_text("{}", encoding="utf-8")
            (magic / "in_mono.pdf").write_bytes(b"%PDF-1.4 mono")
            (magic / "in_dual.pdf").write_bytes(b"%PDF-1.4 dual")
            svc._collect_magicpdf_results(tid, str(out), 1)
            st = svc.get_task_state(tid)
            assert st.status == TaskStage.COMPLETED.value
            names = [rf["name"] for rf in st.result_files]
            assert names == ["in_dual.pdf", "in_mono.pdf"]
        finally:
            svc.shutdown()


# ── P1-2 ────────────────────────────────────────────────────────────────────
class TestEventRingBuffer:
    def test_ring_caps_and_drop_accounting(self):
        svc = RuntimeService()
        try:
            svc._store.create_task("t_ring")
            total = _EVENT_RING_SIZE + 500
            for i in range(total):
                svc._store.add_event(
                    "t_ring",
                    TaskProgressEvent(
                        task_id="t_ring", stage="s", progress=i, message=str(i)
                    ),
                )
            events = svc._store.get_events("t_ring")
            assert len(events) <= _EVENT_RING_SIZE
            assert svc._store._dropped["t_ring"] == total - len(events)
            # 游标校正:since=0 回退到最早可用
            assert events[0].message == str(total - len(events))
        finally:
            svc.shutdown()

    def test_ring_prune_drops_counter(self):
        svc = RuntimeService()
        try:
            svc._store.create_task("t_ring2")
            svc._store.add_event(
                "t_ring2", TaskProgressEvent(task_id="t_ring2", stage="s", progress=1)
            )
            svc._store._dropped["t_ring2"] = 5
            svc._store.remove_task("t_ring2")
            assert "t_ring2" not in svc._store._dropped
        finally:
            svc.shutdown()


# ── P2-3 ────────────────────────────────────────────────────────────────────
class TestEventWakeSignal:
    def test_wait_task_signal_wakes_on_event(self):
        svc = RuntimeService()
        try:
            svc._store.create_task("t_wake")
            got = []

            def _waiter():
                svc.wait_task_signal("t_wake", 5.0)
                got.append(True)

            th = threading.Thread(target=_waiter)
            th.start()
            svc._store.add_event(
                "t_wake", TaskProgressEvent(task_id="t_wake", stage="s", progress=1)
            )
            th.join(timeout=3)
            assert got == [True]
        finally:
            svc.shutdown()


# ── P2-1 / P2-2 ─────────────────────────────────────────────────────────────
class TestGuiRawAndHash:
    def test_worker_uses_source_file_without_copy(self):
        """P2-1: GUI 提交原文件路径,不做 shutil.copy 复制。

        GUI 已模块化(``pdf2zh/gui/worker.py``);上传路径由
        ``_resolve_source_paths`` 透传,不产生副本。
        """
        from pdf2zh.gui.worker import _resolve_source_paths

        src_dir = Path(__file__).resolve().parent.parent / "pdf2zh" / "gui"
        sources = "\n".join(
            p.read_text(encoding="utf-8") for p in src_dir.rglob("*.py")
        )
        assert "shutil.copy2" not in sources
        assert "shutil.copyfile" not in sources

        tmp = Path(os.environ.get("TEMP", "/tmp"))
        f1, f2 = tmp / "p2_1_a.pdf", tmp / "p2_1_b.pdf"
        for f in (f1, f2):
            f.write_bytes(b"%PDF-1.4 stub")
        try:
            resolved = _resolve_source_paths("file", [str(f1), str(f2)], "", None)
            assert resolved == [str(f1), str(f2)]
        finally:
            for f in (f1, f2):
                f.unlink(missing_ok=True)

    def test_file_hash_stays_in_cache_layer(self):
        """P2-2: hash 只在缓存层按需计算,不在上传/提交路径同步执行。

        旧实现在 gui.py 里用 ``_spawn_file_hash`` 预热;模块化后 GUI 不再需要
        预热 —— 文件 hash 由 ``pdf2zh.cache`` 在查缓存时惰性计算。
        """
        from pdf2zh import cache as cache_mod

        assert callable(cache_mod.compute_file_hash)
        assert "compute_file_hash" in (Path(cache_mod.__file__).read_text("utf-8"))
        src_dir = Path(__file__).resolve().parent.parent / "pdf2zh" / "gui"
        sources = "\n".join(
            p.read_text(encoding="utf-8") for p in src_dir.rglob("*.py")
        )
        # GUI 提交路径不得同步计算整文件 hash
        assert "compute_file_hash" not in sources
        assert "md5" not in sources
        assert "sha256" not in sources
