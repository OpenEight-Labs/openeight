"""Bounded SemVer validation shared by producer entry points."""

import re

_PATTERN = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)


def is_semver(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 128:
        return False
    match = _PATTERN.fullmatch(value)
    if match is None or any(int(n) > 2**64 - 1 for n in match.group(1, 2, 3)):
        return False
    for part in (match.group(4) or "").split("."):
        if part.isdigit() and (
            len(part) > 1 and part[0] == "0" or int(part) > 2**64 - 1
        ):
            return False
    return True
