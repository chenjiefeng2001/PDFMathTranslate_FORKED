"""Module: Feature Flags — V4 Engine Gradual Rollout Switches.

Phase 4, Step 4.3: Provides fine-grained feature flags to control
the gradual migration from legacy engine to V4 graph-driven engine.

Allows safe toggling per component, enabling A/B testing
and instant rollback capability.

Usage:
    from pdf2zh.v3.feature_flags import FeatureFlags

    flags = FeatureFlags()
    flags.use_v4_engine = True       # Master switch
    flags.use_v4_translator = True   # Individual switches
    flags.use_v4_layout = False
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class FeatureFlags:
    """Centralized feature flags for V4 engine migration.

    All flags default to False for safe legacy fallback.
    Set to True to enable V4 components incrementally.
    """

    # ── Master Switches ──────────────────────────────────────────────

    use_v4_engine: bool = False
    """Master switch: enable the entire V4 pipeline."""

    use_v4_translator: bool = False
    """Enable V4 TranslationRuntime with Router + ContextBuilder."""

    use_v4_layout: bool = False
    """Enable V4 ConstraintSolver + VisualTree layout engine."""

    use_v4_repair: bool = False
    """Enable V4 Diagnostic + RepairScheduler auto-fix loop."""

    use_v4_renderer: bool = False
    """Enable V4 VisualTree-based unified renderer."""

    use_v4_gate: bool = False
    """Enable the V8.4 write-back relayout gate on the legacy path."""

    relink_links: bool = True
    """V8.5: re-anchor link annotations on translated pages to the rendered
    translation geometry (fixed in high_level, guarded, side-channel data)."""

    use_v4_image_engine: bool = False
    """V8.6: Image Translation Engine (独立图片决策，不修改 legacy 主链路，
    仅当显式开启并接入渲染后端时才影响输出)."""

    use_v4_content_preservation: bool = False
    """V8.6: Content Preservation Engine (统一 translate/preserve/overlay
    决策层，写回 IR 角色；side-channel，需显式开启)."""

    # ── Individual Feature Toggles ───────────────────────────────────

    use_v4_visual_tree_builder: bool = False
    """Enable DocumentGraph → VisualTree builder."""

    use_v4_diagnostic: bool = False
    """Enable DiagnosticReport generation in evaluator."""

    use_v4_repair_scheduler: bool = False
    """Enable RepairScheduler auto-repair execution."""

    use_v4_fix_validate_loop: bool = False
    """Enable the Evaluate → Repair → Re-evaluate closed loop."""

    use_v4_feature_flags: bool = True
    """Enable feature flags system itself."""

    # ── Extended Config ──────────────────────────────────────────────

    max_repair_passes: int = 2
    """Maximum iterations for the fix-validate loop."""

    log_feature_usage: bool = True
    """Log feature flag usage for debugging."""

    metadata: dict = field(default_factory=dict)
    """Additional metadata for context propagation."""

    rollout_policy: Optional["RolloutPolicy"] = None
    """Optional rollout policy for dynamic per-document feature decisions."""

    telemetry: Optional["FallbackTelemetry"] = None
    """Optional fallback telemetry sink (records legacy-fallback events)."""

    def __post_init__(self) -> None:
        if self.use_v4_engine:
            # Master switch enables all sub-features
            self.use_v4_translator = True
            self.use_v4_layout = True
            self.use_v4_repair = True
            self.use_v4_renderer = True
            self.use_v4_visual_tree_builder = True
            self.use_v4_diagnostic = True
            self.use_v4_repair_scheduler = True
            self.use_v4_fix_validate_loop = True

    def summary(self) -> str:
        """Return a human-readable summary of enabled features."""
        enabled = [k for k, v in self._as_dict().items()
                   if isinstance(v, bool) and v and k != "use_v4_feature_flags"]
        disabled = [k for k, v in self._as_dict().items()
                    if isinstance(v, bool) and not v and k != "use_v4_feature_flags"]
        return (
            f"FeatureFlags: {len(enabled)} enabled, {len(disabled)} disabled\n"
            f"  Enabled: {', '.join(sorted(enabled))}\n"
            f"  Disabled: {', '.join(sorted(disabled))}"
        )

    def _as_dict(self) -> Dict[str, bool]:
        return {
            "use_v4_engine": self.use_v4_engine,
            "use_v4_translator": self.use_v4_translator,
            "use_v4_layout": self.use_v4_layout,
            "use_v4_repair": self.use_v4_repair,
            "use_v4_renderer": self.use_v4_renderer,
            "use_v4_visual_tree_builder": self.use_v4_visual_tree_builder,
            "use_v4_diagnostic": self.use_v4_diagnostic,
            "use_v4_repair_scheduler": self.use_v4_repair_scheduler,
            "use_v4_fix_validate_loop": self.use_v4_fix_validate_loop,
            "use_v4_feature_flags": self.use_v4_feature_flags,
        }

    def enable_all(self) -> None:
        """Enable all V4 features."""
        self.use_v4_engine = True

    def disable_all(self) -> None:
        """Disable all V4 features."""
        self.use_v4_engine = False
        self.use_v4_translator = False
        self.use_v4_layout = False
        self.use_v4_repair = False
        self.use_v4_renderer = False
        self.use_v4_visual_tree_builder = False
        self.use_v4_diagnostic = False
        self.use_v4_repair_scheduler = False
        self.use_v4_fix_validate_loop = False

    def evaluate(self, *, page_num: int = 0, doc_type: str = "pdf",
                 user_id: str = "anonymous") -> bool:
        """Decide whether the V4 engine is active for this document.

        When a ``rollout_policy`` is configured it wins over the static
        ``use_v4_engine`` flag, giving per-document gradual rollout.
        """
        if self.rollout_policy is not None:
            return self.rollout_policy.enabled(
                page_num=page_num, doc_type=doc_type, user_id=user_id,
                flags=self)
        return self.use_v4_engine

    def record_fallback(self, event: dict) -> None:
        """Record a legacy-fallback event (no-op without a telemetry sink)."""
        if self.telemetry is not None:
            self.telemetry.record({"flags": self._as_dict(), **event})


# Singleton instance for global access
_global_flags: Optional[FeatureFlags] = None


def get_feature_flags() -> FeatureFlags:
    """Get the global FeatureFlags singleton."""
    global _global_flags
    if _global_flags is None:
        _global_flags = FeatureFlags()
    return _global_flags


def set_feature_flags(flags: FeatureFlags) -> None:
    """Override the global FeatureFlags singleton (for testing)."""
    global _global_flags
    _global_flags = flags


def reset_feature_flags() -> None:
    """Reset the global FeatureFlags to defaults."""
    global _global_flags
    _global_flags = FeatureFlags()


# ═══════════════════════════════════════════════════════════════════
# V8.2 Rollout Rules Engine
# ═══════════════════════════════════════════════════════════════════

@dataclass
class RolloutDecision:
    """Outcome of evaluating a rollout policy."""

    enabled: bool
    reason: str = ""
    rule: str = ""


class RolloutRule:
    """Base class for rollout rules (pure predicates on document context)."""

    name: str = "base"

    def matches(self, *, page_num: int = 0, doc_type: str = "pdf",
                user_id: str = "anonymous") -> bool:
        raise NotImplementedError


class PercentRolloutRule(RolloutRule):
    """Stable-percentage rollout keyed by a stable hash of the user/page.

    The same document + user always lands in the same bucket, so a user
    never flips between engines mid-session.
    """

    def __init__(self, percent: float, key: str = "user") -> None:
        if not 0.0 <= percent <= 100.0:
            raise ValueError("percent must be in [0, 100]")
        self.percent = percent
        self.key = key
        self.name = f"percent_{percent:.0f}"

    @staticmethod
    def _bucket(value: str) -> int:
        # stable cross-process hash (built-in hash() is salted per run)
        import hashlib
        digest = hashlib.md5(value.encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % 100

    def matches(self, *, page_num: int = 0, doc_type: str = "pdf",
                user_id: str = "anonymous") -> bool:
        if self.key == "page":
            h = self._bucket(f"page:{page_num}")
        else:
            h = self._bucket(f"user:{user_id or 'anonymous'}")
        return h < self.percent


class PageRangeRolloutRule(RolloutRule):
    """Roll out to the given page numbers; optionally expand beyond them.

    With ``include_external=False`` only the listed pages take part (first
    wave). With ``include_external=True`` pages *beyond* the last listed page
    also take part, modelling "roll out early pages first, then expand".
    """

    def __init__(self, pages, include_external: bool = False) -> None:
        self.pages = set(int(p) for p in pages)
        self.include_external = include_external
        self.name = f"pages_{sorted(self.pages)}"

    def matches(self, *, page_num: int = 0, doc_type: str = "pdf",
                user_id: str = "anonymous") -> bool:
        if page_num in self.pages:
            return True
        if self.include_external and self.pages:
            return page_num > max(self.pages)
        return False


class DocTypeRolloutRule(RolloutRule):
    """Roll out only to specific document types."""

    def __init__(self, doc_types) -> None:
        self.doc_types = set(doc_types)
        self.name = f"doc_types_{sorted(self.doc_types)}"

    def matches(self, *, page_num: int = 0, doc_type: str = "pdf",
                user_id: str = "anonymous") -> bool:
        return doc_type in self.doc_types


class UserAllowlistRolloutRule(RolloutRule):
    """Roll out only to an explicit allowlist of users (internal beta)."""

    def __init__(self, users) -> None:
        self.users = set(users)
        self.name = f"users_{len(self.users)}"

    def matches(self, *, page_num: int = 0, doc_type: str = "pdf",
                user_id: str = "anonymous") -> bool:
        return user_id in self.users


@dataclass
class RolloutPolicy:
    """Ordered set of rollout rules; first match wins."""

    rules: list = field(default_factory=list)

    def add(self, rule: RolloutRule) -> "RolloutPolicy":
        self.rules.append(rule)
        return self

    def decide(self, *, page_num: int = 0, doc_type: str = "pdf",
               user_id: str = "anonymous", flags: Optional[FeatureFlags] = None) -> RolloutDecision:
        for rule in self.rules:
            if rule.matches(page_num=page_num, doc_type=doc_type,
                            user_id=user_id):
                return RolloutDecision(enabled=True,
                                       reason=f"matched rule {rule.name}",
                                       rule=rule.name)
        # No rule matched → conservative default: keep legacy unless the
        # static master switch is on.
        enabled = bool(flags) and flags.use_v4_engine
        return RolloutDecision(enabled=enabled, reason="no rule matched")

    def enabled(self, *, page_num: int = 0, doc_type: str = "pdf",
                user_id: str = "anonymous",
                flags: Optional[FeatureFlags] = None) -> bool:
        return self.decide(page_num=page_num, doc_type=doc_type,
                           user_id=user_id, flags=flags).enabled


class FallbackTelemetry:
    """In-memory telemetry sink for legacy-fallback events (V8.2).

    Records every fallback so the migration team can quantify how often the
    V4 engine had to hand back to the legacy pipeline.
    """

    def __init__(self, backend=None) -> None:
        self.backend = backend   # optional callable(event) external sink
        self._events: list = []

    def record(self, event: dict) -> None:
        self._events.append(dict(event))
        if self.backend is not None:
            try:
                self.backend(event)
            except Exception:
                pass

    def events(self) -> list:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()

    def count(self, reason: str = "") -> int:
        if reason:
            return sum(1 for e in self._events if e.get("reason") == reason)
        return len(self._events)


__all__ = [
    "FeatureFlags",
    "get_feature_flags",
    "set_feature_flags",
    "reset_feature_flags",
    "RolloutDecision",
    "RolloutRule",
    "PercentRolloutRule",
    "PageRangeRolloutRule",
    "DocTypeRolloutRule",
    "UserAllowlistRolloutRule",
    "RolloutPolicy",
    "FallbackTelemetry",
]
