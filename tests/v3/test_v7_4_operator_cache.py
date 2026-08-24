"""V7.4 Operator Result Cache — cache-aside 算子结果级缓存.

Covers the V7.4 iteration (see doc/v7_operator_runtime_report.md §六):

  - OperatorCacheSpec: per-operator input/output path declarations.
  - OperatorResultCache: content-addressed LRU cache with deepcopy isolation.
  - OperatorGraph.run(cache=...): cache-aside (hit → restore, miss → execute).
  - RuntimeService integration: cross-session / same-session reuse, input
    change invalidation, incremental re-runs reuse stable results.

Run with:
    python -m pytest tests/v3/test_v7_4_operator_cache.py -v
"""

from __future__ import annotations

import pytest

from pdf2zh.v3.operator_cache import (
    OperatorResultCache,
    apply_outputs,
    get_operator_cache_key,
)
from pdf2zh.v3.operators import (
    OperatorContext,
    OperatorGraph,
    ParseOperator,
    TranslateOperator,
)
from pdf2zh.v3.runtime_service import RuntimeService
from pdf2zh.v3.transformation_pipeline import PipelineConfig

BLOCKS = [
    {
        "id": "n0",
        "text": "Transformer models achieve state of the art " "results.",
        "type": "paragraph",
        "page": 0,
    },
    {"id": "n1", "text": "E = mc^2 is a famous formula.", "type": "formula", "page": 0},
]


@pytest.fixture()
def service(tmp_path) -> RuntimeService:
    return RuntimeService(persistence_dir=str(tmp_path))


@pytest.fixture()
def parse_graph() -> OperatorGraph:
    graph = OperatorGraph()
    graph.add(ParseOperator())
    return graph


# ── OperatorResultCache unit ──────────────────────────────────────────


class TestOperatorResultCache:
    def test_lru_eviction(self):
        cache = OperatorResultCache(max_entries=2)
        for key in ("k0", "k1", "k2", "k3"):
            cache.put(key, OperatorContext(), ParseOperator())
        assert len(cache) == 2
        # k0/k1 evicted (insertion order), k2/k3 remain
        assert cache.keys() == ["k2", "k3"]

    def test_get_moves_entry_to_lru_back(self):
        cache = OperatorResultCache(max_entries=2)
        cache.put("a", OperatorContext(), ParseOperator())
        cache.put("b", OperatorContext(), ParseOperator())
        assert cache.get("a") is not None
        assert cache.keys() == ["b", "a"]
        cache.put("c", OperatorContext(), ParseOperator())
        assert "b" not in cache.keys()

    def test_stats_and_clear(self):
        cache = OperatorResultCache()
        assert cache.stats()["hit_rate"] == 0.0
        cache.get("missing")  # a miss
        assert cache.stats()["misses"] == 1
        cache.clear()
        assert cache.stats() == {
            "entries": 0,
            "max_entries": 256,
            "hits": 0,
            "misses": 0,
            "skips": 0,
            "hit_rate": 0.0,
        }

    def test_skips_unknown_operator(self):
        cache = OperatorResultCache()
        graph = OperatorGraph()
        graph.add(ParseOperator())
        ctx = OperatorContext(document=BLOCKS, config=PipelineConfig())
        graph.run(ctx, cache=cache)
        assert cache.stats()["skips"] == 0

        # an operator without a declared cache spec is never cached
        class NoSpecOperator(ParseOperator):
            name = "no-spec-operator"

        custom = OperatorGraph()
        custom.add(NoSpecOperator())
        ctx2 = OperatorContext(document=BLOCKS, config=PipelineConfig())
        custom.run(ctx2, cache=cache)
        assert cache.stats()["skips"] == 1
        assert len(cache) == 1  # only "parse" was stored

    def test_apply_outputs_restores_paths(self):
        ctx = OperatorContext()
        ctx.extra["manifest"] = {}
        apply_outputs(
            ctx,
            {
                "extra.manifest": {"blocks": [1, 2]},
                "metrics.nodes": 5,
                "document_graph": object(),
            },
        )
        assert ctx.extra["manifest"]["blocks"] == [1, 2]
        assert ctx.metrics["nodes"] == 5
        assert ctx.document_graph is not None

    def test_key_content_addressing(self):
        a = OperatorContext(document=BLOCKS, config=PipelineConfig())
        b = OperatorContext(document=BLOCKS, config=PipelineConfig())
        c = OperatorContext(
            document=[dict(BLOCKS[0], text="changed.")] + [BLOCKS[1]],
            config=PipelineConfig(),
        )
        key_a = get_operator_cache_key(a, ParseOperator())
        key_b = get_operator_cache_key(b, ParseOperator())
        key_c = get_operator_cache_key(c, ParseOperator())
        assert key_a == key_b
        assert key_a != key_c
        assert key_a.startswith("parse:")

    def test_key_includes_provider_signature(self):
        from pdf2zh.v3.operator_cache import CACHE_SPECS, input_view_of
        from pdf2zh.v3.operators import _as_jsonable
        from pdf2zh.v3.transformation_pipeline import RuleBasedProvider
        import json as _json

        ctx = OperatorContext(
            document=BLOCKS,
            config=PipelineConfig(),
            provider=RuleBasedProvider("zh-CN"),
        )
        view = input_view_of(ctx, CACHE_SPECS["translate"])
        serialized = _json.dumps(_as_jsonable(view), sort_keys=True)
        assert "RuleBasedProvider" in serialized
        assert "zh-CN" in serialized
        # a different provider signature ⇒ different key
        other = OperatorContext(
            document=BLOCKS, config=PipelineConfig(), provider=RuleBasedProvider("en")
        )
        assert get_operator_cache_key(
            ctx, TranslateOperator()
        ) != get_operator_cache_key(other, TranslateOperator())


# ── OperatorGraph cache-aside ─────────────────────────────────────────


class TestOperatorGraphCaching:
    def test_second_run_reuses_parse_result(self, parse_graph):
        cache = OperatorResultCache()
        graph = parse_graph
        ctx1 = OperatorContext(document=BLOCKS, config=PipelineConfig())
        graph.run(ctx1, cache=cache)
        assert cache.stats()["misses"] == 1
        ctx2 = OperatorContext(document=BLOCKS, config=PipelineConfig())
        graph.run(ctx2, cache=cache)
        assert graph.trace[0]["cached"] is True
        assert cache.stats()["hits"] == 1
        # identical graph content restored
        assert ctx1.document_graph.to_dot() == ctx2.document_graph.to_dot()

    def test_changed_document_invalidates_parse(self, parse_graph):
        cache = OperatorResultCache()
        graph = parse_graph
        graph.run(
            OperatorContext(document=BLOCKS, config=PipelineConfig()), cache=cache
        )
        changed = [dict(BLOCKS[0], text="Transformer CHANGED.")] + [BLOCKS[1]]
        graph.run(
            OperatorContext(document=changed, config=PipelineConfig()), cache=cache
        )
        assert graph.trace[0]["cached"] is False
        assert cache.stats()["hits"] == 0
        assert cache.stats()["misses"] == 2

    def test_run_without_cache_executes_everything(self, parse_graph):
        graph = parse_graph
        graph.run(OperatorContext(document=BLOCKS, config=PipelineConfig()))
        assert graph.trace[0]["cached"] is False


# ── RuntimeService cache integration ──────────────────────────────────


class TestRuntimeServiceCaching:
    def test_second_session_same_document_hits_everything(self, service):
        s1 = service.open(BLOCKS)
        service.execute(s1.session_id)
        s2 = service.open(BLOCKS)
        service.execute(s2.session_id)
        trace = service.operator_graph.trace
        assert all(t["cached"] for t in trace)
        assert s1.translations == s2.translations
        stats = service.cache.stats()
        assert stats["hits"] == 7
        assert stats["misses"] == 7

    def test_second_run_same_session_reuses_unchanged_ops(self, service):
        s = service.open(BLOCKS)
        service.execute(s.session_id)
        service.execute(s.session_id)
        cached = {t["operator"] for t in service.operator_graph.trace if t["cached"]}
        # parse/analyze/plan/review/render read only the source + config;
        # translate/layout keys move because the session now carries the
        # previously produced translations (incremental-friendly carry-over).
        assert {"parse", "analyze", "plan", "review", "render"} <= cached
        assert service.cache.stats()["hit_rate"] > 0.0

    def test_input_change_invalidates_downstream(self, service):
        s = service.open(BLOCKS)
        service.execute(s.session_id)
        s.document[0]["text"] = "A completely different first sentence."
        service.execute(s.session_id)
        trace = {t["operator"]: t["cached"] for t in service.operator_graph.trace}
        assert trace["parse"] is False
        assert trace["translate"] is False

    def test_incremental_runs_reuse_stable_results(self, service):
        s = service.open(BLOCKS)
        service.execute(s.session_id)
        service.execute_incremental(s.session_id, ["n0"])
        first_inc = service.operator_graph.trace
        service.execute_incremental(s.session_id, ["n0"])
        second_inc = {t["operator"]: t["cached"] for t in service.operator_graph.trace}
        # nothing changed between the two incremental runs → everything in the
        # affected sub-graph is served from cache on the second pass.
        assert second_inc["parse"] is True
        assert second_inc["translate"] is True
        assert second_inc["render"] is True
        assert first_inc != second_inc  # first pass executed, second reused

    def test_distinct_documents_do_not_share_results(self, service):
        s1 = service.open(BLOCKS)
        service.execute(s1.session_id)
        other = [
            {"id": "x0", "text": "Unrelated content.", "type": "paragraph", "page": 1}
        ]
        s2 = service.open(other)
        service.execute(s2.session_id)
        trace = {t["operator"]: t["cached"] for t in service.operator_graph.trace}
        # parse / analyze / translate / layout / review / render depend on the
        # document content → must miss; plan depends only on config → reuse OK.
        for op in ("parse", "analyze", "translate", "layout", "review", "render"):
            assert trace[op] is False, f"{op} should not hit the cache"
        assert s1.translations["n0"] != s2.translations["x0"]

    def test_cache_exposed_in_stats(self, service):
        assert "cache" in service.stats()
        assert service.stats()["cache"]["max_entries"] >= 1

    def test_cache_can_be_disabled_explicitly(self, tmp_path):
        service = RuntimeService(persistence_dir=str(tmp_path), cache=False)
        assert service.cache is None
        assert service.stats()["cache"] == {"enabled": False}
        s = service.open(BLOCKS)
        service.execute(s.session_id)
        service.execute(s.session_id)
        trace = {t["operator"]: t["cached"] for t in service.operator_graph.trace}
        assert all(not v for v in trace.values())
