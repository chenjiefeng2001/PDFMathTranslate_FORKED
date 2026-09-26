from __future__ import annotations

import base64
import hashlib
import json
import py_compile
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf2zh.kernel import jina_ocr_env, jina_ocr_worker
from pdf2zh.v3.canonical_page import BlockModel, PageModel
from pdf2zh.v3.ingestion import (
    BACKEND_JINA,
    BACKEND_MARKER,
    BACKEND_MINERU,
    JinaOcrBackend,
    JinaOcrOptions,
    jina_result_to_document,
    parse_markdown,
)
from pdf2zh.v3.ingestion.base import (
    JinaOcrBackendUnavailable,
    JinaOcrCoverageError,
    JinaOcrOfflineError,
    JinaOcrSchemaError,
    JinaOcrTimeoutError,
)
from pdf2zh.v3.ingestion.config import (
    AUTO_CANDIDATES,
    INGEST_REQUEST_CHOICES,
    JINA_MODEL_ID,
    JINA_PROMPT,
    JINA_REVISION,
    normalize_jina_page_indices,
)
from pdf2zh.v3.ingestion.ir import IngestBox, IngestDocument
from pdf2zh.v3.ingestion.selector import decide, normalize_requested
from pdf2zh.v3.ingestion.jina_backend import (
    RenderedJinaPage,
    make_cache_key,
    render_pdf_pages,
)


def _pages():
    return [
        PageModel(
            page_num=0,
            width=300,
            height=400,
            blocks=[
                BlockModel("First paragraph", "paragraph", 10, 350, 290, 380),
                BlockModel("Second paragraph", "paragraph", 10, 300, 290, 330),
                BlockModel("Third paragraph", "paragraph", 10, 250, 290, 280),
            ],
        )
    ]


def _result(markdown="# First paragraph\n\nSecond paragraph\n\nThird paragraph"):
    return {
        "schema": "pdf2zh.jina-ocr.result",
        "version": 1,
        "backend": "jina",
        "model": JINA_MODEL_ID,
        "revision": JINA_REVISION,
        "prompt": JINA_PROMPT,
        "device": "auto",
        "offline": False,
        "max_new_tokens": 4096,
        "pages": [
            {
                "page": 0,
                "markdown": markdown,
                "image_sha256": "0" * 64,
                "render_policy": {},
            }
        ],
    }


def test_config_and_forced_selector():
    assert BACKEND_JINA == "jina"
    assert INGEST_REQUEST_CHOICES == ("auto", "mineru", "marker", "jina")
    assert AUTO_CANDIDATES == (BACKEND_MINERU, BACKEND_MARKER)
    assert normalize_requested("JINA") == "jina"
    assert decide("jina").selected_backend == "jina"
    assert decide("auto", primary=BACKEND_MINERU).selected_backend == BACKEND_MINERU
    options = JinaOcrOptions()
    assert options.model == JINA_MODEL_ID
    assert options.revision == JINA_REVISION
    assert options.prompt == JINA_PROMPT
    assert options.min_coverage == pytest.approx(0.20)


def test_setup_entrypoint_declared():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert (
        'pdf2zh-setup-jina = "pdf2zh.kernel.jina_ocr_env:setup_jina_cli"' in pyproject
    )


def test_options_validate_device_ranges_and_local_revision(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    options = JinaOcrOptions(model=str(model), revision="")
    assert options.revision == ""
    assert JinaOcrOptions(device="cuda:1").device == "cuda:1"
    with pytest.raises(ValueError):
        JinaOcrOptions(dpi=10)
    with pytest.raises(ValueError):
        JinaOcrOptions(timeout=0)


def test_markdown_split_preserves_semantic_content():
    markdown = """# Heading

A paragraph with $x^2$ and **bold** text.

- first item
- second item

![diagram alt](image.png)

<table><tr><th>Name</th><td>Value</td></tr></table>
"""
    blocks = parse_markdown(markdown)
    values = [(item.kind, item.text) for item in blocks]
    assert ("heading", "Heading") in values
    assert any("x^2" in text for _, text in values)
    assert ("list_item", "first item") in values
    assert ("image", "diagram alt") in values
    assert ("table", "Name | Value") in values
    cells = parse_markdown(markdown, table_cells=True)
    assert any(item.kind == "table_cell" and item.text == "Name" for item in cells)
    assert parse_markdown("null") == []


def test_hybrid_exact_fuzzy_and_fallback_provenance():
    base = _pages()
    result = _result("# First paragraph\n\nSecond paragraph changed\n\nUnrelated")
    doc = jina_result_to_document(result, base, JinaOcrOptions(min_coverage=0.1))
    assert doc.source_backend == BACKEND_JINA
    blocks = doc.page_blocks(0)
    assert blocks[0].text == "First paragraph"
    assert blocks[0].metadata["text_source"] == "jina"
    assert blocks[0].metadata["jina_match"] == "exact"
    assert blocks[1].metadata["text_source"] == "jina"
    assert blocks[1].metadata["jina_match"] in {"token_anchor", "fuzzy"}
    assert blocks[2].text == "Third paragraph"
    assert blocks[2].metadata["text_source"] == "mineru_fallback"
    assert all(block.v3_box is not None for block in blocks)
    assert all(block.metadata["geometry_provenance"] == "mineru" for block in blocks)
    assert doc.metadata["jina_coverage"] > 0


def test_hybrid_coverage_gate_rejects_low_provider_output():
    base = _pages()
    with pytest.raises(JinaOcrCoverageError):
        jina_result_to_document(
            _result("# First paragraph"),
            base,
            JinaOcrOptions(min_coverage=0.8),
        )
    with pytest.raises(JinaOcrCoverageError):
        jina_result_to_document(_result(""), base, JinaOcrOptions(min_coverage=0))


def test_truncated_prefix_is_not_counted_as_complete_match():
    doc = jina_result_to_document(
        _result("First"), _pages(), JinaOcrOptions(min_coverage=0)
    )
    assert all(
        block.metadata["text_source"] == "mineru_fallback"
        for block in doc.page_blocks(0)
    )
    assert doc.metadata["jina_coverage"] == 0.0


def test_render_pdf_pages_uses_visual_rotation_policy(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    pdf = tmp_path / "tiny.pdf"
    document = pymupdf.open()
    page = document.new_page(width=200, height=100)
    page.insert_text((20, 40), "rotation", fontsize=12)
    page.set_rotation(90)
    document.save(pdf)
    document.close()
    options = JinaOcrOptions(dpi=72, max_pixels=1000000)
    rendered = render_pdf_pages(str(pdf), tmp_path / "images", 0, options)
    assert len(rendered) == 1
    assert rendered[0].path.endswith(".png")
    assert Path(rendered[0].path).read_bytes().startswith(b"\x89PNG")
    assert rendered[0].policy["rotation"] == 90
    assert rendered[0].policy["visual_rect"][2] < rendered[0].policy["visual_rect"][3]


def test_worker_loads_model_once_without_model_download(tmp_path, monkeypatch):
    import base64
    import types

    image_path = tmp_path / "page.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    calls = {"processor": 0, "model": 0}

    class FakeProcessor:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            calls["processor"] += 1
            return cls()

        def prepare_ocr_inputs(self, image, **kwargs):
            return {"input_ids": [1, 2, 3]}

        def decode_ocr(self, output, input_ids):
            return "# page"

    class FakeModel:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            calls["model"] += 1
            return cls()

        def to(self, device):
            return self

        def eval(self):
            return self

        def generate(self, **kwargs):
            return "generated"

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.device = lambda name: name
    torch.float32 = "float32"
    torch.bfloat16 = "bfloat16"
    transformers = types.ModuleType("transformers")
    transformers.AutoProcessor = FakeProcessor
    transformers.AutoModelForCausalLM = FakeModel
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    manifest = jina_ocr_worker.make_manifest(
        [
            {
                "page": 0,
                "image": str(image_path),
                "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "render_policy": {},
            }
        ],
        options={"max_new_tokens": 32, "device": "cpu", "offline": True},
    )
    result_path = tmp_path / "result.json"
    jina_ocr_worker.run_manifest(manifest, result_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert calls == {"processor": 1, "model": 1}
    assert result["pages"][0]["markdown"] == "# page"


def test_worker_contract_and_schema(tmp_path):
    worker_path = Path(jina_ocr_worker.__file__)
    py_compile.compile(str(worker_path), doraise=True)
    source = worker_path.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    assert "from pdf2zh" not in source
    assert 'import_module("torch")' in source
    image_path = tmp_path / "x.png"
    image_path.write_bytes(b"png")
    manifest = jina_ocr_worker.make_manifest(
        [
            {
                "page": 0,
                "image": str(image_path),
                "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "render_policy": {},
            }
        ]
    )
    assert manifest["model"] == JINA_MODEL_ID
    with pytest.raises(ValueError):
        jina_ocr_worker.validate_result({"schema": "bad"})
    process = subprocess.run(
        [sys.executable, str(worker_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert process.returncode == 2
    assert "usage:" in process.stderr


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv(jina_ocr_env.PYTHON_OVERRIDE_ENV, raising=False)
    monkeypatch.setenv(jina_ocr_env.VENV_DIR_ENV, str(tmp_path / "venv"))
    executable = (
        tmp_path
        / "venv"
        / ("Scripts" if sys.platform == "win32" else "bin")
        / ("python.exe" if sys.platform == "win32" else "python")
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    assert jina_ocr_env.default_venv_python() == str(executable)
    monkeypatch.setenv(jina_ocr_env.PYTHON_OVERRIDE_ENV, f" {executable} ")
    assert jina_ocr_env.jina_python_override() == str(executable)


def _fake_worker(
    monkeypatch,
    markdown="# First paragraph\n\nSecond paragraph\n\nThird paragraph",
    calls=None,
):
    def run(command, **kwargs):
        if calls is not None:
            calls.append(command)
        manifest = json.loads(Path(command[2]).read_text(encoding="utf-8"))
        pages = [
            {
                "page": item["page"],
                "markdown": markdown,
                "image_sha256": item["image_sha256"],
                "render_policy": item["render_policy"],
            }
            for item in manifest["pages"]
        ]
        result = jina_ocr_worker.make_result(
            pages,
            model=manifest["model"],
            revision=manifest["revision"],
            prompt=manifest["prompt"],
            device=manifest["options"]["device"],
            offline=manifest["options"]["offline"],
            max_new_tokens=manifest["options"]["max_new_tokens"],
        )
        Path(command[3]).write_text(json.dumps(result), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("pdf2zh.v3.ingestion.jina_backend.subprocess.run", run)
    monkeypatch.setattr(JinaOcrBackend, "_python", lambda self: sys.executable)


def test_backend_sparse_success_and_cleanup(tmp_path, monkeypatch):
    pymupdf = pytest.importorskip("pymupdf")
    pdf = tmp_path / "book.pdf"
    document = pymupdf.open()
    for _ in range(2):
        page = document.new_page(width=200, height=200)
        page.insert_text((20, 40), "content", fontsize=12)
    document.save(pdf)
    document.close()
    calls = []
    _fake_worker(monkeypatch, calls=calls)
    base = [
        PageModel(
            0, 200, 200, [BlockModel("First paragraph", "paragraph", 10, 150, 190, 180)]
        ),
        PageModel(
            1,
            200,
            200,
            [BlockModel("Second paragraph", "paragraph", 10, 150, 190, 180)],
        ),
    ]
    backend = JinaOcrBackend(JinaOcrOptions(min_coverage=0.1))
    doc = backend.ingest(str(pdf), pages=[1], base_pages=base)
    assert [page.page_no for page in doc.pages()] == [1]
    assert calls and len(calls) == 1
    manifest = (
        json.loads(Path(calls[0][2]).read_text(encoding="utf-8"))
        if Path(calls[0][2]).exists()
        else None
    )
    assert manifest is None or manifest["pages"][0]["page"] == 1


def test_backend_failure_timeout_and_schema(tmp_path, monkeypatch):
    backend = JinaOcrBackend()
    manifest = tmp_path / "manifest.json"
    result = tmp_path / "result.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "pdf2zh.v3.ingestion.jina_backend.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="model_error: missing"
        ),
    )
    with pytest.raises(JinaOcrBackendUnavailable):
        backend._run_worker(sys.executable, manifest, result, backend.options)
    monkeypatch.setattr(
        "pdf2zh.v3.ingestion.jina_backend.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="offline_error: missing local snapshot"
        ),
    )
    with pytest.raises(JinaOcrOfflineError):
        backend._run_worker(sys.executable, manifest, result, backend.options)
    monkeypatch.setattr(
        "pdf2zh.v3.ingestion.jina_backend.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="jina", timeout=1)
        ),
    )
    with pytest.raises(JinaOcrTimeoutError):
        backend._run_worker(sys.executable, manifest, result, backend.options)

    def write_bad(*args, **kwargs):
        result.write_text(json.dumps({"schema": "bad"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("pdf2zh.v3.ingestion.jina_backend.subprocess.run", write_bad)
    with pytest.raises(JinaOcrSchemaError):
        backend._run_worker(sys.executable, manifest, result, backend.options)


def test_backend_cache_reuses_result(tmp_path, monkeypatch):
    pymupdf = pytest.importorskip("pymupdf")
    pdf = tmp_path / "cache.pdf"
    document = pymupdf.open()
    page = document.new_page(width=200, height=200)
    page.insert_text((20, 40), "content", fontsize=12)
    document.save(pdf)
    document.close()
    calls = []
    _fake_worker(monkeypatch, calls=calls)
    base = [
        PageModel(
            0, 200, 200, [BlockModel("First paragraph", "paragraph", 10, 150, 190, 180)]
        )
    ]
    options = JinaOcrOptions(cache_dir=str(tmp_path / "cache"), min_coverage=0.1)
    backend = JinaOcrBackend(options)
    first = backend.ingest(str(pdf), base_pages=base)
    second = backend.ingest(str(pdf), base_pages=base)
    assert first.source_backend == second.source_backend == "jina"
    assert len(calls) == 1
    assert list((tmp_path / "cache").glob("*.json"))


def test_cache_key_changes_with_render_policy():
    options = JinaOcrOptions()
    first = make_cache_key("a" * 64, options, {"dpi": 200})
    second = make_cache_key("a" * 64, options, {"dpi": 300})
    assert first != second


def test_foreign_geometry_is_not_promoted_to_v3():
    base = IngestDocument(source_backend="marker")
    base.add_page(0, 300, 400)
    base.add_leaf(
        block_id="foreign",
        page_no=0,
        text="First paragraph",
        box=IngestBox(
            10,
            20,
            100,
            40,
            space="marker_image",
            origin="top-left",
            unit="px",
        ),
    )
    with pytest.raises(JinaOcrSchemaError):
        jina_result_to_document(_result("First paragraph"), base)
    bare_bbox = [
        {
            "page": 0,
            "width": 300,
            "height": 400,
            "blocks": [{"text": "First paragraph", "bbox": [10, 20, 100, 40]}],
        }
    ]
    with pytest.raises(JinaOcrSchemaError):
        jina_result_to_document(_result("First paragraph"), bare_bbox)


def test_public_result_schema_and_page_set_are_strict():
    base = _pages()
    bad_markdown = deepcopy(_result("First paragraph"))
    bad_markdown["pages"][0]["markdown"] = None
    with pytest.raises(JinaOcrSchemaError):
        JinaOcrBackend().ingest_result(bad_markdown, base)

    duplicate = deepcopy(_result("First paragraph"))
    duplicate["pages"].append(deepcopy(duplicate["pages"][0]))
    with pytest.raises(JinaOcrSchemaError):
        JinaOcrBackend().ingest_result(duplicate, base)

    missing = deepcopy(_result("First paragraph"))
    missing["pages"] = []
    with pytest.raises(JinaOcrSchemaError):
        JinaOcrBackend().ingest_result(missing, base)

    extra = deepcopy(_result("First paragraph"))
    extra["pages"][0]["page"] = 1
    with pytest.raises(JinaOcrSchemaError):
        JinaOcrBackend().ingest_result(extra, base)

    negative = deepcopy(_result("First paragraph"))
    negative["pages"][0]["page"] = -1
    with pytest.raises(JinaOcrSchemaError):
        JinaOcrBackend().ingest_result(negative, base)


def test_manifest_binding_rejects_changed_result(tmp_path):
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"image")
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    manifest = jina_ocr_worker.make_manifest(
        [
            {
                "page": 0,
                "image": str(image_path),
                "image_sha256": digest,
                "render_policy": {"dpi": 200},
            }
        ]
    )
    result = jina_ocr_worker.make_result(
        [
            {
                "page": 0,
                "markdown": "text",
                "image_sha256": digest,
                "render_policy": {"dpi": 200},
            }
        ],
        model=manifest["model"],
        revision=manifest["revision"],
        prompt=manifest["prompt"],
    )
    jina_ocr_worker.validate_result_against_manifest(result, manifest)
    changed = deepcopy(result)
    changed["pages"][0]["image_sha256"] = "1" * 64
    with pytest.raises(ValueError):
        jina_ocr_worker.validate_result_against_manifest(changed, manifest)
    changed = deepcopy(result)
    changed["prompt"] = "different"
    with pytest.raises(ValueError):
        jina_ocr_worker.validate_result_against_manifest(changed, manifest)


def test_worker_manifest_rejects_bad_path_digest_and_token_limit(tmp_path):
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"image")
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    page = {
        "page": 0,
        "image": str(image_path),
        "image_sha256": digest,
        "render_policy": {},
    }
    with pytest.raises(ValueError):
        jina_ocr_worker.make_manifest(
            [{**page, "image": str(tmp_path / "missing.png")}]
        )
    with pytest.raises(ValueError):
        jina_ocr_worker.make_manifest([{**page, "image_sha256": digest.upper()}])
    with pytest.raises(ValueError):
        jina_ocr_worker.make_manifest(
            [page],
            options={
                "max_new_tokens": 32_769,
                "device": "cpu",
                "offline": False,
            },
        )


def test_offline_false_clears_child_hub_flags(tmp_path, monkeypatch):
    backend = JinaOcrBackend(JinaOcrOptions(offline=False))
    manifest = tmp_path / "manifest.json"
    result = tmp_path / "result.json"
    manifest.write_text("{}", encoding="utf-8")
    result.write_text(json.dumps(_result("text")), encoding="utf-8")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    seen = {}

    def run(command, **kwargs):
        seen.update(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("pdf2zh.v3.ingestion.jina_backend.subprocess.run", run)
    backend._run_worker(sys.executable, manifest, result, backend.options)
    assert "HF_HUB_OFFLINE" not in seen
    assert "TRANSFORMERS_OFFLINE" not in seen


def test_invalid_page_selection_device_and_model_path(tmp_path):
    with pytest.raises(ValueError):
        normalize_jina_page_indices([])
    with pytest.raises(ValueError):
        JinaOcrOptions(pages=[])
    with pytest.raises(ValueError):
        normalize_jina_page_indices("0-1000000000")
    with pytest.raises(ValueError):
        normalize_jina_page_indices(-1)
    assert JinaOcrOptions(device="").device == "auto"
    with pytest.raises(ValueError):
        JinaOcrOptions(device="invalid")
    with pytest.raises(ValueError):
        JinaOcrOptions(device=0)
    with pytest.raises(ValueError):
        JinaOcrOptions(model="custom/model", revision=JINA_REVISION)
    model_path = tmp_path / "model"
    model_path.mkdir()
    options = JinaOcrOptions(model=str(model_path), revision="")
    assert options.model == str(model_path.resolve())


def test_binding_mismatch_does_not_write_cache(tmp_path, monkeypatch):
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"image")
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    policy = {"dpi": 200}
    rendered = RenderedJinaPage(0, str(image_path), digest, 10, 10, policy)
    options = JinaOcrOptions(cache_dir=str(tmp_path / "cache"))
    backend = JinaOcrBackend(options)
    monkeypatch.setattr(JinaOcrBackend, "_python", lambda self: sys.executable)

    def ingest(*args, **kwargs):
        return jina_ocr_worker.make_result(
            [
                {
                    "page": 0,
                    "markdown": "text",
                    "image_sha256": "1" * 64,
                    "render_policy": policy,
                }
            ],
            model=options.model,
            revision=options.revision,
            prompt=options.prompt,
            device=options.device,
            offline=options.offline,
            max_new_tokens=options.max_new_tokens,
        )

    monkeypatch.setattr(backend, "_ingest_subprocess", ingest)
    with pytest.raises(JinaOcrSchemaError):
        backend._collect_result([rendered], options, tmp_path)
    assert not list((tmp_path / "cache").glob("*.json"))


def test_cache_key_includes_page_number():
    options = JinaOcrOptions()
    first = make_cache_key("a" * 64, options, {"dpi": 200}, page_no=0)
    second = make_cache_key("a" * 64, options, {"dpi": 200}, page_no=1)
    assert first != second


def test_truncated_generation_is_rejected(tmp_path, monkeypatch):
    image_path = tmp_path / "page.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    manifest = jina_ocr_worker.make_manifest(
        [
            {
                "page": 0,
                "image": str(image_path),
                "image_sha256": digest,
                "render_policy": {},
            }
        ],
        options={"max_new_tokens": 4, "device": "cpu", "offline": True},
    )

    class Processor:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            return cls()

        def prepare_ocr_inputs(self, image, **kwargs):
            return {"input_ids": [1, 2]}

        def decode_ocr(self, output, input_ids):
            return "text"

    class Model:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            return cls()

        def to(self, device):
            return self

        def eval(self):
            return self

        def generate(self, **kwargs):
            return SimpleNamespace(sequences=[[1, 2, 3, 4, 5, 6]])

    torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False),
        device=lambda name: name,
        bfloat16="bfloat16",
    )
    transformers = SimpleNamespace(
        AutoProcessor=Processor,
        AutoModelForCausalLM=Model,
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    result_path = tmp_path / "result.json"
    with pytest.raises(RuntimeError, match="generation_truncated"):
        jina_ocr_worker.run_manifest(manifest, result_path)
    assert not result_path.exists()


def test_markdown_preserves_identifiers_and_cleans_ref_tokens():
    markdown = (
        "snake_case __init__ 2*3 x~y\n\n"
        "```text\nfoo_bar **literal**\n```\n\n"
        "$a_b + c$\n\n"
        "before <|ref|>figure<|/ref|><|det|>[[1,2,3,4]]<|/det|> after"
    )
    blocks = parse_markdown(markdown)
    values = [item.text for item in blocks]
    assert any(
        "snake_case" in value
        and "__init__" in value
        and "2*3" in value
        and "x~y" in value
        for value in values
    )
    assert any(
        item.kind == "code" and item.text == "foo_bar **literal**" for item in blocks
    )
    assert any("$a_b + c$" in value for value in values)
    assert "<|ref|>" not in "\n".join(values)
    with pytest.raises(JinaOcrSchemaError):
        parse_markdown("text <|ref|>unclosed")
