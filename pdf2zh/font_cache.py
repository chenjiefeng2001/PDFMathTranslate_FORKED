"""
Document-level font cache for pdf2zh 2.0.

Prevents per-page font re-embedding by caching font resources
at the Document level. Each font style is registered once and
reused across all pages, reducing PDF size significantly.
"""
import logging
from typing import Dict, Optional

from pymupdf import Document, Font

logger = logging.getLogger(__name__)


class DocumentFontCache:
    """Document-level cache for font resources.

    Ensures each font style is embedded only once per document,
    rather than once per page. Fonts are identified by their
    filename/base name for cache key purposes.

    Usage:
        cache = DocumentFontCache(doc)
        font_name = cache.register(doc, font_path)
    """

    def __init__(self, doc: Document):
        self.doc = doc
        # font_path -> (font_name, pymupdf.Font, font_path)
        self._cache: Dict[str, tuple] = {}
        self._counter = 0

    def register(self, font_path: str) -> str:
        """Register a font and return its document-level font name.

        If the font was already registered, returns the existing name.
        Otherwise creates a new Font object and inserts it.

        Args:
            font_path: Absolute path to the font file

        Returns:
            Short font name (e.g. 'f0') for use in PDF operators
        """
        if font_path in self._cache:
            font_name, _, _ = self._cache[font_path]
            return font_name

        font_name = f"f{self._counter}"
        self._counter += 1
        noto = Font(font_name, font_path)
        self._cache[font_path] = (font_name, noto, font_path)
        logger.debug(
            "Registered font '%s' -> '%s'", font_path, font_name
        )
        return font_name

    def get_font(self, font_path: str) -> Optional[Font]:
        """Get the pymupdf Font object for a registered font.

        Args:
            font_path: Font file path used in register()

        Returns:
            pymupdf Font object, or None if not registered
        """
        if font_path in self._cache:
            _, noto, _ = self._cache[font_path]
            return noto
        return None

    def get_name(self, font_path: str) -> Optional[str]:
        """Get the short name for a registered font.

        Args:
            font_path: Font file path used in register()

        Returns:
            Short font name, or None if not registered
        """
        if font_path in self._cache:
            font_name, _, _ = self._cache[font_path]
            return font_name
        return None

    def get_registered_fonts(self) -> list:
        """Get all (font_name, Font, font_path) tuples."""
        return list(self._cache.values())

    @property
    def count(self) -> int:
        return len(self._cache)
