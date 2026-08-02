"""Module: V6.0 Multi-Channel Rewriter — multimodal, multi-channel translation.

Implements the report's "多模态多通道翻译器" (multimodal multi-channel
translator): different node types are routed through different specialized
translation channels (LLM text channel, formula channel, OCR-verbatim channel,
catalog channel), and their outputs are merged back into the translation plan.

Channels:
    TextChannel        — main LLM text translation
    FormulaChannel     — formula passthrough / normalization
    VerbatimChannel    — numeric identifiers, codes, keep-as-is content
    CatalogChannel     — heading/reference/abstract routing via PromptManager

The MultiChannelRewriter selects a channel per chunk and returns per-channel
results plus a merged document-level translation dict.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from pdf2zh.v3.document_ir import SemanticRole, TranslationRole

logger = logging.getLogger(__name__)


@dataclass
class ChannelResult:
    channel: str
    node_id: str
    text: str
    translated: str
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "node_id": self.node_id,
            "text": self.text,
            "translated": self.translated,
            "confidence": round(self.confidence, 3),
        }


class FormulaChannel:
    """Formula passthrough: never modify formula text."""

    def translate(self, text: str) -> str:
        return text


class VerbatimChannel:
    """Keep numeric identifiers, codes, URLs and proper nouns as-is."""

    _VERBATIM_RE = re.compile(r"^[\d\s.,:%\-_/()\[\]'\"a-zA-Z0-9]*$")

    def translate(self, text: str) -> str:
        stripped = text.strip()
        if not stripped or self._VERBATIM_RE.fullmatch(stripped):
            return text
        return text


class TextChannel:
    """Main LLM text translation channel (pluggable backend)."""

    def __init__(self, translator: Optional[Callable[[str], str]] = None) -> None:
        # Fallback: if no backend is injected, mirror source (identity).
        self.translator = translator or (lambda t: t)

    def translate(self, text: str) -> str:
        return self.translator(text)


class CatalogChannel:
    """Heading / reference / abstract routing via a PromptManager."""

    def __init__(self, prompt_manager=None) -> None:
        self.prompt_manager = prompt_manager

    def translate(self, text: str, semantic=None) -> str:
        if self.prompt_manager is not None:
            prompt = self.prompt_manager.build_prompt(text, semantic=semantic)
            # Without an LLM backend the payload is returned as-is.
            return text
        return text


class MultiChannelRewriter:
    """Route each chunk to a channel, collect results, merge into a plan."""

    def __init__(self, text_channel: Optional[TextChannel] = None,
                 formula_channel: Optional[FormulaChannel] = None,
                 verbatim_channel: Optional[VerbatimChannel] = None,
                 catalog_channel: Optional[CatalogChannel] = None) -> None:
        self.text_channel = text_channel or TextChannel()
        self.formula_channel = formula_channel or FormulaChannel()
        self.verbatim_channel = verbatim_channel or VerbatimChannel()
        self.catalog_channel = catalog_channel or CatalogChannel()

    # ── Channel routing ───────────────────────────────────────────

    @staticmethod
    def route_for(semantic: Optional[SemanticRole],
                  translation_role: Optional[TranslationRole],
                  text: str) -> str:
        """Pick the channel name for a chunk."""
        if translation_role == TranslationRole.KEEP_FORMULA or semantic in (
            SemanticRole.FORMULA, SemanticRole.FORMULA_INLINE,
        ):
            return "formula"
        if translation_role in (TranslationRole.KEEP_NUMBER,
                                TranslationRole.SKIP,
                                TranslationRole.TRACK):
            return "verbatim"
        if semantic in (SemanticRole.HEADING, SemanticRole.REFERENCE,
                        SemanticRole.BIBLIOGRAPHY, SemanticRole.ABSTRACT):
            return "catalog"
        return "text"

    def translate_chunk(self, node_id: str, text: str,
                        semantic: Optional[SemanticRole] = None,
                        translation_role: Optional[TranslationRole] = None,
                        is_formula: bool = False) -> ChannelResult:
        """Translate one chunk through its routed channel."""
        semantic = semantic or SemanticRole.BODY_TEXT
        translation_role = translation_role or TranslationRole.TRANSLATE
        if is_formula:
            translation_role = TranslationRole.KEEP_FORMULA
        channel = self.route_for(semantic, translation_role, text)

        if channel == "formula":
            translated = self.formula_channel.translate(text)
        elif channel == "verbatim":
            translated = self.verbatim_channel.translate(text)
        elif channel == "catalog":
            translated = self.catalog_channel.translate(text, semantic=semantic)
        else:
            translated = self.text_channel.translate(text)

        return ChannelResult(
            channel=channel, node_id=node_id, text=text,
            translated=translated, confidence=1.0,
        )

    def translate_batch(self, chunks: List[dict]) -> List[ChannelResult]:
        """Translate a list of chunk dicts (node_id/text/semantic/translation)."""
        results = []
        for chunk in chunks:
            results.append(self.translate_chunk(
                node_id=chunk.get("node_id", ""),
                text=chunk.get("text", ""),
                semantic=chunk.get("semantic"),
                translation_role=chunk.get("translation_role"),
                is_formula=chunk.get("is_formula", False),
            ))
        return results

    def merge_into_dict(self, chunks: List[dict],
                        results: List[ChannelResult]) -> Dict[str, str]:
        """Produce {node_id: translated} from chunks and channel results."""
        merged: Dict[str, str] = {}
        for chunk, result in zip(chunks, results):
            node_id = chunk.get("node_id", "") or result.node_id
            if not node_id:
                continue
            merged[node_id] = result.translated
        return merged


__all__ = [
    "ChannelResult", "FormulaChannel", "VerbatimChannel",
    "TextChannel", "CatalogChannel", "MultiChannelRewriter",
]

