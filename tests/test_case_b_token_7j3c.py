"""7J-3C / 7J-4 — Case B token contract latches.

7J-3C proved the current stack preserves special code points (ï — ► →)
end to end *iff* the translator keeps the BabelDOC placeholder tokens
verbatim; a token-garbling translator leaks ``<b1>``-shaped tokens as
visible literal text.  The whole restore path (``parse_translate_output``)
matches these tokens by regex, so the token *shape* is a contract: if
BabelDOC ever changes it, this latch flags the change before any release.

Engine-free on purpose: the full-pipeline token-faithfulness check lives in
the release gate (``doc/7j4/release_gate.py``), which runs the real engine.
"""

from __future__ import annotations

import re

import pytest

babeldoc = pytest.importorskip("babeldoc")

from babeldoc.translator.translator import BaseTranslator  # noqa: E402


@pytest.fixture()
def translator() -> BaseTranslator:
    """Minimal BaseTranslator subclass (all required methods stubbed)."""

    class _T(BaseTranslator):
        name = "token-latch"

        def __init__(self, lang_in: str = "en", lang_out: str = "zh") -> None:
            self.lang_in = lang_in
            self.lang_out = lang_out
            self.ignore_cache = True
            self.translate_call_count = 0
            self.translate_cache_call_count = 0

        def do_translate(self, text: str, rate_limit_params: dict = None) -> str:
            return text

        def do_llm_translate(self, text: str, rate_limit_params=None) -> str:
            raise NotImplementedError

    return _T("en", "zh")


def test_rich_text_left_placeholder_shape(translator: BaseTranslator) -> None:
    """Left placeholder must be ``<b{n}>`` (what 7J-3C observed at first divergence)."""
    assert translator.get_rich_text_left_placeholder(1) == "<b1>"
    assert translator.get_rich_text_left_placeholder(7) == "<b7>"


def test_rich_text_right_placeholder_shape(translator: BaseTranslator) -> None:
    """Right placeholder must be ``</b{n}>`` (paired token)."""
    assert translator.get_rich_text_right_placeholder(1) == "</b1>"
    assert translator.get_rich_text_right_placeholder(7) == "</b7>"


def test_formula_placeholder_reuses_left_shape(translator: BaseTranslator) -> None:
    """Formula placeholders use the same ``<b{n}>`` shape (7J-3C: ►/→ → <b1>/<b2>)."""
    assert translator.get_formular_placeholder(
        2
    ) == translator.get_rich_text_left_placeholder(2)


def test_token_shape_matches_restore_regex(translator: BaseTranslator) -> None:
    """The token must be matchable by the restore path's regex (il_translator).

    parse_translate_output removes/restores placeholder-shaped tokens with
    ``<b<digits>>`` / ``</b<digits>>`` patterns, so an integer-id token
    must match.
    """
    left = translator.get_rich_text_left_placeholder(1)
    right = translator.get_rich_text_right_placeholder(1)
    assert re.fullmatch(r"<b\d+>", left) is not None
    assert re.fullmatch(r"</b\d+>", right) is not None


def test_token_is_not_pdf_text_layer_corruption() -> None:
    """A leaked token is *visible* literal text, never NUL - the 7J-3C failure mode."""
    leaked = "<b1>"
    assert "\x00" not in leaked
    assert leaked.isprintable()
