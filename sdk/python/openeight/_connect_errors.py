"""Bounded decoding of the public governance ErrorInfo detail."""

import base64

from openeight._error_catalog import ERROR_DETAILS


def governance_reason(status: int, code: str, details: object) -> str | None:
    """Ignore unrecognized details; never retain server messages or metadata."""
    if not isinstance(details, list) or len(details) > 8:
        return None
    infos = [
        d
        for d in details
        if isinstance(d, dict) and d.get("type") == "google.rpc.ErrorInfo"
    ]
    if len(infos) != 1:
        return None
    encoded = infos[0].get("value")
    if not isinstance(encoded, str) or len(encoded) > 1024:
        return None
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        pos = 0

        def varint():
            nonlocal pos
            value = 0
            for shift in range(0, 35, 7):
                byte = raw[pos]
                pos += 1
                value |= (byte & 127) << shift
                if byte < 128:
                    return value
            raise ValueError

        fields = {}
        while pos < len(raw):
            tag = varint()
            # ErrorInfo contains reason (1), domain (2), and metadata entries (3).
            if tag not in (10, 18, 26):
                return None
            length = varint()
            end = pos + length
            if end > len(raw):
                return None
            if tag != 26:
                if tag in fields:
                    return None
                fields[tag] = raw[pos:end].decode("utf-8", errors="strict")
            pos = end
        reason = fields.get(10)
        if ERROR_DETAILS.get(reason) == (fields.get(18), status, code):
            return reason
    except (ValueError, IndexError, UnicodeError):
        pass
    return None
