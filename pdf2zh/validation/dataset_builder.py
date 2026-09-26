"""DatasetBuilder — 数据集构建器。

从失败案例构建高质量标注数据集。

用法：
    builder = DatasetBuilder()
    dataset = builder.build_from_failures(failure_dir)
    print(dataset.summary())
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from pdf2zh.validation.annotation_schema import AnnotationSchema, HumanAnnotation
from pdf2zh.validation.failure_replay import FailureReplayer, FailureSnapshot


@dataclass
class DatasetEntry:
    """数据集条目。"""

    entry_id: str = ""
    source: str = ""
    translation: str = ""
    reference: Optional[str] = None
    failure_type: str = ""
    domain: str = "general"
    difficulty: str = "medium"
    annotation: Optional[HumanAnnotation] = None


@dataclass
class Dataset:
    """数据集。"""

    name: str = ""
    entries: List[DatasetEntry] = field(default_factory=list)
    created_at: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.entries)

    def summary(self) -> str:
        failure_types = {}
        for entry in self.entries:
            ft = entry.failure_type
            failure_types[ft] = failure_types.get(ft, 0) + 1

        lines = [
            f"Dataset: {self.name}",
            f"Size: {self.size} entries",
            "Failure types:",
        ]
        for ft, count in sorted(failure_types.items(), key=lambda x: -x[1]):
            lines.append(f"  {ft}: {count}")

        return "\n".join(lines)


class DatasetBuilder:
    """数据集构建器。"""

    def __init__(self, storage_dir: str = "datasets") -> None:
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._annotation_schema = AnnotationSchema()

    def build_from_failures(
        self,
        failure_dir: str = "failures",
        max_entries: int = 100,
    ) -> Dataset:
        """从失败案例构建数据集。"""
        replayer = FailureReplayer(failure_dir)
        entries = []

        # 获取所有失败
        failure_ids = replayer.list_failures()

        for failure_id in failure_ids[:max_entries]:
            snapshot = replayer.load_failure(failure_id)
            if snapshot:
                entry = DatasetEntry(
                    entry_id=failure_id,
                    source=snapshot.source_text,
                    translation=snapshot.translated_text,
                    failure_type=snapshot.failure_type,
                )
                entries.append(entry)

        dataset = Dataset(
            name="failure_dataset",
            entries=entries,
            created_at=time.time(),
        )

        return dataset

    def build_from_corpus(
        self,
        corpus_dir: str = "tests/golden_translation",
    ) -> Dataset:
        """从参考语料库构建数据集。"""
        from pdf2zh.validation.reference_corpus import ReferenceCorpus

        corpus = ReferenceCorpus(corpus_dir)
        pairs = corpus.get_pairs()
        entries = []

        for i, pair in enumerate(pairs):
            entry = DatasetEntry(
                entry_id=f"corpus_{i}",
                source=pair.source,
                translation=pair.reference,  # 参考翻译作为基准
                reference=pair.reference,
                domain=pair.domain,
            )
            entries.append(entry)

        dataset = Dataset(
            name="corpus_dataset",
            entries=entries,
            created_at=time.time(),
        )

        return dataset

    def add_annotations(
        self,
        dataset: Dataset,
        annotations: List[HumanAnnotation],
    ) -> Dataset:
        """添加标注。"""
        # 按 source 匹配
        ann_map = {ann.source: ann for ann in annotations}

        for entry in dataset.entries:
            if entry.source in ann_map:
                entry.annotation = ann_map[entry.source]

        return dataset

    def split_dataset(
        self,
        dataset: Dataset,
        train_ratio: float = 0.7,
        val_ratio: float = 0.15,
        test_ratio: float = 0.15,
    ) -> Dict[str, Dataset]:
        """分割数据集。"""
        import random

        entries = list(dataset.entries)
        random.shuffle(entries)

        n = len(entries)
        n_train = int(n * train_ratio)
        n_val = int(n * val_ratio)

        train = Dataset(
            name=f"{dataset.name}_train",
            entries=entries[:n_train],
            created_at=time.time(),
        )
        val = Dataset(
            name=f"{dataset.name}_val",
            entries=entries[n_train : n_train + n_val],
            created_at=time.time(),
        )
        test = Dataset(
            name=f"{dataset.name}_test",
            entries=entries[n_train + n_val :],
            created_at=time.time(),
        )

        return {"train": train, "val": val, "test": test}

    def export_dataset(
        self,
        dataset: Dataset,
        output_path: str,
    ) -> None:
        """导出数据集。"""
        data = {
            "name": dataset.name,
            "size": dataset.size,
            "created_at": dataset.created_at,
            "entries": [
                {
                    "entry_id": e.entry_id,
                    "source": e.source,
                    "translation": e.translation,
                    "reference": e.reference,
                    "failure_type": e.failure_type,
                    "domain": e.domain,
                }
                for e in dataset.entries
            ],
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
