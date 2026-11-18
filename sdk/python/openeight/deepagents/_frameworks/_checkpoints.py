"""Project exact saved native Send tasks before rebuilding live request evidence."""

from dataclasses import dataclass

from langgraph.types import Send

from openeight._framework._types import FrameworkSnapshot
from openeight.errors import ConfigurationError

from ._identity import NativeObservations


@dataclass(frozen=True)
class NativeTaskProjection:
    namespace: str
    child_path: tuple[str, ...]
    input_checkpoint_id: str
    call: dict
    task: object
    pending_writes: tuple
    child: object = None


@dataclass(frozen=True)
class DeepSnapshot:
    root: object
    tasks: tuple[NativeTaskProjection, ...]
    observations: NativeObservations


async def read_deep_checkpoint(
    compatibility, graph, config, *, checkpoint_id=None, checkpoint_ids=None
):
    if checkpoint_id is None:
        checkpoint_id = config.get("configurable", {}).get("checkpoint_id")
    if checkpoint_id is not None:
        config = {
            **config,
            "configurable": {**config["configurable"], "checkpoint_id": checkpoint_id},
        }
    thread_id = config["configurable"]["thread_id"]
    observations = NativeObservations(paths=compatibility._graphs, thread_id=thread_id)
    projected = []
    saver = graph.checkpointer

    async def read(path, namespace, parent_map):
        selectors = {
            "thread_id": thread_id,
            "checkpoint_ns": namespace,
            "__pregel_checkpointer": saver,
        }
        selected_id = (checkpoint_ids or {}).get(namespace)
        if selected_id is not None:
            selectors["checkpoint_id"] = selected_id
        elif not namespace and checkpoint_id is not None:
            selectors["checkpoint_id"] = checkpoint_id
        native_config = {**config, "configurable": selectors}
        native_graph = compatibility._graphs[path]
        snapshot = await native_graph.aget_state(native_config)
        identifier = (
            snapshot.config.get("configurable", {}).get("checkpoint_id")
            if snapshot.config
            else None
        )
        if identifier is None:
            if selected_id is not None or not namespace and checkpoint_id is not None:
                raise ConfigurationError("durable_checkpoint_missing")
            return snapshot
        saved = await saver.aget_tuple(
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": namespace,
                    "checkpoint_id": identifier,
                }
            }
        )
        if saved is None:
            raise ConfigurationError("durable_checkpoint_missing")
        if saved.metadata.get("parents", {}) != parent_map:
            raise ConfigurationError("invalid_resume_correlation")
        sources = saved.checkpoint["channel_values"].get("__pregel_tasks", [])
        if type(sources) is not list:
            raise ConfigurationError("invalid_resume_correlation")
        for task in snapshot.tasks:
            if task.name != "tools":
                continue
            location = task.path
            if (
                len(location) != 3
                or location[0] != "__pregel_push"
                or type(location[1]) is not int
                or location[1] < 0
                or location[1] >= len(sources)
                or location[2] is not False
            ):
                raise ConfigurationError("invalid_resume_correlation")
            source = sources[location[1]]
            if (
                not isinstance(source, Send)
                or source.node != "tools"
                or type(source.arg) is not list
                or len(source.arg) != 1
                or type(source.arg[0]) is not dict
            ):
                raise ConfigurationError("invalid_resume_correlation")
            call = source.arg[0]
            if compatibility._bindings.resolve(path, call.get("name")) is None:
                raise ConfigurationError("invalid_resume_correlation")
            current = (namespace + "|" if namespace else "") + "tools:" + task.id
            native_map = {**parent_map, namespace: identifier}
            projected_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": current,
                    "__pregel_task_id": task.id,
                    "checkpoint_map": native_map,
                },
                "metadata": {
                    "langgraph_checkpoint_ns": current,
                    "langgraph_node": "tools",
                },
            }
            observations.observe(projected_config, call, child_path=path)
            writes = tuple(w for w in saved.pending_writes if w[0] == task.id)
            child_snapshot = None
            if call.get("name") == "task" and (
                checkpoint_ids is None or current in checkpoint_ids
            ):
                target = (*path, call.get("args", {}).get("subagent_type"))
                if target not in compatibility._graphs:
                    raise ConfigurationError("invalid_resume_correlation")
                # The child namespace is supplied by this exact native parent Send task.
                child_snapshot = await read(target, current, native_map)
            projected.append(
                NativeTaskProjection(
                    current, path, identifier, call, task, writes, child_snapshot
                )
            )
        return snapshot

    root = await read((), "", {})
    identifier = (
        root.config.get("configurable", {}).get("checkpoint_id")
        if root.config
        else None
    )
    messages = root.values.get("messages", []) if type(root.values) is dict else []
    ids = [message.id for message in messages]
    if any(type(value) is not str or not value for value in ids) or len(
        set(ids)
    ) != len(ids):
        raise ConfigurationError("durable_checkpoint_conflict")
    return FrameworkSnapshot(
        identifier,
        bool(root.next),
        frozenset(ids),
        DeepSnapshot(root, tuple(projected), observations),
    )
