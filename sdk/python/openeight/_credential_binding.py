"""Stable credential binding shared by local enrollment boundaries."""

import hashlib


def credential_binding(credential):
    return hashlib.sha256(
        b"openeight-incident-key-v1\0" + credential.encode("utf-8")
    ).hexdigest()
