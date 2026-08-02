"""Module: V6.0 Sentence Detector & Paragraph Reconstructor.

Implements the report's Phase 2 "段落语义重构" (paragraph semantic
reconstruction). Physical lines from the PDF are rebuilt into natural
sentences and then into complete paragraphs before being handed to the LLM,
so translation operates on semantically complete units instead of chopped
physical lines.

The SentenceDetector recognizes common false sentence boundaries:

    A. B.   (initials, not paragraph end)
    e.g.    (abbreviation, not a period)
    Fig. 3  (figure abbreviation)
    etc.    etc.

The ParagraphReconstructor merges physical lines into paragraphs using
indentation / justification / trailing-hyphen cues, then splits them into
sentences via the detector.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Abbreviations that should NOT be treated as sentence endings.
ABBREVIATIONS = {
    "e.g.", "i.e.", "etc.", "vs.", "cf.", "al.", "fig.", "figs.", "tab.",
    "tabs.", "eq.", "eqs.", "sec.", "ref.", "refs.", "no.", "nos.",
    "vol.", "pp.", "ed.", "eds.", "dept.", "est.", "approx.", "min.",
    "max.", "avg.", "viz.", "et", "al",
}

SINGLE_LETTER_INITIALS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Common title honorifics that are also not sentence ends.
HONORIFICS = {"mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st."}


@dataclass
class Sentence:
    text: str
    start: int
    end: int
    node_ids: List[str] = field(default_factory=list)
    is_final: bool = True

    def to_dict(self) -> dict:
        return {
            "text": self.text, "start": self.start, "end": self.end,
            "node_ids": list(self.node_ids), "is_final": self.is_final,
        }


@dataclass
class Paragraph:
    text: str
    node_ids: List[str] = field(default_factory=list)
    sentences: List[Sentence] = field(default_factory=list)

    @property
    def sentence_count(self) -> int:
        return len(self.sentences)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "node_ids": list(self.node_ids),
            "sentences": [s.to_dict() for s in self.sentences],
        }


class SentenceDetector:
    """Detect sentence boundaries in a stream of text.

    The detector tracks whether a period ends a sentence, accounting for
    abbreviations (e.g., Fig., etc.) and single-letter initials (A. B.).
    """

    def __init__(self, abbreviations: Optional[set] = None,
                 extra_honorifics: Optional[set] = None) -> None:
        self.abbreviations = set(ABBREVIATIONS) | set(HONORIFICS)
        if abbreviations:
            self.abbreviations.update(abbreviations)
        if extra_honorifics:
            self.abbreviations.update(extra_honorifics)
        self._abbr_lower = {a.lower() for a in self.abbreviations}

    @staticmethod
    def _token_before(text: str, idx: int) -> str:
        """Extract the token ending right before idx."""
        m = re.search(r"[A-Za-z0-9.']+\s*$", text[:idx])
        return m.group(0).strip() if m else ""

    def is_boundary(self, text: str, idx: int) -> bool:
        """Is the character at idx (expected '.') a sentence boundary?"""
        if idx >= len(text) or text[idx] != ".":
            return False
        prev_char = text[idx - 1] if idx > 0 else ""
        next_char = text[idx + 1] if idx + 1 < len(text) else ""

        if not prev_char.isalpha():
            # '1.' in an enumeration or '3.14' decimal — boundary only if
            # followed by whitespace and the token is a bare number.
            if prev_char.isdigit():
                after = text[idx + 1] if idx + 1 < len(text) else ""
                return after.isspace() or after == ""
            return False

        token = self._token_before(text, idx + 1).lower()
        if token in self._abbr_lower:
            return False
        # Single letter initial: "A. B."
        token_plain = token.rstrip(".")
        if len(token_plain) == 1 and token_plain.isalpha():
            following = text[idx + 1:]
            following = following.lstrip()
            if following:
                nxt = following[0]
                if nxt.isalpha() and nxt.isupper():
                    return False  # another initial
                if nxt == ".":
                    return False  # "A.." weirdness
            return following == "" or following[0] in ('"', "'", "(")
        # Common case: period followed by whitespace + uppercase/quote or EOF.
        if next_char in ("", "\n"):
            return True
        if next_char.isspace():
            following = text[idx + 1:].lstrip()
            if not following:
                return True
            first = following[0]
            if first.isupper() or first in ('"', "'", "("):
                return True
            return False
        # Period inside a number or identifier: "3.14", "v1.2"
        return False

    def split_sentences(self, text: str) -> List[Sentence]:
        """Split text into Sentence objects."""
        sentences: List[Sentence] = []
        start = 0
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "." and self.is_boundary(text, i):
                # Include the period and any trailing whitespace.
                end = i + 1
                while end < n and text[end].isspace():
                    end += 1
                sentences.append(Sentence(
                    text=text[start:end].strip(),
                    start=start, end=end,
                ))
                start = end
                i = end
            else:
                i += 1
        tail = text[start:].strip()
        if tail:
            sentences.append(Sentence(text=tail, start=start, end=n))
        if sentences:
            sentences[-1].is_final = True
        return sentences


class ParagraphReconstructor:
    """Group physical lines into paragraphs and split into sentences.

    Heuristics:
      - Lines with trailing '-' (hyphenation) merge with the next line.
      - Lines with a large vertical gap to the previous line start a new paragraph.
      - Blank lines separate paragraphs.
    """

    def __init__(self, detector: Optional[SentenceDetector] = None,
                 line_gap_ratio: float = 1.6) -> None:
        self.detector = detector or SentenceDetector()
        self.line_gap_ratio = line_gap_ratio

    @staticmethod
    def _strip_hyphen(text: str) -> Tuple[str, bool]:
        text = text.rstrip()
        if text.endswith("-") and len(text) > 1:
            return text[:-1], True
        return text, False

    def reconstruct_from_nodes(self, nodes) -> List[Paragraph]:
        """Reconstruct paragraphs from DocumentNode-like objects with .text and .bbox.

        Nodes are assumed sorted in reading order (page / y / x).
        """
        if not nodes:
            return []
        paragraphs: List[Paragraph] = []
        current_texts: List[str] = []
        current_ids: List[str] = []
        prev_y0: Optional[float] = None
        prev_font: Optional[float] = None

        def flush():
            if not current_texts:
                return
            text = " ".join(current_texts)
            sentences = self.detector.split_sentences(text)
            for s in sentences:
                s.node_ids = list(current_ids)
            paragraphs.append(Paragraph(
                text=text, node_ids=list(current_ids), sentences=sentences,
            ))
            current_texts.clear()
            current_ids.clear()

        for node in nodes:
            text = (getattr(node, "text", "") or "").strip()
            if not text:
                continue
            bbox = getattr(node, "bbox", None)
            y0 = bbox[1] if bbox else None
            font_size = getattr(node, "font_size", None)

            text, hyphenated = self._strip_hyphen(text)

            is_new_para = False
            if prev_y0 is not None and y0 is not None:
                gap = y0 - prev_y0
                line_height = (font_size or prev_font or 10.0)
                if gap > line_height * self.line_gap_ratio:
                    is_new_para = True

            if is_new_para and current_texts:
                flush()
            if hyphenated and current_texts:
                # Merge with previous without space
                current_texts[-1] = current_texts[-1] + text
            else:
                current_texts.append(text)
            current_ids.append(getattr(node, "id", ""))
            prev_y0 = y0
            prev_font = font_size or prev_font

        flush()
        return paragraphs

    def reconstruct_lines(self, lines: List[str]) -> List[Paragraph]:
        """Reconstruct from plain text lines (one physical line each)."""
        if not lines:
            return []
        paragraphs: List[Paragraph] = []
        current: List[str] = []

        def flush():
            if not current:
                return
            text = " ".join(current)
            sentences = self.detector.split_sentences(text)
            paragraphs.append(Paragraph(text=text, sentences=sentences))
            current.clear()

        for line in lines:
            stripped = line.strip()
            if not stripped:
                flush()
                continue
            stripped, hyphenated = self._strip_hyphen(stripped)
            if hyphenated and current:
                current[-1] = current[-1] + stripped
            else:
                current.append(stripped)
        flush()
        return paragraphs


__all__ = [
    "ABBREVIATIONS", "Sentence", "Paragraph",
    "SentenceDetector", "ParagraphReconstructor",
]


