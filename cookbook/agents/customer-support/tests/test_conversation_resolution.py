"""Conversation is ordinary output; business resolution remains governed."""

import asyncio
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from initialization import setup
from local import LocalError
from service import DemoService
from helpers_remedies import confirm, resolve
from test_durable_agent import Transport
from test_modes import ConversationModel, command


def service_for(tmp_path, monkeypatch, mode):
    for key in ("OPENEIGHT_ENDPOINT", "OPENEIGHT_AGENT_NAME", "OPENEIGHT_API_KEY", "OPENEIGHT_CONSOLE_ORIGIN"):
        monkeypatch.delenv(key, raising=False)
    Transport.requests, Transport.writes = [], []
    Transport.verdict, Transport.approval = "allow", "pending"
    Transport.lose_decide = Transport.lose_observe = False
    settings = {} if mode == "standalone" else {
        "endpoint": "http://localhost:7777", "agent_name": "support-resolution",
        "api_key": "ctk_synthetic_test",
    }
    state = tmp_path / "state"
    setup(state, deployment_id="conversation-resolution", mode=mode, apply=True, **settings)
    return DemoService(state)


def completed_refund(service):
    retailer = service.retailer("case_small")
    action = confirm(retailer)
    retailer.mutate("issue_refund", retailer.execution_arguments(action), str(uuid4()))
    order = retailer.read("get_order", {"order_id": retailer.context.order_id})
    return retailer, action, {
        "case_id": retailer.context.case_id, "action_id": action,
        "facts_revision": order["facts_revision"],
    }


@pytest.mark.parametrize("completed_business", [False, True])
@pytest.mark.parametrize("mode", ["standalone", "integrated"])
def test_assistant_turn_is_persisted_without_tool_decision_or_case_resolution(tmp_path, monkeypatch, mode, completed_business):
    service = service_for(tmp_path, monkeypatch, mode)
    if completed_business:
        completed_refund(service)
    before = service.retailer("case_small").receipts()
    Transport.verdict = "approval_required"
    service.model_factory = lambda: ConversationModel(responses=[
        AIMessage(content="Please review the remedy options; nothing is confirmed.")
    ])
    accepted = command(service, "My item arrived damaged.", case="case_small")
    view = asyncio.run(service.execute(accepted["command_id"]))
    assert view["status"] == "waiting_customer"
    assert view["case"]["status"] != "resolved"
    assert view["turns"][-1]["body"] == "Please review the remedy options; nothing is confirmed."
    assert len(view["receipts"]) == len(before)
    assert all(r["kind"] not in {"closure", "reply"} for r in view["receipts"])
    assert Transport.requests == []
    assert service.store.attempts(view["thread_id"]) == ()


@pytest.mark.parametrize("verdict", ["allow", "deny", "approval_required"])
def test_resolution_is_authorized_separately_from_assistant_output(tmp_path, monkeypatch, verdict):
    service = service_for(tmp_path, monkeypatch, "integrated")
    retailer, action, args = completed_refund(service)
    calls = 0
    original = retailer.mutate

    def mutate(name, arguments, key):
        nonlocal calls
        if name == "resolve_case":
            calls += 1
        return original(name, arguments, key)

    monkeypatch.setattr(retailer, "mutate", mutate)
    monkeypatch.setattr(service, "retailer", lambda _: retailer)
    Transport.verdict = verdict
    service.model_factory = lambda: ConversationModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "resolve", "name": "resolve_case", "args": args}]),
        AIMessage(content="Case resolved." if verdict == "allow" else "Case resolution is still blocked."),
    ])
    accepted = command(service, "Please resolve my completed remedy.", case="case_small")
    view = asyncio.run(service.execute(accepted["command_id"]))
    assert calls == (1 if verdict == "allow" else 0)
    assert sum(r["kind"] == "closure" for r in view["receipts"]) == calls
    assert sum(r["kind"] == "refund" for r in view["receipts"]) == 1
    assert all(r["kind"] != "reply" for r in view["receipts"])
    if verdict == "allow":
        assert view["status"] == "completed" and view["case"]["status"] == "resolved"
        assert view["turns"][-1]["body"] == "Case resolved."
    elif verdict == "approval_required":
        assert view["status"] == "waiting" and view["case"]["status"] != "resolved"
    else:
        assert view["case"]["status"] != "resolved"


def test_approved_resolution_reopens_same_attempt_without_duplicate_business_effect(tmp_path, monkeypatch):
    service = service_for(tmp_path, monkeypatch, "integrated")
    retailer, action, args = completed_refund(service)
    Transport.verdict = "approval_required"
    service.model_factory = lambda: ConversationModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "resolve", "name": "resolve_case", "args": args}]),
    ])
    accepted = command(service, "Resolve my completed remedy.", case="case_small")
    waiting = asyncio.run(service.execute(accepted["command_id"]))
    original = next(a for a in service.store.attempts(waiting["thread_id"]) if a.tool_name == "resolve_case")
    assert original.status == "waiting" and not any(r["kind"] == "closure" for r in waiting["receipts"])
    Transport.approval = "approved"
    restarted = DemoService(service.state, model_factory=lambda: ConversationModel(responses=[AIMessage(content="Your case is resolved.")]))
    resumed = restarted.accept({"kind": "resume", "command_id": str(uuid4()), "thread_id": waiting["thread_id"]})
    completed = asyncio.run(restarted.execute(resumed["command_id"]))
    final = next(a for a in restarted.store.attempts(waiting["thread_id"]) if a.tool_name == "resolve_case")
    assert final.decision_id == original.decision_id and final.idempotency_key == original.idempotency_key
    assert final.native_call_json == original.native_call_json and final.status == "succeeded"
    assert completed["status"] == "completed" and completed["case"]["status"] == "resolved"
    assert sum(r["kind"] == "closure" for r in completed["receipts"]) == 1
    assert sum(r["kind"] == "refund" for r in completed["receipts"]) == 1
    assert completed["turns"][-1]["body"] == "Your case is resolved."
    assert restarted.store.pending(waiting["thread_id"]) == ()


def test_resolution_rejects_stale_facts_and_replays_exact_receipt(tmp_path, monkeypatch):
    service = service_for(tmp_path, monkeypatch, "standalone")
    retailer, action, args = completed_refund(service)
    with pytest.raises(LocalError, match="stale_facts"):
        retailer.mutate("resolve_case", {**args, "facts_revision": args["facts_revision"] - 1}, str(uuid4()))
    assert not any(r["kind"] == "closure" for r in retailer.receipts())
    key = str(uuid4())
    receipt = retailer.mutate("resolve_case", args, key)
    assert retailer.mutate("resolve_case", args, key) == receipt
    with pytest.raises(LocalError, match="receipt_conflict"):
        retailer.mutate("resolve_case", {**args, "facts_revision": args["facts_revision"] + 1}, key)
    with pytest.raises(LocalError, match="case_terminal"):
        resolve(retailer, action)
    assert sum(r["kind"] == "closure" for r in retailer.receipts()) == 1


@pytest.mark.parametrize("point,committed", [("before_receipt_commit", False), ("after_receipt_commit", True)])
def test_resolution_and_receipt_crash_atomically(tmp_path, monkeypatch, point, committed):
    service = service_for(tmp_path, monkeypatch, "standalone")
    retailer, action, args = completed_refund(service)
    key = str(uuid4())
    retailer.fault = lambda p: (_ for _ in ()).throw(RuntimeError("crash")) if p == point else None
    with pytest.raises(RuntimeError):
        retailer.mutate("resolve_case", args, key)
    reopened = service.retailer("case_small")
    assert (reopened.lookup_receipt(key) is not None) == committed
    assert (reopened.read("get_case", {"case_id": "case_small"})["status"] == "resolved") == committed
    reopened.mutate("resolve_case", args, key)
    assert sum(r["kind"] == "closure" for r in reopened.receipts()) == 1


def test_old_deployment_is_refused_before_journal_changes(tmp_path, monkeypatch):
    import json

    service = service_for(tmp_path, monkeypatch, "standalone")
    path = service.state / "deployment.json"
    old = json.loads(path.read_text())
    old["schema_version"] = 3
    path.write_text(json.dumps(old))
    before = {p.name: p.read_bytes() for p in service.state.iterdir() if p.is_file()}
    with pytest.raises(LocalError, match="unsupported_deployment"):
        DemoService(service.state)
    assert {p.name: p.read_bytes() for p in service.state.iterdir() if p.is_file()} == before
