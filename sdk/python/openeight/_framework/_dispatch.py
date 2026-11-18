"""In-memory executable integrity, kept separate from persisted measurements."""

from inspect import getclosurevars, isfunction, ismethod


def dispatch_signature(graph):
    """Retain executable identities in memory, separately from persisted measurement."""
    active = set()

    def value(item):
        if item is None or type(item) in (str, int, bool, float, bytes):
            return type(item), item
        if id(item) in active:
            return "recursive", id(item)
        if ismethod(item):
            return "method", item.__func__, id(item.__self__)
        if isfunction(item):
            active.add(id(item))
            try:
                return (
                    "function",
                    item.__code__,
                    id(item.__globals__),
                    tuple(
                        (key, value(child))
                        for key, child in sorted(getclosurevars(item).nonlocals.items())
                    ),
                )
            finally:
                active.remove(id(item))
        if type(item) in (tuple, list, dict):
            active.add(id(item))
            try:
                if type(item) is dict:
                    return "mapping", frozenset(
                        (value(key), value(child)) for key, child in item.items()
                    )
                return "sequence", tuple(value(child) for child in item)
            finally:
                active.remove(id(item))
        return "identity", type(item), id(item)

    nodes = []
    for name, node in sorted(graph.nodes.items()):
        bound = node.bound
        builder = graph.builder.nodes.get(name)
        nodes.append(
            (
                name,
                id(bound),
                id(builder.runnable) if builder else None,
                tuple(
                    (method, value(getattr(bound, method, None)))
                    for method in (
                        "func",
                        "afunc",
                        "_wrap_tool_call",
                        "_awrap_tool_call",
                    )
                ),
            )
        )
    channels = tuple(
        (name, type(channel), value(getattr(channel, "operator", None)))
        for name, channel in sorted(graph.channels.items())
    )
    edges = tuple(
        sorted(
            (edge.source, edge.target, value(edge.data), edge.conditional)
            for edge in graph.get_graph().edges
        )
    )
    return tuple(nodes), channels, edges, id(graph.checkpointer), id(graph.store)
