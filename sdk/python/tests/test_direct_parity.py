import json

import httpx
import pytest

from openeight import (
    Client,
    ConfigurationError,
    Event,
    PreparedDecision,
    RunCheckpoint,
    prepare_decision,
    prepare_run_identity,
)

RUN = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b12"
TOOL = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b13"
WORKFLOW = "018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b14"
TIME = "2026-09-01T12:00:00.123456789Z"


def client(handler):
    return Client(
        "https://example.com",
        api_key="ctk_synthetic_example",
        agent_name="example",
        transport=httpx.MockTransport(handler),
    )


def ack(request):
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            key: [{"id": row["id"], "replayed": False} for row in body.get(key, [])]
            for key in ("runs", "events")
        },
    )


def prepared(**kwargs):
    return prepare_decision(
        run_id=RUN,
        tool_id=TOOL,
        tool_version_id=TOOL,
        input={},
        policy_contract_set_digest="a" * 64,
        selected_values=kwargs.pop("selected_values", []),
        **kwargs,
    )


def test_prepared_mutation_and_exact_retry_with_stored_approval_verdict():
    values = [{"path": "amount", "value": {"n": 1}}]
    decision = prepared(
        selected_values=values, workflow_id=WORKFLOW, workflow_version="1.0.0"
    )
    assert isinstance(decision, PreparedDecision)
    values[0]["value"]["n"] = 2
    decision.selected_values[0]["value"]["n"] = 3
    requests = []

    def handler(request):
        requests.append(request.content)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "decision_id": body["decision_id"],
                "run_id": body["run_id"],
                "verdict": "approval_required",
                "reason": "approval",
                "replayed": True,
            },
        )

    with client(handler) as c:
        assert c.decide_prepared(decision).needs_approval
        assert c.decide_prepared(decision).replayed
    assert requests[0] == requests[1]
    assert json.loads(json.loads(requests[0])["selected_values_json"])[0]["value"] == {
        "n": 1
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"workflow_id": "slug", "workflow_version": "1.0.0"},
        {"workflow_id": WORKFLOW},
        {"workflow_id": WORKFLOW, "workflow_version": "1.0.0+" + "a" * 123},
        {"decision_id": TOOL, "source_time": "0001-01-01T00:00:00Z"},
    ],
)
def test_preparation_rejects_invalid_workflow_and_time(kwargs):
    with pytest.raises(ConfigurationError):
        prepared(**kwargs)


def test_checkpoint_preserves_lexical_attributes_and_legacy_absence():
    data = {
        "version": 1,
        "state": "suspended",
        "run_id": RUN,
        "started_at": TIME,
        "attributes": {"a": 1, "z": 2},
        "attributes_json": '{"z":2,"a":1.0}',
        "workflow_id": WORKFLOW,
        "workflow_version": "1.0.0",
    }
    cp = RunCheckpoint.from_dict(data)
    data["attributes"]["a"] = 8
    assert cp.attributes == {"a": 1, "z": 2}
    assert cp.to_dict()["attributes_json"] == '{"z":2,"a":1.0}'
    legacy = cp.to_dict()
    legacy.pop("attributes_json")
    assert RunCheckpoint.from_dict(legacy).attributes_json is None
    assert "attributes_json" not in RunCheckpoint.from_dict(legacy).to_dict()
    for raw in ['{"a":1,"a":1}', '{"a":NaN}', '{"a":2}', "[]"]:
        with pytest.raises(ConfigurationError):
            RunCheckpoint.from_dict({**cp.to_dict(), "attributes_json": raw})


def test_completion_retries_exact_payload_and_seals_admission():
    writes = []
    failed = False

    def handler(request):
        nonlocal failed
        body = json.loads(request.content)
        writes.append(body)
        if body["runs"][0]["status"] == "completed" and not failed:
            failed = True
            return httpx.Response(503, json={"code": "unavailable"})
        return ack(request)

    c = client(handler)
    identity = prepare_run_identity(
        run_id=RUN,
        started_at=TIME,
        attributes={"n": 1.0},
        workflow_id=WORKFLOW,
        workflow_version="1.0.0",
    )
    run = c.governed_run(identity=identity)
    with pytest.raises(Exception):
        run.close()
    with pytest.raises(ConfigurationError):
        run.submit_event(Event("example", "span", "invoke", TIME, "success"))
    with pytest.raises(ConfigurationError):
        run.suspend()
    run.close()
    run.close()
    c.close()
    completions = [w for w in writes if w["runs"][0]["status"] == "completed"]
    assert len(completions) == 2 and completions[0] == completions[1]
    assert completions[0]["runs"][0]["attributes_json"] == identity.attributes_json
    assert completions[0]["runs"][0]["workflow_id"] == WORKFLOW


def test_repeated_suspend_resume_and_local_conflicts():
    with client(ack) as c:
        run = c.governed_run(identity=prepare_run_identity(run_id=RUN, started_at=TIME))
        with pytest.raises(ConfigurationError):
            c.governed_run(run_id=RUN, started_at=TIME)
        cp = run.suspend()
        for _ in range(2):
            run = c.resume_governed_run(RunCheckpoint.from_dict(cp.to_dict()))
            cp = run.suspend()
        run = c.resume_governed_run(cp)
        run.close()
        with pytest.raises(ConfigurationError):
            c.resume_governed_run(cp)


def test_completion_cleanup_failure_is_terminal_without_resending(monkeypatch):
    writes = []

    def handler(request):
        writes.append(json.loads(request.content))
        return ack(request)

    c = client(handler)
    run = c.governed_run(
        identity=prepare_run_identity(
            run_id=RUN, started_at=TIME, attributes={"large": "x" * 60000}
        )
    )
    writer = run._writer
    original = writer.close

    def fail_cleanup(**kwargs):
        original(**kwargs)
        raise ConfigurationError("writer_closed")

    monkeypatch.setattr(writer, "close", fail_cleanup)
    for _ in range(2):
        with pytest.raises(ConfigurationError):
            run.close()
    assert sum(w["runs"][0]["status"] == "completed" for w in writes) == 1
    assert c._direct_identities == {} and c._owned_runs == {}
    c.close()


def test_timed_out_completion_keeps_ownership_until_transport_settles(monkeypatch):
    import threading

    from openeight import TransportError

    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    writes = []

    def handler(request):
        b = json.loads(request.content)
        writes.append(b)
        if b["runs"][0]["status"] == "completed" and len(writes) == 2:
            entered.set()
            assert release.wait(2)
            settled.set()
        return ack(request)

    monkeypatch.setattr("openeight.run._CLOSE_TIMEOUT_SECONDS", 0.02)
    c = client(handler)
    run = c.governed_run(identity=prepare_run_identity(run_id=RUN, started_at=TIME))
    try:
        with pytest.raises(TransportError):
            run.close()
        assert entered.is_set()
        with pytest.raises(ConfigurationError):
            run.close()
        assert len(writes) == 2 and c._owned_runs
    finally:
        release.set()
        assert settled.wait(2)
    assert c._direct_settlements[RUN].wait(2)
    run.close()
    assert writes[1] == writes[2]
    c.close()


@pytest.mark.parametrize(
    "operation", ["decide", "prepared", "startup", "resume", "checkpoint", "prepare"]
)
def test_direct_errors_do_not_retain_credentials_or_content(operation):
    from test_errors import _assert_exported_error_has_no_unsafe_sdk_traceback_state

    c = client(ack)
    try:
        with pytest.raises(ConfigurationError) as caught:
            if operation == "decide":
                c.decide(
                    run_id=RUN,
                    tool_id="invalid",
                    tool_version_id=TOOL,
                    input={"private": "content-canary"},
                    policy_contract_set_digest="a" * 64,
                    selected_values=[],
                )
            elif operation == "prepared":
                c.decide_prepared({"private": "content-canary"})
            elif operation == "startup":
                c.governed_run(
                    run_id="invalid", attributes={"private": "content-canary"}
                )
            elif operation == "resume":
                c.resume_governed_run({"private": "content-canary"})
            elif operation == "checkpoint":
                RunCheckpoint.from_dict({"private": "content-canary"})
            else:
                prepare_decision(
                    run_id=RUN,
                    tool_id="bad",
                    tool_version_id=TOOL,
                    input={},
                    policy_contract_set_digest="a" * 64,
                    selected_values=[{"path": "a", "value": "content-canary"}],
                )
        _assert_exported_error_has_no_unsafe_sdk_traceback_state(
            caught.value, {"ctk_synthetic_example", "content-canary"}
        )
    finally:
        c.close()


def test_raw_validation_preserves_extensions_metrics_and_edge_correlations():
    writes = []
    edge = "6ba7b810-9dad-51d1-80b4-00c04fd430c8"

    def handler(request):
        writes.append(json.loads(request.content))
        return ack(request)

    e = {
        "id": TOOL,
        "run_id": RUN,
        "name": "test",
        "observation_kind": "llm_call",
        "operation_kind": "invoke",
        "outcome": "success",
        "occurred_at": TIME,
        "input_tokens": "9223372036854775807",
        "output_tokens": 0,
        "parent_event_id": edge,
        "extension": {"safe": 1},
    }
    with client(handler) as c:
        c.write_telemetry(
            runs=[
                {
                    "id": RUN,
                    "started_at": TIME,
                    "status": "active",
                    "parent_run_id": edge,
                }
            ],
            events=[e],
        )
        for patch in [
            {"outcome": "invalid"},
            {"outcome": "error"},
            {"outcome": "success", "error_json": "{}"},
            {"cache_read_input_tokens": "9223372036854775808"},
            {"ttft_ms": 10, "latency_ms": 9},
            {"tool_id": TOOL},
            {"attributes_json": "[]"},
        ]:
            with pytest.raises(ConfigurationError):
                c.write_telemetry(events=[{**e, **patch}])
    assert len(writes) == 1
    assert writes[0]["events"][0] == e


def test_checkpoint_roundtrip_through_typescript_preserves_run_identity():
    import shutil
    import subprocess
    from pathlib import Path

    if shutil.which("node") is None:
        pytest.skip("Node is required for cross-language checkpoint qualification")
    module = (Path(__file__).resolve().parents[2] / "typescript/src/run.ts").as_uri()
    original = {
        "version": 1,
        "state": "suspended",
        "run_id": RUN,
        "started_at": TIME,
        "attributes": {"z": 2, "a": 1.0},
        "attributes_json": '{"z":2,"a":1.0}',
        "workflow_id": WORKFLOW,
        "workflow_version": "1.0.0",
    }
    code = f"import {{importRunCheckpoint,exportRunCheckpoint}} from {json.dumps(module)};let s='';for await(const c of process.stdin)s+=c;process.stdout.write(JSON.stringify(exportRunCheckpoint(importRunCheckpoint(JSON.parse(s)))));"
    result = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", code],
        input=json.dumps(original),
        text=True,
        capture_output=True,
        check=True,
    )
    restored = RunCheckpoint.from_dict(json.loads(result.stdout))
    assert restored.attributes_json == original["attributes_json"]
    assert (
        restored.workflow_id == WORKFLOW
        and restored.attributes == original["attributes"]
    )


def test_invalid_unicode_workflow_is_a_safe_configuration_failure():
    from test_errors import _assert_exported_error_has_no_unsafe_sdk_traceback_state

    c = client(ack)
    try:
        with pytest.raises(ConfigurationError) as caught:
            c.decide(
                run_id=RUN,
                tool_id=TOOL,
                tool_version_id=TOOL,
                input={},
                policy_contract_set_digest="a" * 64,
                selected_values=[],
                workflow_id=WORKFLOW,
                workflow_version="\ud800",
            )
        assert caught.value.reason == "invalid_workflow"
        _assert_exported_error_has_no_unsafe_sdk_traceback_state(
            caught.value, {"ctk_synthetic_example"}
        )
    finally:
        c.close()


def test_completion_clock_skew_retains_an_ordered_end_timestamp():
    from datetime import datetime, timedelta, timezone

    writes = []

    def handler(request):
        writes.append(json.loads(request.content))
        return ack(request)

    identity = prepare_run_identity(
        started_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    with client(handler) as c:
        run = c.governed_run(identity=identity)
        run.close()
    assert writes[-1]["runs"][0]["ended_at"] == identity.started_at
