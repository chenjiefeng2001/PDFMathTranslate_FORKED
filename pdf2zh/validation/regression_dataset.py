"""RegressionDataset — 回归测试数据集。

保存历史 bug，防止回归。

用法：
    regression = RegressionDataset()
    regression.add_bug(
        bug_id="bug_001",
        description="Number changed in translation",
        source="The error rate is 5.2%",
        expected="错误率为 5.2%",
        failure_type="number_changed",
    )
    regression.save("regression/")
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class RegressionBug:
    """回归 bug。"""

    bug_id: str = ""
    description: str = ""
    source: str = ""
    expected: str = ""
    actual: str = ""
    failure_type: str = ""
    domain: str = "general"
    created_at: float = 0.0
    fixed_at: Optional[float] = None
    regression_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_fixed(self) -> bool:
        return self.fixed_at is not None


class RegressionDataset:
    """回归测试数据集。"""

    def __init__(self, storage_dir: str = "regression") -> None:
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._bugs: Dict[str, RegressionBug] = {}
        self._load_bugs()

    def _load_bugs(self) -> None:
        """加载 bugs。"""
        for bug_file in self._storage_dir.glob("*.json"):
            try:
                with open(bug_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                bug = RegressionBug(**data)
                self._bugs[bug.bug_id] = bug
            except Exception:
                pass

    def add_bug(
        self,
        bug_id: str,
        description: str,
        source: str,
        expected: str,
        failure_type: str,
        actual: str = "",
        domain: str = "general",
    ) -> RegressionBug:
        """添加 bug。"""
        bug = RegressionBug(
            bug_id=bug_id,
            description=description,
            source=source,
            expected=expected,
            actual=actual,
            failure_type=failure_type,
            domain=domain,
            created_at=time.time(),
        )
        self._bugs[bug_id] = bug
        self._save_bug(bug)
        return bug

    def _save_bug(self, bug: RegressionBug) -> None:
        """保存 bug。"""
        file_path = self._storage_dir / f"{bug.bug_id}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "bug_id": bug.bug_id,
                    "description": bug.description,
                    "source": bug.source,
                    "expected": bug.expected,
                    "actual": bug.actual,
                    "failure_type": bug.failure_type,
                    "domain": bug.domain,
                    "created_at": bug.created_at,
                    "fixed_at": bug.fixed_at,
                    "regression_count": bug.regression_count,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )

    def mark_fixed(self, bug_id: str) -> None:
        """标记 bug 已修复。"""
        if bug_id in self._bugs:
            self._bugs[bug_id].fixed_at = time.time()
            self._save_bug(self._bugs[bug_id])

    def check_regression(
        self,
        bug_id: str,
        actual_translation: str,
    ) -> bool:
        """检查是否回归。"""
        if bug_id not in self._bugs:
            return False

        bug = self._bugs[bug_id]
        if not bug.is_fixed:
            return False

        # 检查是否再次失败
        if actual_translation != bug.expected:
            bug.regression_count += 1
            bug.fixed_at = None  # 标记为未修复
            self._save_bug(bug)
            return True

        return False

    def get_bug(self, bug_id: str) -> Optional[RegressionBug]:
        """获取 bug。"""
        return self._bugs.get(bug_id)

    def list_bugs(
        self,
        failure_type: Optional[str] = None,
        fixed_only: bool = False,
    ) -> List[RegressionBug]:
        """列出 bugs。"""
        bugs = list(self._bugs.values())
        if failure_type:
            bugs = [b for b in bugs if b.failure_type == failure_type]
        if fixed_only:
            bugs = [b for b in bugs if b.is_fixed]
        return bugs

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计。"""
        bugs = list(self._bugs.values())
        total = len(bugs)
        fixed = sum(1 for b in bugs if b.is_fixed)
        regressed = sum(1 for b in bugs if b.regression_count > 0)

        type_counts = {}
        for b in bugs:
            ft = b.failure_type
            type_counts[ft] = type_counts.get(ft, 0) + 1

        return {
            "total": total,
            "fixed": fixed,
            "regressed": regressed,
            "by_type": type_counts,
        }

    @property
    def size(self) -> int:
        return len(self._bugs)
