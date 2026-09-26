"""Experiment — 翻译实验平台。

Phase 11.3:
    - ExperimentManager: A/B 测试
    - Variant: 实验变体

用法：
    from pdf2zh.experiment import ExperimentManager, Variant

    manager = ExperimentManager()
    exp = manager.create_experiment(
        name="test",
        variants=[Variant(temperature=0.2), Variant(temperature=0.1)],
    )
"""

from pdf2zh.experiment.manager import (
    Experiment,
    ExperimentManager,
    ExperimentResults,
    Variant,
    VariantResult,
)

__all__ = [
    "ExperimentManager",
    "Experiment",
    "Variant",
    "VariantResult",
    "ExperimentResults",
]
