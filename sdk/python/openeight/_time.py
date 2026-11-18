"""UTC timestamps shared by telemetry and execution lifecycles."""

from datetime import datetime, timezone


def utc_timestamp() -> str:
    """Return the current UTC time in RFC3339 form with a Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
