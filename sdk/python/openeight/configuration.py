"""Framework-free managed configuration with construction-time environment capture."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Callable,
    Literal,
    Mapping,
    NotRequired,
    TypedDict,
)

from openeight.client import _validate_base_url
from openeight.content import JSONValue, TelemetryContentContext
from openeight.delivery import DeliveryConfig
from openeight.errors import ConfigurationError
from openeight.governance import Governance

if TYPE_CHECKING:
    from openeight.checkpoints import CheckpointAccess
    from openeight.langchain.configuration import ApprovalConfig
    from openeight.langchain.durable import DurableApprovalConfig
    from openeight.mcp.configuration import DiscoveryConfig, MCPSource, MCPTools

ContentCapture = Literal["none", "inputs", "outputs", "all"]
Redactor = Callable[[TelemetryContentContext, JSONValue], JSONValue | None]
ToolSelections = Mapping[tuple[str, ...], tuple["MCPTools", ...]]


def _freeze_json(value):
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Mapping) and all(type(key) is str for key in value):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if type(value) in (list, tuple):
        return tuple(_freeze_json(item) for item in value)
    raise ConfigurationError("invalid_integration_configuration")


@dataclass(frozen=True, kw_only=True)
class ModelVariant:
    name: str
    configurable: Mapping[str, JSONValue]

    def __post_init__(self):
        if (
            type(self.name) is not str
            or not self.name.strip()
            or not isinstance(self.configurable, Mapping)
        ):
            raise ConfigurationError("invalid_integration_configuration")
        object.__setattr__(self, "configurable", _freeze_json(self.configurable))


@dataclass(frozen=True, kw_only=True)
class RuntimeModelConfig:
    variants: tuple[ModelVariant, ...]

    def __post_init__(self):
        variants = tuple(self.variants)
        if (
            not variants
            or any(not isinstance(item, ModelVariant) for item in variants)
            or len({item.name for item in variants}) != len(variants)
        ):
            raise ConfigurationError("invalid_integration_configuration")
        object.__setattr__(self, "variants", variants)


@dataclass(frozen=True, kw_only=True)
class OpenEightConfig:
    """Configure managed integration identity, governance and optional resources.

    Missing endpoint and credential values are read from OPENEIGHT_ENDPOINT
    and OPENEIGHT_API_KEY when the agent is constructed.
    """

    agent_name: str
    endpoint: str | None = None
    credential: str | None = field(default=None, repr=False)
    sources: tuple[MCPSource, ...] = ()
    mcp_tools: ToolSelections | None = None
    delivery: DeliveryConfig | None = None
    discovery: DiscoveryConfig | None = None
    on_block: Literal["message", "raise"] = "message"
    approval: ApprovalConfig | None = None
    durable: DurableApprovalConfig | None = None
    capture_content: ContentCapture = "none"
    redact_content: Redactor | None = field(default=None, repr=False)
    runtime_models: RuntimeModelConfig | None = None
    checkpoints: CheckpointAccess | None = field(default=None, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "sources", tuple(self.sources))
        if self.mcp_tools is not None:
            if not isinstance(self.mcp_tools, Mapping):
                raise ConfigurationError("invalid_integration_configuration")
            object.__setattr__(
                self,
                "mcp_tools",
                MappingProxyType(
                    {key: tuple(value) for key, value in self.mcp_tools.items()}
                ),
            )


class OpenEightConfigDict(TypedDict):
    agent_name: str
    endpoint: NotRequired[str | None]
    credential: NotRequired[str | None]
    sources: NotRequired[tuple[MCPSource, ...]]
    mcp_tools: NotRequired[ToolSelections | None]
    delivery: NotRequired[DeliveryConfig | None]
    discovery: NotRequired[DiscoveryConfig | None]
    on_block: NotRequired[Literal["message", "raise"]]
    approval: NotRequired[ApprovalConfig | None]
    durable: NotRequired[DurableApprovalConfig | None]
    capture_content: NotRequired[ContentCapture]
    redact_content: NotRequired[Redactor | None]
    runtime_models: NotRequired[RuntimeModelConfig | None]
    checkpoints: NotRequired[CheckpointAccess | None]


@dataclass(frozen=True, kw_only=True)
class NormalizedConfig:
    governance: Governance
    runtime_models: RuntimeModelConfig | None
    checkpoints: CheckpointAccess | None


def normalize_config(
    value: OpenEightConfig | OpenEightConfigDict,
    *,
    environ: Mapping[str, str],
    observed: bool,
) -> NormalizedConfig:
    if type(value) is dict:
        if set(value) - {item.name for item in fields(OpenEightConfig)}:
            raise ConfigurationError("invalid_integration_configuration")
        try:
            value = OpenEightConfig(**value)
        except (TypeError, ValueError):
            raise ConfigurationError("invalid_integration_configuration") from None
    if not isinstance(value, OpenEightConfig):
        raise ConfigurationError("invalid_integration_configuration")
    if observed and (
        value.approval is not None
        or value.durable is not None
        or value.on_block != "message"
    ):
        raise ConfigurationError("invalid_integration_configuration")
    if value.runtime_models is not None and not isinstance(
        value.runtime_models, RuntimeModelConfig
    ):
        raise ConfigurationError("invalid_integration_configuration")
    if value.approval is not None:
        from openeight.langchain.configuration import ApprovalConfig

        if not isinstance(value.approval, ApprovalConfig):
            raise ConfigurationError("invalid_integration_configuration")
    if value.durable is not None:
        from openeight.langchain.durable import DurableApprovalConfig

        if not isinstance(value.durable, DurableApprovalConfig):
            raise ConfigurationError("invalid_integration_configuration")
    if value.checkpoints is not None and not callable(
        getattr(value.checkpoints, "claim", None)
    ):
        raise ConfigurationError("invalid_integration_configuration")
    arguments = {item.name: getattr(value, item.name) for item in fields(Governance)}
    arguments["endpoint"] = (
        value.endpoint
        if value.endpoint is not None
        else environ.get("OPENEIGHT_ENDPOINT")
    )
    arguments["credential"] = (
        value.credential
        if value.credential is not None
        else environ.get("OPENEIGHT_API_KEY")
    )
    arguments["endpoint"] = _validate_base_url(arguments["endpoint"])
    return NormalizedConfig(
        governance=Governance(**arguments),
        runtime_models=value.runtime_models,
        checkpoints=value.checkpoints,
    )
