"""Headless tests for Phase 2 — P3a: LLMProvider + PostProcessor + Stats."""

from __future__ import annotations
import os, sys, unittest, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType
from pdf2zh.v3.memory import DocumentMemory
from pdf2zh.v3.planner import TranslationPlanner, PlannerConfig

B = (0, 0, 0, 0)


# ====================================================================
# LLMProvider
# ====================================================================
class TestLLMProvider(unittest.TestCase):
    def test_llm_response_defaults(self):
        from pdf2zh.v3.translator import LLMResponse

        r = LLMResponse(text="hello", model="mock")
        self.assertEqual(r.text, "hello")
        self.assertEqual(r.provider, "mock")
        self.assertEqual(r.finish_reason, "stop")
        self.assertEqual(r.token_count, 0)

    def test_mock_provider_instantiation(self):
        from pdf2zh.v3.translator import MockLLMProvider

        p = MockLLMProvider()
        self.assertIsNotNone(p)

    def test_mock_provider_complete(self):
        from pdf2zh.v3.translator import MockLLMProvider

        p = MockLLMProvider()
        resp = p.complete(
            [{"role": "user", "content": "Text to translate: Hello world"}]
        )
        self.assertIn("mock]", resp.text)
        self.assertEqual(resp.provider, "mock")

    def test_mock_provider_with_model(self):
        from pdf2zh.v3.translator import MockLLMProvider

        p = MockLLMProvider()
        resp = p.complete(
            [{"role": "user", "content": "Text to translate: x"}], model="gpt-4o"
        )
        self.assertEqual(resp.model, "gpt-4o")

    def test_mock_provider_stream(self):
        from pdf2zh.v3.translator import MockLLMProvider

        chunks = list(MockLLMProvider().stream([{"role": "user", "content": "Test"}]))
        self.assertEqual(len(chunks), 1)

    def test_mock_provider_delay(self):
        from pdf2zh.v3.translator import MockLLMProvider

        p = MockLLMProvider(delay_ms=10)
        start = time.time()
        p.complete([{"role": "user", "content": "x"}])
        self.assertGreaterEqual((time.time() - start) * 1000, 5)

    def test_openai_provider_instantiation(self):
        from pdf2zh.v3.translator import OpenAIProvider

        p = OpenAIProvider(api_key="test")
        self.assertEqual(p._default_model, "gpt-4o")

    def test_abstract_provider_is_abc(self):
        from pdf2zh.v3.translator import LLMProvider
        from abc import ABC

        self.assertTrue(issubclass(LLMProvider, ABC))


# ====================================================================
# PostProcessor
# ====================================================================
class TestPostProcessor(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.translator import PostProcessor

        self.pp = PostProcessor()

    def test_basic_cleanup(self):
        r = self.pp.process(" Hello , World . ")
        self.assertEqual(r.text, "Hello, World.")

    def test_parenthesis_cleanup(self):
        self.assertEqual(self.pp.process("( test )").text, "(test)")

    def test_whitespace_normalization(self):
        self.assertEqual(
            self.pp.process("  Line1\n  \nLine2  ").text, "Line1\n  \nLine2"
        )

    def test_empty_text(self):
        self.assertEqual(self.pp.process("").text, "")

    def test_no_issues_on_clean_text(self):
        self.assertEqual(len(self.pp.process("Clean text.").issues), 0)

    def test_with_memory_glossary_mismatch(self):
        from pdf2zh.v3.translator import PostProcessor

        mem = DocumentMemory()
        mem.remember_glossary("PDF", "PDF file", context="", confidence=1.0)
        node = DocumentNode(
            id="n1", node_type=NodeType.PARAGRAPH, bbox=B, text="PDF file format"
        )
        pp = PostProcessor(memory=mem)
        result = pp.process("The document is a Portable Document.", node=node)
        self.assertIn("term_mismatch", str(result.issues))

    def test_process_batch(self):
        from pdf2zh.v3.translator import PostProcessor

        results = PostProcessor().process_batch([(" Hello ", None), ("(World)", None)])
        self.assertEqual(results[0].text, "Hello")
        self.assertEqual(results[1].text, "(World)")

    def test_post_process_result_issues(self):
        from pdf2zh.v3.translator import PostProcessResult

        self.assertEqual(PostProcessResult(text="hi", issues=["warn"]).issues, ["warn"])


# ====================================================================
# TranslationStats
# ====================================================================
class TestTranslationStats(unittest.TestCase):
    def test_defaults(self):
        from pdf2zh.v3.translator import TranslationStats

        s = TranslationStats()
        self.assertEqual(s.total_nodes, 0)
        self.assertEqual(s.translated, 0)
        self.assertEqual(s.failed, 0)

    def test_merge(self):
        from pdf2zh.v3.translator import TranslationStats

        a = TranslationStats(
            total_nodes=10, translated=8, total_latency_ms=100, total_tokens=500
        )
        b = TranslationStats(total_nodes=5, translated=4, failed=1, total_latency_ms=50)
        a.merge(b)
        self.assertEqual(a.total_nodes, 15)
        self.assertEqual(a.total_latency_ms, 150)

    def test_to_dict(self):
        from pdf2zh.v3.translator import TranslationStats

        d = TranslationStats(
            total_nodes=5, translated=4, total_latency_ms=200, total_tokens=100
        ).to_dict()
        self.assertEqual(d["total_nodes"], 5)
        self.assertAlmostEqual(d["avg_latency_ms"], 50.0)

    def test_avg_latency_zero(self):
        from pdf2zh.v3.translator import TranslationStats

        self.assertEqual(TranslationStats().to_dict()["avg_latency_ms"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
