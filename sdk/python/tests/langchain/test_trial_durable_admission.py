"""Commercial refusal gates and receipt recovery at the durable dispatch boundary."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from openeight import APIError
from openeight.langchain._durable_runtime import DurableInvocation


def invocation(status, decide):
    attempt = SimpleNamespace(
        status=status,
        thread_id="thread",
        deployment_id="deployment",
        native_call_json='{"id":"call","name":"refund","args":{}}',
        decision_id="decision",
        tool_call_id="call",
        tool_name="refund",
        tool_id="tool",
        tool_version_id="version",
        source_time="2026-10-24T00:00:00Z",
        decision_input_json='{"input":{},"selected_values":[],"policy_contract_set_digest":"digest"}',
        idempotency_key="external-key",
        revision=1,
    )

    async def run_call(call):
        return call()

    async def storage_call(call):
        return call()

    instance = object.__new__(DurableInvocation)
    instance.execution_lock = asyncio.Lock()
    instance.execution_tail = None
    instance.native_cancelled = False
    instance.thread_id, instance.run_id = "thread", "run"
    instance.store = SimpleNamespace(
        get_attempt=lambda *_: attempt,
        load=lambda *_: SimpleNamespace(deployment_id="deployment"),
    )
    instance.contracts = {
        "refund": SimpleNamespace(
            tool=SimpleNamespace(id="tool"), tool_version=SimpleNamespace(id="version")
        )
    }
    from openeight.langchain._frameworks.v1 import LangChainV1

    instance.owner = SimpleNamespace(
        _integration="langchain",
        _compatibility=LangChainV1(),
        _run_client_call=run_call,
        client=SimpleNamespace(decide=decide, approval_state=lambda _: "approved"),
        control=SimpleNamespace(durable=SimpleNamespace()),
    )
    instance.storage_call = storage_call
    # Correlation/schema preparation is covered by the managed adapter suite.
    instance._prepare = lambda *_: (
        attempt,
        {"id": "call", "name": "refund", "args": {}},
        None,
    )
    return instance, attempt


def test_durable_approved_resume_replays_before_dispatch():
    async def exercise():
        requests, effects = [], []

        def decide(**request):
            requests.append(request)
            if len(requests) == 2:
                raise APIError(403, "permission_denied", "TRIAL_EXPIRED")
            return SimpleNamespace(
                decision_id="decision", run_id="run", needs_approval=True
            )

        current, _ = invocation("waiting", decide)

        async def handler(_):
            effects.append("native dispatch")

        with pytest.raises(APIError) as failure:
            await current.tool_call(SimpleNamespace(tool_call={"id": "call"}), handler)
        assert failure.value.code == "TRIAL_EXPIRED"
        assert len(requests) == 2 and requests[0] == requests[1]
        assert effects == []

    asyncio.run(exercise())


def test_durable_confirmed_receipt_recovers_without_new_admission():
    async def exercise():
        effects, settled = [], []

        def refuse(**_):
            raise AssertionError("receipt recovery requested new execution authority")

        current, attempt = invocation("executing", refuse)

        async def reconcile(_):
            return SimpleNamespace(
                status="confirmed",
                result_json='{"content":"receipt recorded","status":"success"}',
            )

        async def settle(saved, result, **options):
            settled.append(options)
            saved.result_json = json.dumps(
                {"content": result.content, "status": result.status}
            )
            return saved

        current.owner.control.durable.reconcile = reconcile
        current.settle = settle

        async def handler(_):
            effects.append("duplicate dispatch")

        result = await current.tool_call(
            SimpleNamespace(tool_call={"id": "call"}), handler
        )
        assert result.content == "receipt recorded"
        assert settled == [{"recovered": True}]
        assert effects == [] and attempt.idempotency_key == "external-key"

    asyncio.run(exercise())
