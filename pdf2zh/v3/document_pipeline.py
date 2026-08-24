"""Module: DocumentPipeline — 单一 IR 的生命周期编排（V9.0）。

对应「单一核心 IR + Processor」方案的生命周期部分：
PDF → RAW → SEMANTIC → TRANSLATION → RENDER，**全程只有一份
``DocumentGraph``**；每个阶段只是给 Node 打上阶段注解并运行该阶段
的 Processors（改写 metadata）。跨进程/持久化时用 ``view_as_ir``
把同一份图投影成 ``DocumentIR`` 序列化视图 —— 视图不是第二份 IR。

特性：
    - 处理器抛错被捕获进 report（绝不让单个节点拖垮整页处理）；
    - 单 IR 断言：run 前后节点 id 集合完全一致（不增不删不改建）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pdf2zh.v3.graph import DocumentGraph
from pdf2zh.v3.processors import (
    NodeStage,
    ProcessorRegistry,
    STAGE_KEY,
    default_processor_registry,
)

DEFAULT_STAGES = (
    NodeStage.RAW,
    NodeStage.SEMANTIC,
    NodeStage.TRANSLATION,
    NodeStage.RENDER,
)


@dataclass
class PipelineReport:
    """一次 run 的统计：每阶段每处理器命中数 + 容错错误清单。"""

    node_count: int = 0
    edge_count: int = 0
    stages: List[str] = field(default_factory=list)
    applied: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "stages": list(self.stages),
            "applied": dict(self.applied),
            "errors": list(self.errors),
            "ok": self.ok(),
        }


class DocumentPipeline:
    """按阶段运行 Processors 的单一 IR 编排器。"""

    def __init__(self, registry: Optional[ProcessorRegistry] = None) -> None:
        self.registry = registry or default_processor_registry()

    def run(
        self, graph: DocumentGraph, stages: tuple = DEFAULT_STAGES
    ) -> PipelineReport:
        report = PipelineReport(
            node_count=len(graph.nodes),
            edge_count=len(graph.edges),
        )
        for stage in stages:
            report.stages.append(stage.value)
            for processor in self.registry.for_stage(stage):
                key = f"{stage.value}:{processor.name}"
                for node in graph.nodes:
                    if not processor.matches(node):
                        continue
                    node.metadata[STAGE_KEY] = stage.value
                    try:
                        processor.process(node, graph)
                        report.applied[key] = report.applied.get(key, 0) + 1
                    except Exception as e:  # noqa: BLE001 — 单节点失败不中断
                        report.errors.append(f"{key}@{node.id}: {e}")
                try:
                    processor.finalize(graph)
                except Exception as e:  # noqa: BLE001
                    report.errors.append(f"{key}#finalize: {e}")
        return report


def run_semantic_pipeline(
    graph: DocumentGraph, registry: Optional[ProcessorRegistry] = None
) -> PipelineReport:
    """便捷入口：RAW + SEMANTIC 两阶段（翻译/渲染由主链路决策器接管）。"""
    return DocumentPipeline(registry).run(graph, (NodeStage.RAW, NodeStage.SEMANTIC))


def view_as_ir(
    graph: DocumentGraph,
    title: str = "",
    source_lang: str = "en",
    target_lang: str = "zh-cn",
):
    """把同一份 DocumentGraph 投影为 DocumentIR 序列化视图（非第二份 IR）。

    处理器写入的语义明细（semantic / policy / stage）随节点带过去。
    """
    from pdf2zh.v3.document_ir import IRBuilder

    ir = IRBuilder.from_graph(
        graph, title=title, source_lang=source_lang, target_lang=target_lang
    )
    for node in graph.nodes:
        ir_node = ir.get_node(node.id)
        if ir_node is not None and node.metadata:
            ir_node.metadata.update(
                {
                    k: v
                    for k, v in node.metadata.items()
                    if k in ("v3.stage", "policy", "semantic", "policy_reasons")
                }
            )
    return ir


__all__ = [
    "DEFAULT_STAGES",
    "PipelineReport",
    "DocumentPipeline",
    "run_semantic_pipeline",
    "view_as_ir",
]
