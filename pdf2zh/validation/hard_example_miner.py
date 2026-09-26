"""HardExampleMiner — 困难样本挖掘。

从失败案例中挖掘高价值样本。

用法：
    miner = HardExampleMiner()
    hard_examples = miner.mine(failure_dir, max_examples=100)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pdf2zh.validation.failure_replay import FailureReplayer, FailureSnapshot


@dataclass
class HardExample:
    """困难样本。"""

    example_id: str = ""
    source: str = ""
    translation: str = ""
    failure_type: str = ""
    severity: str = "medium"
    difficulty_score: float = 0.0
    priority: int = 0  # 越高越优先
    metadata: Dict[str, Any] = field(default_factory=dict)


# 优先级映射
FAILURE_PRIORITY = {
    "number_changed": 100,
    "formula_changed": 95,
    "entity_changed": 90,
    "meaning_loss": 85,
    "wrong_term": 80,
    "hallucination": 75,
    "omission": 70,
    "reference_break": 65,
    "translation_overflow": 60,
    "font_missing": 50,
    "style_issue": 30,
}


class HardExampleMiner:
    """困难样本挖掘器。"""

    def __init__(self) -> None:
        self._replayer: Optional[FailureReplayer] = None

    def mine(
        self,
        failure_dir: str = "failures",
        max_examples: int = 100,
        min_priority: int = 50,
    ) -> List[HardExample]:
        """挖掘困难样本。"""
        self._replayer = FailureReplayer(failure_dir)
        examples = []

        # 获取所有失败
        failure_ids = self._replayer.list_failures()

        for failure_id in failure_ids:
            snapshot = self._replayer.load_failure(failure_id)
            if snapshot:
                example = self._snapshot_to_example(snapshot)
                if example.priority >= min_priority:
                    examples.append(example)

        # 按优先级排序
        examples.sort(key=lambda x: -x.priority)

        return examples[:max_examples]

    def _snapshot_to_example(self, snapshot: FailureSnapshot) -> HardExample:
        """将快照转为困难样本。"""
        priority = FAILURE_PRIORITY.get(snapshot.failure_type, 50)

        # 计算难度分数
        difficulty = self._compute_difficulty(snapshot)

        return HardExample(
            example_id=snapshot.failure_id,
            source=snapshot.source_text,
            translation=snapshot.translated_text,
            failure_type=snapshot.failure_type,
            severity=snapshot.severity,
            difficulty_score=difficulty,
            priority=priority,
            metadata={
                "pdf_path": snapshot.pdf_path,
                "page_index": snapshot.page_index,
            },
        )

    def _compute_difficulty(self, snapshot: FailureSnapshot) -> float:
        """计算难度分数。"""
        score = 0.0

        # 基于失败类型
        priority = FAILURE_PRIORITY.get(snapshot.failure_type, 50)
        score += priority / 100 * 0.5

        # 基于文本长度
        if len(snapshot.source_text) > 200:
            score += 0.2
        elif len(snapshot.source_text) > 100:
            score += 0.1

        # 基于严重程度
        severity_score = {"critical": 0.3, "high": 0.2, "medium": 0.1, "low": 0.0}
        score += severity_score.get(snapshot.severity, 0.1)

        return min(1.0, score)

    def mine_from_dataset(
        self,
        entries: List[Dict[str, Any]],
        max_examples: int = 100,
    ) -> List[HardExample]:
        """从数据集挖掘困难样本。"""
        examples = []

        for entry in entries:
            errors = entry.get("errors", [])
            if not errors:
                continue

            # 找最高优先级错误
            max_priority = 0
            worst_error = None
            for error in errors:
                error_type = error.get("error_type", "unknown")
                priority = FAILURE_PRIORITY.get(error_type, 50)
                if priority > max_priority:
                    max_priority = priority
                    worst_error = error

            if worst_error and max_priority >= 50:
                example = HardExample(
                    example_id=entry.get("id", ""),
                    source=entry.get("source", ""),
                    translation=entry.get("translation", ""),
                    failure_type=worst_error.get("error_type", ""),
                    severity=worst_error.get("severity", "medium"),
                    priority=max_priority,
                )
                examples.append(example)

        examples.sort(key=lambda x: -x.priority)
        return examples[:max_examples]

    def get_statistics(self, examples: List[HardExample]) -> Dict[str, Any]:
        """获取统计。"""
        if not examples:
            return {"total": 0}

        type_counts = {}
        for ex in examples:
            ft = ex.failure_type
            type_counts[ft] = type_counts.get(ft, 0) + 1

        return {
            "total": len(examples),
            "by_type": type_counts,
            "avg_priority": sum(e.priority for e in examples) / len(examples),
        }
