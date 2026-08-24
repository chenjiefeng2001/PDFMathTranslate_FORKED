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
    1. Vertical shift: Move text DOWN (body-flow direction, y decreases) first,
       then UP, to avoid overlap.  Downward placement is the natural escape for
       text that expands after EN->CJK translation.
    2. Width reduction: Narrow text column to avoid side elements
    3. Column splitting: Split into multiple columns around obstacles
    4. Font size reduction: Reduce font size as last resort
    """

    def __init__(
        self,
        max_shrink: float = 0.8,
        margin: float = 2.0,
        paragraph_gap: float = 0.3,
        max_pass_iterations: int = 16,
    ):
        """
        Args:
            max_shrink: Maximum font size reduction factor (0.8 = 80% min)
            margin: Safety margin (points) kept around resolved obstacles
            paragraph_gap: Inter-paragraph gap added below the pushed-down
                position, expressed as a multiple of font_size
            max_pass_iterations: Cap for the greedy push pass (defensive)
        """
        self.max_shrink = max_shrink
        self.margin = margin
        self.paragraph_gap = paragraph_gap
        self.max_pass_iterations = max_pass_iterations

    def resolve(
        self,
        text_bbox: BoundingBox,
        obstacles: List[BoundingBox],
        font_size: float,
        page_rect: Optional[BoundingBox] = None,
        return_strategy: bool = False,
    ) -> Tuple[float, float, float]:
        """Resolve collisions by adjusting position and size.

        Args:
            text_bbox: Desired text bounding box
            obstacles: List of obstacle bounding boxes (figures, tables,
                previously rendered paragraphs).  The FULL set must be passed:
                after moving down, the text may hit elements that were not
                colliding in the original position.
            font_size: Original font size in points
            page_rect: Page bounds (PDF coordinates, y-axis up) used to clamp
                the shifted position inside the page.
            return_strategy: When True, return a 4-tuple
                (adjusted_x, adjusted_y, adjusted_font_size, strategy) where
                strategy is one of "noop" / "clear" / "vertical" / "width" /
                "shrink" / "none".

        Returns:
            Tuple of (adjusted_x, adjusted_y, adjusted_font_size), or a
            4-tuple when return_strategy is True.
        """
        if not obstacles:
            result = (text_bbox.x0, text_bbox.y0, font_size, "noop")
            return result if return_strategy else result[:3]

        x, y, size = text_bbox.x0, text_bbox.y0, font_size

        # Check for collisions
        colliding = [obs for obs in obstacles if text_bbox.overlaps(obs)]

        if not colliding:
            result = (x, y, size, "clear")
            return result if return_strategy else result[:3]

        # Strategy 1: Vertical shift (try moving DOWN first, then UP)
        # 传入全部障碍物而非仅当前重叠项：
        # 向下偏移后可能撞到原本未重叠的下方元素，需一并避让。
        vertical_shift = self._try_vertical_shift(
            text_bbox, obstacles, font_size, page_rect
        )
        if vertical_shift is not None:
            result = (x, vertical_shift, size, "vertical")
            return result if return_strategy else result[:3]

        # Strategy 2: Width reduction
        width_adjusted = self._try_width_reduction(text_bbox, colliding, font_size)
        if width_adjusted is not None:
            result = (*width_adjusted, "width")
            return result if return_strategy else result[:3]

        # Strategy 3: Font size reduction
        shrunk = self._try_shrink(text_bbox, colliding, font_size)
        if shrunk is not None:
            result = (*shrunk, "shrink")
            return result if return_strategy else result[:3]

        # Fallback: return original with minimal shift
        result = (x, y, size, "none")
        return result if return_strategy else result[:3]

    def _try_vertical_shift(
        self,
        text_bbox: BoundingBox,
        colliding: List[BoundingBox],
        font_size: float,
        page_rect: Optional[BoundingBox] = None,
    ) -> Optional[float]:
        """Try shifting text vertically to avoid collisions.

        PDF y-axis points up: "down" is decreasing y and is the natural body
        flow direction for EN->CJK expansion.  A greedy exact push is used
        instead of the old fixed-step probing: each pass moves the box below
        the lowest blocking obstacle, so stacked obstacles are resolved in
        O(passes) regardless of line-count expansion.

        Returns y coordinate if found, None otherwise.
        """
        height = text_bbox.height
        # ---- Priority 1: push DOWN (正文推进方向，y 减小) ----
        y_down = self._push_down(text_bbox, colliding, height, font_size, page_rect)
        if y_down is not None:
            return y_down
        # ---- Priority 2: push UP (y 增大) ----
        return self._push_up(text_bbox, colliding, height, font_size, page_rect)

    def _bbox_at(self, text_bbox: BoundingBox, y0: float) -> BoundingBox:
        return BoundingBox(text_bbox.x0, y0, text_bbox.x1, y0 + text_bbox.height)

    def _push_down(
        self,
        text_bbox: BoundingBox,
        colliding: List[BoundingBox],
        height: float,
        font_size: float,
        page_rect: Optional[BoundingBox],
    ) -> Optional[float]:
        """Greedy downward placement below every blocking obstacle.

        Each pass finds the obstacles still overlapping the candidate box,
        then re-anchors the box just below the lowest (largest y0) blocker,
        leaving a margin + paragraph gap.  Repeats until clear or stuck.
        """
        y_candidate = text_bbox.y0
        for _ in range(self.max_pass_iterations):
            bbox = self._bbox_at(text_bbox, y_candidate)
            blocking = [obs for obs in colliding if bbox.overlaps(obs)]
            if not blocking:
                return y_candidate
            lowest_top = max(obs.y0 for obs in blocking)
            new_y = (
                lowest_top
                - height
                - 2 * self.margin
                - font_size * self.paragraph_gap
                - 0.01
            )
            if page_rect is not None:
                new_y = max(new_y, page_rect.y0 + font_size)
            if new_y >= y_candidate:
                # 无法继续向下推进（已到页面底部 / 数值异常）
                return None
            y_candidate = new_y
        return None

    def _push_up(
        self,
        text_bbox: BoundingBox,
        colliding: List[BoundingBox],
        height: float,
        font_size: float,
        page_rect: Optional[BoundingBox],
    ) -> Optional[float]:
        """Greedy upward placement above every blocking obstacle."""
        y_candidate = text_bbox.y0
        for _ in range(self.max_pass_iterations):
            bbox = self._bbox_at(text_bbox, y_candidate)
            blocking = [obs for obs in colliding if bbox.overlaps(obs)]
            if not blocking:
                return y_candidate
            highest_bottom = min(obs.y1 for obs in blocking)
            new_y = highest_bottom + 2 * self.margin + 0.01
            if page_rect is not None:
                # P2：顶部夹紧保留一个字号空间，避免字形 ascent 把 bbox 顶出页面
                # （否则 push_up 到 page_rect.y1 时 pymupdf bbox.y0 < 0）
                new_y = min(new_y, page_rect.y1 - height - font_size)
            if new_y <= y_candidate:
                return None
            y_candidate = new_y
        return None

    def _try_width_reduction(
        self,
        text_bbox: BoundingBox,
        colliding: List[BoundingBox],
        font_size: float,
    ) -> Optional[Tuple[float, float, float]]:
        """Try reducing text width to avoid side collisions."""
        # Find side obstacles and narrow the column
        left_obs = [
            o
            for o in colliding
            if o.x1 > text_bbox.x0 and o.x0 < text_bbox.x0 + text_bbox.width * 0.5
        ]
        right_obs = [
            o
            for o in colliding
            if o.x0 < text_bbox.x1 and o.x1 > text_bbox.x1 - text_bbox.width * 0.5
        ]

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
