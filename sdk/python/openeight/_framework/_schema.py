"""Dependency-free validation for the packaged runtime support declaration."""

import re

_FEATURES = {
    "managed_integrations",
    "ordinary",
    "sync",
    "stream",
    "checkpoint",
    "mcp",
    "durable",
    "local_durable",
}
_FIELDS = {
    "id",
    "integration",
    "python",
    "packages",
    "implementation_id",
    "implementation_revision",
    "feature_sets",
    "checkpoint_format",
}
_IMPLEMENTATIONS = {"langchain": "langchain-v1", "deepagents": "deepagents-v1"}
_PACKAGES = (
    "langchain",
    "langchain-core",
    "langgraph",
    "langgraph-prebuilt",
    "langgraph-checkpoint",
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate registry key")
        result[key] = value
    return result


def validate_registry(value):
    """Validate maintainer data before it can grant any runtime support."""
    if (
        type(value) is not dict
        or set(value) != {"format_version", "entries"}
        or type(value["format_version"]) is not int
        or value["format_version"] != 2
        or type(value["entries"]) is not list
        or not value["entries"]
    ):
        raise ValueError("invalid registry")
    ids = set()
    selectors = set()
    for row in value["entries"]:
        if type(row) is not dict or set(row) != _FIELDS:
            raise ValueError("invalid registry row")
        identifier = row["id"]
        if (
            type(identifier) is not str
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", identifier)
            or identifier in ids
        ):
            raise ValueError("invalid qualification id")
        ids.add(identifier)
        integration = row["integration"]
        if (
            integration not in _IMPLEMENTATIONS
            or row["implementation_id"] != _IMPLEMENTATIONS[integration]
            or type(row["implementation_revision"]) is not int
            or row["implementation_revision"] not in (1, 2)
        ):
            raise ValueError("unknown implementation")
        for field in ("python",):
            items = row[field]
            if (
                type(items) is not list
                or not items
                or any(type(s) is not str or not s or not s.isascii() for s in items)
                or len(set(items)) != len(items)
            ):
                raise ValueError("invalid registry list")
        if any(not re.fullmatch(r"3\.(11|12|13|14|15)", p) for p in row["python"]):
            raise ValueError("invalid Python version")
        packages = row["packages"]
        if (
            type(packages) is not dict
            or not set(_PACKAGES) <= set(packages)
            or (integration == "deepagents" and "deepagents" not in packages)
            or any(
                type(k) is not str
                or type(v) is not str
                or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", v)
                for k, v in packages.items()
            )
        ):
            raise ValueError("invalid package versions")
        checkpoint = row["checkpoint_format"]
        if checkpoint is not None and (type(checkpoint) is not str or not checkpoint):
            raise ValueError("invalid checkpoint format")
        combinations = row["feature_sets"]
        if type(combinations) is not list or not combinations:
            raise ValueError("invalid feature sets")
        previous = set()
        for features in combinations:
            if (
                type(features) is not list
                or not features
                or any(type(f) is not str or f not in _FEATURES for f in features)
                or features != sorted(set(features))
                or tuple(features) in previous
            ):
                raise ValueError("invalid feature combination")
            previous.add(tuple(features))
            if (
                set(features) & {"checkpoint", "durable", "local_durable"}
                and not checkpoint
            ):
                raise ValueError("checkpoint feature without format")
            if "durable" in features and not {
                "langgraph-checkpoint-sqlite",
                "aiosqlite",
            } <= set(packages):
                raise ValueError("durable packages missing")
            for python in row["python"]:
                selector = (
                    integration,
                    python,
                    tuple(sorted(packages.items())),
                    tuple(features),
                )
                if selector in selectors:
                    raise ValueError("ambiguous qualification")
                selectors.add(selector)
    # Different closures with extra optional packages must not overlap for a feature.
    for index, left in enumerate(value["entries"]):
        for right in value["entries"][index + 1 :]:
            if (
                left["integration"] == right["integration"]
                and set(left["python"]) & set(right["python"])
                and set(map(tuple, left["feature_sets"]))
                & set(map(tuple, right["feature_sets"]))
                and all(
                    left["packages"][k] == right["packages"][k]
                    for k in set(left["packages"]) & set(right["packages"])
                )
            ):
                raise ValueError("ambiguous qualification")
    return value
