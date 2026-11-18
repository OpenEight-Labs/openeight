"""Runtime pause/resume evidence survives pending retries and restart."""

import asyncio
import json
import pytest
from langchain_core.messages import AIMessage
from test_durable_agent import Transport


def test_targeted_lifecycle_and_timing(runtime):
    async def exercise():
        Transport.verdict = "approval_required"
        request = runtime.admit()
        async with runtime.owner(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "issue_refund",
                            "id": "refund",
                            "args": {
                                "action_id": runtime.action_id,
                                "facts_revision": 2,
                                "amount": 180,
                                "currency": "USD",
                            },
                        }
                    ],
                )
            ]
        ) as owner:
            await runtime.invoke(owner, request)
        first = runtime.store.attempts(request.thread_id)[0]
        pause = json.loads(first.pause_event_json)
        assert pause["name"] == "issue_refund.paused"
        assert pause["outcome"] == "interrupted"
        assert pause.get("parent_event_id")
        assert "input_json" not in pause and "latency_ms" not in pause
        before = [
            event for body in Transport.writes for event in body.get("events", [])
        ]
        roots = [e for e in before if e.get("span_role") == "graph"]
        assert roots and all(e["outcome"] == "interrupted" for e in roots)
        pending = runtime.admit("resume", request.thread_id)
        async with runtime.owner() as owner:
            await runtime.invoke(owner, pending)
        assert (
            runtime.store.attempts(request.thread_id)[0].pause_event_json
            == first.pause_event_json
        )
        Transport.approval = "approved"
        resumed = runtime.admit("resume", request.thread_id)
        async with runtime.owner([AIMessage(content="Refund complete")]) as owner:
            await runtime.invoke(owner, resumed)
        attempt = runtime.store.attempts(request.thread_id)[0]
        resume = json.loads(attempt.resume_event_json)
        terminal = json.loads(attempt.terminal_event_json)
        assert (
            resume["name"] == "issue_refund.resumed"
            and resume["outcome"] == "unspecified"
        )
        assert resume["parent_event_id"] == pause["parent_event_id"]
        assert terminal["name"] == "issue_refund"
        assert json.loads(terminal["input_json"]) == json.loads(
            attempt.decision_input_json
        )
        assert "output_json" not in terminal
        assert terminal["occurred_at"] >= resume["occurred_at"] > pause["occurred_at"]
        assert terminal["latency_ms"] >= 0
        assert terminal["decision_id"] == resume["decision_id"] == pause["decision_id"]
        assert attempt.status == "succeeded"

    asyncio.run(exercise())


def test_atomic_transition_outbox_rollback_and_acknowledged_retention(tmp_path):
    import pytest
    from openeight import Event
    from openeight.langchain.durable import AttemptTransition, DurableObserveRecord
    from openeight.telemetry import uuid7
    from local import Deployment, canonical, now
    from storage import SQLiteExecutionStore
    from test_storage import start, executing

    store = SQLiteExecutionStore(
        tmp_path,
        Deployment(
            "deployment",
            "http://127.0.0.1:8080",
            "workspace",
            "agent",
            "agent-id",
            "key-id",
            "integrated",
            4,
            now(),
        ),
    )
    store.initialize()
    command, _ = start(store)
    with store.acquire(command.thread_id):
        _, attempt = executing(store, command)
        attempt = store.transition_attempt(
            attempt.thread_id,
            attempt.decision_id,
            attempt.revision,
            AttemptTransition("reconciliation_required"),
        )
        at, event_id = now(), uuid7()
        payload = Event(
            "issue_refund.paused",
            "lifecycle",
            "unspecified",
            at,
            "interrupted",
            decision_id=attempt.decision_id,
            tool_id=attempt.tool_id,
            tool_version_id=attempt.tool_version_id,
            tool_name=attempt.tool_name,
        )._as_write()
        payload.update(id=event_id, run_id=store.load(attempt.thread_id).run_id)
        event = DurableObserveRecord(
            event_id, attempt.thread_id, "event", canonical(payload), at
        )
        before = store.pending(attempt.thread_id)

        def crash(point):
            if point == "after_attempt_transition":
                raise RuntimeError("crash")

        store.fault = crash
        with pytest.raises(RuntimeError):
            store.transition_attempt(
                attempt.thread_id,
                attempt.decision_id,
                attempt.revision,
                AttemptTransition("waiting", event=event),
            )
        assert store.get_attempt(attempt.thread_id, attempt.tool_call_id) == attempt
        assert store.pending(attempt.thread_id) == before
        store.fault = lambda point: None
        saved = store.transition_attempt(
            attempt.thread_id,
            attempt.decision_id,
            attempt.revision,
            AttemptTransition("waiting", event=event),
        )
        store.acknowledge(attempt.thread_id, event_id)
        assert (
            store.get_attempt(attempt.thread_id, attempt.tool_call_id).pause_event_json
            == event.payload_json
        )
        store.transition_attempt(
            attempt.thread_id,
            attempt.decision_id,
            saved.revision,
            AttemptTransition("waiting"),
        )
        assert all(r.id != event_id for r in store.pending(attempt.thread_id))


def test_obsolete_sqlite_attempt_format_is_refused_without_reset(tmp_path):
    import pytest
    from local import Deployment, connection, now, LocalError
    from storage import SQLiteExecutionStore

    store = SQLiteExecutionStore(
        tmp_path,
        Deployment(
            "deployment",
            "http://127.0.0.1:8080",
            "workspace",
            "agent",
            "agent-id",
            "key-id",
            "integrated",
            4,
            now(),
        ),
    )
    store.initialize()
    with connection(store.path) as db:
        db.execute(
            "ALTER TABLE attempts RENAME COLUMN decision_input_json TO selected_input_json"
        )
    before = store.path.read_bytes()
    with pytest.raises(LocalError, match="durable_request_format_unsupported"):
        store.initialize()
    assert store.path.read_bytes() == before


def test_receipt_recovery_has_observation_time_without_invented_latency(runtime):
    import pytest

    async def exercise():
        command = runtime.admit()

        def crash(point):
            if point == "after_attempt_result":
                raise RuntimeError("settlement interrupted")

        runtime.store.fault = crash
        async with runtime.owner(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "issue_refund",
                            "id": "refund",
                            "args": runtime.retailer.execution_arguments(
                                runtime.action_id
                            ),
                        }
                    ],
                )
            ]
        ) as owner:
            with pytest.raises(RuntimeError):
                await runtime.invoke(owner, command)
        attempt = runtime.store.attempts(command.thread_id)[0]
        assert attempt.status == "executing" and attempt.terminal_event_json is None
        runtime.store.fault = lambda point: None
        resumed = runtime.admit("resume", command.thread_id)
        async with runtime.owner([AIMessage(content="Recovered")]) as owner:
            await runtime.invoke(owner, resumed)
        settled = runtime.store.attempts(command.thread_id)[0]
        event = json.loads(settled.terminal_event_json)
        assert (
            json.loads(event["attributes_json"])["timing_source"] == "receipt_recovery"
        )
        assert "latency_ms" not in event and event["occurred_at"] > attempt.source_time
        assert settled.resume_event_json is None
        assert (
            len([r for r in runtime.retailer.receipts() if r["kind"] != "confirmation"])
            == 1
        )

    asyncio.run(exercise())


def test_redaction_recovery_and_old_frozen_outbox_ignore_new_policy(runtime):
    import pytest
    from openeight import TransportError

    async def exercise():
        calls = []
        def redact(ctx, value):
            calls.append((ctx.kind, ctx.direction))
            return "[REDACTED]"
        command = runtime.admit()
        def crash(point):
            if point == "after_attempt_result":
                raise RuntimeError("interrupt settlement after effect")
        runtime.store.fault = crash
        async with runtime.owner([
            AIMessage(content="", tool_calls=[{
                "name": "issue_refund", "id": "refund",
                "args": runtime.retailer.execution_arguments(runtime.action_id),
            }]),
        ], capture_content="outputs", redact_content=redact) as owner:
            with pytest.raises(RuntimeError):
                await runtime.invoke(owner, command)
        assert calls.count(("tool_call", "output")) == 1
        runtime.store.fault = lambda _: None
        resumed = runtime.admit("resume", command.thread_id)
        # Failed delivery leaves the sanitized terminal Event frozen in SQLite.
        original_write = Transport.write_telemetry
        def fail_delivery(self, **body):
            if any(e.get("decision_id") for e in body.get("events", [])):
                raise TransportError()
            return original_write(self, **body)
        Transport.write_telemetry = fail_delivery
        try:
            async with runtime.owner([AIMessage(content="Recovered")], capture_content="outputs", redact_content=redact) as owner:
                with pytest.raises(TransportError):
                    await runtime.invoke(owner, resumed)
        finally:
            Transport.write_telemetry = original_write
        attempt = runtime.store.attempts(command.thread_id)[0]
        assert calls.count(("tool_call", "output")) == 2  # new receipt-derived field
        assert attempt.status == "succeeded" and "[REDACTED]" not in attempt.result_json
        frozen = attempt.terminal_event_json
        payload = json.loads(frozen)
        assert json.loads(payload["output_json"]) == "[REDACTED]"
        assert json.loads(payload["input_json"]) == json.loads(attempt.decision_input_json)
        assert json.loads(payload["attributes_json"])["timing_source"] == "receipt_recovery"
        previous_calls = len(calls)
        retried = runtime.admit("resume", command.thread_id)
        async with runtime.owner([AIMessage(content="Done")], redact_content=redact) as owner:
            await runtime.invoke(owner, retried)
        assert len(calls) == previous_calls  # no capture for old outbox or new default fields
        assert runtime.store.attempts(command.thread_id)[0].terminal_event_json == frozen
        delivered = [e for body in Transport.writes for e in body.get("events", []) if e.get("id") == attempt.event_id]
        assert delivered and all(e == payload for e in delivered)
        assert len([r for r in runtime.retailer.receipts() if r["kind"] != "confirmation"]) == 1

    asyncio.run(exercise())






def test_existing_content_bearing_outbox_replays_after_opt_out(runtime):
    from openeight import TransportError
    async def exercise():
        command = runtime.admit()
        Transport.lose_observe = True
        async with runtime.owner([AIMessage(content="OLD-CONTENT-SECRET")], capture_content="all") as owner:
            with pytest.raises(TransportError):
                await runtime.invoke(owner, command)
        frozen = tuple(row for row in runtime.store.pending(command.thread_id) if "OLD-CONTENT-SECRET" in row.payload_json)
        assert frozen
        def forbidden(*_):
            pytest.fail("old outbox replay invoked redactor")
        resumed = runtime.admit("resume", command.thread_id)
        async with runtime.owner(redact_content=forbidden) as owner:
            await runtime.invoke(owner, resumed)
        for row in frozen:
            payload = json.loads(row.payload_json)
            sent = [e for body in Transport.writes for e in body.get("events", []) if e["id"] == row.id]
            assert len(sent) >= 1 and all(e == payload for e in sent)
        assert not runtime.store.pending(command.thread_id)
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["none", "inputs", "outputs", "all"])
def test_all_durable_modes_preserve_native_receipt_and_required_governance(runtime, mode):
    async def exercise():
        command = runtime.admit(message="DURABLE-INPUT-SECRET")
        async with runtime.owner([
            AIMessage(content="", tool_calls=[{
                "name": "issue_refund", "id": "refund",
                "args": runtime.retailer.execution_arguments(runtime.action_id),
            }]),
            AIMessage(content="DURABLE-OUTPUT-SECRET"),
        ], capture_content=mode) as owner:
            result = await runtime.invoke(owner, command)
            assert "DURABLE-OUTPUT-SECRET" in result.output
        attempt, = runtime.store.attempts(command.thread_id)
        terminal = json.loads(attempt.terminal_event_json)
        assert json.loads(terminal["input_json"]) == json.loads(attempt.decision_input_json)
        assert ("output_json" in terminal) == (mode in ("outputs", "all"))
        if mode in ("outputs", "all"):
            assert json.loads(terminal["output_json"])["content"] == json.loads(attempt.result_json)["content"]
        models = [e for body in Transport.writes for e in body.get("events", []) if e["observation_kind"] == "llm_call"]
        assert models
        assert all(("input_json" in e) == (mode in ("inputs", "all")) for e in models)
        assert all(("output_json" in e) == (mode in ("outputs", "all")) for e in models)
        assert len([r for r in runtime.retailer.receipts() if r["kind"] != "confirmation"]) == 1
    asyncio.run(exercise())
