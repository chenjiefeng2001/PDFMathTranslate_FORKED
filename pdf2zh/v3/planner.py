"""Module 5: Translation Planner.

Generates translation strategies for each node in the DocumentGraph.

Unlike a simple "one prompt fits all" approach, the Translation Planner
produces a per-node TranslationPlan that specifies:

  - Prompt template (differentiated by NodeType)
  - Context window (preceding/following nodes, document title, abstract)
  - Glossary entries (ensuring term consistency)
  - Temperature / model selection
  - Chunk strategy (how to split long content)

Typical usage::

    planner = TranslationPlanner()
    plans: Dict[str, TranslationPlan] = planner.plan_all(graph, doc_metadata)
    plan = plans[node_id]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from pdf2zh.v3.graph import (
    DocumentGraph,
    DocumentNode,
    EdgeType,
    NodeType,
)

# ── Constants ──────────────────────────────────────────────────────────

DEFAULT_TEMPERATURE = 0.1
DEFAULT_MODEL = "gpt-4o"


# ── Prompt Templates ──────────────────────────────────────────────────

PROMPT_TEMPLATES: Dict[NodeType, str] = {}

PROMPT_TEMPLATES[NodeType.PARAGRAPH] = """\
You are a professional academic translator. Translate the following text from {source_lang} to {target_lang}.

{glossary_section}
Context:
{context}

Text to translate:
{text}

Translation rules:
- Preserve all original paragraph breaks.
- Keep all numbers, citations ([1], [2,3]), and URLs unchanged.
- Use natural {target_lang} phrasing.
- Output ONLY the translated text, no explanations.
"""

PROMPT_TEMPLATES[NodeType.HEADING] = """\
You are a professional academic translator. Translate the following heading from {source_lang} to {target_lang}.

{glossary_section}
Text to translate:
{text}

Translation rules:
- Keep concise and match the heading style.
- Preserve any numbering (e.g., "3.1", "Section 4").
- Capitalize appropriately for {target_lang} heading style.
- Output ONLY the translated heading.
"""

PROMPT_TEMPLATES[NodeType.CAPTION] = """\
You are a professional academic translator. Translate the following figure/table caption from {source_lang} to {target_lang}.

{glossary_section}
Text to translate:
{text}

Translation rules:
- Preserve ALL original numbers, labels, and identifiers (e.g., "Figure 1", "Table 2").
- Keep any parenthetical references, citations, or notes.
- Translate only the descriptive text.
- Output ONLY the translated caption.
"""

PROMPT_TEMPLATES[NodeType.ABSTRACT] = """\
You are a professional academic translator. Translate the following abstract from {source_lang} to {target_lang}.

{glossary_section}
Text to translate:
{text}

Translation rules:
- Maintain formal academic tone.
- Preserve all technical terms, acronyms, and citations.
- Keep numbers and equations unchanged.
- Output ONLY the translated abstract.
"""

PROMPT_TEMPLATES[NodeType.FORMULA] = """\
You are a professional academic translator working with mathematical content.

Content to process:
{text}

Instructions:
- Do NOT modify any LaTeX commands, $...$ or $$...$$ delimiters.
- Translate only surrounding text or explanatory parts.
- If this is purely mathematical notation, return it unchanged.
"""

PROMPT_TEMPLATES[NodeType.REFERENCE] = """\
You are a professional academic translator. Process the following reference entry.

Text:
{text}

Instructions:
- Preserve ALL author names, DO NOT translate them.
- Preserve ALL numbers, years, volume/issue/page numbers, DOIs, and URLs.
- Preserve journal/conference names; only optionally translate common words.
- If the reference is already in {target_lang}, return it unchanged.
- Output ONLY the processed reference.
"""

PROMPT_TEMPLATES[NodeType.FOOTNOTE] = """\
You are a professional academic translator. Translate the following footnote from {source_lang} to {target_lang}.

Text to translate:
{text}

Translation rules:
- Keep all superscript markers, numbers, and symbols.
- Translate only the descriptive text.
- Output ONLY the translated footnote.
"""

PROMPT_TEMPLATES[NodeType.HEADER] = """\
You are a professional translator. Translate the following page header from {source_lang} to {target_lang}.

Text to translate:
{text}

Rules:
- Keep it very short (header length must not increase significantly).
- Preserve any page numbers or running head formatting.
- Output ONLY the translated header.
"""

PROMPT_TEMPLATES[NodeType.FOOTER] = PROMPT_TEMPLATES[NodeType.HEADER]

PROMPT_TEMPLATES[NodeType.LIST_ITEM] = """\
You are a professional academic translator. Translate the following list item from {source_lang} to {target_lang}.

{glossary_section}
Text to translate:
{text}

Rules:
- Preserve any bullet/number markers.
- Keep all technical terms consistent via glossary.
- Output ONLY the translated list item.
"""

PROMPT_TEMPLATES[NodeType.CODE] = """\
You are a technical translator. Process the following code block.

Text:
{text}

Instructions:
- Do NOT modify any code syntax, keywords, variable names, or comments.
- Return the code block completely unchanged.
"""


FALLBACK_PROMPT = """\
You are a professional translator. Translate the following text from {source_lang} to {target_lang}.

{glossary_section}
Text to translate:
{text}

Output ONLY the translated text.
"""


# ── Data Structures ───────────────────────────────────────────────────


@dataclass
class TranslationChunk:
    """A single chunk of text to be translated together."""
    text: str
    node_ids: List[str]
    chunk_index: int = 0


@dataclass
class ContextWindow:
    """Context surrounding a translation unit."""
    preceding_texts: List[str] = field(default_factory=list)
    following_texts: List[str] = field(default_factory=list)
    doc_title: str = ""
    abstract: str = ""
    section_title: str = ""


@dataclass
class TranslationPlan:
    """Complete translation plan for one node or chunk.

    Attributes:
        prompt: Assembled prompt string ready to send to LLM.
        template_name: Name of the template used.
        temperature: LLM temperature parameter.
        model: Model identifier.
        chunks: List of TranslationChunk (usually 1; >1 for long content).
        glossary: List of (term, translation) pairs.
        context_window: Context surrounding this translation unit.
        node_ids: IDs of nodes covered by this plan.
        strategy: Chunk strategy name.
        preserve_newlines: Whether to preserve newlines in output.
    """
    prompt: str = ""
    template_name: str = ""
    temperature: float = DEFAULT_TEMPERATURE
    model: str = DEFAULT_MODEL
    chunks: List[TranslationChunk] = field(default_factory=list)
    glossary: List[Tuple[str, str]] = field(default_factory=list)
    context_window: ContextWindow = field(default_factory=ContextWindow)
    node_ids: List[str] = field(default_factory=list)
    strategy: str = "single"
    preserve_newlines: bool = True

# ── Prompt Manager ────────────────────────────────────────────────────


class PromptManager:
    """Manages prompt templates and assembles final prompts.

    Supports:
      - Template selection by NodeType
      - Glossary injection
      - Context injection
      - Custom instructions per node
    """

    def __init__(self, templates: Optional[Dict[NodeType, str]] = None):
        self.templates = PROMPT_TEMPLATES.copy()
        if templates:
            self.templates.update(templates)

    def get_template(self, node_type: NodeType) -> str:
        """Return the prompt template for the given node type."""
        return self.templates.get(node_type, FALLBACK_PROMPT)

    def render(
        self,
        node_type: NodeType,
        text: str,
        context_window: ContextWindow,
        glossary: List[Tuple[str, str]],
        source_lang: str = "auto",
        target_lang: str = "zh-CN",
        custom_instructions: str = "",
    ) -> str:
        """Assemble the final prompt by filling the template."""
        template = self.get_template(node_type)

        # Build glossary section
        glossary_section = ""
        if glossary:
            entries = "\n".join(
                f"  \"{term}\" → \"{translation}\""
                for term, translation in glossary
            )
            glossary_section = f"Glossary (use these translations consistently):\n{entries}\n"

        # Build context
        context_parts = []
        if context_window.doc_title:
            context_parts.append(f"Document title: {context_window.doc_title}")
        if context_window.abstract:
            context_parts.append(f"Abstract: {context_window.abstract}")
        if context_window.section_title:
            context_parts.append(f"Section: {context_window.section_title}")
        if context_window.preceding_texts:
            context_parts.append("Preceding text: " + " | ".join(context_window.preceding_texts[-3:]))
        if context_window.following_texts:
            context_parts.append("Following text: " + " | ".join(context_window.following_texts[:2]))

        context = "\n".join(context_parts) if context_parts else "(no additional context)"

        instructions = custom_instructions if custom_instructions else "(follow default translation rules)"

        return template.format(
            text=text,
            context=context,
            glossary_section=glossary_section,
            instructions=instructions,
            source_lang=source_lang,
            target_lang=target_lang,
        )


# ── Context Builder ───────────────────────────────────────────────────


class ContextBuilder:
    """Builds context windows for nodes in the DocumentGraph.

    Uses FOLLOWS edges, document metadata, and section hierarchy
    to determine preceding/following context for any node.
    """

    def __init__(self, max_preceding: int = 3, max_following: int = 2):
        self.max_preceding = max_preceding
        self.max_following = max_following

    def build(
        self,
        graph: DocumentGraph,
        node: DocumentNode,
    ) -> ContextWindow:
        """Build context window for a given node."""
        cw = ContextWindow()

        # Collect preceding/following texts via content ordering
        all_content = [
            n for n in graph.nodes
            if n.node_type not in (NodeType.DOCUMENT, NodeType.PAGE,
                                   NodeType.HEADER, NodeType.FOOTER)
        ]
        content_ids = [n.id for n in all_content]

        if node.id in content_ids:
            idx = content_ids.index(node.id)
            start = max(0, idx - self.max_preceding)
            preceding = all_content[start:idx]
            end = min(len(all_content), idx + 1 + self.max_following)
            following = all_content[idx + 1:end]

        cw.preceding_texts = [n.text for n in preceding if n.text.strip()]
        cw.following_texts = [n.text for n in following if n.text.strip()]

        # Find document title
        doc_nodes = graph.get_nodes_by_type(NodeType.DOCUMENT)
        if doc_nodes and doc_nodes[0].text:
            cw.doc_title = doc_nodes[0].text

        # Find abstract
        abstract_nodes = graph.get_nodes_by_type(NodeType.ABSTRACT)
        if abstract_nodes:
            cw.abstract = "\n".join(n.text for n in abstract_nodes if n.text.strip())

        # Find section title via SAME_SECTION edges
        section_edges = [
            e for e in node.out_edges + node.in_edges
            if e.edge_type == EdgeType.SAME_SECTION
        ]
        for edge in section_edges:
            other_id = edge.target_id if edge.source_id == node.id else edge.source_id
            other_node = graph.get_node(other_id)
            if other_node and other_node.node_type in (NodeType.SECTION, NodeType.SUBSECTION):
                cw.section_title = other_node.text
                break
        return cw



# ── Glossary Manager ──────────────────────────────────────────────────


@dataclass
class GlossaryEntry:
    """A single glossary entry."""
    source_term: str
    target_term: str
    aliases: List[str] = field(default_factory=list)
    category: str = "general"
    confidence: float = 1.0


class GlossaryManager:
    """Manages translation glossary / terminology database.

    Supports:
      - Term → canonical translation lookup
      - Alias resolution (e.g., LLM → Large Language Model → 大语言模型)
      - Category filtering
    """

    def __init__(self):
        self._entries: Dict[str, GlossaryEntry] = {}
        self._alias_map: Dict[str, str] = {}

    def add_entry(self, entry: GlossaryEntry) -> None:
        key = entry.source_term.lower()
        self._entries[key] = entry
        self._alias_map[key] = key
        for alias in entry.aliases:
            self._alias_map[alias.lower()] = key

    def add_term(self, source: str, target: str, aliases: Optional[List[str]] = None) -> None:
        self.add_entry(GlossaryEntry(
            source_term=source,
            target_term=target,
            aliases=aliases or [],
        ))

    def resolve(self, term: str) -> Optional[str]:
        key = self._alias_map.get(term.lower())
        if key and key in self._entries:
            return self._entries[key].target_term
        return None

    def get_all_entries(self, category: Optional[str] = None) -> List[GlossaryEntry]:
        if category is None:
            return list(self._entries.values())
        return [e for e in self._entries.values() if e.category == category]

    def to_pairs(self, category: Optional[str] = None) -> List[Tuple[str, str]]:
        return [
            (e.source_term, e.target_term)
            for e in self.get_all_entries(category)
        ]

    def clear(self) -> None:
        self._entries.clear()
        self._alias_map.clear()


# ── Chunk Strategies ──────────────────────────────────────────────────


class ChunkStrategy(Enum):
    """Strategies for splitting long content into chunks."""
    SINGLE = "single"
    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"
    TOKEN_BUDGET = "token"


class ChunkSplitter:
    """Splits long text into manageable chunks."""

    def __init__(self, max_chars: int = 2000):
        self.max_chars = max_chars

    def split(self, text: str, strategy: ChunkStrategy = ChunkStrategy.SINGLE) -> List[str]:
        if strategy == ChunkStrategy.SINGLE:
            return [text]
        if strategy == ChunkStrategy.PARAGRAPH:
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
            return self._merge_by_budget(paragraphs)
        if strategy == ChunkStrategy.SENTENCE:
            sentences = re.split(r"(?<=[.?!])\s+", text)
            sentences = [s.strip() for s in sentences if s.strip()]
            return self._merge_by_budget(sentences)
        if strategy == ChunkStrategy.TOKEN_BUDGET:
            chunks = []
            remaining = text
            while len(remaining) > self.max_chars:
                split_at = remaining.rfind(". ", 0, self.max_chars)
                if split_at == -1 or split_at < self.max_chars // 2:
                    split_at = self.max_chars
                else:
                    split_at += 1
                chunks.append(remaining[:split_at].strip())
                remaining = remaining[split_at:].strip()
            if remaining:
                chunks.append(remaining)
            return chunks
        return [text]

    @staticmethod
    def _merge_by_budget(segments: List[str], budget: int = 2000) -> List[str]:
        chunks: List[str] = []
        current = ""
        for seg in segments:
            if len(current) + len(seg) + 1 <= budget:
                current = (current + "\n\n" + seg).strip()
            else:
                if current:
                    chunks.append(current)
                current = seg
        if current:
            chunks.append(current)
        return chunks


# ── Translation Planner ───────────────────────────────────────────────


@dataclass
class PlannerConfig:
    """Configuration for the Translation Planner."""
    source_lang: str = "auto"
    target_lang: str = "zh-CN"
    temperature: float = DEFAULT_TEMPERATURE
    model: str = DEFAULT_MODEL
    chunk_strategy: ChunkStrategy = ChunkStrategy.SINGLE
    max_chunk_chars: int = 2000
    context_preceding: int = 3
    context_following: int = 2
    custom_prompts: Optional[Dict[NodeType, str]] = None


class TranslationPlanner:
    """Main planner: generates TranslationPlan for each node in a DocumentGraph.

    Usage::

        planner = TranslationPlanner(config)
        graph = ...  # from SemanticAnalyzer
        plans = planner.plan_all(graph)
        plan = planner.plan(graph, node_id)
    """

    def __init__(
        self,
        config: Optional[PlannerConfig] = None,
        glossary: Optional[GlossaryManager] = None,
    ):
        self.config = config or PlannerConfig()
        self.prompt_manager = PromptManager(self.config.custom_prompts)
        self.context_builder = ContextBuilder(
            max_preceding=self.config.context_preceding,
            max_following=self.config.context_following,
        )
        self.glossary = glossary or GlossaryManager()
        self.chunk_splitter = ChunkSplitter(max_chars=self.config.max_chunk_chars)

    def plan(self, graph: DocumentGraph, node_id: str) -> TranslationPlan:
        node = graph.get_node(node_id)
        if node is None:
            raise ValueError(f"Node {node_id} not found in graph")

        context = self.context_builder.build(graph, node)
        glossary_pairs = self.glossary.to_pairs()

        # Select temperature based on node type
        temperature = self.config.temperature
        if node.node_type in (NodeType.FORMULA, NodeType.FORMULA_INLINE, NodeType.CODE):
            temperature = 0.0
        elif node.node_type in (NodeType.ABSTRACT, NodeType.HEADING):
            temperature = min(temperature, 0.1)

        # Select chunk strategy
        strategy = self.config.chunk_strategy
        if node.node_type == NodeType.REFERENCE:
            strategy = ChunkStrategy.SINGLE
        elif node.node_type in (NodeType.FORMULA, NodeType.CODE):
            strategy = ChunkStrategy.SINGLE

        # Split into chunks
        chunks_text = self.chunk_splitter.split(node.text, strategy)
        chunks = [
            TranslationChunk(text=t, node_ids=[node_id], chunk_index=i)
            for i, t in enumerate(chunks_text)
        ]

        prompt = self.prompt_manager.render(
            node_type=node.node_type,
            text=node.text,
            context_window=context,
            glossary=glossary_pairs,
            source_lang=self.config.source_lang,
            target_lang=self.config.target_lang,
        )

        template_name = node.node_type.value if node.node_type in PROMPT_TEMPLATES else "fallback"

        return TranslationPlan(
            prompt=prompt,
            template_name=template_name,
            temperature=temperature,
            model=self.config.model,
            chunks=chunks,
            glossary=glossary_pairs,
            context_window=context,
            node_ids=[node_id],
            strategy=strategy.value,
            preserve_newlines=(node.node_type not in (NodeType.HEADING, NodeType.CAPTION)),
        )

    def plan_all(self, graph: DocumentGraph) -> Dict[str, TranslationPlan]:
        plans: Dict[str, TranslationPlan] = {}
        skip_types = {
            NodeType.DOCUMENT, NodeType.PAGE, NodeType.FIGURE, NodeType.TABLE,
        }
        for node in graph.nodes:
            if node.node_type in skip_types:
                continue
            if not node.text.strip():
                continue
            if node.node_type == NodeType.CODE:
                continue
            plans[node.id] = self.plan(graph, node.id)
        return plans

    def plan_by_section(self, graph: DocumentGraph) -> Dict[str, List[TranslationPlan]]:
        sections: Dict[str, List[TranslationPlan]] = {}
        skip_types = {
            NodeType.DOCUMENT, NodeType.PAGE, NodeType.FIGURE, NodeType.TABLE,
        }
        for node in graph.nodes:
            if node.node_type not in (NodeType.SECTION, NodeType.SUBSECTION):
                continue
            section_plans: List[TranslationPlan] = []
            for edge in node.out_edges:
                if edge.edge_type == EdgeType.CONTAINS:
                    child = graph.get_node(edge.target_id)
                    if child and child.text.strip() and child.node_type not in skip_types:
                        try:
                            section_plans.append(self.plan(graph, child.id))
                        except ValueError:
                            continue
            if section_plans:
                sections[node.id] = section_plans
        return sections


__all__ = [
    "TranslationPlan", "TranslationChunk", "ContextWindow",
    "PromptManager", "ContextBuilder",
    "GlossaryEntry", "GlossaryManager",
    "ChunkStrategy", "ChunkSplitter",
    "PlannerConfig", "TranslationPlanner",
    "PROMPT_TEMPLATES",
]