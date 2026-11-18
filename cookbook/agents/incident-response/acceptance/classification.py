"""Human-authenticated acceptance helpers, never imported by the agent runtime."""

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ExpectedTool:
    tool_id: str
    tool_version_id: str
    catalog_name: str
    expected: Literal["read", "write"]


@dataclass(frozen=True)
class ClassificationObservation:
    tool_id: str
    tool_version_id: str
    catalog_name: str
    expected: Literal["read", "write"]
    actual: Literal["read", "write"] | None
    status: str
    error_code: str | None


class ClassificationError(Exception):
    def __init__(self, code, observations=()):
        super().__init__(code)
        self.code, self.observations = code, observations


class ClassificationReader(Protocol):
    async def read(
        self, tool_ids: tuple[str, ...]
    ) -> tuple[ClassificationObservation, ...]: ...


class HTTPClassificationReader:
    """The harness owns the scoped authenticated client and its lifetime."""

    def __init__(self, *, client, agent_id: str, expected: tuple[ExpectedTool, ...]):
        self.client, self.agent_id = client, agent_id
        self.expected = {e.tool_id: e for e in expected}
        if len(self.expected) != len(expected):
            raise ClassificationError("manifest_invalid")

    async def read(self, tool_ids):
        results = []
        for identity in tool_ids:
            expected = self.expected[identity]
            try:
                response = await self.client.get("/api/v1/tools/" + identity)
                response.raise_for_status()
                data = response.json()
                if (
                    data["id"] != identity
                    or data["agent_id"] != self.agent_id
                    or data["name"] != expected.catalog_name
                    or data["current_version_id"] != expected.tool_version_id
                ):
                    raise ClassificationError("catalog_changed")
                capability = data["capability"]
                status, error = (
                    data["classification"]["status"],
                    data["classification"]["error_code"],
                )
                if (
                    capability not in ("read", "write", "")
                    or status
                    not in ("unclassified", "queued", "running", "failed", "classified")
                    or error
                    not in (
                        None,
                        "timeout",
                        "provider_unavailable",
                        "uncertain",
                        "refused",
                        "invalid_output",
                        "capacity",
                    )
                ):
                    raise ClassificationError("classification_protocol_error")
                results.append(
                    ClassificationObservation(
                        identity,
                        expected.tool_version_id,
                        expected.catalog_name,
                        expected.expected,
                        capability or None,
                        status,
                        error,
                    )
                )
            except ClassificationError:
                raise
            except Exception:
                raise ClassificationError("classification_read_failed") from None
        return tuple(results)


async def await_classifications(
    *,
    reader: ClassificationReader,
    expected: tuple[ExpectedTool, ...],
    timeout_seconds: float = 120.0,
    poll_seconds: float = 2.0,
) -> tuple[ClassificationObservation, ...]:
    import math

    if not expected or any(
        not math.isfinite(x) or x <= 0 for x in (timeout_seconds, poll_seconds)
    ):
        raise ClassificationError("manifest_invalid")
    manifest = {e.tool_id: e for e in expected}
    if len(manifest) != len(expected):
        raise ClassificationError("manifest_invalid")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    observations = ()
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                observations = await reader.read(tuple(manifest))
                if len(observations) != len(manifest) or {
                    o.tool_id for o in observations
                } != set(manifest):
                    raise ClassificationError(
                        "classification_records_missing", observations
                    )
                for observation in observations:
                    e = manifest[observation.tool_id]
                    if (
                        observation.tool_version_id,
                        observation.catalog_name,
                        observation.expected,
                    ) != (e.tool_version_id, e.catalog_name, e.expected):
                        raise ClassificationError("catalog_changed", observations)
                    if (
                        observation.status == "failed"
                        or observation.error_code is not None
                    ):
                        raise ClassificationError("classification_failed", observations)
                    if (
                        observation.status == "classified"
                        and observation.actual != e.expected
                    ):
                        raise ClassificationError(
                            "classification_mismatch", observations
                        )
                if all(o.status == "classified" for o in observations):
                    return observations
                await asyncio.sleep(
                    min(
                        poll_seconds,
                        max(0, deadline - asyncio.get_running_loop().time()),
                    )
                )
    except TimeoutError:
        raise ClassificationError("classification_timeout", observations) from None
