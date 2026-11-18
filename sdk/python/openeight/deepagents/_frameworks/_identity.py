"""Native task observations establish identity, never execution authority."""

from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from openeight.errors import ConfigurationError


@dataclass(frozen=True)
class NativeInvocationIdentity:
    child_path: tuple[str, ...]
    checkpoint_namespace: str
    ancestor_namespaces: tuple[str, ...]
    parent_native_tool_call_id: str | None


@dataclass(frozen=True)
class NativeObservation:
    identity: NativeInvocationIdentity
    native_call_id: str
    target_child_path: tuple[str, ...] | None


def _namespace_parts(value):
    if type(value) is not str or not value or len(value.encode()) > 8192:
        raise ValueError
    parts = value.split("|")
    for part in parts:
        node, identifier = part.split(":")
        if node != "tools" or str(UUID(identifier)) != identifier:
            raise ValueError
    return parts


class NativeObservations:
    def __init__(self, *, paths, thread_id):
        self.paths = frozenset(paths)
        self.thread_id = thread_id
        self._observations = {}

    @property
    def observations(self):
        return MappingProxyType(self._observations)

    def validate_runtime(self, config):
        try:
            selectors = config["configurable"]
            namespace = selectors["checkpoint_ns"]
            parts = _namespace_parts(namespace)
            ancestors = tuple("|".join(parts[:i]) for i in range(1, len(parts)))
            current = self._observations[namespace]
            if (
                selectors.get("thread_id") != self.thread_id
                or parts[-1] != "tools:" + selectors["__pregel_task_id"]
                or set(selectors["checkpoint_map"]) != {"", *ancestors}
                or current.identity.ancestor_namespaces != ancestors
                or config["metadata"].get("langgraph_checkpoint_ns") != namespace
                or config["metadata"].get("langgraph_node") != "tools"
            ):
                raise ValueError
            return current
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ConfigurationError("invalid_resume_correlation") from None

    def observe(self, config, call, *, child_path):
        try:
            if child_path not in self.paths:
                raise ValueError
            selectors = config["configurable"]
            if selectors.get("thread_id") != self.thread_id:
                raise ValueError
            namespace = selectors["checkpoint_ns"]
            task_id = selectors["__pregel_task_id"]
            parts = _namespace_parts(namespace)
            if parts[-1] != "tools:" + task_id:
                raise ValueError
            native_map = selectors["checkpoint_map"]
            if type(native_map) is not dict:
                raise ValueError
            # Grammar alone does not prove ancestry: each ancestor must be an exact
            # member of the native checkpoint map and an observed native delegation.
            ancestors = tuple("|".join(parts[:i]) for i in range(1, len(parts)))
            if set(native_map) != {"", *ancestors} or any(
                type(value) is not str or not value for value in native_map.values()
            ):
                raise ValueError
            metadata = config["metadata"]
            if (
                metadata.get("langgraph_checkpoint_ns") != namespace
                or metadata.get("langgraph_node") != "tools"
            ):
                raise ValueError
            native_id = call["id"]
            if (
                type(native_id) is not str
                or not native_id
                or len(native_id.encode()) > 256
            ):
                raise ValueError
            target = None
            if call["name"] == "task":
                target = (*child_path, call["args"]["subagent_type"])
                if target not in self.paths:
                    raise ValueError
            if not ancestors:
                if child_path:
                    raise ValueError
                parent_id = None
            else:
                parent = self._observations.get(ancestors[-1])
                if (
                    parent is None
                    or parent.target_child_path != child_path
                    or parent.identity.ancestor_namespaces != ancestors[:-1]
                ):
                    raise ValueError
                parent_id = parent.native_call_id
            identity = NativeInvocationIdentity(
                child_path, namespace, ancestors, parent_id
            )
            current = NativeObservation(identity, native_id, target)
            previous = self._observations.get(namespace)
            if previous is not None and previous != current:
                raise ValueError
            self._observations[namespace] = current
            return identity
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ConfigurationError("invalid_resume_correlation") from None
