"""RuleLayoutStage — 基于规则的 layout。

最大覆盖：简单页面直接用几何规则，不调用模型。

覆盖场景：
    - 单栏论文
    - 双栏论文
    - 纯文本页面
"""

from __future__ import annotations

from collections import Counter
from typing import List, Tuple

from pdf2zh.layout.result import LayoutBlock, LayoutResult
from pdf2zh.layout.stage import LayoutConfidence, LayoutStage
from pdf2zh.parser.page_snapshot import PageSnapshot, TextSpan


class RuleLayoutStage(LayoutStage):
    """规则 layout stage。

    逻辑：
        1. 检测列数（x 坐标分布）
        2. 按列分组 text spans
        3. 生成 blocks
    """

    @property
    def name(self) -> str:
        return "rule"

    def can_handle(self, snapshot: PageSnapshot) -> bool:
        """简单页面：文本占比 >90%，无图片。"""
        if snapshot.has_images:
            return False

        total_area = snapshot.width * snapshot.height
        if total_area <= 0:
            return False

        text_area = sum(
            (s.bbox[2] - s.bbox[0]) * (s.bbox[3] - s.bbox[1])
            for s in snapshot.text_spans
        )
        text_ratio = text_area / total_area

        return text_ratio >= 0.90

    def infer(self, snapshot: PageSnapshot) -> LayoutResult:
        """规则推理。"""
        import time

        t0 = time.perf_counter()

        # 检测列
        columns = self._detect_columns(snapshot)

        # 按列分组
        if len(columns) <= 1:
            blocks = self._single_column(snapshot)
        else:
            blocks = self._multi_column(snapshot, columns)

        inference_ms = (time.perf_counter() - t0) * 1000

        return LayoutResult(
            page_index=snapshot.page_index,
            blocks=blocks,
            model_version="rule_v1",
            inference_ms=inference_ms,
            source="rule",
        )

    def confidence(
        self, snapshot: PageSnapshot, result: LayoutResult
    ) -> LayoutConfidence:
        """规则路径置信度。"""
        # 几何分：文本覆盖度
        total_area = snapshot.width * snapshot.height
        text_area = sum(
            (s.bbox[2] - s.bbox[0]) * (s.bbox[3] - s.bbox[1])
            for s in snapshot.text_spans
        )
        geometry = min(1.0, text_area / total_area) if total_area > 0 else 0

        # 顺序分：y 坐标单调性
        order = self._check_reading_order(snapshot)

        # 块一致性
        block_consistency = 0.9 if len(result.blocks) > 0 else 0.5

        return LayoutConfidence(
            geometry_score=geometry,
            reading_order_score=order,
            block_consistency_score=block_consistency,
            semantic_score=0.8,
        )

    def _detect_columns(self, snapshot: PageSnapshot) -> List[Tuple[float, float]]:
        """检测列边界。"""
        if not snapshot.text_spans:
            return [(0, snapshot.width)]

        # 收集 x 坐标
        x_starts = [s.bbox[0] for s in snapshot.text_spans]
        x_ends = [s.bbox[2] for s in snapshot.text_spans]

        if not x_starts:
            return [(0, snapshot.width)]

        # 简单直方图
        min_x = min(x_starts)
        max_x = max(x_ends)
        page_width = snapshot.width

        # 检查是否有明显的列间隙
        mid = page_width / 2
        left_spans = [s for s in snapshot.text_spans if s.bbox[2] < mid]
        right_spans = [s for s in snapshot.text_spans if s.bbox[0] > mid]
        gap_spans = [s for s in snapshot.text_spans if s.bbox[0] <= mid <= s.bbox[2]]

        # 如果左右都有文本且中间间隙足够大
        if left_spans and right_spans and len(gap_spans) < len(left_spans) * 0.1:
            left_max = max(s.bbox[2] for s in left_spans)
            right_min = min(s.bbox[0] for s in right_spans)
            gap = right_min - left_max
            if gap > page_width * 0.03:
                return [(min_x, left_max), (right_min, max_x)]

        return [(min_x, max_x)]

    def _single_column(self, snapshot: PageSnapshot) -> List[LayoutBlock]:
        """单栏：所有文本作为一个 block。"""
        if not snapshot.text_spans:
            return []

        # 按 y 坐标排序
        sorted_spans = sorted(snapshot.text_spans, key=lambda s: (s.bbox[1], s.bbox[0]))

        # 合并连续行为一个 block
        blocks = []
        current_lines = []
        last_y = None

        for span in sorted_spans:
            y = span.bbox[1]
            if last_y is not None and abs(y - last_y) > span.height * 2:
                if current_lines:
                    blocks.append(self._merge_to_block(current_lines, len(blocks)))
                current_lines = []
            current_lines.append(span)
            last_y = y

        if current_lines:
            blocks.append(self._merge_to_block(current_lines, len(blocks)))

        return blocks

    def _multi_column(
        self,
        snapshot: PageSnapshot,
        columns: List[Tuple[float, float]],
    ) -> List[LayoutBlock]:
        """多栏：按列分组。"""
        blocks = []

        for col_idx, (col_x0, col_x1) in enumerate(columns):
            col_spans = [
                s
                for s in snapshot.text_spans
                if s.bbox[0] >= col_x0 - 5 and s.bbox[2] <= col_x1 + 5
            ]

            if not col_spans:
                continue

            # 按 y 排序
            sorted_spans = sorted(col_spans, key=lambda s: (s.bbox[1], s.bbox[0]))

            # 生成 block
            current_lines = []
            last_y = None

            for span in sorted_spans:
                y = span.bbox[1]
                if last_y is not None and abs(y - last_y) > span.height * 2:
                    if current_lines:
                        blocks.append(
                            self._merge_to_block(current_lines, len(blocks), col_idx)
                        )
                    current_lines = []
                current_lines.append(span)
                last_y = y

            if current_lines:
                blocks.append(self._merge_to_block(current_lines, len(blocks), col_idx))

        return blocks

    def _merge_to_block(
        self,
        spans: List[TextSpan],
        order: int,
        column: int = 0,
    ) -> LayoutBlock:
        """合并多个 span 为一个 block。"""
        if not spans:
            return LayoutBlock()

        x0 = min(s.bbox[0] for s in spans)
        y0 = min(s.bbox[1] for s in spans)
        x1 = max(s.bbox[2] for s in spans)
        y1 = max(s.bbox[3] for s in spans)
        text = " ".join(s.text for s in spans if s.text.strip())

        # 检测 block 类型
        block_type = "text"
        if any(s.is_bold for s in spans) and len(text) < 100:
            block_type = "title"

        return LayoutBlock(
            block_type=block_type,
            bbox=(x0, y0, x1, y1),
            confidence=0.9,
            text=text[:200],
            column=column,
            order=order,
        )

    def _check_reading_order(self, snapshot: PageSnapshot) -> float:
        """检查阅读顺序（y 坐标单调性）。"""
        if len(snapshot.text_spans) < 2:
            return 1.0

        sorted_spans = sorted(snapshot.text_spans, key=lambda s: (s.bbox[1], s.bbox[0]))

        # 检查 y 坐标是否大致单调递增
        monotonic_count = 0
        for i in range(1, len(sorted_spans)):
            if sorted_spans[i].bbox[1] >= sorted_spans[i - 1].bbox[1]:
                monotonic_count += 1

        return monotonic_count / (len(sorted_spans) - 1)
