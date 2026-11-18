"""Inline approval polling configuration."""

import math
from dataclasses import dataclass

from openeight import ConfigurationError


@dataclass(frozen=True)
class ApprovalConfig:
    """Bound the in-process approval wait and its polling backoff."""

    timeout: float = 3600.0
    poll_interval: float = 0.5
    poll_backoff_multiplier: float = 2.0
    poll_max_interval: float = 5.0
    poll_jitter: float = 0.1

    def __post_init__(self):
        values = (
            self.timeout,
            self.poll_interval,
            self.poll_backoff_multiplier,
            self.poll_max_interval,
            self.poll_jitter,
        )
        if (
            not all(
                type(value) in (int, float) and math.isfinite(value) for value in values
            )
            or min(values[:4]) <= 0
            or self.poll_backoff_multiplier < 1
            or self.poll_max_interval < self.poll_interval
            or not 0 <= self.poll_jitter <= 1
        ):
            raise ConfigurationError("invalid_integration_configuration") from None
