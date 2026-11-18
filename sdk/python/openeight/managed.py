"""Public managed lifecycle records with lazy optional framework loading."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .langchain._managed import DurableAgent, LocalDurableAgent, ManagedAgent
    from .langchain._sync import ManagedStream

from ._managed_types import (
    CatalogReadiness,
    CatalogSnapshot,
    CatalogStatus,
    ManagedResourceFactory,
    ManagedResources,
)

__all__ = [
    "ManagedResources",
    "ManagedResourceFactory",
    "ManagedAgent",
    "DurableAgent",
    "LocalDurableAgent",
    "ManagedStream",
    "CatalogSnapshot",
    "CatalogStatus",
    "CatalogReadiness",
]


def __getattr__(name):
    if name in {"ManagedAgent", "DurableAgent", "LocalDurableAgent"}:
        from .langchain import _managed

        return getattr(_managed, name)
    if name == "ManagedStream":
        from .langchain._sync import ManagedStream

        return ManagedStream
    raise AttributeError(name)
