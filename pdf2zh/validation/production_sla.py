"""ProductionSLA — 生产 SLA 指标。

定义和追踪生产环境性能目标。

用法：
    sla = ProductionSLA()
    report = sla.evaluate(metrics)
    print(report.summary())
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SLATarget:
    """SLA 目标。"""

    name: str = ""
    target_value: float = 0.0
    actual_value: float = 0.0
    unit: str = ""
    threshold_warning: float = 0.0
    threshold_critical: float = 0.0

    @property
    def is_met(self) -> bool:
        return self.actual_value <= self.target_value if self.target_value > 0 else True

    @property
    def status(self) -> str:
        if not self.is_met:
            return "FAIL"
        if self.actual_value > self.threshold_warning:
            return "WARN"
        return "OK"


@dataclass
class SLAReport:
    """SLA 报告。"""

    timestamp: float = 0.0
    targets: List[SLATarget] = field(default_factory=list)
    overall_status: str = "OK"
    pass_count: int = 0
    warn_count: int = 0
    fail_count: int = 0

    def summary(self) -> str:
        lines = [
            "Production SLA Report",
            "=" * 60,
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.timestamp))}",
            f"Overall Status: {self.overall_status}",
            f"Pass: {self.pass_count} | Warn: {self.warn_count} | Fail: {self.fail_count}",
            "",
            "SLA Targets:",
        ]

        for target in self.targets:
            status_icon = {"OK": "[OK]", "WARN": "[!!]", "FAIL": "[X]"}.get(
                target.status, "[?]"
            )
            lines.append(
                f"  {status_icon} {target.name:<30} "
                f"target={target.target_value:.1f}{target.unit} "
                f"actual={target.actual_value:.1f}{target.unit}"
            )

        return "\n".join(lines)


class ProductionSLA:
    """生产 SLA。"""

    def __init__(self) -> None:
        self._targets = self._default_targets()

    def _default_targets(self) -> List[SLATarget]:
        """默认 SLA 目标。"""
        return [
            SLATarget(
                name="Parse Time (per page)",
                target_value=50.0,
                unit="ms",
                threshold_warning=30.0,
                threshold_critical=45.0,
            ),
            SLATarget(
                name="Layout Time (per page)",
                target_value=100.0,
                unit="ms",
                threshold_warning=60.0,
                threshold_critical=80.0,
            ),
            SLATarget(
                name="Assembly Time (per page)",
                target_value=50.0,
                unit="ms",
                threshold_warning=30.0,
                threshold_critical=40.0,
            ),
            SLATarget(
                name="Total Time (730 pages)",
                target_value=300.0,
                unit="s",
                threshold_warning=180.0,
                threshold_critical=240.0,
            ),
            SLATarget(
                name="Memory Peak",
                target_value=4096.0,
                unit="MB",
                threshold_warning=2048.0,
                threshold_critical=3072.0,
            ),
            SLATarget(
                name="Fidelity Score",
                target_value=0.95,
                unit="",
                threshold_warning=0.90,
                threshold_critical=0.85,
            ),
            SLATarget(
                name="Cache Hit Ratio",
                target_value=0.70,
                unit="",
                threshold_warning=0.50,
                threshold_critical=0.30,
            ),
        ]

    def evaluate(self, metrics: Optional[Dict[str, float]] = None) -> SLAReport:
        """评估 SLA。"""
        report = SLAReport(timestamp=time.time())

        if metrics:
            for target in self._targets:
                if target.name in metrics:
                    target.actual_value = metrics[target.name]

        report.targets = self._targets

        # 统计
        for target in self._targets:
            if target.status == "OK":
                report.pass_count += 1
            elif target.status == "WARN":
                report.warn_count += 1
            else:
                report.fail_count += 1

        # 总体状态
        if report.fail_count > 0:
            report.overall_status = "FAIL"
        elif report.warn_count > 0:
            report.overall_status = "WARN"
        else:
            report.overall_status = "OK"

        return report

    def add_target(self, target: SLATarget) -> None:
        """添加目标。"""
        self._targets.append(target)

    def get_target(self, name: str) -> Optional[SLATarget]:
        """获取目标。"""
        for t in self._targets:
            if t.name == name:
                return t
        return None
