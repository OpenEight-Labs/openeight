"""Validate immutable deployment facts before any read-only catalog admission."""

import platform
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from openeight._sdk_version import resolve_sdk_version
from openeight.client import _validate_base_url
from openeight.errors import ConfigurationError
from openeight.langchain._catalog import CURRENT_INITIAL_BIND, report_catalog
from openeight.langchain._injected import hidden_action_key

from . import _deployment as model
from ._deployment import BoundCatalogSnapshot, BoundExecutionContract


@dataclass(frozen=True)
class LocalDeploymentContext:
    revision: str
    configuration_sha256: str


def _runtime_packages():
    names = (
        "deepagents",
        "langchain",
        "langchain-core",
        "langgraph",
        "langgraph-prebuilt",
        "langgraph-checkpoint",
        "langgraph-checkpoint-sqlite",
        "aiosqlite",
    )
    try:
        packages = {name: version(name) for name in names}
    except PackageNotFoundError:
        raise ConfigurationError("durable_dependencies_missing") from None
    label = resolve_sdk_version()
    if not label:
        raise ConfigurationError("durable_deployment_mismatch")
    return {**packages, "openeight": label, "python": platform.python_version()}


def _current_identity(owner, deployment):
    expected = model.identity(deployment.identity_json)
    kind = (
        "verified_issuance"
        if owner._durable_deployment is not None
        else "credential_bound"
    )
    if (
        expected["kind"] != kind
        or model.credential_binding(owner.binding[2])
        != deployment.credential_binding_sha256
        or _validate_base_url(owner.binding[1]) != expected["endpoint"]
        or owner.binding[0] != expected["agent_name"]
    ):
        raise ConfigurationError("durable_deployment_mismatch")
    return expected


def _current_runtime(owner, deployment):
    model.uuid(deployment.deployment_id)
    model.digest(deployment.credential_binding_sha256)
    if type(deployment.format_version) is not int or deployment.format_version != 1:
        raise ConfigurationError("durable_deployment_mismatch")
    local = owner._local_deployment
    advanced = owner._durable_deployment
    if (local is None) == (advanced is None):
        raise ConfigurationError("durable_deployment_mismatch")
    if advanced is not None and advanced.deployment_id != deployment.deployment_id:
        raise ConfigurationError("durable_deployment_mismatch")
    selection = owner._compatibility_selection
    if not selection or not selection.checkpoint_format:
        raise ConfigurationError("durable_deployment_mismatch")
    composition = owner._composition
    if not composition or set(composition) != {
        "topology",
        "backend",
        "saver",
        "catalog_naming",
        "configuration_sha256",
        "effective_configuration",
        "effective_configuration_sha256",
    }:
        raise ConfigurationError("durable_deployment_mismatch")
    if composition["backend"] != {"kind": "state", "format_version": 1} or composition[
        "saver"
    ] != {"kind": "async-sqlite", "format_version": 1, "pickle_fallback": False}:
        raise ConfigurationError("durable_deployment_mismatch")
    model.digest(
        local.configuration_sha256
        if local is not None
        else advanced.configuration_sha256
    )
    measurement_failed = False
    measured = None
    try:
        measured = owner._compatibility.measure_configuration()
    except Exception:
        measurement_failed = True
    if measurement_failed:
        raise ConfigurationError("durable_deployment_mismatch")
    from openeight._framework._measurement import plain

    if (
        measured.sha256 != composition["effective_configuration_sha256"]
        or plain(measured.descriptor) != composition["effective_configuration"]
    ):
        raise ConfigurationError("durable_deployment_mismatch")

    current = dict(
        format_version=2,
        integration="deepagents",
        deployment_id=deployment.deployment_id,
        identity=_current_identity(owner, deployment),
        application={"kind": "revision", "value": local.revision}
        if local is not None
        else {"kind": "artifact_sha256", "value": advanced.application_build_sha256},
        configuration_sha256=local.configuration_sha256
        if local is not None
        else advanced.configuration_sha256,
        packages=_runtime_packages(),
        backend=composition["backend"],
        saver=composition["saver"],
        compatibility={
            "implementation_id": selection.implementation_id,
            "implementation_revision": selection.implementation_revision,
            "qualification_id": selection.qualification_id,
            "checkpoint_format": selection.checkpoint_format,
        },
        catalog_naming=composition["catalog_naming"],
        effective_configuration=plain(measured.descriptor),
        effective_configuration_sha256=measured.sha256,
    )
    model.unbound_manifest(
        model.canonical({**current, "topology": composition["topology"]})
    )
    if (
        local is not None
        and local.configuration_sha256 != composition["configuration_sha256"]
    ):
        raise ConfigurationError("durable_deployment_mismatch")
    return current


def _validate_local_manifest_identity(owner, deployment):
    expected = model.manifest(deployment.manifest_json)
    current = _current_runtime(owner, deployment)
    if any(expected[k] != v for k, v in current.items()):
        raise ConfigurationError("durable_deployment_mismatch")
    topology = owner._composition["topology"]
    if len(expected["topology"]) != len(topology):
        raise ConfigurationError("durable_deployment_mismatch")
    owner.catalog.validate()
    for stored, live in zip(expected["topology"], topology):
        if (
            stored["path"] != live["path"]
            or stored["delegates_to"] != live["delegates_to"]
            or len(stored["tools"]) != len(live["tools"])
        ):
            raise ConfigurationError("durable_deployment_mismatch")
        for old, new in zip(stored["tools"], live["tools"]):
            if any(
                old[key] != new[key]
                for key in ("native_name", "catalog_name", "input_schema_sha256")
            ):
                raise ConfigurationError("durable_deployment_mismatch")
            binding = owner.catalog.resolve(tuple(live["path"]), new["native_name"])
            if old["recovery_kind"] not in _possible_kinds(owner, binding):
                raise ConfigurationError("durable_deployment_mismatch")
    return expected


def _possible_kinds(owner, binding):
    generated = owner._compatibility.generated_recovery_kind(binding)
    if generated == "delegation":
        return {"delegation"}
    if generated == "state_write":
        return {"state_write"}
    return {"read", "business_write"} if hidden_action_key(binding.tool) else {"read"}


def _contract(owner, binding, ready, *, tool_id, version_id):
    if (
        ready.name != binding.facts.name
        or ready.tool_id != tool_id
        or ready.tool_version_id != version_id
        or ready.registration_status != "registered"
        or not ready.eligible
        or not ready.policy_ready
    ):
        raise ConfigurationError("governance_not_ready")
    if ready.reason == "read_only":
        kind = "read"
    else:
        if (
            not ready.governed
            or ready.reason != "ready"
            or not ready.policy_contract_set_digest
            or not ready.policy_input_contracts
        ):
            raise ConfigurationError("governance_unconfigured")
        generated = owner._compatibility.generated_recovery_kind(binding)
        if generated is not None:
            kind = generated
        elif hidden_action_key(binding.tool):
            kind = "business_write"
        else:
            raise ConfigurationError("unsupported_tool_definition")
    if kind not in _possible_kinds(owner, binding):
        raise ConfigurationError("unsupported_tool_definition")
    if kind == "read" and ready.governance_revision_id is None:
        raise ConfigurationError("governance_not_ready")
    return BoundExecutionContract(
        binding.child_path,
        binding.native_name,
        binding.facts.name,
        tool_id,
        version_id,
        binding.schema_bytes.decode("utf-8"),
        kind,
        ready,
    )


async def _read_bound_catalog(owner, deployment, *, deadline=None):
    expected = model.manifest(deployment.manifest_json)
    tools = [
        (tuple(node["path"]), tool)
        for node in expected["topology"]
        for tool in node["tools"]
    ]
    result = await owner._run_client_call(
        lambda: owner.client.get_tool_readiness(
            [tool["tool_version_id"] for _, tool in tools]
        ),
        deadline=deadline,
    )
    by_version = {ready.tool_version_id: ready for ready in result.tools}
    if len(by_version) != len(tools) or len(result.tools) != len(tools):
        raise ConfigurationError("governance_not_ready")
    contracts = []
    for path, tool in tools:
        binding = owner.catalog.resolve(path, tool["native_name"])
        if binding is None:
            raise ConfigurationError("durable_deployment_mismatch")
        ready = by_version.get(tool["tool_version_id"])
        if ready is None:
            raise ConfigurationError("governance_not_ready")
        contract = _contract(
            owner,
            binding,
            ready,
            tool_id=tool["tool_id"],
            version_id=tool["tool_version_id"],
        )
        if contract.recovery_kind != tool["recovery_kind"]:
            raise ConfigurationError("durable_deployment_mismatch")
        contracts.append(contract)
    return BoundCatalogSnapshot(tuple(contracts)), result


def _candidate_deployment_manifest(owner, deployment, catalog):
    value = _current_runtime(owner, deployment)
    contracts = {(c.child_path, c.native_name): c for c in catalog.contracts}
    topology = []
    for node in owner._composition["topology"]:
        tools = []
        for tool in node["tools"]:
            contract = contracts.get((tuple(node["path"]), tool["native_name"]))
            if contract is None:
                raise ConfigurationError("durable_deployment_mismatch")
            tools.append(
                {
                    **tool,
                    "tool_id": contract.tool_id,
                    "tool_version_id": contract.tool_version_id,
                    "recovery_kind": contract.recovery_kind,
                }
            )
        topology.append(
            {"path": node["path"], "delegates_to": node["delegates_to"], "tools": tools}
        )
    if len(contracts) != sum(len(n["tools"]) for n in topology):
        raise ConfigurationError("durable_deployment_mismatch")
    value["topology"] = topology
    raw = model.canonical(value)
    model.manifest(raw)
    return raw


async def _validate_durable_admission(owner, *, bind=False, deadline=None):
    owner._ensure_open()
    if (
        owner._integration != "deepagents"
        or owner.control is None
        or owner.control.durable is None
    ):
        raise ConfigurationError("unsupported_execution_method")
    store = owner.control.durable.store
    deployment = store.deployment()
    _current_runtime(owner, deployment)
    if deployment.manifest_json is not None:
        _validate_local_manifest_identity(owner, deployment)
        catalog, readiness = await _read_bound_catalog(
            owner, deployment, deadline=deadline
        )
    else:
        if not bind:
            raise ConfigurationError("durable_deployment_unbound")
        owner.catalog.validate()
        # Authentication precedes even the first empty saver read.
        await owner._run_client_call(lambda: owner.client.get_tool_readiness([]))
        async for _ in owner._graph.checkpointer.alist(None, limit=1):
            raise ConfigurationError("durable_checkpoint_conflict")
        token = CURRENT_INITIAL_BIND.set(owner)
        try:
            # Initial binding must never obtain CAS authority to replace an existing schema.
            owner.catalog_delivery.publisher.current = {}
            results = await owner._run_client_call(
                lambda: report_catalog(
                    owner.catalog_delivery.publisher, owner.catalog.facts
                )
            )
        finally:
            CURRENT_INITIAL_BIND.reset(token)
        by_name = {result.tool.name: result for result in results}
        if len(by_name) != len(owner.catalog.facts):
            raise ConfigurationError("governance_unconfigured")
        versions = [result.tool_version.id for result in results]
        fresh = await owner._run_client_call(
            lambda: owner.client.get_tool_readiness(versions)
        )
        ready = {item.tool_version_id: item for item in fresh.tools}
        if len(ready) != len(results):
            raise ConfigurationError("governance_not_ready")
        contracts = []
        for binding in owner.catalog.native.values():
            result = by_name[binding.facts.name]
            item = ready.get(result.tool_version.id)
            if item is None:
                raise ConfigurationError("governance_not_ready")
            contracts.append(
                _contract(
                    owner,
                    binding,
                    item,
                    tool_id=result.tool.id,
                    version_id=result.tool_version.id,
                )
            )
        catalog = BoundCatalogSnapshot(tuple(contracts))
        readiness = fresh
    candidate = _candidate_deployment_manifest(owner, deployment, catalog)
    if deployment.manifest_json is None:
        store.bind_deployment(deployment.deployment_id, candidate)
    elif candidate != deployment.manifest_json:
        raise ConfigurationError("durable_deployment_mismatch")
    if store.deployment().manifest_json != candidate:
        raise ConfigurationError("durable_deployment_mismatch")
    owner._bound_catalog = catalog
    owner._bound_readiness = readiness
    return catalog
