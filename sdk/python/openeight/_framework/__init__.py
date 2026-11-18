"""Private qualification records; importing these does not load a framework."""

from ._registry import (
    CompatibilitySelection,
    InstalledFramework,
    detect_framework,
    select_compatibility,
)

__all__ = [
    "load_compatibility",
    "CompatibilitySelection",
    "InstalledFramework",
    "detect_framework",
    "select_compatibility",
]

from ._loader import load_compatibility
