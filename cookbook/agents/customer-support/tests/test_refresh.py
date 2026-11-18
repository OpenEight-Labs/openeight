"""Real native/SDK execution refreshes factual failures once across restarts."""

import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from local import LocalError
from refresh import RefreshMiddleware
from service import DemoService
from initialization import setup, write_private
from helpers_remedies import confirm
from test_modes import ConversationModel, command
from test_durable_agent import Transport


@pytest.fixture(params=["standalone", "integrated"])
def runtime(tmp_path, monkeypatch, request):
    Transport.requests, Transport.writes = [], []
    Transport.verdict, Transport.approval = "allow", "pending"
    Transport.lose_decide = Transport.lose_observe = False
    for key in ("OPENEIGHT_API_KEY", "OPENEIGHT_ENDPOINT", "OPENEIGHT_AGENT_NAME"):
        monkeypatch.delenv(key, raising=False)
    state = tmp_path / "state"
    kwargs = {}
    if request.param == "integrated":
        key = tmp_path / "key"
        write_private(key, "ctk_test")
        kwargs = {
            "endpoint": "http://127.0.0.1:7777",
            "agent_name": "remedies-agent",
            "api_key_file": key,
        }
    setup(state, deployment_id="new-remedies", mode=request.param, apply=True, **kwargs)
    service = DemoService(state)
    r = service.retailer("case_damaged")
    action = confirm(r)
    monkeypatch.setattr(service, "retailer", lambda _: r)
    return service, r, action


def change_revision(r):
    r.control(
        {
            "command_id": str(uuid4()),
            "thread_id": str(uuid4()),
            "case_id": r.context.case_id,
            "facts_revision": r.read("get_order", {"order_id": r.context.order_id})[
                "facts_revision"
            ],
            "kind": "assessment",
            "payload": {"verified_issue": "lost"},
        }
    )


def attempts(service, thread):
    return (
        service.store.attempts(thread)
        if service.deployment.mode == "integrated"
        else service.store.local_attempts(thread)
    )


@pytest.mark.parametrize("failures", [1, 2])
def test_generic_no_effect_error_refreshes_once_with_fresh_governance(
    runtime, monkeypatch, failures
):
    service, r, action = runtime
    frozen = r.execution_arguments(action)
    original = r.mutate
    invoked = 0

    def mutate(tool, args, key):
        nonlocal invoked
        if tool == "issue_refund":
            invoked += 1
            if invoked <= failures:
                change_revision(r)
        return original(tool, args, key)

    monkeypatch.setattr(r, "mutate", mutate)
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"id": "original", "name": "issue_refund", "args": frozen}],
            ),
            AIMessage(content="Waiting for progress."),
            AIMessage(content="Waiting for progress."),
        ]
    )
    accepted = command(service, "Please execute my confirmed remedy.")
    view = asyncio.run(service.execute(accepted["command_id"]))
    records = attempts(service, view["thread_id"])
    assert len(records) == 2 and records[0].status == "failed"
    assert records[1].status == ("succeeded" if failures == 1 else "failed")
    first, second = (json.loads(a.native_call_json) for a in records)
    assert first["args"]["facts_revision"] == frozen["facts_revision"]
    assert second["args"]["facts_revision"] > frozen["facts_revision"]
    assert first["id"] != second["id"] and invoked == 2
    assert len([x for x in r.receipts() if x["kind"] == "refund"]) == (
        1 if failures == 1 else 0
    )
    if service.deployment.mode == "integrated":
        assert len({x["decision_id"] for x in Transport.requests}) == 2
        assert (
            Transport.requests[0]["selected_values"]
            != Transport.requests[-1]["selected_values"]
        )


def test_unchanged_facts_do_not_refresh(runtime, monkeypatch):
    service, r, action = runtime
    original = r.mutate

    def fail(tool, args, key):
        if tool == "issue_refund":
            raise LocalError("opaque_failure")
        return original(tool, args, key)

    monkeypatch.setattr(r, "mutate", fail)
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "original",
                        "name": "issue_refund",
                        "args": r.execution_arguments(action),
                    }
                ],
            ),
            AIMessage(content="Review needed."),
        ]
    )
    accepted = command(service, "Execute the confirmed action.")
    view = asyncio.run(service.execute(accepted["command_id"]))
    assert len(attempts(service, view["thread_id"])) == 1
    assert all(x["kind"] != "refund" for x in r.receipts())


def test_restart_after_second_failure_does_not_reset_retry_budget(runtime, monkeypatch):
    service, r, action = runtime
    original = r.mutate

    def fail(tool, args, key):
        if tool == "issue_refund":
            change_revision(r)
        return original(tool, args, key)

    monkeypatch.setattr(r, "mutate", fail)
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "original",
                        "name": "issue_refund",
                        "args": r.execution_arguments(action),
                    }
                ],
            ),
            AIMessage(content="Waiting"),
            AIMessage(content="Waiting"),
        ]
    )
    injected = False

    def crash(point):
        nonlocal injected
        if point == "before_command_settlement_commit" and not injected:
            injected = True
            raise RuntimeError("crash")

    service.store.fault = crash
    accepted = command(service, "Execute the confirmed action.")
    with pytest.raises(RuntimeError):
        asyncio.run(service.execute(accepted["command_id"]))
    assert len(attempts(service, accepted["thread_id"])) == 2
    restarted = DemoService(
        service.state,
        model_factory=lambda: ConversationModel(
            responses=[AIMessage(content="No retry.")]
        ),
    )
    recovery = restarted.continue_conversation(accepted["thread_id"], str(uuid4()))
    asyncio.run(restarted.execute(recovery["command_id"]))
    assert len(attempts(restarted, accepted["thread_id"])) == 2


def test_approval_resume_without_recovery_chain_keeps_origin(runtime, monkeypatch):
    service, r, action = runtime
    if service.deployment.mode != "integrated":
        pytest.skip("Approval is an integrated SDK contract")
    Transport.verdict = "approval_required"
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "original",
                        "name": "issue_refund",
                        "args": r.execution_arguments(action),
                    }
                ],
            )
        ]
    )
    accepted = command(service, "Execute the confirmed remedy.")
    view = asyncio.run(service.execute(accepted["command_id"]))
    assert view["status"] == "waiting" and not any(
        x["kind"] == "refund" for x in r.receipts()
    )
    change_revision(r)
    Transport.approval = "approved"
    Transport.verdict = "allow"
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(content="Waiting"),
            AIMessage(content="Updated refund completed"),
        ]
    )
    resumed = service.continue_conversation(view["thread_id"], str(uuid4()))
    assert service.store.find_command(resumed["command_id"]).recover_command_id is None
    done = asyncio.run(service.execute(resumed["command_id"]))
    records = attempts(service, done["thread_id"])
    assert (
        len(records) == 2
        and records[0].status == "failed"
        and records[1].status == "succeeded"
    )


@pytest.mark.parametrize("verdict", ["deny", "approval_required"])
def test_denied_or_waiting_attempt_never_refreshes(runtime, verdict):
    service, r, action = runtime
    if service.deployment.mode != "integrated":
        pytest.skip("OpenEightConfig outcome is an integrated SDK contract")
    Transport.verdict = verdict
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "original",
                        "name": "issue_refund",
                        "args": r.execution_arguments(action),
                    }
                ],
            ),
            AIMessage(content="Review needed"),
        ]
    )
    accepted = command(service, "Execute the confirmed remedy.")
    view = asyncio.run(service.execute(accepted["command_id"]))
    assert len(attempts(service, view["thread_id"])) == 1
    assert all(x["kind"] != "refund" for x in r.receipts())


def test_missing_checkpoint_origin_fails_closed(runtime, monkeypatch):
    service, r, action = runtime
    original = r.mutate

    def fail(tool, args, key):
        if tool == "issue_refund":
            change_revision(r)
        return original(tool, args, key)

    monkeypatch.setattr(r, "mutate", fail)
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "original",
                        "name": "issue_refund",
                        "args": r.execution_arguments(action),
                    }
                ],
            ),
            AIMessage(content="Wait"),
            AIMessage(content="Wait"),
        ]
    )
    admitted = command(service, "Execute")
    asyncio.run(service.execute(admitted["command_id"]))
    refresh = RefreshMiddleware(service.store, r, admitted["thread_id"])
    assert refresh.policy.evaluate(
        action, [HumanMessage(content="Unbound", id=str(uuid4()))]
    ).state == "blocked"


def test_approval_resume_cannot_execute_superseded_confirmation(runtime):
    service, r, action = runtime
    if service.deployment.mode != "integrated":
        pytest.skip("Approval is an integrated SDK contract")
    Transport.verdict = "approval_required"
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "original",
                        "name": "issue_refund",
                        "args": r.execution_arguments(action),
                    }
                ],
            )
        ]
    )
    accepted = command(service, "Execute my confirmed refund.")
    view = asyncio.run(service.execute(accepted["command_id"]))
    assert view["status"] == "waiting"
    new_action = confirm(r, "replacement", thread_id=view["thread_id"])
    Transport.approval = "approved"
    Transport.verdict = "allow"
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(content="Please continue the new confirmation explicitly.")
        ]
    )
    resumed = service.continue_conversation(view["thread_id"], str(uuid4()))
    asyncio.run(service.execute(resumed["command_id"]))
    records = attempts(service, view["thread_id"])
    assert len(records) == 1 and records[0].status == "failed"
    assert not any(x["kind"] == "refund" for x in r.receipts())
    assert r.execution_arguments(new_action)["action_id"] == new_action


def test_unknown_receipt_outcome_stops_before_any_refresh(runtime, monkeypatch):
    service, r, action = runtime
    if service.deployment.mode != "integrated":
        pytest.skip("This checks integrated reconciliation")
    original = r.mutate

    def fail(tool, args, key):
        if tool == "issue_refund":
            change_revision(r)
        return original(tool, args, key)

    monkeypatch.setattr(r, "mutate", fail)
    monkeypatch.setattr(
        r,
        "lookup_receipt",
        lambda key: (_ for _ in ()).throw(LocalError("storage_unavailable")),
    )
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "original",
                        "name": "issue_refund",
                        "args": r.execution_arguments(action),
                    }
                ],
            ),
            AIMessage(content="No new attempt"),
        ]
    )
    accepted = command(service, "Execute")
    with pytest.raises(Exception):
        asyncio.run(service.execute(accepted["command_id"]))
    assert len(attempts(service, accepted["thread_id"])) == 1
    assert service.store.load(accepted["thread_id"]).status == "reconciliation_required"
    assert not any(x["kind"] == "refund" for x in r.receipts())


@pytest.mark.parametrize(
    "value,label", [(50, "return31"), (50.0, "return31"), (50.25, "fractional")]
)
def test_real_sdk_publication_selection_and_execution_preserve_numeric_type(
    tmp_path, monkeypatch, value, label
):
    import fixtures as fixtures_module

    monkeypatch.setattr(
        fixtures_module,
        "SCENARIOS",
        (
            *fixtures_module.SCENARIOS,
            (
                "fractional",
                50.25,
                "delivered",
                "damaged",
                "A fractional currency order",
            ),
        ),
    )
    Transport.requests, Transport.writes = [], []
    Transport.verdict, Transport.approval = "allow", "pending"
    Transport.lose_decide = Transport.lose_observe = False
    for key in ("OPENEIGHT_API_KEY", "OPENEIGHT_ENDPOINT", "OPENEIGHT_AGENT_NAME"):
        monkeypatch.delenv(key, raising=False)
    keyfile = tmp_path / "key"
    write_private(keyfile, "ctk_test")
    state = tmp_path / "state"
    setup(
        state,
        deployment_id="numeric",
        mode="integrated",
        endpoint="http://127.0.0.1:7777",
        agent_name="numeric-agent",
        api_key_file=keyfile,
        apply=True,
    )
    service = DemoService(state)
    r = service.retailer("case_" + label)
    action = confirm(r)
    args = {**r.execution_arguments(action), "amount": value}
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"id": "number", "name": "issue_refund", "args": args}],
            ),
            AIMessage(content="Refund receipt recorded"),
        ]
    )
    accepted = command(service, "Execute the confirmed refund", case=r.context.case_id)
    view = asyncio.run(service.execute(accepted["command_id"]))
    record = service.store.attempts(view["thread_id"])[0]
    assert record.status == "succeeded"
    native_amount = json.loads(record.native_call_json)["args"]["amount"]
    selected_amount = next(
        entry["value"]
        for entry in json.loads(record.decision_input_json)["selected_values"]
        if entry["path"] == "amount"
    )
    assert type(native_amount) is type(value) and type(selected_amount) is type(value)
    assert type(
        next(
            entry["value"]
            for entry in Transport.requests[-1]["selected_values"]
            if entry["path"] == "amount"
        )
    ) is type(value)
    with service.store._db() as db:
        schema = json.loads(
            db.execute(
                "SELECT schema_json FROM catalog_versions WHERE tool_name='issue_refund'"
            ).fetchone()[0]
        )
        assert "anyOf" in schema["properties"]["amount"]


@pytest.mark.parametrize("repeat_refund", [False, True])
def test_refreshed_success_preserves_customer_reply_and_resolution(
    runtime, monkeypatch, repeat_refund
):
    service, r, action = runtime
    original = r.mutate
    refund_calls = 0

    def mutate(tool, args, key):
        nonlocal refund_calls
        if tool == "issue_refund":
            refund_calls += 1
            if refund_calls == 1:
                change_revision(r)
        return original(tool, args, key)

    monkeypatch.setattr(r, "mutate", mutate)
    service.model_factory = lambda: ConversationModel(
        responses=[
            AIMessage(content="", tool_calls=[{
                "id": "original", "name": "issue_refund",
                "args": r.execution_arguments(action),
            }]),
            AIMessage(content="Waiting for refreshed facts."),
            *([AIMessage(content="", tool_calls=[{
                "id": "duplicate", "name": "issue_refund",
                "args": r.execution_arguments(action),
            }])] if repeat_refund else []),
            AIMessage(content="", tool_calls=[{
                "id": "notify", "name": "resolve_case",
                "args": {"case_id": r.context.case_id, "action_id": action,
                         "facts_revision": r.execution_arguments(action)["facts_revision"] + 2},
            }]),
            AIMessage(content="Your refund is complete."),
        ]
    )
    accepted = command(service, "Execute my confirmed refund.")
    view = asyncio.run(service.execute(accepted["command_id"]))
    records = attempts(service, view["thread_id"])
    expected = [("issue_refund", "failed"), ("issue_refund", "succeeded")]
    if repeat_refund and service.deployment.mode == "standalone":
        # Standalone journals the rejected native call; the retailer is not entered.
        expected.append(("issue_refund", "failed"))
    expected.append(("resolve_case", "succeeded"))
    assert [(a.tool_name, a.status) for a in records] == expected
    assert refund_calls == 2
    assert [x["kind"] for x in view["receipts"]].count("refund") == 1
    assert [x["kind"] for x in view["receipts"]].count("closure") == 1
    assert view["case"]["status"] == "resolved" and view["status"] == "completed"
    assert view["turns"][-1]["body"] == "Your refund is complete."
