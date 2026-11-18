"""Closed controls save atomic retailer progress without dispatching a graph."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from local import LocalError, connection
from service import DemoService
from storage import SQLiteExecutionStore
from helpers_remedies import confirm, event
from test_modes import prepare, ConversationModel, command
from test_retailer import retailer  # noqa: F401


@pytest.fixture
def waiting(tmp_path, monkeypatch):
    state = prepare(tmp_path, monkeypatch)
    service = DemoService(
        state,
        model_factory=lambda: ConversationModel(
            responses=[AIMessage(content="Review the remedy.")]
        ),
    )
    accepted = command(service, "Please help.")
    asyncio.run(service.execute(accepted["command_id"]))
    return service, accepted["thread_id"]


def envelope(service, thread, **changes):
    view = service.snapshot(thread)
    result = {
        "command_id": str(uuid4()),
        "thread_id": thread,
        "case_id": view["case"]["case_id"],
        "facts_revision": view["case"]["facts_revision"],
        "kind": "confirmation",
        "payload": {
            "remedy": "refund",
            "amount": view["case"]["refundable_amount"],
            "currency": view["case"]["currency"],
            "reason": "Confirmed",
        },
    }
    result.update(changes)
    return result


def test_control_exact_replay_and_full_envelope_conflicts(waiting):
    service, thread = waiting
    request = envelope(service, thread)
    first = service.control(request)
    assert service.control(request) == first
    assert not service.store.local_attempts(thread)
    for changed in (
        {"facts_revision": request["facts_revision"] + 1},
        {"kind": "assessment", "payload": {"verified_issue": "lost"}},
        {"payload": {**request["payload"], "reason": "Different"}},
    ):
        with pytest.raises(LocalError, match="receipt_conflict"):
            service.control({**request, **changed})
    assert len(service.receipts(thread)) == 1


def test_cross_thread_case_and_global_command_reuse_fail(waiting):
    service, thread = waiting
    request = envelope(service, thread)
    service.control(request)
    second = command(service, "Another order", case="case_small")
    asyncio.run(service.execute(second["command_id"]))
    with pytest.raises(LocalError, match="not_found"):
        service.control({**request, "thread_id": second["thread_id"]})
    other_request = envelope(
        service, second["thread_id"], command_id=request["command_id"]
    )
    with pytest.raises(LocalError, match="receipt_conflict"):
        service.control(other_request)
    assert not service.receipts(second["thread_id"])


def test_stale_revision_and_thread_lock_contention_have_no_effect(waiting):
    service, thread = waiting
    request = envelope(service, thread)
    with service.store.acquire(thread):
        with pytest.raises(LocalError, match="execution_busy"):
            service.control(request)
    service.control(
        envelope(service, thread, kind="assessment", payload={"verified_issue": "lost"})
    )
    with pytest.raises(LocalError, match="stale_facts"):
        service.control(request)
    assert [r["kind"] for r in service.receipts(thread)] == ["assessment"]


@pytest.mark.parametrize(
    "extra", [{"amount": 1}, {"currency": "USD"}, {"shipping_method": "standard"}]
)
@pytest.mark.parametrize("kind", ["return", "cancellation"])
def test_return_cancellation_confirmation_has_no_placeholder_fields(
    waiting, extra, kind
):
    service, thread = waiting
    with pytest.raises(LocalError, match="invalid_input"):
        service.control(
            envelope(
                service,
                thread,
                payload={"remedy": kind, "reason": "Confirmed", **extra},
            )
        )
    assert not service.receipts(thread)


@pytest.mark.parametrize(
    "point,committed",
    [("before_receipt_commit", False), ("after_receipt_commit", True)],
)
def test_confirmation_restart_before_explicit_continue(
    waiting, monkeypatch, point, committed
):
    service, thread = waiting
    request = envelope(service, thread)
    r = service.retailer("case_damaged")
    r.fault = (
        lambda p: (_ for _ in ()).throw(RuntimeError("crash")) if p == point else None
    )
    monkeypatch.setattr(service, "retailer", lambda _: r)
    with pytest.raises(RuntimeError):
        service.control(request)
    restarted = DemoService(service.state)
    assert bool(restarted.receipts(thread)) == committed
    first = restarted.control(request)
    assert restarted.control(request) == first
    assert len(restarted.snapshot(thread)["case"]["actions"]) == 1
    assert not restarted.store.local_attempts(thread)
    with connection(service.state / "recovery.sqlite") as db:
        assert {r[0] for r in db.execute("SELECT kind FROM ui_commands")} == {"start"}


def test_duplicate_physical_event_binds_new_command_without_transition(retailer):  # noqa: F811
    r = retailer[2]
    action = confirm(r, "replacement")
    r.mutate("create_replacement", r.execution_arguments(action), str(uuid4()))
    first = event(r, action, "dispatch")
    request = {
        "command_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "case_id": r.context.case_id,
        "facts_revision": r.read("get_order", {"order_id": r.context.order_id})[
            "facts_revision"
        ],
        "kind": "physical_event",
        "payload": {"action_id": action, "event": "dispatch"},
    }
    second = r.control(request)
    assert (
        second["already_applied"]
        and second["original_receipt_id"] == first["receipt_id"]
    )
    assert r.control(request) == second
    assert (
        r.read("get_order", {"order_id": r.context.order_id})["facts_revision"]
        == request["facts_revision"]
    )
    with pytest.raises(LocalError, match="receipt_conflict"):
        r.control({**request, "payload": {"action_id": action, "event": "delivery"}})


@pytest.mark.parametrize("old_version", [2, 3])
def test_old_binding_refuses_without_rewrite(waiting, old_version):
    service, _ = waiting
    old = replace(service.deployment, schema_version=old_version)
    with pytest.raises(LocalError, match="unsupported_deployment"):
        old.validate()
    with pytest.raises(LocalError):
        SQLiteExecutionStore(service.state, old).initialize()
    assert DemoService(service.state).deployment.schema_version == 4
