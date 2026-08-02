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


__all__ = [
    "FeatureFlags",
    "get_feature_flags",
    "set_feature_flags",
    "reset_feature_flags",
]
