"""Module: ImageCalibrate — V8.6 图片分类器真实语料调参（P2）。

规则分类器的阈值（``RuleClassifierConfig``）目前是启发式基线；真实语料
（Photo / Diagram / Chart 边界）需要在**带标签样本**上标定。本模块提供
确定性网格搜索标定：

    CalibrationSample(features_dict, label)
        └─ score(classifier)── 对每个候选配置计算分类准确率
        └─ 返回 CalibrationReport（baseline / best / grid 规模）

标定只改 ``RuleClassifierConfig``（保持纯规则，不引入模型依赖）；
对真实语料运行时把最优 config 喂给 ``RuleImageClassifier(config=best)``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from pdf2zh.v3.image_engine import (
    ImageClass, ImageFeatures, RuleClassifierConfig, RuleImageClassifier,
    classify_image,
)


@dataclass
class CalibrationSample:
    """一条带标签的标定样本（features 为 dict，可 JSON 序列化）。"""

    features: dict
    label: ImageClass


@dataclass
class CalibrationReport:
    """标定结果：基线准确率 / 最优配置 / 每轮改善。"""

    baseline_accuracy: float = 0.0
    best_accuracy: float = 0.0
    best_config: Dict[str, float] = field(default_factory=dict)
    grid_size: int = 0
    per_config: List[Dict[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "baseline_accuracy": round(self.baseline_accuracy, 4),
            "best_accuracy": round(self.best_accuracy, 4),
            "best_config": dict(self.best_config),
            "grid_size": self.grid_size,
            "per_config": list(self.per_config),
        }

    def summary(self) -> str:
        delta = self.best_accuracy - self.baseline_accuracy
        return (f"ImageCalibrate baseline={self.baseline_accuracy:.3f} "
                f"best={self.best_accuracy:.3f} ({delta:+.3f}) grid={self.grid_size}")


def accuracy(classifier, samples: Sequence[CalibrationSample]) -> float:
    """分类器在标定集上的准确率（0~1）。"""
    if not samples:
        return 0.0
    correct = 0
    for s in samples:
        features = _features_from_dict(s.features)
        predicted, _ = classify_image(features, backend=classifier)
        if predicted == s.label:
            correct += 1
    return correct / len(samples)


def _features_from_dict(features: dict) -> ImageFeatures:
    payload = {
        k: v for k, v in (features or {}).items()
        if k in ImageFeatures.__dataclass_fields__
    }
    payload.setdefault("width", 400)
    payload.setdefault("height", 300)
    return ImageFeatures(**payload)


_DEFAULT_GRID = {
    "photo_max_edge": (0.10, 0.40, 7),    # (floor, ceil, steps)
    "chart_min_edge": (0.10, 0.30, 5),
    "photo_min_colors": (128, 256, 3),
}


def calibrate(samples: Sequence[CalibrationSample],
              classifier: Optional[RuleImageClassifier] = None,
              grid: Optional[Dict[str, Tuple[float, float, int]]] = None,
              base_config: Optional[RuleClassifierConfig] = None) -> CalibrationReport:
    """网格搜索最优阈值配置。

    grid 形如 {config_field: (floor, ceil, n_steps)}；默认搜三个对
    Photo/Chart 边界最敏感的阈值（真实语料上这三个旋钮最常见）。
    """
    classifier = classifier or RuleImageClassifier(
        config=base_config or RuleClassifierConfig())
    base = classifier.config.tuned()
    report = CalibrationReport(
        baseline_accuracy=accuracy(classifier, samples),
        best_config=dict(base),
        best_accuracy=accuracy(classifier, samples),
    )
    grid = grid or _DEFAULT_GRID
    valid_fields = set(RuleClassifierConfig.__dataclass_fields__)
    grid = {k: v for k, v in grid.items() if k in valid_fields}
    fields = sorted(grid.keys())
    candidate_values: List[List[Tuple[str, float]]] = []
    for fname, (floor, ceil, n) in grid.items():
        if n <= 1:
            candidate_values.append([(fname, base.get(fname, floor))])
            continue
        step = (ceil - floor) / (n - 1)
        candidate_values.append([(fname, floor + i * step) for i in range(n)])
    report.grid_size = 1
    for c in candidate_values:
        report.grid_size *= len(c)

    def _iter_configs():
        if not fields:
            yield dict(base)
            return
        indices = [0] * len(fields)
        while True:
            config = dict(base)
            for k, (fname, _) in enumerate(zip(fields, candidate_values)):
                config[fname] = candidate_values[k][indices[k]][1]
            yield config
            k = 0
            while k < len(indices):
                indices[k] += 1
                if indices[k] < len(candidate_values[k]):
                    break
                indices[k] = 0
                k += 1
            if k == len(indices):
                break

    best_acc = report.best_accuracy
    best_config = dict(base)
    for config in _iter_configs():
        tuned = RuleClassifierConfig(**{**base, **config})
        acc = accuracy(RuleImageClassifier(config=tuned), samples)
        report.per_config.append({"accuracy": round(acc, 4), "config": config})
        if acc > best_acc:
            best_acc = acc
            best_config = dict(config)
    report.best_accuracy = best_acc
    report.best_config = {k: round(v, 4) if isinstance(v, float) else v
                          for k, v in best_config.items()}
    return report


def load_samples_from_dir(samples_dir: str) -> List[CalibrationSample]:
    """从目录加载带标签标定样本（真实语料接入点）。

    每个 ``*.json`` 形如 ``{"features": {...}, "label": "photo"}``；
    目录为空或全部失败时返回空列表（调用方自行兜底）。
    """
    import glob
    import json
    import os
    samples: List[CalibrationSample] = []
    for path in sorted(glob.glob(os.path.join(samples_dir, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            label = str(payload.get("label", "")).lower().strip()
            image_class = ImageClass(label) if label else ImageClass.UNKNOWN
            samples.append(CalibrationSample(
                features=dict(payload.get("features", {})),
                label=image_class,
            ))
        except Exception:  # noqa: BLE001 — 单样本损坏跳过
            continue
    return samples


def calibrate_corpus_dir(samples_dir: str, out_json: str = "",
                         classifier: Optional[RuleImageClassifier] = None,
                         grid: Optional[Dict[str, Tuple[float, float, int]]] = None,
                         base_config: Optional[RuleClassifierConfig] = None) -> Optional[CalibrationReport]:
    """真实语料标定入口：目录样本 → 网格标定 → 报告落盘。

    ``out_json`` 缺省时不落盘。落盘内容含 baseline/best config，可直接
    喂给 ``RuleImageClassifier(config=CalibrationReport.best_config)``。
    样本缺失/全损坏时返回 None（side-channel 纪律）。
    """
    import json
    samples = load_samples_from_dir(samples_dir)
    if not samples:
        return None
    report = calibrate(samples, classifier=classifier, grid=grid,
                       base_config=base_config)
    if out_json:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    return report


__all__ = [
    "CalibrationSample", "CalibrationReport",
    "accuracy", "calibrate",
    "load_samples_from_dir", "calibrate_corpus_dir",
    "RuleClassifierConfig",
]