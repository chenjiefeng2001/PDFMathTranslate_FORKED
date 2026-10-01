"""TranslationRouter — 翻译路由。

根据 block 类型选择不同 translator。

用法：
    router = TranslationRouter()
    target = router.route(block)
    # TranslatorTarget(model="gpt-4", reason="technical_formula")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TranslatorTarget:
    """翻译目标。"""

    model: str = "default"
    temperature: float = 0.3
    max_tokens: int = 2000
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


# 路由规则
ROUTING_RULES: Dict[str, Dict] = {
    "paragraph": {"model": "fast", "temperature": 0.3},
    "title": {"model": "fast", "temperature": 0.2, "max_tokens": 100},
    "heading": {"model": "fast", "temperature": 0.2, "max_tokens": 100},
    "toc": {"model": "constrained", "temperature": 0.1, "max_tokens": 200},
    "formula": {"model": "skip", "temperature": 0.0},
    "code": {"model": "code", "temperature": 0.1, "max_tokens": 500},
    "caption": {"model": "fast", "temperature": 0.3, "max_tokens": 100},
    "footnote": {"model": "fast", "temperature": 0.2, "max_tokens": 200},
    "table": {"model": "layout_aware", "temperature": 0.2, "max_tokens": 1000},
}


class TranslationRouter:
    """翻译路由。"""

    def __init__(self) -> None:
        self._rules = dict(ROUTING_RULES)
        self._history: Dict[str, List[float]] = {}

    def route(self, block: Any) -> TranslatorTarget:
        """路由 block。"""
        block_type = getattr(block, "type", "paragraph")
        text = getattr(block, "text", "")

        rule = self._rules.get(block_type, self._rules["paragraph"])

        # 调整 max_tokens
        text_len = len(text)
        max_tokens = max(rule.get("max_tokens", 2000), text_len * 2)

        return TranslatorTarget(
            model=rule["model"],
            temperature=rule.get("temperature", 0.3),
            max_tokens=max_tokens,
            reason=f"block_type={block_type}",
        )

    def add_rule(self, block_type: str, rule: Dict) -> None:
        """添加规则。"""
        self._rules[block_type] = rule

    def get_available_models(self) -> List[str]:
        """获取可用模型。"""
        models = set()
        for rule in self._rules.values():
            models.add(rule.get("model", "default"))
        return list(models)

    def summary(self) -> str:
        return f"TranslationRouter: {len(self._rules)} rules, {len(self.get_available_models())} models"
