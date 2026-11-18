"""Startup schema publication through the real client without graph execution."""

import asyncio
import json
import threading
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from test_framework_boundary import ToolModel, fixture

from openeight import (
    APIError,
    Client,
    ConfigurationError,
    DeliveryConfig,
    TransportError,
)
from _binding_helpers import instrument
from openeight.langchain import _catalog
from openeight.langchain._authorization import OpenEightMiddleware
from openeight.langchain import _owner as implementation


def registration_response(request, statuses=None, *, governed=False):
    facts = json.loads(request.content)["tools"]
    statuses = statuses or ["registered"] * len(facts)
    results = []
    for index, (fact, status) in enumerate(zip(facts, statuses)):
        result = {"request_index": index, "status": status, "reason": ""}
        if status in {"registered", "replayed"}:
            tool_id = str(uuid5(NAMESPACE_URL, fact["name"]))
            version_id = str(
                uuid5(NAMESPACE_URL, fact["name"] + fact["input_schema_json"])
            )
            result.update(
                tool={
                    "id": tool_id,
                    "name": fact["name"],
                    "description": fact["description"],
                    "capability": "write" if governed else "",
                    "governed": governed,
                    "connector": "test" if governed else "",
                    "action": "echo" if governed else "",
                    "created_at": "2026-10-06T00:00:00Z",
                },
                tool_version={
                    "id": version_id,
                    "tool_id": tool_id,
                    "version": "schema:test",
                    "content_digest": "sha256:" + "0" * 64,
                    "input_schema_json": fact["input_schema_json"],
                    "tool_contract_digest": "sha256:" + "1" * 64 if governed else None,
                    "created_at": "2026-10-06T00:00:00Z",
                },
                readiness={
                    "request_index": index,
                    "name": fact["name"],
                    "tool_id": tool_id,
                    "tool_version_id": version_id,
                    "registration_status": "registered",
                    "governed": governed,
                    "policy_ready": governed,
                    "eligible": governed,
                    "reason": "",
                },
            )
        results.append(result)
    return httpx.Response(200, json={"results": results})


@pytest.fixture
def setup_owner(monkeypatch):
    owners, requests = [], []
    async_client = httpx.AsyncClient

    def setup_owner(handler=None, *, graph=None, enforcing=False, delivery=None):
        def send(request):
            if request.url.path.endswith("/GetToolReadiness"):
                return httpx.Response(
                    200,
                    json={
                        "readiness": {
                            "tools": [],
                            "fully_registered": True,
                            "registration_reason": "ready",
                            "fully_policy_ready": True,
                            "policy_reason": "ready",
                        }
                    },
                )
            requests.append(request)
            if request.url.path.endswith("/RegisterTools"):
                return handler(request) if handler else registration_response(request)
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    kind: [
                        {"id": item["id"], "replayed": False}
                        for item in payload.get(kind, [])
                    ]
                    for kind in ("runs", "events")
                },
            )

        transport = httpx.MockTransport(send)
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: async_client(transport=transport, **kwargs),
        )
        monkeypatch.setattr(
            implementation,
            "Client",
            lambda *args, **kwargs: Client(*args, **kwargs, transport=transport),
        )
        graph = graph or fixture()[0]
        kwargs = dict(
            agent_name="test", endpoint="https://example.test", credential="ctk_test"
        )
        control = None
        if enforcing:
            control = OpenEightMiddleware(**kwargs, delivery=delivery)
            native_tools = list(_catalog.BoundToolCatalog(graph).entries.values())
            graph = create_agent(
                ToolModel(responses=[AIMessage(content="done")]),
                tools=[entry.tool for entry in native_tools],
                middleware=[control],
            )
            owner = control.instrument(graph)
        else:
            owner = instrument(graph, **kwargs, delivery=delivery)
        owners.append(owner)
        return owner, graph, control

    setup_owner.requests = requests
    yield setup_owner
    for owner in owners:
        owner.close(2)


@pytest.mark.parametrize("enforcing", [False, True])
def test_startup_publishes_without_invocation_or_synthetic_telemetry(
    setup_owner, enforcing
):
    graph, effects, callbacks = fixture()
    owner, _, _ = setup_owner(graph=graph, enforcing=enforcing)
    status = owner.flush(2)
    assert status.catalog_delivery_state == "acknowledged"
    assert status.catalog_acknowledged_tools == 2
    assert (
        status.pending_records == status.failed_records == status.dropped_records == 0
    )
    assert effects == callbacks == []
    assert graph.nodes["model"].bound is not None
    assert not owner.roots
    requests = setup_owner.requests
    assert len(requests) == 1 and requests[0].url.path.endswith("/RegisterTools")
    assert {fact["name"] for fact in json.loads(requests[0].content)["tools"]} == {
        "echo",
        "unused",
    }
    assert all(
        set(fact) == {"name", "description", "input_schema_json"}
        for fact in json.loads(requests[0].content)["tools"]
    )


@pytest.mark.parametrize("enforcing", [False, True])
def test_rebinding_owner_does_not_schedule_startup_again(setup_owner, enforcing):
    owner, graph, control = setup_owner(enforcing=enforcing)
    assert owner.flush(2).catalog_delivery_state == "acknowledged"
    for _ in range(5):
        rebound = (
            control.instrument(graph)
            if enforcing
            else instrument(
                owner,
                agent_name="test",
                endpoint="https://example.test",
                credential="ctk_test",
            )
        )
        assert rebound is owner
    assert owner.flush(2).catalog_acknowledged_tools == 2
    assert len(setup_owner.requests) == 1


def test_separate_owners_replay_without_a_global_registry(setup_owner):
    graph, _, _ = fixture()
    first, _, _ = setup_owner(graph=graph)
    assert first.flush(2).catalog_delivery_state == "acknowledged"
    second, _, _ = setup_owner(
        graph=graph,
        handler=lambda request: registration_response(
            request, ["replayed", "replayed"]
        ),
    )
    assert second.flush(2).catalog_delivery_state == "acknowledged"
    assert len(setup_owner.requests) == 2 and first is not second


@pytest.mark.parametrize("failure", ["configuration", "graph", "middleware"])
def test_invalid_initialization_never_creates_a_client_or_worker(monkeypatch, failure):
    clients = []
    monkeypatch.setattr(
        implementation, "Client", lambda *args, **kwargs: clients.append(True)
    )
    threads = {thread.ident for thread in threading.enumerate()}
    kwargs = dict(
        agent_name="test", endpoint="https://example.test", credential="ctk_test"
    )
    graph, _, _ = fixture()
    with pytest.raises(ConfigurationError):
        if failure == "configuration":
            instrument(graph, **kwargs, delivery=object())
        elif failure == "graph":
            instrument(object(), **kwargs)
        else:
            OpenEightMiddleware(**kwargs).instrument(graph)
    assert not clients
    assert {thread.ident for thread in threading.enumerate()} <= threads


def test_worker_start_failure_closes_constructor_resources(monkeypatch):
    clients, requests = [], []
    start = threading.Thread.start

    def client(*args, **kwargs):
        created = Client(
            *args, **kwargs, transport=httpx.MockTransport(requests.append)
        )
        clients.append(created)
        return created

    def unavailable_thread(thread):
        if thread.name == "openeight-delivery":
            raise RuntimeError("worker unavailable")
        return start(thread)

    monkeypatch.setattr(implementation, "Client", client)
    monkeypatch.setattr(threading.Thread, "start", unavailable_thread)
    graph, effects, callbacks = fixture()
    with pytest.raises(RuntimeError, match="worker unavailable"):
        instrument(
            graph,
            agent_name="test",
            endpoint="https://example.test",
            credential="ctk_test",
        )
    assert len(clients) == 1 and clients[0]._closed
    assert requests == effects == callbacks == []


@pytest.mark.parametrize("completeness", ["complete", "partial", "unknown"])
def test_empty_discovery_diagnostics_do_not_fabricate_registration(
    monkeypatch, setup_owner, completeness
):
    monkeypatch.setattr(
        _catalog,
        "inspect_agent",
        lambda *args: SimpleNamespace(
            tools=(),
            discovery_completeness=completeness,
            discovery_diagnostic=None
            if completeness == "complete"
            else "unsupported_graph_shape",
        ),
    )
    owner, _, _ = setup_owner()
    status = owner.flush(2)
    assert status.catalog_delivery_state == (
        "empty" if completeness == "complete" else "not_started"
    )
    assert status.discovery_completeness == completeness
    assert status.catalog_total_tools == 0 and owner.worker.thread is None
    assert setup_owner.requests == []


def test_partial_discovery_reports_known_subset_without_claiming_completeness(
    monkeypatch, setup_owner
):
    graph, _, _ = fixture()
    known = _catalog.BoundToolCatalog(graph).entries["echo"].tool
    monkeypatch.setattr(
        _catalog,
        "inspect_agent",
        lambda *args: SimpleNamespace(
            tools=(known,),
            discovery_completeness="partial",
            discovery_diagnostic="unsupported_tool_definition",
        ),
    )
    owner, _, _ = setup_owner(graph=graph)
    status = owner.flush(2)
    assert status.catalog_delivery_state == "acknowledged"
    assert status.catalog_acknowledged_tools == 1
    assert status.discovery_completeness == "partial"
    assert status.discovery_diagnostic == "unsupported_tool_definition"


def many_tools_graph(monkeypatch):
    graph, _, _ = fixture()
    original = _catalog.BoundToolCatalog(graph).entries["echo"].tool
    tools = [
        original.model_copy(update={"name": f"tool_{index}"}) for index in range(201)
    ]
    # Exercise the batch sender independently of the native discovery count cap.
    monkeypatch.setattr(
        _catalog,
        "inspect_agent",
        lambda *args: SimpleNamespace(
            tools=tools,
            discovery_completeness="partial",
            discovery_diagnostic="tool_discovery_limit_exceeded",
        ),
    )
    return create_agent(ToolModel(responses=[AIMessage(content="done")]), tools=tools)


def test_multiple_batches_retain_success_and_per_item_failures(
    monkeypatch, setup_owner
):
    def send(request):
        count = len(json.loads(request.content)["tools"])
        statuses = ["registered"] * count
        if count == 200:
            statuses[3], statuses[17] = "invalid", "conflict"
        return registration_response(request, statuses)

    owner, _, _ = setup_owner(graph=many_tools_graph(monkeypatch), handler=send)
    status = owner.flush(2)
    assert status.catalog_delivery_state == "partial"
    assert status.catalog_total_tools == 201
    assert status.catalog_acknowledged_tools == 199 and status.catalog_failed_tools == 2
    assert status.failed_records == 1 and status.catalog_pending_tools == 0
    assert [
        len(json.loads(request.content)["tools"]) for request in setup_owner.requests
    ] == [200, 1]
    assert {
        result.request_index for result in owner.catalog_delivery.snapshot()
    } == set(range(201)) - {3, 17}


def test_later_batch_failure_keeps_earlier_acknowledgments(monkeypatch, setup_owner):
    def send(request):
        if len(json.loads(request.content)["tools"]) == 1:
            return httpx.Response(
                403, json={"code": "permission_denied", "message": ""}
            )
        return registration_response(request)

    owner, _, _ = setup_owner(graph=many_tools_graph(monkeypatch), handler=send)
    status = owner.flush(2)
    assert status.catalog_acknowledged_tools == 200 and status.catalog_failed_tools == 1
    assert status.catalog_delivery_state == "partial" and status.pending_records == 0
    assert len(setup_owner.requests) == 2


def test_native_catalog_splits_on_encoded_wire_bytes(setup_owner):
    graph, _, _ = fixture()
    original = _catalog.BoundToolCatalog(graph).entries["echo"].tool
    tools = [
        original.model_copy(
            update={"name": f"escaped_{index}", "description": '"' * 4000}
        )
        for index in range(80)
    ]
    graph = create_agent(ToolModel(responses=[AIMessage(content="done")]), tools=tools)
    owner, _, _ = setup_owner(graph=graph)
    status = owner.flush(3)
    assert status.discovery_completeness == "complete"
    assert status.catalog_acknowledged_tools == 80
    assert len(setup_owner.requests) > 1
    assert all(len(request.content) <= 1048576 for request in setup_owner.requests)


@pytest.mark.parametrize("failure", ["lost_ack", "503", "429"])
def test_transient_startup_retry_reuses_identical_facts(setup_owner, failure):
    calls = []

    def send(request):
        calls.append(request.content)
        if len(calls) == 1:
            if failure == "lost_ack":
                raise httpx.ReadError("lost acknowledgment", request=request)
            return httpx.Response(
                int(failure),
                json={
                    "code": "unavailable" if failure == "503" else "resource_exhausted",
                    "message": "",
                },
            )
        return registration_response(request, ["replayed", "replayed"])

    owner, _, _ = setup_owner(handler=send)
    status = owner.flush(3)
    assert (
        status.catalog_delivery_state == "acknowledged" and status.failed_records == 0
    )
    assert len(calls) == 2 and calls[0] == calls[1]


def test_close_drops_pending_retry_and_reports_it(setup_owner):
    owner, _, _ = setup_owner(
        handler=lambda request: httpx.Response(
            503, json={"code": "unavailable", "message": ""}
        )
    )
    assert owner.flush(0.05).catalog_pending_tools == 2
    status = owner.close(0.05)
    assert status.catalog_delivery_state == "dropped"
    assert status.catalog_dropped_tools == 2 and status.catalog_pending_tools == 0
    assert owner.close(2).closed


def test_queue_capacity_reports_startup_loss_without_execution(setup_owner):
    owner, _, _ = setup_owner(delivery=DeliveryConfig(max_bytes=512, flush_bytes=512))
    status = owner.flush(1)
    assert status.catalog_delivery_state == "dropped"
    assert status.catalog_dropped_tools == status.catalog_total_tools == 2
    assert status.dropped_records > 0 and setup_owner.requests == []
    assert status.last_error_code == "queue_full"


def test_cache_pressure_preserves_server_acknowledgment_counts(setup_owner):
    owner, _, _ = setup_owner(delivery=DeliveryConfig(max_bytes=2200, flush_bytes=2200))
    status = owner.flush(2)
    assert status.catalog_delivery_state == "acknowledged"
    assert status.catalog_acknowledged_tools == 2 and status.catalog_dropped_tools == 0
    assert status.last_error_code == "catalog_cache_full"
    assert len(owner.catalog_delivery.snapshot()) < 2
    assert owner.worker.catalog_cache_bytes <= owner.delivery.max_bytes


def test_observation_continues_without_retrying_registration_conflict(setup_owner):
    calls = []

    def send(request):
        calls.append(True)
        return (
            registration_response(request, ["invalid", "conflict"])
            if len(calls) == 1
            else registration_response(request)
        )

    owner, _, _ = setup_owner(handler=send)
    assert owner.flush(2).catalog_delivery_state == "failed"
    assert (
        owner.invoke({"messages": [("user", "hello")]})["messages"][-1].content
        == "done"
    )
    status = owner.flush(2)
    assert status.catalog_delivery_state == "failed" and status.failed_records >= 1
    assert len(calls) == 1


def test_malformed_http_success_is_failed_not_acknowledged(setup_owner):
    owner, _, _ = setup_owner(
        handler=lambda request: httpx.Response(200, json={"results": []})
    )
    status = owner.flush(2)
    assert (
        status.catalog_delivery_state == "failed" and status.catalog_failed_tools == 2
    )
    assert status.failed_records == 1


def test_invocation_shares_inflight_startup_and_telemetry_can_progress(setup_owner):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def send(request):
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return registration_response(request)

    owner, _, _ = setup_owner(handler=send)
    try:
        assert entered.wait(1)
        assert (
            owner.invoke({"messages": [("user", "hello")]})["messages"][-1].content
            == "done"
        )
        assert len(calls) == 1
        assert owner.flush(0.01).catalog_pending_tools == 2
    finally:
        release.set()
    assert owner.flush(2).catalog_delivery_state == "acknowledged"
    assert any(
        not request.url.path.endswith("/RegisterTools")
        for request in setup_owner.requests
    )
    assert len(calls) == 1


def test_catalog_batches_yield_to_ready_telemetry(monkeypatch, setup_owner):
    entered, release = threading.Event(), threading.Event()

    def send(request):
        if len(json.loads(request.content)["tools"]) == 200:
            entered.set()
            assert release.wait(2)
        return registration_response(request)

    owner, _, _ = setup_owner(graph=many_tools_graph(monkeypatch), handler=send)
    try:
        assert entered.wait(1)
        owner.invoke({"messages": [("user", "hello")]})
    finally:
        release.set()
    assert owner.flush(2).catalog_acknowledged_tools == 201
    paths = [request.url.path.rsplit("/", 1)[-1] for request in setup_owner.requests]
    assert paths[:3] == ["RegisterTools", "WriteTelemetry", "RegisterTools"]


def test_publication_releases_owner_lock_before_invocation_update(
    monkeypatch, setup_owner
):
    from openeight.langchain._runtime import AgentInvocation

    entered, release, updating = threading.Event(), threading.Event(), threading.Event()

    def send(request):
        entered.set()
        assert release.wait(2)
        return registration_response(request)

    owner, _, _ = setup_owner(handler=send)
    assert entered.wait(1)
    invocation = AgentInvocation(owner, "invoke")
    original = invocation.update_tool_contracts

    def update(*args, **kwargs):
        updating.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(invocation, "update_tool_contracts", update)
    invocation.lock.acquire()
    with owner.lock:
        owner.roots.add(invocation)
    try:
        release.set()
        assert updating.wait(1)
        acquired = owner.lock.acquire(timeout=0.2)
        assert acquired, "catalog callback inverted invocation/owner settlement locks"
        owner.lock.release()
    finally:
        invocation.lock.release()
        with owner.lock:
            owner.roots.discard(invocation)
    assert owner.flush(2).catalog_delivery_state == "acknowledged"


def test_async_flush_and_close_cover_startup(setup_owner):
    owner, _, _ = setup_owner()

    async def check():
        assert (await owner.aflush(2)).catalog_delivery_state == "acknowledged"
        assert (await owner.aclose(2)).closed

    asyncio.run(check())


@pytest.mark.parametrize("operation", ["delivery_status", "flush", "close", "refresh"])
def test_process_checks_precede_inherited_locks(setup_owner, operation):
    owner, _, _ = setup_owner()
    assert owner.flush(2).catalog_delivery_state == "acknowledged"
    original_pid = owner.pid
    owner.pid = -1
    try:
        method = (
            owner.catalog_delivery.request_refresh
            if operation == "refresh"
            else getattr(owner, operation)
        )
        with pytest.raises(ConfigurationError, match="after fork"):
            method()
    finally:
        owner.pid = original_pid


@pytest.mark.parametrize("failure", ["revoked", "transport", "invalid"])
def test_enforcement_refresh_does_not_reuse_startup_authority(setup_owner, failure):
    calls = []

    def send(request):
        calls.append(True)
        if len(calls) > 1:
            if failure == "transport":
                raise httpx.ConnectError("unavailable", request=request)
            if failure == "invalid":
                return registration_response(request, ["invalid", "conflict"])
            return httpx.Response(
                403, json={"code": "permission_denied", "message": ""}
            )
        return registration_response(request, governed=True)

    owner, _, _ = setup_owner(enforcing=True, handler=send)
    assert owner.flush(2).catalog_delivery_state == "acknowledged"
    error = {
        "revoked": APIError,
        "transport": TransportError,
        "invalid": ConfigurationError,
    }[failure]
    with pytest.raises(error):
        owner.invoke({"messages": [("user", "hello")]})
    assert len(calls) == 2 and not owner.roots
    assert all(
        request.url.path.endswith("/RegisterTools") for request in setup_owner.requests
    )


def test_concurrent_middleware_binding_shares_one_owner(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from test_enforcement_facade import GovernedClient

    monkeypatch.setattr(implementation, "Client", GovernedClient)
    control = OpenEightMiddleware(
        agent_name="test", endpoint="https://example.test", credential="ctk_test"
    )
    graph, _, _ = fixture()
    tools = [entry.tool for entry in _catalog.BoundToolCatalog(graph).entries.values()]
    graph = create_agent(
        ToolModel(responses=[AIMessage(content="done")]),
        tools=tools,
        middleware=[control],
    )
    barrier = threading.Barrier(4)

    def bind(_):
        barrier.wait(timeout=2)
        return control.instrument(graph)

    with ThreadPoolExecutor(max_workers=4) as executor:
        owners = list(executor.map(bind, range(4)))
    owner = owners[0]
    try:
        assert all(candidate is owner for candidate in owners)
        assert owner.flush(2).catalog_delivery_state == "acknowledged"
        assert owner.catalog_delivery.generation == 1
    finally:
        owner.close(2)
