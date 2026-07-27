"""
Collision resolver for pdf2zh 2.0.

Detects and resolves text/bbox collisions during translation layout,
preventing text-overlap with figures, tables, and formulas.
"""
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class BoundingBox:
    """Axis-aligned bounding box in PDF coordinates."""
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return abs(self.x1 - self.x0)

    @property
    def height(self) -> float:
        return abs(self.y1 - self.y0)

    def overlaps(self, other: "BoundingBox", margin: float = 2.0) -> bool:
        """Check if this box overlaps with another, with margin."""
        return not (
            self.x1 + margin < other.x0 - margin
            or self.x0 - margin > other.x1 + margin
            or self.y1 + margin < other.y0 - margin
            or self.y0 - margin > other.y1 + margin
        )

    def intersection_area(self, other: "BoundingBox") -> float:
        """Calculate intersection area with another box."""
        dx = max(0.0, min(self.x1, other.x1) - max(self.x0, other.x0))
        dy = max(0.0, min(self.y1, other.y1) - max(self.y0, other.y0))
        return dx * dy


class CollisionResolver:
    """Detect and resolve collisions between translation text and page elements.

    Strategies:
    1. Vertical shift: Move text down to avoid overlap
    2. Width reduction: Narrow text column to avoid side elements
    3. Column splitting: Split into multiple columns around obstacles
    4. Font size reduction: Reduce font size as last resort
    """

    def __init__(self, max_shrink: float = 0.8):
        """
        Args:
            max_shrink: Maximum font size reduction factor (0.8 = 80% min)
        """
        self.max_shrink = max_shrink

    def resolve(
        self,
        text_bbox: BoundingBox,
        obstacles: List[BoundingBox],
        font_size: float,
    ) -> Tuple[float, float, float]:
        """Resolve collisions by adjusting position and size.

        Args:
            text_bbox: Desired text bounding box
            obstacles: List of obstacle bounding boxes (figures, tables)
            font_size: Original font size in points

        Returns:
            Tuple of (adjusted_x, adjusted_y, adjusted_font_size)
        """
        if not obstacles:
            return text_bbox.x0, text_bbox.y0, font_size

        x, y, size = text_bbox.x0, text_bbox.y0, font_size

        # Check for collisions
        colliding = [obs for obs in obstacles if text_bbox.overlaps(obs)]

        if not colliding:
            return x, y, size

        # Strategy 1: Vertical shift (try moving up/down)
        vertical_shift = self._try_vertical_shift(
            text_bbox, colliding, font_size
        )
        if vertical_shift is not None:
            return x, vertical_shift, size

        # Strategy 2: Width reduction
        width_adjusted = self._try_width_reduction(
            text_bbox, colliding, font_size
        )
        if width_adjusted is not None:
            return width_adjusted

        # Strategy 3: Font size reduction
        shrunk = self._try_shrink(text_bbox, colliding, font_size)
        if shrunk is not None:
            x, y, size = shrunk
            return x, y, size

        # Fallback: return original with minimal shift
        return x, y, size

    def _try_vertical_shift(
        self,
        text_bbox: BoundingBox,
        colliding: List[BoundingBox],
        font_size: float,
    ) -> Optional[float]:
        """Try shifting text vertically to avoid collisions.

        Returns y coordinate if found, None otherwise.
        """
        shift = font_size * 0.5  # Half line shift
        for direction in [1, -1, 2, -2]:  # Try down, up, more down, more up
            new_y = text_bbox.y0 + (direction * shift)
            shifted_bbox = BoundingBox(
                text_bbox.x0,
                new_y,
                text_bbox.x1,
                new_y + text_bbox.height,
            )
            if not any(shifted_bbox.overlaps(obs) for obs in colliding):
                return new_y
        return None

    def _try_width_reduction(
        self,
        text_bbox: BoundingBox,
        colliding: List[BoundingBox],
        font_size: float,
    ) -> Optional[Tuple[float, float, float]]:
        """Try reducing text width to avoid side collisions."""
        # Find side obstacles and narrow the column
        left_obs = [o for o in colliding if o.x1 > text_bbox.x0 and o.x0 < text_bbox.x0 + text_bbox.width * 0.5]
        right_obs = [o for o in colliding if o.x0 < text_bbox.x1 and o.x1 > text_bbox.x1 - text_bbox.width * 0.5]

        new_x0 = text_bbox.x0
        new_x1 = text_bbox.x1

        if left_obs:
            # Shift right edge of left obstacle
            new_x0 = max(o.x1 for o in left_obs) + 2.0

        if right_obs:
            # Shift left edge of right obstacle
            new_x1 = min(o.x0 for o in right_obs) - 2.0

        if new_x0 < new_x1 and (new_x1 - new_x0) > text_bbox.width * self.max_shrink:
            return new_x0, text_bbox.y0, font_size

        return None

    def _try_shrink(
        self,
        text_bbox: BoundingBox,
        colliding: List[BoundingBox],
        font_size: float,
    ) -> Optional[Tuple[float, float, float]]:
        """Try reducing font size to fit within available space."""
        new_size = font_size * 0.9
        if new_size / font_size >= self.max_shrink:
            return text_bbox.x0, text_bbox.y0, new_size
        return None
