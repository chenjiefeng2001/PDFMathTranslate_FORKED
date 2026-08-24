"""Module: V6.0 Prompt Manager & Context Builder.

Implements the report's "推理继承式提示词" (reasoning-inherited prompts):
prompts are assembled from a persistent inheritance chain instead of rebuilt
from scratch every call.

    [System Base]
       └─ [Style]            from StyleMemory   (tone/voice/formality)
          └─ [Domain]        from ReasoningMemory
             └─ [Glossary]   from Document+Entity memory
                └─ [Task]    translate paragraph / caption / keep formula

The ContextBuilder produces the doc-context payload; the PromptManager turns
it into actual chat prompts for the target translator backend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pdf2zh.v3.document_ir import SemanticRole, TranslationRole

logger = logging.getLogger(__name__)


SYSTEM_BASE = (
    "You are a professional technical document translator. "
    "Translate the source text into {target_lang}. "
    "Preserve all mathematical formulas, numbers, citations and reference "
    "markers exactly as-is. Output only the translated text, no commentary."
)


@dataclass
class PromptTemplate:
    """One prompt template variant, keyed by SemanticRole."""

    name: str
    task: str
    extra_instructions: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "task": self.task,
            "extra_instructions": self.extra_instructions,
        }


# Default prompt templates per semantic role.
DEFAULT_TEMPLATES: Dict[str, PromptTemplate] = {
    "paragraph": PromptTemplate(
        name="paragraph",
        task=(
            "Translate the following paragraph as a coherent unit. Keep the "
            "original line breaks implied by sentence structure; do not split "
            "into unrelated fragments."
        ),
    ),
    "heading": PromptTemplate(
        name="heading",
        task=(
            "Translate the heading. Use the document's established terminology. "
            "Return a short heading only."
        ),
        extra_instructions="Keep the heading concise and hierarchical.",
    ),
    "caption": PromptTemplate(
        name="caption",
        task=(
            "Translate the figure/table caption. Preserve figure/table numbers "
            "such as 'Fig. 3' or 'Table 1' exactly."
        ),
        extra_instructions=(
            "Anchor the translation to the referenced figure/table; keep the "
            "number prefix unchanged."
        ),
    ),
    "formula": PromptTemplate(
        name="formula",
        task=(
            "This is a mathematical formula. Do not translate its content. "
            "Return the formula verbatim."
        ),
        extra_instructions="NEVER modify formula symbols.",
    ),
    "footnote": PromptTemplate(
        name="footnote",
        task="Translate the footnote text.",
        extra_instructions="Keep footnote markers [1], [2] unchanged.",
    ),
    "table": PromptTemplate(
        name="table",
        task="Translate the table cell text.",
        extra_instructions="Preserve the tabular structure and column alignment.",
    ),
    "reference": PromptTemplate(
        name="reference",
        task=(
            "Translate the bibliographic reference. Keep author names, years "
            "and journal names consistent with the rest of the document."
        ),
    ),
    "abstract": PromptTemplate(
        name="abstract",
        task="Translate the abstract in formal academic language.",
        extra_instructions="Keep keywords consistent with the body translation.",
    ),
    "default": PromptTemplate(
        name="default",
        task="Translate the following text faithfully.",
    ),
}


class ContextBuilder:
    """Build the document-level context payload from the memory layers."""

    def __init__(self, memory_hub=None) -> None:
        self.memory_hub = memory_hub

    def build(self) -> dict:
        if self.memory_hub is None:
            return {"glossary": {}, "style": {}, "domain": ""}
        return self.memory_hub.build_prompt_context()


class PromptManager:
    """Assemble system + style + domain + glossary + task into a prompt."""

    def __init__(
        self,
        context_builder: Optional[ContextBuilder] = None,
        templates: Optional[Dict[str, PromptTemplate]] = None,
        target_lang: str = "zh-cn",
    ) -> None:
        self.context_builder = context_builder or ContextBuilder()
        self.templates = templates or DEFAULT_TEMPLATES
        self.target_lang = target_lang

    # ── Template selection ────────────────────────────────────────

    @staticmethod
    def template_for(semantic) -> str:
        """Map a SemanticRole (or node_type) to a template name."""
        if isinstance(semantic, SemanticRole):
            name = semantic.value
        else:
            name = str(semantic)
        mapping = {
            "body_text": "paragraph",
            "paragraph": "paragraph",
            "heading": "heading",
            "caption": "caption",
            "formula": "formula",
            "formula_inline": "formula",
            "footnote": "footnote",
            "table": "table",
            "table_cell": "table",
            "reference": "reference",
            "bibliography": "reference",
            "abstract": "abstract",
            "keywords": "abstract",
            "list": "paragraph",
            "list_item": "paragraph",
        }
        return mapping.get(name, "default")

    def get_template(self, semantic) -> PromptTemplate:
        name = self.template_for(semantic)
        return self.templates.get(name, self.templates["default"])

    # ── Prompt assembly ───────────────────────────────────────────

    def _build_system(self) -> str:
        ctx = self.context_builder.build()
        system = SYSTEM_BASE.format(target_lang=self.target_lang)
        parts = [system]
        style = ctx.get("style") or {}
        if style:
            rules = "; ".join(f"{k}: {v}" for k, v in style.items())
            parts.append(f"[Style] {rules}")
        domain = ctx.get("domain") or ""
        if domain:
            parts.append(f"[Domain] This document is in the domain: {domain}")
        glossary = ctx.get("glossary") or {}
        if glossary:
            lines = "; ".join(f"{k}->{v}" for k, v in glossary.items())
            parts.append(f"[Glossary] Use these fixed terms: {lines}")
        entities = ctx.get("entities") or {}
        if entities:
            lines = ", ".join(sorted(entities.keys()))
            parts.append(f"[Entities] Keep these proper nouns unchanged: {lines}")
        return "\n".join(parts)

    def build_prompt(
        self,
        text: str,
        semantic=None,
        is_formula: bool = False,
        keep_numbers: bool = False,
    ) -> dict:
        """Assemble the full chat prompt payload."""
        template = self.get_template(semantic)
        system = self._build_system()
        instructions = template.task
        if template.extra_instructions:
            instructions = f"{instructions}\n{template.extra_instructions}"
        if keep_numbers:
            instructions += "\nKeep all numbers exactly as-is."
        if is_formula or (
            isinstance(semantic, SemanticRole)
            and semantic in (SemanticRole.FORMULA, SemanticRole.FORMULA_INLINE)
        ):
            instructions += (
                "\nWARNING: the content contains formulas; copy them verbatim."
            )
        user = f"{instructions}\n\n---\n{text}"
        return {"system": system, "user": user, "template": template.name}

    def build_batch_prompt(self, chunks: List[dict]) -> dict:
        """Build a JSON batch prompt from chunk dicts with node_ids."""
        items = []
        for chunk in chunks:
            items.append(
                {
                    "node_id": chunk.get("node_id", ""),
                    "text": chunk.get("text", ""),
                    "instruction": self.get_template(chunk.get("semantic")).task,
                }
            )
        return {
            "system": self._build_system(),
            "user": (
                "Translate each item below. Return a JSON array of objects with "
                "'node_id' and 'translated' fields. Keep all formulas intact.\n\n"
                + str(items)
            ),
        }


__all__ = ["PromptTemplate", "ContextBuilder", "PromptManager", "DEFAULT_TEMPLATES"]
