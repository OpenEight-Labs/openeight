"""Resolve an optional diagnostic SDK release label without network access."""

import re
from importlib.metadata import PackageNotFoundError, version

_LABEL = re.compile(r"[0-9][A-Za-z0-9.+!_-]{0,127}", re.ASCII)


def resolve_sdk_version() -> str | None:
    try:
        label = version("openeight")
    except PackageNotFoundError:
        from openeight import __version__

        label = __version__
    except Exception:
        return None
    return label if isinstance(label, str) and _LABEL.fullmatch(label) else None
