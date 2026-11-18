"""Opaque native values cross this private boundary; authority remains shared."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Protocol

if TYPE_CHECKING:
    from openeight.deepagents.durable import GeneratedStateReceipt


@dataclass(frozen=True)
class ManagedBuild:
    graph: Any
    bindings: Any
    controls: tuple[Any, ...]
    composition: Mapping[str, Any]
    verify_configuration: Any | None = None


@dataclass(frozen=True)
class FrameworkBuildRequest:
    model: Any
    tools: tuple[Any, ...]
    options: Mapping[str, Any]
    controls: Mapping[tuple[str, ...], Any]
    checkpointer: Any | None
    store: Any | None
    resolved: Any | None = None
    response_spec: Any | None = None
    variant_name: str | None = None


@dataclass(frozen=True)
class FrameworkSnapshot:
    checkpoint_id: str | None
    has_next: bool
    message_ids: frozenset[str]
    native: Any


@dataclass(frozen=True)
class NativeToolResult:
    tool_call_id: str
    tool_name: str
    content: str
    status: str


@dataclass(frozen=True)
class CapturedGeneratedResult:
    result: NativeToolResult
    receipt: "GeneratedStateReceipt"
    native_result: Any


@dataclass(frozen=True)
class GeneratedReplayState:
    disposition: str
    native: Any


class FrameworkBuilder(Protocol):
    def validate(self, selection, *, required): ...
    def build(self, request: FrameworkBuildRequest) -> ManagedBuild: ...
    def inspect(self, build: ManagedBuild) -> None: ...


class CheckpointCompatibility(Protocol):
    async def read_checkpoint(
        self, graph, config, *, checkpoint_id=None
    ) -> FrameworkSnapshot: ...
    def visible_output(
        self, snapshot: FrameworkSnapshot, *, command_id: str
    ) -> tuple[str, ...]: ...
    def output_since(
        self, snapshot: FrameworkSnapshot, *, before: FrameworkSnapshot
    ) -> tuple[str, ...]: ...
    def structured_response(self, snapshot: FrameworkSnapshot) -> tuple[bool, Any]: ...
    def message_input(self, messages: tuple[tuple[str, str], ...]) -> Any: ...
    def tool_result(self, message) -> NativeToolResult: ...
    def tool_message(self, result: NativeToolResult) -> Any: ...


class DurableCompatibility(Protocol):
    def pause_for_approval(self, decision_id: str) -> Any: ...
    def suspend_reconciliation(self) -> None: ...
    def validate_durable_saver(self, graph) -> None: ...
    def execution_observations(self, *, thread_id: str, snapshot=None) -> Any: ...
    def recovery_namespaces(self, snapshot: FrameworkSnapshot) -> frozenset[str]: ...
    def generated_recovery_kind(self, binding) -> str | None: ...
    def is_interrupt(self, error: BaseException) -> bool: ...
    async def invoke_durable(self, graph, native_input, config) -> None: ...
    def pending_delegations(
        self, snapshot: FrameworkSnapshot, *, invocation
    ) -> frozenset[str]: ...
    def resume_input(
        self, snapshot: FrameworkSnapshot, *, invocation, continuations
    ): ...


class GeneratedToolCompatibility(Protocol):
    def native_identity(self, request, *, invocation, child_path): ...
    async def recover_generated(self, invocation, attempt, continuation, snapshot): ...
    async def capture_generated(
        self, request, handler, *, invocation, attempt, continuation
    ) -> CapturedGeneratedResult: ...
    async def inspect_generated_replay(
        self, snapshot: FrameworkSnapshot, *, invocation, attempt, continuation, receipt
    ) -> GeneratedReplayState: ...
    def generated_result(
        self,
        result: NativeToolResult,
        *,
        invocation,
        continuation,
        receipt,
        replay: GeneratedReplayState,
    ) -> Any: ...


class FrameworkCompatibility(
    FrameworkBuilder, CheckpointCompatibility, DurableCompatibility, Protocol
):
    """Capabilities shared by the qualified LangChain and Deep Agents adapters."""


class DeepAgentsCompatibility(
    FrameworkCompatibility, GeneratedToolCompatibility, Protocol
):
    """Additional native identities and generated-effect recovery used by Deep Agents."""
