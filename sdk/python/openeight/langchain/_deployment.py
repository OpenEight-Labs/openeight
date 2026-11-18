"""Immutable application-owned binding for measured LangChain durable graphs."""

import hashlib
import json
import platform
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version
from uuid import UUID

from openeight._credential_binding import credential_binding
from openeight._framework._measurement import plain, qualified
from openeight._sdk_version import resolve_sdk_version
from openeight._time import utc_timestamp
from openeight.errors import ConfigurationError

_CURRENT_DEPLOYMENT = ContextVar("openeight_langchain_deployment", default=None)


@dataclass(frozen=True)
class _RecordScope:
    deployment_id: str
    store: object = field(repr=False)


@contextmanager
def record_scope(owner):
    inherited = _CURRENT_DEPLOYMENT.get()
    approval = owner.control.durable
    binding = getattr(approval, "deployment_binding", None)
    if inherited is not None and inherited.store is approval.store and binding is None:
        mismatch()
    token = _CURRENT_DEPLOYMENT.set(None)
    try:
        yield
    finally:
        _CURRENT_DEPLOYMENT.reset(token)


def mismatch():
    raise ConfigurationError("durable_deployment_mismatch")


@contextmanager
def ownership(binding):
    manager, failed, busy = None, False, False
    try:
        manager = binding.acquire_deployment()
        manager.__enter__()
    except ConfigurationError as error:
        failed, busy = (
            True,
            error.reason in {"durable_deployment_busy", "integration_busy"},
        )
    except Exception:
        failed = True
    if failed:
        if busy:
            raise ConfigurationError("durable_deployment_busy")
        mismatch()
    primary = None
    token = _CURRENT_DEPLOYMENT.set(
        _RecordScope(binding.deployment_id, binding.execution_store)
    )
    try:
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        failed = False
        try:
            manager.__exit__(None, None, None)
        except Exception:
            failed = True
        finally:
            _CURRENT_DEPLOYMENT.reset(token)
        if failed and primary is None:
            mismatch()


def validate_binding(binding, store):
    failed = False
    try:
        identifier, revision = binding.deployment_id, binding.revision
        if (
            type(identifier) is not str
            or str(UUID(identifier)) != identifier
            or type(revision) is not str
            or not revision
            or len(revision.encode()) > 4096
            or "\0" in revision
            or binding.execution_store is not store
            or any(
                not callable(getattr(binding, method, None))
                for method in (
                    "acquire_deployment",
                    "deployment",
                    "is_empty",
                    "bind_deployment",
                )
            )
        ):
            failed = True
    except Exception:
        failed = True
    if failed:
        raise ConfigurationError("invalid_integration_configuration")


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _unique(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def current_manifest(owner):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    approval = owner.control.durable
    binding = approval.deployment_binding
    validate_binding(binding, approval.store)
    saver = owner._graph.checkpointer
    if (
        type(saver) is not AsyncSqliteSaver
        or type(saver.serde) is not JsonPlusSerializer
        or saver.serde.pickle_fallback is not False
    ):
        mismatch()
    verify = getattr(owner._compatibility, "_verify_configuration", None)
    if verify is None or owner._composition is None:
        mismatch()
    nodes = plain(verify())
    if nodes != owner._composition.get("checkpoint_nodes"):
        mismatch()
    sdk = resolve_sdk_version()
    if sdk is None:
        mismatch()
    selection = owner._compatibility_selection
    packages = dict(selection.installed.packages)
    packages["python"] = platform.python_version()
    packages.update(
        {
            "openeight": sdk,
            "langgraph-checkpoint-sqlite": version("langgraph-checkpoint-sqlite"),
            "aiosqlite": version("aiosqlite"),
        }
    )
    packages["adapter_revision"] = selection.implementation_revision
    packages["qualification_id"] = selection.qualification_id
    agent, endpoint, credential = owner.binding[:3]
    variant = owner._composition.get("runtime_model_configuration")
    manifest = {
        "format_version": 1,
        "purpose": "langchain-durable",
        "deployment_id": binding.deployment_id,
        "identity": {
            "kind": "credential_bound",
            "endpoint": endpoint,
            "agent_name": agent,
            "credential_binding_sha256": credential_binding(credential),
        },
        "graph_revision": binding.revision,
        "packages": packages,
        "saver": {
            "kind": "async-sqlite",
            "checkpoint_format": "langgraph-checkpoint-v4",
            "serializer": qualified(saver.serde),
            "pickle_fallback": False,
        },
        "variant": {
            "name": variant["name"] if variant is not None else None,
            "nodes": nodes,
        },
        "execution": {
            "max_model_calls": approval.max_model_calls,
            "max_tool_attempts": approval.max_tool_attempts,
        },
    }
    raw = canonical(manifest)
    if len(raw.encode()) > 1024 * 1024:
        mismatch()
    return raw


def validate_enrollment(value, expected, identifier):
    from .durable import DurableDeploymentEnrollment

    failed = False
    try:
        if (
            type(value) is not DurableDeploymentEnrollment
            or value.deployment_id != identifier
            or type(value.format_version) is not int
            or value.format_version != 1
        ):
            raise ValueError
        if (
            type(value.created_at) is not str
            or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
                value.created_at,
            )
            is None
        ):
            raise ValueError
        datetime.fromisoformat(value.created_at.replace("Z", "+00:00"))
        if (
            type(value.manifest_json) is not str
            or len(value.manifest_json.encode()) > 1024 * 1024
        ):
            raise ValueError
        parsed = json.loads(value.manifest_json, object_pairs_hook=_unique)
        if (
            canonical(parsed) != value.manifest_json
            or value.manifest_json != expected
            or value.manifest_sha256 != hashlib.sha256(expected.encode()).hexdigest()
        ):
            raise ValueError
    except Exception:
        failed = True
    if failed:
        mismatch()


def validate_current(owner, *, allow_unbound=False):
    failed, value = False, None
    try:
        binding = owner.control.durable.deployment_binding
        expected = current_manifest(owner)
        value = binding.deployment()
        if value is None:
            if not allow_unbound:
                mismatch()
        else:
            validate_enrollment(value, expected, binding.deployment_id)
    except Exception:
        failed = True
    if failed:
        mismatch()
    return value


async def authenticate(owner, *, deadline=None):
    await owner._run_client_call(
        lambda: owner.client.get_tool_readiness([]), deadline=deadline
    )


async def validate_admission(owner, *, bind=False, deadline=None):
    from .durable import DurableDeploymentEnrollment

    binding = owner.control.durable.deployment_binding
    value = validate_current(owner, allow_unbound=bind)
    await authenticate(owner, deadline=deadline)
    if value is None:
        failed, empty = False, None
        try:
            empty = binding.is_empty()
        except Exception:
            failed = True
        if failed or empty is not True:
            mismatch()
        iterator = owner._graph.checkpointer.alist(None, limit=1)
        failed, checkpoint = False, None
        try:
            try:
                checkpoint = await anext(iterator, None)
            finally:
                await iterator.aclose()
        except Exception:
            failed = True
        if failed or checkpoint is not None:
            mismatch()
        expected = current_manifest(owner)
        value = DurableDeploymentEnrollment(
            deployment_id=binding.deployment_id,
            format_version=1,
            created_at=utc_timestamp(),
            manifest_json=expected,
            manifest_sha256=hashlib.sha256(expected.encode()).hexdigest(),
        )
        failed = False
        try:
            binding.bind_deployment(value)
        except Exception:
            failed = True
        if failed:
            mismatch()
        stored = validate_current(owner)
        if stored != value:
            mismatch()
    if owner._defer_catalog or bind:
        owner._defer_catalog = False
        owner.catalog_delivery.request_refresh()


def checked_record(
    value,
    *,
    kind=None,
    thread_id=None,
    command_id=None,
    tool_call_id=None,
    decision_id=None,
    run_id=None,
    allow_none=False,
):
    scope = _CURRENT_DEPLOYMENT.get()
    if scope is None:
        return value
    identifier = scope.deployment_id
    from .durable import (
        DurableAttempt,
        DurableCommand,
        DurableObserveRecord,
        DurableThread,
    )

    if type(value) in (tuple, list):
        for item in value:
            checked_record(
                item,
                kind=kind,
                thread_id=thread_id,
                command_id=command_id,
                tool_call_id=tool_call_id,
                decision_id=decision_id,
                run_id=run_id,
            )
        return value
    if value is None:
        if not allow_none:
            mismatch()
        return value
    expected = {
        "thread": DurableThread,
        "command": DurableCommand,
        "attempt": DurableAttempt,
        "observation": DurableObserveRecord,
    }.get(kind)
    if expected is not None and type(value) is not expected:
        mismatch()
    if type(value) is DurableObserveRecord:
        if value.thread_id != thread_id or value.status != "pending":
            mismatch()
        try:
            payload = json.loads(value.payload_json)
        except (ValueError, TypeError):
            payload = None
        if type(payload) is not dict or value.kind not in {
            "run_active",
            "event",
            "run_complete",
        }:
            mismatch()
        if value.kind == "event":
            if payload.get("run_id") != run_id or payload.get("id") != value.id:
                mismatch()
        elif payload.get("id") != run_id:
            mismatch()
        return value
    if (
        type(value) not in (DurableAttempt, DurableCommand, DurableThread)
        or value.deployment_id != identifier
    ):
        mismatch()
    actual_thread = value.id if type(value) is DurableThread else value.thread_id
    if (
        thread_id is not None
        and actual_thread != thread_id
        or command_id is not None
        and value.id != command_id
        or tool_call_id is not None
        and value.tool_call_id != tool_call_id
        or decision_id is not None
        and value.decision_id != decision_id
    ):
        mismatch()
    return value
