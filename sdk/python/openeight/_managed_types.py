"""Framework-free managed resource and catalog records."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, AsyncContextManager, Mapping

from openeight.tools import AgentReadiness, RegistrationResult, ToolRegistration


@dataclass(frozen=True)
class ManagedResources:
    """Borrowed native resources; the caller or resource factory owns their lifetime."""

    checkpointer: Any | None = None
    store: Any | None = None


ManagedResourceFactory = Callable[[], AsyncContextManager[ManagedResources]]


@dataclass(frozen=True)
class CatalogSnapshot:
    """Captured publication facts; publication alone does not authorize execution."""

    generation: int
    facts: tuple[ToolRegistration, ...]
    results: tuple[RegistrationResult, ...]
    bindings: tuple[Mapping[str, Any], ...]
    discovery_completeness: str = "complete"
    publication_complete: bool = False


@dataclass(frozen=True)
class CatalogStatus:
    """Local discovery and publication status for one catalog generation."""

    generation: int
    sources: tuple[Mapping[str, str | int], ...]
    publication: Mapping[str, str | int]
    error_code: str | None


@dataclass(frozen=True)
class CatalogReadiness:
    """Server readiness and unpublished tools observed for one catalog generation."""

    generation: int
    as_of: str
    readiness: AgentReadiness
    unpublished: tuple[str, ...]
