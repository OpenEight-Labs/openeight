"""Immutable direct Decision preparation; authorization remains server-owned."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from openeight._direct import identifier, safe_direct_errors, timestamp, workflow
from openeight._json import decision_envelope
from openeight.errors import ConfigurationError
from openeight.telemetry import uuid7


@dataclass(frozen=True, init=False, repr=False)
class PreparedDecision:
    """Retain one validated request for retries under the same machine actor."""

    _body: bytes

    def __init__(self) -> None:
        raise ConfigurationError("invalid_input")

    @property
    def decision_id(self) -> str:
        return json.loads(self._body)["decision_id"]

    @property
    def run_id(self) -> str:
        return json.loads(self._body)["run_id"]

    @property
    def source_time(self) -> str:
        return json.loads(self._body)["source_time"]

    @property
    def tool_id(self) -> str:
        return json.loads(self._body)["tool_id"]

    @property
    def tool_version_id(self) -> str:
        return json.loads(self._body)["tool_version_id"]

    @property
    def input(self) -> dict[str, Any]:
        return json.loads(json.loads(self._body)["input_json"])

    @property
    def policy_contract_set_digest(self) -> str:
        return json.loads(self._body)["policy_contract_set_digest"]

    @property
    def selected_values(self) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(json.loads(self._body)["selected_values_json"]))

    @property
    def workflow_id(self) -> str | None:
        return json.loads(self._body).get("workflow_id")

    @property
    def workflow_version(self) -> str | None:
        return json.loads(self._body).get("workflow_version")


@safe_direct_errors
def prepare_decision(
    *,
    run_id: str,
    tool_id: str,
    tool_version_id: str,
    input: Any,
    policy_contract_set_digest: str,
    selected_values: Sequence[dict[str, Any]],
    decision_id: str = "",
    source_time: datetime | str | None = None,
    workflow_id: str | None = None,
    workflow_version: str | None = None,
) -> PreparedDecision:
    """Snapshot an attempt before dispatch; explicit ID and time must be paired."""
    if isinstance(decision_id, str):
        decision_id = str(decision_id)
    if not isinstance(decision_id, str) or bool(decision_id) != (
        source_time is not None
    ):
        raise ConfigurationError("invalid_decision_replay")
    envelope = json.loads(
        decision_envelope(input, policy_contract_set_digest, selected_values)
    )
    body = {
        "decision_id": identifier(
            decision_id or uuid7(), versions={7}, reason="invalid_decision_id"
        ),
        "run_id": identifier(run_id, versions={7}),
        "source_time": timestamp(
            datetime.now(timezone.utc) if source_time is None else source_time,
            decision=True,
        ),
        "tool_id": identifier(tool_id, reason="invalid_tool_id"),
        "tool_version_id": identifier(
            tool_version_id, reason="invalid_tool_version_id"
        ),
        "input_json": json.dumps(
            envelope["input"], ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ),
        "policy_contract_set_digest": policy_contract_set_digest,
        "selected_values_json": json.dumps(
            envelope["selected_values"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        **workflow(workflow_id, workflow_version),
    }
    prepared = object.__new__(PreparedDecision)
    object.__setattr__(
        prepared,
        "_body",
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
    )
    return prepared
