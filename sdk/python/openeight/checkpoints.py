"""Application-owned authority and persistence for ordinary checkpoint workflows."""

from dataclasses import dataclass, field
from typing import Any, AsyncContextManager, Literal, Mapping, Protocol


@dataclass(frozen=True, kw_only=True)
class CheckpointBinding:
    """Application-authorized thread selectors and graph revision for one claim."""

    scope_id: str
    thread_id: str
    checkpoint_ns: str
    graph_revision: str
    model_variant: str | None
    mode: Literal["new", "continue", "adopt"]


@dataclass(frozen=True, kw_only=True)
class CheckpointEnrollment:
    """Persisted identity and graph binding used to reject incompatible continuation."""

    scope_id: str
    thread_id: str
    checkpoint_ns: str
    format_version: int
    agent_name: str
    endpoint: str
    identity_kind: Literal["credential_bound"]
    credential_binding_sha256: str = field(repr=False)
    graph_revision: str
    integration: Literal["langchain", "deepagents"]
    configuration_sha256: str = field(repr=False)
    origin: Literal["new", "adopted"]
    adoption_checkpoint_id: str | None
    adopted_at: str | None


@dataclass(frozen=True, kw_only=True)
class CheckpointExecution:
    """Persisted execution state for detecting an interrupted checkpoint turn."""

    scope_id: str
    thread_id: str
    checkpoint_ns: str
    execution_id: str
    run_id: str
    started_at: str
    finished_at: str | None
    state: Literal["running", "settled", "native_interrupt", "recovery_required"]
    input_checkpoint_id: str | None
    output_checkpoint_id: str | None
    model_variant: str | None


class CheckpointClaim(Protocol):
    """Application-held thread ownership; keep it valid through execution and cleanup."""

    binding: CheckpointBinding

    async def read_binding(self) -> CheckpointEnrollment | None: ...
    async def bind(self, value: CheckpointEnrollment) -> None: ...
    async def assert_held(self) -> None: ...
    async def begin(self, value: CheckpointExecution) -> None: ...
    async def finish(self, value: CheckpointExecution) -> None: ...
    async def last_execution(self) -> CheckpointExecution | None: ...


class CheckpointAccess(Protocol):
    """Authorize caller context and acquire exclusive ownership of its checkpoint thread."""

    def claim(
        self, *, config: Mapping[str, Any], context: Any
    ) -> AsyncContextManager[CheckpointClaim]: ...
