"""Immutable catalog projections; callers retain lifecycle and publication ownership."""

import time
from types import MappingProxyType

from openeight._managed_types import CatalogSnapshot, CatalogStatus
from openeight.errors import ConfigurationError


def catalog_snapshot(owner, coordinator) -> CatalogSnapshot:
    with owner.lock:
        return CatalogSnapshot(
            coordinator.current.number if coordinator else 1,
            owner.catalog.facts,
            owner.catalog_delivery.snapshot(),
            owner.catalog.snapshot(),
            "complete",
            not owner._defer_catalog
            and len(owner.catalog_delivery.snapshot()) == len(owner.catalog.facts)
            and owner.catalog_delivery.diagnostics()["catalog_delivery_state"]
            in {"acknowledged", "empty"},
        )


def catalog_status(owner, coordinator) -> CatalogStatus:
    if owner is None:
        raise ConfigurationError("integration_not_entered")
    sources = []
    if coordinator:
        for source in coordinator.sources.values():
            current = source.available and time.monotonic() < source.fresh_until
            sources.append(
                MappingProxyType(
                    {
                        "source_id": source.config.source_id,
                        "binding_id": source.config.binding_id,
                        "epoch": source.epoch,
                        "state": "unavailable"
                        if source.failed
                        else "current"
                        if current and source.descriptors
                        else "empty"
                        if current
                        else "stale",
                    }
                )
            )
    return CatalogStatus(
        coordinator.current.number if coordinator else 1,
        tuple(sources),
        MappingProxyType(owner.catalog_delivery.diagnostics()),
        coordinator.error if coordinator else None,
    )
