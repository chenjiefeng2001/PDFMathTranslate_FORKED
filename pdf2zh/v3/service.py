"""Module: Service Registry.

Global dependency injection container for all V3/V4 services.
Upgrades the existing KernelRegistry (which only manages translation
kernels) into a full ServiceRegistry that manages every module.

Usage::
    registry = ServiceRegistry.get_instance()
    registry.register(ParserService, PDFParser())
    registry.register(AnalyzerService, SemanticAnalyzer())
    parser = registry.get(ParserService)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ServiceInterface:
    """Base class for all service interfaces."""


# ── Service Registry ──────────────────────────────────────────────────


class ServiceRegistry:
    """Global service registry — a simple DI container.

    Supports:
      - Register services by interface type
      - Get service by interface type (singleton scope)
      - Replace/override services at runtime
      - List all registered services
    """

    _instance: Optional["ServiceRegistry"] = None

    def __init__(self) -> None:
        self._services: Dict[type, Any] = {}
        self._factories: Dict[type, callable] = {}

    @classmethod
    def get_instance(cls) -> "ServiceRegistry":
        """Get the singleton registry instance."""
        if cls._instance is None:
            cls._instance = ServiceRegistry()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (for testing)."""
        cls._instance = None

    def register(
        self,
        interface: type,
        implementation: Any,
        *,
        replace: bool = False,
    ) -> None:
        """Register a service.

        Args:
            interface: The interface class (or type key).
            implementation: The service instance.
            replace: If True, overwrite existing registration.

        Raises:
            ValueError: If already registered and replace=False.
        """
        if interface in self._services and not replace:
            raise ValueError(
                f"Service {interface.__name__} already registered. "
                "Use replace=True to override."
            )
        self._services[interface] = implementation
        logger.debug(
            "Registered service: %s → %s",
            interface.__name__,
            type(implementation).__name__,
        )

    def register_factory(
        self,
        interface: type,
        factory: callable,
        *,
        replace: bool = False,
    ) -> None:
        """Register a factory function (called once, result cached)."""
        if interface in self._factories and not replace:
            raise ValueError(f"Factory for {interface.__name__} already registered.")
        # Remove any existing singleton so next get() calls the factory
        self._services.pop(interface, None)
        self._factories[interface] = factory

    def get(self, interface: type) -> Any:
        """Get a registered service by interface.

        Returns:
            The service instance.

        Raises:
            KeyError: If not registered.
        """
        # Check singleton cache first
        if interface in self._services:
            return self._services[interface]

        # Check factory — call and cache
        if interface in self._factories:
            instance = self._factories[interface]()
            self._services[interface] = instance
            return instance

        raise KeyError(
            f"No service registered for {interface.__name__}. "
            f"Registered: {[k.__name__ for k in self._services]}"
        )

    def get_or_default(self, interface: type, default: Any = None) -> Any:
        """Get a registered service, returning default if not found."""
        try:
            return self.get(interface)
        except KeyError:
            return default

    def replace(self, interface: type, implementation: Any) -> None:
        """Replace a registered service (idempotent)."""
        self._services[interface] = implementation

    def unregister(self, interface: type) -> None:
        """Remove a registered service."""
        self._services.pop(interface, None)
        self._factories.pop(interface, None)

    def has(self, interface: type) -> bool:
        """Check if a service is registered."""
        return interface in self._services or interface in self._factories

    def list_services(self) -> List[str]:
        """List all registered service names."""
        names = set()
        for t in self._services:
            names.add(t.__name__)
        for t in self._factories:
            names.add(f"{t.__name__} (factory)")
        return sorted(names)

    def clear(self) -> None:
        """Clear all registered services."""
        self._services.clear()
        self._factories.clear()


# ── Predefined service interfaces ─────────────────────────────────────


class ParserService(ServiceInterface):
    """Interface for PDF parsing services."""


class AnalyzerService(ServiceInterface):
    """Interface for document analysis services."""


class PlannerService(ServiceInterface):
    """Interface for translation planning services."""


class TranslatorService(ServiceInterface):
    """Interface for translation engine services."""


class LayoutService(ServiceInterface):
    """Interface for layout engine services."""


class RendererService(ServiceInterface):
    """Interface for rendering services."""


class QAService(ServiceInterface):
    """Interface for quality evaluation services."""


class MemoryService(ServiceInterface):
    """Interface for document memory / knowledge services."""


__all__ = [
    "ServiceRegistry",
    "ServiceInterface",
    "ParserService",
    "AnalyzerService",
    "PlannerService",
    "TranslatorService",
    "LayoutService",
    "RendererService",
    "QAService",
    "MemoryService",
]
