"""产物命名修复回归测试。

两个修复点:
1. 上传文件名保留原名:此前前端上传文件被无条件改写为
   ``{uuid8}_{原名}`` 落盘,翻译产物 ``{stem}-mono/-dual.pdf`` 因此与
   源文件对不上,且对未超长、不重名的文件也一律生效。修复后文件保留
   原名,同名覆盖由批次子目录隔离 + 同批重名顺延解决。
2. 「全部下载」ZIP 防重复命名:此前前端硬编码 ``pdf2zh-results.zip``。
   修复后后端打包时按 ``{stem}-translated-{yyyymmdd_hhmmss}.zip`` 组合
   下载名并存入任务状态 ``result_zip_name``,API 端点与前端统一使用。
"""

import os
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from pdf2zh.services.api import _safe_upload_name, create_api_app
from pdf2zh.services.runtime_service import (
    RuntimeService,
    TaskState,
    _sanitize_zip_stem,
    result_zip_download_name,
)


@pytest.fixture()
def fresh_service():
    from pdf2zh.services.runtime_singleton import reset_runtime_service

    reset_runtime_service()
    yield RuntimeService()
    reset_runtime_service()


class TestSafeUploadName:
    """_safe_upload_name:最小清洗 + 原名保留 + 同批重名顺延。"""

    def test_plain_name_unchanged(self):
        assert _safe_upload_name("Report final.pdf") == "Report final.pdf"
        assert _safe_upload_name("报告 2024.pdf") == "报告 2024.pdf"

    def test_path_components_stripped(self):
        assert _safe_upload_name("a/b/c.pdf") == "c.pdf"
        assert _safe_upload_name("a\\b\\c.pdf") == "c.pdf"
        assert _safe_upload_name("..\\evil.pdf") == "evil.pdf"

    def test_illegal_chars_replaced(self):
        out = _safe_upload_name('bad<>:"/|?*.pdf')
        assert not re.search(r'[<>:"/|?*]', out)
        assert out.endswith(".pdf")

    def test_windows_reserved_stem(self):
        assert _safe_upload_name("CON.pdf") == "_CON.pdf"
        assert _safe_upload_name("com1.pdf") == "_com1.pdf"

    def test_trailing_dots_and_spaces(self):
        assert _safe_upload_name("name. .pdf") == "name.pdf"
        assert _safe_upload_name("  spaced  .pdf") == "spaced.pdf"

    def test_empty_falls_back(self):
        assert _safe_upload_name("") == "document"
        assert _safe_upload_name(None) == "document"

    def test_duplicate_names_sequential(self):
        used = set()
        first = _safe_upload_name("doc.pdf", used)
        second = _safe_upload_name("doc.pdf", used)
        third = _safe_upload_name("doc.pdf", used)
        assert first == "doc.pdf"
        assert second == "doc (1).pdf"
        assert third == "doc (2).pdf"

    def test_duplicate_case_insensitive(self):
        used = {"doc.pdf"}
        assert _safe_upload_name("DOC.pdf", used) == "DOC (1).pdf"


class TestSubmitUploadKeepsOriginalName:
    """submit_task 上传:产物命名链的源头保留原始文件名。"""

    def _client(self, monkeypatch, svc, captured):
        def fake_submit(self, request):
            captured["request"] = request
            return "task_up"

        monkeypatch.setattr(RuntimeService, "submit_task", fake_submit)
        return TestClient(
            create_api_app(service=svc), base_url="http://127.0.0.1:11009"
        )

    def test_upload_keeps_original_filename(self, monkeypatch, fresh_service):
        captured = {}
        client = self._client(monkeypatch, fresh_service, captured)
        resp = client.post(
            "/api/tasks",
            data={"target_lang": "zh"},
            files={"file": ("My Report final.pdf", b"%PDF-1.4 fake")},
        )
        assert resp.status_code == 200
        src = captured["request"].source_path
        assert os.path.basename(src) == "My Report final.pdf"
        # 不再有 uuid 前缀
        assert not re.match(r"^[0-9a-f]{8}_", os.path.basename(src))
        # 落在批次子目录中(跨批次同名隔离)
        parent = os.path.basename(os.path.dirname(src))
        assert parent.startswith("batch_")
        grandparent = os.path.basename(os.path.dirname(os.path.dirname(src)))
        assert grandparent == "pdf2zh_api_uploads"
        assert os.path.exists(src)

    def test_batch_upload_same_name_sequential(self, monkeypatch, fresh_service):
        captured = {}
        client = self._client(monkeypatch, fresh_service, captured)
        resp = client.post(
            "/api/tasks",
            data={"target_lang": "zh"},
            files=[
                ("files", ("doc.pdf", b"AAA")),
                ("files", ("doc.pdf", b"BBB")),
            ],
        )
        assert resp.status_code == 200
        names = [os.path.basename(p) for p in captured["request"].files]
        assert names == ["doc.pdf", "doc (1).pdf"]
        # 同批文件在同一批次子目录,内容各自落盘
        dirs = {os.path.dirname(p) for p in captured["request"].files}
        assert len(dirs) == 1
        for p in captured["request"].files:
            assert os.path.exists(p)


class TestResultZipDownloadName:
    """ZIP 下载名:{stem}-translated-{yyyymmdd_hhmmss}.zip 防重复组合。"""

    def test_format_strips_special_suffix(self):
        state = TaskState(task_id="t", result_files=[{"name": "Report-mono.pdf"}])
        assert re.match(
            r"^Report-translated-\d{8}_\d{6}\.zip$", result_zip_download_name(state)
        )

    def test_suffix_stripped_case_insensitive(self):
        state = TaskState(task_id="t", result_files=[{"name": "B-DUAL.PDF"}])
        assert result_zip_download_name(state).startswith("B-translated-")

    def test_empty_results_fallback(self):
        state = TaskState(task_id="t", result_files=[])
        assert result_zip_download_name(state).startswith("pdf2zh-translated-")

    def test_illegal_chars_sanitized(self):
        state = TaskState(task_id="t", result_files=[{"name": "a<b-c-mono.pdf"}])
        name = result_zip_download_name(state)
        assert not re.search(r'[<>:"]', name)
        assert name.endswith(".zip")

    def test_overlong_stem_truncated_with_hash(self):
        stem = "L" * 200
        assert len(_sanitize_zip_stem(stem)) <= 80
        # 确定性:同输入同输出
        assert _sanitize_zip_stem(stem) == _sanitize_zip_stem(stem)
        state = TaskState(task_id="t", result_files=[{"name": f"{stem}-mono.pdf"}])
        name = result_zip_download_name(state)
        assert (
            len(os.path.basename(name)) <= 120
        )  # stem<=80 + -translated- + stamp + .zip
        assert name.endswith(".zip")

    def test_build_batch_zip_writes_result_zip_name(self, tmp_path):
        svc = RuntimeService()
        try:
            tid = "t_zipname"
            svc._store.create_task(tid)
            mono = tmp_path / "doc-mono.pdf"
            mono.write_bytes(b"%PDF-1.4 mono")
            dual = tmp_path / "doc-dual.pdf"
            dual.write_bytes(b"%PDF-1.4 dual")
            svc._store.update_task(
                tid,
                result_files=[
                    {"name": "doc-mono.pdf", "path": str(mono)},
                    {"name": "doc-dual.pdf", "path": str(dual)},
                ],
            )
            zip_path = svc._build_batch_zip(tid)
            assert zip_path and os.path.exists(zip_path)
            state = svc._store.get_task(tid)
            assert re.match(r"^doc-translated-\d{8}_\d{6}\.zip$", state.result_zip_name)
        finally:
            svc.shutdown()

    def test_result_zip_endpoint_uses_combined_name(self, tmp_path):
        svc = RuntimeService()
        try:
            tid = "t_zipdisp"
            svc._store.create_task(tid)
            out_pdf = tmp_path / "out-mono.pdf"
            out_pdf.write_bytes(b"%PDF-1.4 ok")
            with zipfile.ZipFile(tmp_path / "r.zip", "w") as zf:
                zf.write(out_pdf, arcname="out-mono.pdf")
            svc._store.update_task(
                tid,
                result_zip=str(tmp_path / "r.zip"),
                result_zip_name="doc-translated-20260101_090000.zip",
            )
            client = TestClient(
                create_api_app(service=svc), base_url="http://127.0.0.1:11009"
            )
            resp = client.get(f"/api/tasks/{tid}/result-zip")
            assert resp.status_code == 200
            disposition = resp.headers["content-disposition"]
            assert "doc-translated-20260101_090000.zip" in disposition
        finally:
            svc.shutdown()

    def test_result_zip_endpoint_falls_back_without_name(self, tmp_path):
        svc = RuntimeService()
        try:
            tid = "t_zipnofb"
            svc._store.create_task(tid)
            out_pdf = tmp_path / "out-mono.pdf"
            out_pdf.write_bytes(b"%PDF-1.4 ok")
            with zipfile.ZipFile(tmp_path / "r.zip", "w") as zf:
                zf.write(out_pdf, arcname="out-mono.pdf")
            svc._store.update_task(tid, result_zip=str(tmp_path / "r.zip"))
            client = TestClient(
                create_api_app(service=svc), base_url="http://127.0.0.1:11009"
            )
            resp = client.get(f"/api/tasks/{tid}/result-zip")
            assert resp.status_code == 200
            # 未打包过(result_zip_name 缺失)→ 历史格式兜底
            assert f"pdf2zh-{tid}-results.zip" in resp.headers["content-disposition"]
        finally:
            svc.shutdown()


class TestCompleteFileSetsZipName:
    """_complete_file 单文件完成路径同样确定 ZIP 下载名。"""

    def test_single_file_completion_sets_zip_name(self, tmp_path):
        svc = RuntimeService()
        try:
            mono = tmp_path / "a-mono.pdf"
            mono.write_bytes(b"%PDF-1.7 fake")
            svc._store.create_task("t_cfn")
            svc._complete_file(
                "t_cfn",
                [{"name": "a-mono.pdf", "path": str(mono)}],
                message="Completed",
            )
            ts = svc.get_task_state("t_cfn")
            assert ts.result_zip and ts.result_zip.endswith(".zip")
            assert re.match(r"^a-translated-\d{8}_\d{6}\.zip$", ts.result_zip_name)
        finally:
            svc.shutdown()
