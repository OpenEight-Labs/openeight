"""Framework-neutral deterministic control/model fixtures for adapter contracts."""

from dataclasses import replace

from langchain_core.messages import AIMessage
from test_enforcement_facade import GovernedClient
from test_framework_boundary import ToolModel

from openeight import OpenEightConfig


class PolicyClient(GovernedClient):
    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.integration = kwargs.get("integration")
        self.instances.append(self)
        self.contract_names = {}
        self.reject_name = None
        self.readiness_calls = 0
        self.closed = False

    def register_tools(self, facts, **kwargs):
        self.registrations.append(facts)
        from uuid import uuid4

        from openeight import RegistrationResult, Tool, ToolVersion

        results = []
        for index, fact in enumerate(facts):
            identity = str(uuid4())
            self.contract_names[identity] = fact.name
            version = ToolVersion(
                str(uuid4()),
                identity,
                "config:test",
                "sha256:" + "0" * 64,
                fact.input_schema,
                None if self.pending else "sha256:" + "1" * 64,
                "2026-10-01T00:00:00Z",
            )
            results.append(
                RegistrationResult(
                    index,
                    "replayed",
                    "",
                    Tool(
                        identity,
                        fact.name,
                        fact.description,
                        "write",
                        True,
                        "test",
                        "effect",
                        "2026-10-01T00:00:00Z",
                    ),
                    version,
                    None,
                )
            )
        if not hasattr(self, "publications"):
            self.publications = {}
        self.publications.update({item.tool_version.id: item for item in results})
        return tuple(results)

    def decide(self, **request):
        result = super().decide(**request)
        if self.contract_names[request["tool_id"]] == self.reject_name:
            return replace(result, verdict="deny")
        return result

    def get_tool_readiness(self, ids, **kwargs):
        self.readiness_calls += 1
        return super().get_tool_readiness(ids, **kwargs)

    def close(self):
        self.closed = True


def config(**kwargs):
    return OpenEightConfig(
        agent_name="test",
        endpoint="http://localhost:7777",
        credential="ctk_secret",
        **kwargs,
    )


def call(name="effect", value="original", **args):
    return AIMessage(
        content="",
        tool_calls=[dict(name=name, args=args or {"value": value}, id="call")],
    )


def model(*messages):
    return ToolModel(responses=[*messages, AIMessage(content="done")])
