"""ExperimentManager — 翻译实验平台。

支持 A/B 测试不同翻译策略。

用法：
    manager = ExperimentManager()
    exp = manager.create_experiment(
        name="temperature_test",
        variants=[
            Variant(model="gpt-4", temperature=0.2),
            Variant(model="gpt-4", temperature=0.1),
        ],
    )
    results = manager.run_experiment(exp, blocks)
    print(manager.compare(results))
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Variant:
    """实验变体。"""

    name: str = ""
    model: str = "default"
    temperature: float = 0.3
    policy: str = "normal"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VariantResult:
    """变体结果。"""

    variant: Variant = field(default_factory=Variant)
    scores: List[float] = field(default_factory=list)
    overflow_count: int = 0
    total_blocks: int = 0
    total_ms: float = 0.0
    api_calls: int = 0
    cost_usd: float = 0.0

    @property
    def avg_score(self) -> float:
        return sum(self.scores) / len(self.scores) if self.scores else 0.0

    @property
    def overflow_rate(self) -> float:
        return self.overflow_count / self.total_blocks if self.total_blocks > 0 else 0.0

    @property
    def throughput(self) -> float:
        return self.total_blocks / (self.total_ms / 1000) if self.total_ms > 0 else 0.0


@dataclass
class Experiment:
    """实验。"""

    name: str = ""
    description: str = ""
    variants: List[Variant] = field(default_factory=list)
    created_at: float = 0.0
    status: str = "created"  # created, running, completed


@dataclass
class ExperimentResults:
    """实验结果。"""

    experiment: Experiment = field(default_factory=Experiment)
    variant_results: List[VariantResult] = field(default_factory=list)
    best_variant: str = ""
    completed_at: float = 0.0


class ExperimentManager:
    """实验管理器。"""

    def __init__(self) -> None:
        self._experiments: Dict[str, Experiment] = {}

    def create_experiment(
        self,
        name: str,
        variants: List[Variant],
        description: str = "",
    ) -> Experiment:
        """创建实验。"""
        exp = Experiment(
            name=name,
            description=description,
            variants=variants,
            created_at=time.time(),
        )
        self._experiments[name] = exp
        return exp

    def run_experiment(
        self,
        experiment: Experiment,
        blocks: List[Any],
        translate_func: Callable,
        score_func: Callable,
    ) -> ExperimentResults:
        """运行实验。"""
        results = ExperimentResults(experiment=experiment)

        for variant in experiment.variants:
            variant_result = self._run_variant(
                variant, blocks, translate_func, score_func
            )
            results.variant_results.append(variant_result)

        # 找最佳变体
        if results.variant_results:
            best = max(results.variant_results, key=lambda r: r.avg_score)
            results.best_variant = best.variant.name

        results.completed_at = time.time()
        return results

    def _run_variant(
        self,
        variant: Variant,
        blocks: List[Any],
        translate_func: Callable,
        score_func: Callable,
    ) -> VariantResult:
        """运行单个变体。"""
        result = VariantResult(variant=variant)
        result.total_blocks = len(blocks)
        t0 = time.perf_counter()

        for block in blocks:
            text = getattr(block, "text", str(block))
            # 翻译
            translated = translate_func(text, variant=variant)
            # 评分
            score = score_func(text, translated)
            result.scores.append(score)

            # 检查 overflow
            if len(translated) > len(text) * 1.5:
                result.overflow_count += 1
            result.api_calls += 1

        result.total_ms = (time.perf_counter() - t0) * 1000
        return result

    def compare(self, results: ExperimentResults) -> str:
        """比较实验结果。"""
        lines = [
            f"Experiment: {results.experiment.name}",
            "=" * 60,
            "",
            f"{'Variant':<20} {'Score':>8} {'Overflow':>10} {'Speed':>10} {'Cost':>10}",
            "-" * 60,
        ]

        for vr in results.variant_results:
            lines.append(
                f"{vr.variant.name:<20} "
                f"{vr.avg_score:>8.2f} "
                f"{vr.overflow_rate:>9.1%} "
                f"{vr.throughput:>9.1f}/s "
                f"${vr.cost_usd:>9.2f}"
            )

        lines.append("")
        lines.append(f"Best: {results.best_variant}")

        return "\n".join(lines)

    def get_experiment(self, name: str) -> Optional[Experiment]:
        return self._experiments.get(name)

    def list_experiments(self) -> List[str]:
        return list(self._experiments.keys())
