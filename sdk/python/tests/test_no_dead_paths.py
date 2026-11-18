"""Structural contract for the machine-only core distribution."""

import importlib
import pkgutil
import re
from pathlib import Path

import pytest

import openeight
import openeight.telemetry as telemetry
from openeight import BufferedObserveWriter, Client

EXPECTED_ROOT_EXPORTS = (
    "OpenEightConfig",
    "OpenEightConfigDict",
    "ModelVariant",
    "RuntimeModelConfig",
    "CompatibilityIssue",
    "CompatibilityReport",
    "LifecycleStatus",
    "ExecutionOutcome",
    "OperationStatus",
    "CleanupIncompleteError",
    "ResourceCleanupError",
    "AgentIntegration",
    "Governance",
    "TelemetryContentContext",
    "DeliveryConfig",
    "DeliveryStatus",
    "Client",
    "DecisionResult",
    "Event",
    "GovernedRun",
    "PreparedDecision",
    "prepare_decision",
    "RunIdentity",
    "RunCheckpoint",
    "prepare_run_identity",
    "BufferedObserveWriter",
    "ObserveRunHandle",
    "FieldSelector",
    "PolicyInputRequirement",
    "ToolRegistration",
    "Tool",
    "ToolVersion",
    "RegistrationResult",
    "ToolReadiness",
    "AgentReadiness",
    "SDKError",
    "ConfigurationError",
    "APIError",
    "ProtocolError",
    "TransportError",
    "BlockedError",
    "ApprovalTimeout",
    "ObserveDeliveryError",
    "ObserveQueueFullError",
    "ObserveRunCompletedError",
)

EXPECTED_MODULES = {
    "_direct",
    "_telemetry_validation",
    "decisions",
    "_credential_binding",
    "configuration",
    "compatibility",
    "checkpoints",
    "lifecycle",
    "integrations",
    "_durable_sqlite",
    "_local_storage",
    "_managed_types",
    "managed",
    "_framework",
    "_sdk_version",
    "content",
    "_content_capture",
    "governance",
    "deepagents",
    "mcp",
    "_adapter_http",
    "_connect_errors",
    "_error_catalog",
    "_json",
    "_validation",
    "_semver",
    "_time",
    "_delivery",
    "langchain",
    "delivery",
    "_protocol_constants",
    "_forbidden_fields",
    "client",
    "errors",
    "run",
    "telemetry",
    "integration",
    "tools",
    "types",
}

REMOVED_ROOT_NAMES = {
    "OpenEightClient",
    "OnboardedAgent",
    "onboard",
    "Verdict",
    "Decision",
    "RunContext",
    "ToolCall",
    "HumanActor",
    "MachineActor",
}

REMOVED_CLIENT_METHODS = {
    "ready",
    "provision_agent",
    "issue_credential",
    "task",
    "list_audit_entries",
    "list_audit_entries_page",
    "get_costs",
    "export_bundle",
}

REMOVED_SURFACE_PATTERNS = {
    "OpenEightClient": re.compile(r"\bOpenEightClient\b"),
    "OnboardedAgent": re.compile(r"\bOnboardedAgent\b"),
    "Verdict": re.compile(r"\bVerdict\b"),
    "RunContext": re.compile(r"\bRunContext\b"),
    "ToolCall": re.compile(r"\bToolCall\b"),
    "onboard_call": re.compile(r"\bonboard\s*\("),
    "provision_agent": re.compile(r"\bprovision_agent\b"),
    "issue_credential": re.compile(r"\bissue_credential\b"),
    "agent_credential": re.compile(r"\bagent_credential\b"),
    "list_audit_entries": re.compile(r"\blist_audit_entries\b"),
    "list_audit_entries_page": re.compile(r"\blist_audit_entries_page\b"),
    "get_costs": re.compile(r"\bget_costs\b"),
    "export_bundle": re.compile(r"\bexport_bundle\b"),
    "task_method": re.compile(r"\.task\s*\("),
    "ready_method": re.compile(r"\.ready\s*\("),
    "guarded": re.compile(r"\bguarded\b"),
    "current_decision": re.compile(r"\bcurrent_decision\b"),
}

def test_root_exports_are_the_exact_machine_sdk_contract():
    assert tuple(openeight.__all__) == EXPECTED_ROOT_EXPORTS
    assert all(hasattr(openeight, name) for name in EXPECTED_ROOT_EXPORTS)
    assert REMOVED_ROOT_NAMES.isdisjoint(vars(openeight))


def test_distribution_contains_only_machine_sdk_modules():
    modules = {module.name for module in pkgutil.iter_modules(openeight.__path__)}
    assert modules == EXPECTED_MODULES

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("openeight.onboard")


def test_client_has_no_legacy_control_or_administration_plane():
    assert REMOVED_CLIENT_METHODS.isdisjoint(vars(Client))


def test_telemetry_has_one_deadline_aware_sender_contract():
    assert not hasattr(telemetry, "LegacyObserveSender")
    assert not hasattr(telemetry, "LegacyCompletedObserveSender")
    assert not hasattr(telemetry, "_timeout_mode")
    assert not hasattr(BufferedObserveWriter, "submit")


def test_active_documentation_uses_current_public_apis():
    repository = Path(__file__).resolve().parents[2]
    documentation = repository / "python/README.md"
    contents = documentation.read_text(encoding="utf-8")
    findings = {
        name: len(pattern.findall(contents))
        for name, pattern in REMOVED_SURFACE_PATTERNS.items()
        if pattern.search(contents)
    }
    assert findings == {}


def test_package_metadata_includes_langchain_by_default():
    import tomllib

    metadata_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    metadata = tomllib.loads(metadata_path.read_text(encoding="utf-8"))

    assert metadata["project"]["name"] == "openeight"
    assert metadata["project"]["version"] == "0.0.1"
    assert metadata["project"]["requires-python"] == ">=3.11,<3.16"
    assert metadata["project"]["dependencies"] == [
        "httpx>=0.27",
        "certifi>=2026.10.19",
        "langchain==1.4.3",
        "langchain-core==1.6.7",
        "langgraph==1.2.14",
        "langgraph-prebuilt==1.1.0",
        "langgraph-checkpoint==4.3.0",
        "langgraph-sdk>=0.4.6,<1",
        "urllib3>=2.8.0,<3",
        "packaging>=24",
        "jsonschema==4.26.0",
        "pydantic>=2.14.2; python_version >= '3.15'",
        "ormsgpack>=1.13.0; python_version >= '3.15'",
        "orjson>=3.13.0; python_version >= '3.15'",
        "uuid-utils>=0.19.0; python_version >= '3.15'",
    ]
    assert metadata["project"]["optional-dependencies"]["langchain"] == []
    assert openeight.__version__ == "0.0.1"
