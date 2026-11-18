"""Qualify native approval pauses without replaying uncertain business handlers."""

from langgraph.types import Send


def native_request(value):
    if type(value) is not dict or set(value) != {"action_requests", "review_configs"}:
        return False
    actions, configs = value["action_requests"], value["review_configs"]
    if (
        type(actions) is not list
        or type(configs) is not list
        or not actions
        or len(actions) != len(configs)
    ):
        return False
    for action, config in zip(actions, configs):
        if (
            type(action) is not dict
            or not {"name", "args"} <= set(action)
            or set(action) - {"name", "args", "description"}
            or type(action["name"]) is not str
            or type(action["args"]) is not dict
            or type(config) is not dict
            or config.get("action_name") != action["name"]
            or type(config.get("allowed_decisions")) is not list
        ):
            return False
    return True


async def qualified_interrupt(session, scope):
    root_namespace = scope.binding.checkpoint_ns
    root_saved = await session._saver.aget_tuple(scope.config)
    if root_saved is None:
        return False
    marker = root_saved.metadata.get("openeight_checkpoint")

    async def inspect(path, namespace, parents):
        graph = scope.graphs[path]
        config = {
            "configurable": {
                "thread_id": scope.binding.thread_id,
                "checkpoint_ns": namespace,
                "__pregel_checkpointer": session._saver,
            }
        }
        saved = await session._saver.aget_tuple(config)
        if (
            saved is None
            or saved.metadata.get("parents", {}) != parents
            or saved.metadata.get("openeight_checkpoint") != marker
        ):
            return None
        snapshot = await graph.aget_state(config)
        identifier = saved.config["configurable"]["checkpoint_id"]
        if snapshot.config.get("configurable", {}).get("checkpoint_id") != identifier:
            return None
        sources = saved.checkpoint["channel_values"].get("__pregel_tasks", [])
        if type(sources) is not list:
            return None
        found = set()
        for task in snapshot.tasks:
            if task.error:
                return None
            if not task.interrupts:
                if task.result is None:
                    return None
                continue
            identities = {item.id for item in task.interrupts}
            if task.name in scope.native_hitl_nodes.get(path, ()):
                if not all(native_request(item.value) for item in task.interrupts):
                    return None
            elif task.name == "tools" and session._integration == "deepagents":
                location = task.path
                if (
                    len(location) != 3
                    or location[0] != "__pregel_push"
                    or type(location[1]) is not int
                    or not 0 <= location[1] < len(sources)
                    or location[2] is not False
                ):
                    return None
                source = sources[location[1]]
                if (
                    not isinstance(source, Send)
                    or source.node != "tools"
                    or type(source.arg) is not list
                    or len(source.arg) != 1
                    or type(source.arg[0]) is not dict
                ):
                    return None
                call = source.arg[0]
                if call.get("name") != "task" or type(call.get("args")) is not dict:
                    return None
                target = (*path, call["args"].get("subagent_type"))
                if target not in scope.graphs:
                    return None
                binding = scope.bindings.resolve(path, "task")
                if binding is None or binding.tool is not graph.nodes[
                    "tools"
                ].bound.tools_by_name.get("task"):
                    return None
                child_namespace = (
                    (namespace + "|" if namespace else "") + "tools:" + task.id
                )
                child = await inspect(
                    target,
                    child_namespace,
                    {**parents, namespace: identifier},
                )
                if child != identities:
                    return None
            else:
                return None
            found.update(identities)
        if not found or found != {item.id for item in snapshot.interrupts}:
            return None
        return found

    return await inspect((), root_namespace, {}) is not None
