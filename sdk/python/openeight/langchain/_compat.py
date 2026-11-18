"""Version-sensitive inspection of native create_agent graphs."""

from importlib.metadata import PackageNotFoundError, requires, version

from packaging.requirements import Requirement

from openeight.errors import ConfigurationError


def copy_tool_call_request(request, tool_call):
    from ._frameworks._requests import copy_tool_call_request as copy

    return copy(request, tool_call)


def validate_versions():
    try:
        for text in (
            "langchain>=1.3.18,<=1.4.3",
            "langchain-core>=1.6.2,<2",
            "langgraph>=1.2.11,<=1.2.14",
            "langgraph-prebuilt>=1.1.0,<2",
            "langgraph-checkpoint>=4.2.0,<5",
            "langgraph-sdk>=0.4.6,<1",
            "urllib3>=2.8.0,<3",
        ):
            requirement = Requirement(text)
            if not requirement.specifier.contains(
                version(requirement.name), prereleases=False
            ):
                raise ValueError()
        for name in (
            "langchain",
            "langchain-core",
            "langgraph",
            "langgraph-prebuilt",
            "langgraph-checkpoint",
        ):
            for text in requires(name) or ():
                requirement = Requirement(text)
                if requirement.marker is not None and not requirement.marker.evaluate(
                    {"extra": ""}
                ):
                    continue
                if not requirement.specifier.contains(
                    version(requirement.name), prereleases=False
                ):
                    raise ValueError()
    except (PackageNotFoundError, ValueError):
        raise ConfigurationError("unsupported_framework_version") from None


def inspect_agent(graph, middleware=None):
    from ._frameworks._inspection import inspect_agent as inspect

    return inspect(graph, middleware, validate=validate_versions)


from ._frameworks._inspection import AgentCapabilities  # noqa: F401, E402
