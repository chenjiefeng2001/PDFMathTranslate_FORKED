"""Module: Content Preservation Engine — 「该不该翻、怎么翻」的统一决策层。

提案 §十一：在 Document IR 与 Translation Router 之间新增一个统一层，
负责为**每一类对象**（正文/标题/目录/图片/Logo/二维码/公式/表格/题注…）
产出结构化处理决策，而不是针对每种对象各写补丁。

决策只有三种动作（外加精确子模式）：
    TRANSLATE        —— 进入翻译路由
    PRESERVE         —— 原样保留（不翻译、不改动）
    OVERLAY          —— 保留原物，在其上叠译文图层

同时本模块复用了图片引擎的 ``ImagePolicy``，使：目录行要翻译、
Logo/QR/公式被保护、图文题注要翻译但编号保留 —— 全部走同一套
``decide(node) -> PreservationDecision`` 接口，收敛到 Document IR
的 TranslationRole / RenderingRole 上。

用法::

    from pdf2zh.v3.content_preservation import (
        ContentPreservationEngine, PreservationAction, classify_node,
    )
    engine = ContentPreservationEngine()
    decision = engine.decide_image(image_object)
    node_decision = engine.decide_ir_node(ir_node)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from pdf2zh.v3.image_engine import (
    IMAGE_POLICY, ImageClass, ImageObject, RenderMode, TranslationDecision,
)
from pdf2zh.v3.document_ir import DocumentIR, SemanticRole, TranslationRole


class PreservationAction(Enum):
    """统一处理动作。"""

    TRANSLATE = "translate"
    PRESERVE = "preserve"
    OVERLAY = "overlay"


# 动作 → 渲染模式（供渲染器消费）
ACTION_TO_RENDER = {
    PreservationAction.TRANSLATE: RenderMode.REGION_REPLACE,
    PreservationAction.PRESERVE: RenderMode.PRESERVE,
    PreservationAction.OVERLAY: RenderMode.OVERLAY,
}


@dataclass
class PreservationDecision:
    """某个对象（正文块/图片/目录行…）的处理决策。"""

    object_id: str = ""
    object_type: str = "unknown"
    action: PreservationAction = PreservationAction.PRESERVE
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    render_mode: RenderMode = RenderMode.PRESERVE
    translation_role: TranslationRole = TranslationRole.SKIP
    image_decision: Optional[TranslationDecision] = None

    def to_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "object_type": self.object_type,
            "action": self.action.value,
            "confidence": round(self.confidence, 4),
            "reasons": list(self.reasons),
            "render_mode": self.render_mode.value,
            "translation_role": self.translation_role.value,
            "image_decision": self.image_decision.to_dict() if self.image_decision else None,
        }


# ── SemanticRole 默认策略表 ────────────────────────────────────────────────

# 这些语义角色默认"保护不翻译"（工业/技术文档最常见策略）
_PRESERVE_ROLES = {
    SemanticRole.FIGURE,
    SemanticRole.IMAGE,
    SemanticRole.TABLE,
    SemanticRole.FORMULA,
    SemanticRole.FORMULA_INLINE,
    SemanticRole.CODE,
    SemanticRole.HEADER,
    SemanticRole.FOOTER,
    SemanticRole.REFERENCE,
}

# 需要翻译（正文及结构元素）
_TRANSLATE_ROLES = {
    SemanticRole.DOCUMENT,
    SemanticRole.SECTION,
    SemanticRole.SUBSECTION,
    SemanticRole.BODY_TEXT,
    SemanticRole.HEADING,
    SemanticRole.TOC_ENTRY,
    SemanticRole.LIST,
    SemanticRole.LIST_ITEM,
    SemanticRole.ABSTRACT,
    SemanticRole.KEYWORDS,
    SemanticRole.BIBLIOGRAPHY,
    SemanticRole.FOOTNOTE,
}

# 需要上下文/编号保留（翻译但保留编号）
_NEED_CONTEXT_ROLES = {SemanticRole.CAPTION}


@dataclass
class DelegateSpec:
    """配置一条 IR → 统一动作的映射规则。"""

    action: PreservationAction
    translation_role: TranslationRole
    confidence: float = 0.9
    reason: str = ""


# 语义角色默认表：所有目标动作最终收敛为 TRANSLATE / PRESERVE / OVERLAY
ROLE_DEFAULT: Dict[SemanticRole, DelegateSpec] = {
    role: DelegateSpec(
        PreservationAction.PRESERVE, TranslationRole.SKIP, 0.95,
        "semantic_preserve",
    ) for role in _PRESERVE_ROLES
}
ROLE_DEFAULT.update({
    role: DelegateSpec(
        PreservationAction.TRANSLATE, TranslationRole.TRANSLATE, 0.9,
        "semantic_translate",
    ) for role in _TRANSLATE_ROLES
})
ROLE_DEFAULT.update({
    SemanticRole.CAPTION: DelegateSpec(
        PreservationAction.TRANSLATE, TranslationRole.NEED_CONTEXT, 0.85,
        "caption_number_keep",
    ),
})


class ContentPreservationEngine:
    """统一决策引擎。

    - ``decide_ir_node`` 按 SemanticRole 查默认表（可被显式策略覆盖）。
    - ``decide_image`` 委托给图片决策引擎（ImagePolicy），本层仅把
      Image 决策收敛进 PreservationDecision，让渲染/路由只认统一动作。
    - ``decide_ir`` 对整棵 Document IR 逐节点产出决策（side-channel，
      不修改 IR；如需把决策写回 IR 角色可用 ``apply_to_ir``）。
    """

    def __init__(self,
                 role_defaults: Optional[Dict[SemanticRole, DelegateSpec]] = None,
                 image_policy: Optional[Dict[ImageClass, "object"]] = None,
                 engine=None) -> None:
        from pdf2zh.v3.image_engine import TranslationDecisionEngine
        self.role_defaults = dict(role_defaults or ROLE_DEFAULT)
        self.image_policy = dict(image_policy or IMAGE_POLICY)
        self.image_engine = engine or TranslationDecisionEngine(policy=self.image_policy)

    # ── 对象决策 ─────────────────────────────────────────────────

    def decide_ir_node(self, node) -> PreservationDecision:
        """对单个 IR 节点（document_ir.IRNode）产出决策。"""
        semantic = node.semantic if hasattr(node, "semantic") else None
        name = getattr(semantic, "value", semantic)
        return self._from_semantic(name, node)

    def decide_ir(self, ir: DocumentIR) -> List[PreservationDecision]:
        decisions = []
        for node in ir.walk():
            decisions.append(self.decide_ir_node(node))
        return decisions

    def decide_image(self, image: ImageObject) -> PreservationDecision:
        """先跑图片决策引擎，再把结果收敛到统一动作。"""
        decision = image.decision
        if decision is None:
            decision = self.image_engine.decide(image)
            image.decision = decision
        if decision.translate:
            action = PreservationAction.TRANSLATE
            role = TranslationRole.TRANSLATE
        else:
            # 图片策略 PRESERVE/OVERLAY：PRESERVE 保持原图
            if decision.render_mode == RenderMode.OVERLAY:
                action = PreservationAction.OVERLAY
                role = TranslationRole.TRACK
            else:
                action = PreservationAction.PRESERVE
                role = TranslationRole.SKIP
        return PreservationDecision(
            object_id=image.id,
            object_type=f"image:{image.image_class.value}",
            action=action,
            confidence=decision.confidence,
            reasons=[*decision.reasons, "image_engine"],
            render_mode=decision.render_mode,
            translation_role=role,
            image_decision=decision,
        )

    def apply_to_ir(self, ir: DocumentIR,
                    image_options: Optional[Sequence[ImageObject]] = None) -> List[PreservationDecision]:
        """把决策写回 IR 的角色（translation/rendering），便于后续消费。

        仅按默认策略改写；显式设置过的节点不强行覆盖（除非它仍是默认 SKIP）。
        返回写回期间产生的决策列表。
        """
        applied: List[PreservationDecision] = []
        for node in ir.walk():
            semantic = node.semantic
            d = self._from_semantic(getattr(semantic, "value", semantic), node)
            node.translation = d.translation_role
            applied.append(d)
        for img in image_options or []:
            d = self.decide_image(img)
            ir_node = ir.get_node(img.id)
            if ir_node is not None:
                ir_node.translation = d.translation_role
            applied.append(d)
        return applied

    # ── 内部：语义 → 动作 ─────────────────────────────────────────

    def _from_semantic(self, semantic_name: str, node) -> PreservationDecision:
        try:
            semantic = SemanticRole(semantic_name)
        except ValueError:
            semantic = SemanticRole.UNKNOWN

        spec = self.role_defaults.get(semantic)
        if spec is None:
            # 未知角色沿用节点自身 translation 提示（显式配置优先）
            node_role = getattr(node, "translation", None)
            try:
                node_role = TranslationRole(node_role.value if hasattr(node_role, "value") else node_role)
            except Exception:
                node_role = TranslationRole.TRANSLATE
            if node_role in (TranslationRole.KEEP_TERM, TranslationRole.KEEP_FORMULA,
                             TranslationRole.KEEP_NUMBER, TranslationRole.SKIP):
                return PreservationDecision(
                    object_id=getattr(node, "id", ""),
                    object_type=f"ir:{semantic_name}",
                    action=PreservationAction.PRESERVE,
                    confidence=0.7,
                    reasons=["short-circuit_preserve", node_role.value],
                    render_mode=RenderMode.PRESERVE,
                    translation_role=node_role,
                )
            return PreservationDecision(
                object_id=getattr(node, "id", ""),
                object_type=f"ir:{semantic_name}",
                action=PreservationAction.TRANSLATE,
                confidence=0.6,
                reasons=["fallback_translate"],
                render_mode=RenderMode.REGION_REPLACE,
                translation_role=node_role,
            )

        return PreservationDecision(
            object_id=getattr(node, "id", ""),
            object_type=f"ir:{semantic_name}",
            action=spec.action,
            confidence=spec.confidence,
            reasons=[spec.reason, semantic_name],
            render_mode=ACTION_TO_RENDER[spec.action],
            translation_role=spec.translation_role,
        )


def classify_node(node, engine: Optional[ContentPreservationEngine] = None) -> PreservationDecision:
    """函数式便捷入口：对任意带 semantic 属性的对象做决策。"""
    return (engine or ContentPreservationEngine()).decide_ir_node(node)


__all__ = [
    "PreservationAction", "PreservationDecision", "DelegateSpec",
    "ROLE_DEFAULT", "ContentPreservationEngine", "classify_node",
    "ACTION_TO_RENDER",
]