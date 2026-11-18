"""Independent native model scripts and explicit provider construction."""

import json
from dataclasses import dataclass
from uuid import uuid4
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from simulator import canonical
from contracts import Settings

SCENARIOS = (
    "investigate",
    "restart",
    "rollback",
    "scale",
    "stale-revision",
)


@dataclass(frozen=True)
class ModelSet:
    coordinator: BaseChatModel
    diagnostics: BaseChatModel
    remediation: BaseChatModel
    reporting: BaseChatModel


def call(name, **args):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": str(uuid4())}]
    )


def decoded(message):
    return json.loads(message.content)


class ScriptedModel(BaseChatModel):
    role: str
    scenario: str
    action_id: str

    @property
    def _llm_type(self):
        return "incident-scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)

    def _next(self, messages):
        outputs = [m for m in messages if isinstance(m, ToolMessage)]
        by_name = {}
        names = {
            call["id"]: call["name"]
            for message in messages
            for call in getattr(message, "tool_calls", ())
        }
        for message in outputs:
            by_name.setdefault(names.get(message.tool_call_id), []).append(message)
        human = next(m.content for m in messages if isinstance(m, HumanMessage))
        request = json.loads(human)
        service = request["service_id"]
        if self.role == "coordinator":
            if "write_todos" not in by_name:
                return call(
                    "write_todos",
                    todos=[
                        {
                            "content": f"Investigate {service}, remediate, and verify report",
                            "status": "in_progress",
                        }
                    ],
                )
            tasks = by_name.get("task", [])
            roles = ["diagnostics"]
            if self.scenario in ("restart", "rollback", "scale", "stale-revision"):
                roles.append("remediation")
            roles.append("reporting")
            if len(tasks) < len(roles):
                role = roles[len(tasks)]
                effects = []
                for task in tasks:
                    effects.extend(decoded(task).get("effects", []))
                return call(
                    "task",
                    subagent_type=role,
                    description=canonical(
                        {
                            "service_id": service,
                            "scenario": self.scenario,
                            "effects": effects,
                            "observed_revision": max(
                                (e["after_revision"] for e in effects),
                                default=max(
                                    (
                                        decoded(task).get("observed_revision", 0)
                                        for task in tasks
                                    ),
                                    default=0,
                                ),
                            ),
                        }
                    ),
                )
            if "write_file" not in by_name:
                return call(
                    "write_file",
                    file_path="/reports/incident.json",
                    content=tasks[-1].content,
                )
            return AIMessage(
                content=f"Report for {service} saved; runner must validate it against receipts."
            )
        if self.role != "reporting" and "read_deployments" not in by_name:
            return call("read_deployments", service_id=service)
        if self.role == "diagnostics":
            for name in ("read_logs", "read_metrics"):
                if name not in by_name:
                    return call(name, service_id=service)
        if self.role == "remediation":
            operation, arguments = {
                "restart": ("restart_replica", {"replica_id": "replica-1"}),
                "rollback": ("rollback_release", {"target_release": "v41"}),
                "scale": ("set_replica_count", {"replicas": 3}),
                "stale-revision": ("restart_replica", {"replica_id": "replica-1"}),
            }[self.scenario]
            if operation not in by_name:
                revision = decoded(by_name["read_deployments"][-1])["revision"]
                return call(
                    operation,
                    service_id=service,
                    expected_revision=revision,
                    action_id=self.action_id,
                    **arguments,
                )
            if self.scenario == "stale-revision" and len(by_name[operation]) == 1:
                return call(
                    operation,
                    service_id=service,
                    expected_revision=0,
                    action_id=self.action_id + "-stale",
                    **arguments,
                )
            return AIMessage(
                content=canonical({"effects": [decoded(m) for m in by_name[operation]]})
            )
        if self.role == "reporting":
            return AIMessage(
                content=canonical(
                    {
                        "schema_version": 1,
                        "service_id": service,
                        "observed_revision": request["observed_revision"],
                        "actions": [
                            {
                                "status": "completed",
                                "mutation": e["mutation"],
                                "effect_id": e["effect_id"],
                            }
                            for e in request["effects"]
                        ],
                    }
                )
            )
        return AIMessage(
            content=canonical(
                {
                    "service_id": service,
                    "observed_revision": decoded(by_name["read_deployments"][-1])[
                        "revision"
                    ],
                    "facts": [decoded(m) for m in outputs],
                    "effects": [],
                }
            )
        )


def build_models(*, settings: Settings, scenario: str) -> ModelSet:
    if settings.profile != "default":
        raise ValueError("invalid_profile")
    roles = ("coordinator", "diagnostics", "remediation", "reporting")
    if scenario not in SCENARIOS:
        raise ValueError("invalid_scenario")
    if settings.mode == "scripted":
        return ModelSet(
            *(
                ScriptedModel(
                    name=f"incident-{role}",
                    role=role,
                    scenario=scenario,
                    action_id=str(uuid4()),
                )
                for role in roles
            )
        )
    # Provider selection is explicit and stays local to this example.
    if (
        settings.mode != "provider"
        or not settings.provider_model
        or not settings.provider_credential
    ):
        raise ValueError("provider_configuration_required")
    provider, separator, model = settings.provider_model.partition(":")
    if provider not in {"anthropic", "openai"} or not separator or not model.strip():
        raise ValueError("unsupported_provider")
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        model_type = ChatAnthropic
    else:
        try:
            from langchain_openai import ChatOpenAI
        except ModuleNotFoundError:
            raise ValueError("provider_dependency_missing") from None

        model_type = ChatOpenAI
    options = dict(model=model, api_key=settings.provider_credential, max_retries=0)
    if provider == "openai":
        options["base_url"] = "https://api.openai.com/v1"
    return ModelSet(*(model_type(**options) for _ in range(len(roles))))
