"""Input validation shared by the machine client and delivery lifecycles."""

from __future__ import annotations

import math
from typing import Any

from openeight.errors import ConfigurationError


def validate_timeout(value: Any) -> float:
    """Return finite positive seconds; booleans are not timeout values."""
    normalized: float | None = None
    valid = False
    try:
        if not isinstance(value, bool) and isinstance(value, (int, float)):
            normalized = float(value)
            valid = math.isfinite(normalized) and normalized > 0
    except Exception:
        pass
    if not valid or normalized is None:
        raise ConfigurationError("invalid_timeout") from None
    return normalized
