"""Frozen source configuration; importing it allocates no sessions or transports."""

import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

from openeight.errors import ConfigurationError


def _label(value):
    if type(value) is not str or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value
    ):
        raise ConfigurationError("invalid_integration_configuration")


def _mapping(value):
    value = dict(value or {})
    if any(
        type(k) is not str or not k or type(v) is not str or "\0" in k + v
        for k, v in value.items()
    ):
        raise ConfigurationError("invalid_integration_configuration")
    return MappingProxyType(value)


@dataclass(frozen=True)
class DiscoveryConfig:
    """Bound MCP discovery time, catalog size and retained generations; times are seconds."""

    refresh_interval: float = 30.0
    request_timeout: float = 10.0
    max_tools: int = 200
    max_catalog_bytes: int = 1048576
    max_pages: int = 200
    max_generations: int = 4

    def __post_init__(self):
        for value in (self.refresh_interval, self.request_timeout):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 < value <= 3600
            ):
                raise ConfigurationError("invalid_integration_configuration")
        for value, cap in (
            (self.max_tools, 200),
            (self.max_catalog_bytes, 1048576),
            (self.max_pages, 200),
            (self.max_generations, 4),
        ):
            if type(value) is not int or not 0 < value <= cap:
                raise ConfigurationError("invalid_integration_configuration")


@dataclass(frozen=True)
class MCPTools:
    """Select tools from one source; None selects all names, an empty tuple selects none."""

    source_id: str
    names: tuple[str, ...] | None = None

    def __post_init__(self):
        _label(self.source_id)
        if self.names is not None:
            names = tuple(self.names)
            if any(
                type(name) is not str or not name or len(name.encode()) > 1024
                for name in names
            ) or len(names) != len(set(names)):
                raise ConfigurationError("invalid_integration_configuration")
            object.__setattr__(self, "names", names)


@dataclass(frozen=True, kw_only=True)
class MCPSource:
    """Explicit HTTP or subprocess connection; headers and environment stay out of repr."""

    source_id: str
    binding_id: str
    transport: str
    url: str | None = field(default=None, repr=False)
    headers: object = field(default=None, repr=False)
    command: str | None = field(default=None, repr=False)
    args: tuple = field(default=(), repr=False)
    env: object = field(default=None, repr=False)

    def __post_init__(self):
        _label(self.source_id)
        _label(self.binding_id)
        if self.transport == "http":
            url = urlsplit(self.url or "")
            if (
                url.username
                or url.password
                or url.fragment
                or url.query
                or not url.hostname
                or url.scheme not in {"http", "https"}
                or url.scheme == "http"
                and url.hostname not in {"localhost", "127.0.0.1", "::1"}
                or self.command is not None
                or self.args
                or self.env is not None
            ):
                raise ConfigurationError("invalid_integration_configuration")
            if not callable(self.headers):
                headers = _mapping(self.headers)
                if any("\r" in k + v or "\n" in k + v for k, v in headers.items()):
                    raise ConfigurationError("invalid_integration_configuration")
                object.__setattr__(self, "headers", headers)
        elif self.transport == "stdio":
            if (
                type(self.command) is not str
                or not self.command
                or "\0" in self.command
                or self.url is not None
                or self.headers is not None
            ):
                raise ConfigurationError("invalid_integration_configuration")
            args = tuple(self.args)
            if any(type(arg) is not str or "\0" in arg for arg in args):
                raise ConfigurationError("invalid_integration_configuration")
            object.__setattr__(self, "args", args)
            object.__setattr__(self, "env", _mapping(self.env))
        else:
            raise ConfigurationError("invalid_integration_configuration")

    @classmethod
    def http(cls, *, source_id, binding_id, url, headers=None):
        return cls(
            source_id=source_id,
            binding_id=binding_id,
            transport="http",
            url=url,
            headers=headers,
        )

    @classmethod
    def stdio(cls, *, source_id, binding_id, command, args=(), env=None):
        return cls(
            source_id=source_id,
            binding_id=binding_id,
            transport="stdio",
            command=command,
            args=tuple(args),
            env=env,
        )
