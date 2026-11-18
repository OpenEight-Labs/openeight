"""Bounded facade delivery configuration and immutable local diagnostics."""

import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping

from openeight._content_capture import FAILURE_REASONS
from openeight.errors import ConfigurationError


@dataclass(frozen=True, kw_only=True)
class DeliveryConfig:
    """Bounded in-memory delivery limits; admission never proves server acknowledgement."""

    max_records: int = 1000
    max_bytes: int = 8 * 1024 * 1024
    flush_records: int = 100
    flush_bytes: int = 3 * 1024 * 1024
    flush_interval: float = 1.0
    late_callback_grace: float = 1.0

    def __post_init__(self):
        counts = (
            self.max_records,
            self.max_bytes,
            self.flush_records,
            self.flush_bytes,
        )
        times = (self.flush_interval, self.late_callback_grace)
        if (
            any(type(v) is not int or v <= 0 for v in counts)
            or any(
                type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                for v in times
            )
            or self.flush_records > self.max_records
            or self.flush_bytes > self.max_bytes
            or self.flush_records > 1000
            or self.flush_bytes > 4 * 1024 * 1024
        ):
            raise ConfigurationError("invalid_delivery_configuration")


@dataclass(frozen=True, kw_only=True)
class DeliveryStatus:
    """Local delivery accounting and resource state at the time of inspection."""

    pending_records: int = 0
    pending_bytes: int = 0
    dropped_records: int = 0
    failed_records: int = 0
    last_error_code: str | None = None
    closed: bool = False
    discovery_completeness: Literal["complete", "partial", "unknown"] = "unknown"
    discovery_diagnostic: str | None = "unsupported_graph_shape"
    catalog_delivery_state: Literal[
        "not_started",
        "pending",
        "partial",
        "acknowledged",
        "failed",
        "dropped",
        "empty",
    ] = "not_started"
    catalog_total_tools: int = 0
    catalog_acknowledged_tools: int = 0
    catalog_pending_tools: int = 0
    catalog_failed_tools: int = 0
    catalog_dropped_tools: int = 0

    content_capture_failures: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType(dict.fromkeys(FAILURE_REASONS, 0))
    )
