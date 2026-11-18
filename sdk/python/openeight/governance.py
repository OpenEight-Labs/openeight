"""Framework-independent immutable configuration for managed agents."""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping

from openeight._content_capture import validate_content_policy
from openeight.client import _validate_agent_name, _validate_api_key, _validate_base_url
from openeight.content import JSONValue, TelemetryContentContext
from openeight.delivery import DeliveryConfig
from openeight.errors import ConfigurationError


@dataclass(frozen=True, kw_only=True)
class Governance:
    """Configure governance; transports and tasks belong to the entered adapter."""

    agent_name: str
    endpoint: str
    credential: str = field(repr=False)
    sources: tuple = ()
    mcp_tools: Mapping | None = None
    delivery: DeliveryConfig | None = None
    discovery: Any = None
    on_block: str = "message"
    approval: Any = None
    durable: Any = None
    capture_content: Literal["none", "inputs", "outputs", "all"] = "none"
    redact_content: (
        Callable[[TelemetryContentContext, JSONValue], JSONValue | None] | None
    ) = field(default=None, repr=False)

    def __post_init__(self):
        validate_content_policy(self.capture_content, self.redact_content)
        _validate_agent_name(self.agent_name)
        _validate_base_url(self.endpoint)
        _validate_api_key(self.credential)
        if (
            self.on_block not in {"message", "raise"}
            or (
                self.delivery is not None
                and not isinstance(self.delivery, DeliveryConfig)
            )
            or (self.approval is not None and self.durable is not None)
        ):
            raise ConfigurationError("invalid_integration_configuration")
        object.__setattr__(self, "sources", tuple(self.sources))
        if self.sources or self.discovery is not None or self.mcp_tools is not None:
            from openeight.mcp.configuration import DiscoveryConfig, MCPSource

            if (
                any(not isinstance(source, MCPSource) for source in self.sources)
                or len({source.source_id for source in self.sources})
                != len(self.sources)
                or self.discovery is not None
                and not isinstance(self.discovery, DiscoveryConfig)
            ):
                raise ConfigurationError("invalid_integration_configuration")
        if self.mcp_tools is not None:
            if not isinstance(self.mcp_tools, Mapping) or any(
                type(path) is not tuple
                or any(type(part) is not str or not part for part in path)
                for path in self.mcp_tools
            ):
                raise ConfigurationError("invalid_integration_configuration")
            from openeight.mcp.configuration import MCPTools

            selections = {
                path: tuple(selectors) for path, selectors in self.mcp_tools.items()
            }
            source_ids = {source.source_id for source in self.sources}
            if any(
                not isinstance(selector, MCPTools)
                or selector.source_id not in source_ids
                for selectors in selections.values()
                for selector in selectors
            ):
                raise ConfigurationError("invalid_integration_configuration")
            object.__setattr__(
                self,
                "mcp_tools",
                MappingProxyType(
                    {path: tuple(selectors) for path, selectors in selections.items()}
                ),
            )
