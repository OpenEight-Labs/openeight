"""Opt-in real-time Decision outage and telemetry-backoff soak.

Set OPENEIGHT_OUTAGE_SECONDS=3600 to hold the local endpoint unavailable for
one hour. Catalog admission uses fixtures; Decision and telemetry HTTP use the
production client. No deployed service or clock is changed.
"""

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import test_enforcement_facade

from openeight import APIError, Client
from openeight._delivery import DeliveryWorker
from openeight._time import utc_timestamp
from openeight.delivery import DeliveryConfig
from openeight.telemetry import uuid7

make = test_enforcement_facade.make


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("OPENEIGHT_OUTAGE_SECONDS"), reason="opt-in wall-clock outage soak"
)
async def test_sustained_outage_blocks_decisions_and_retries_telemetry(make):
    duration = float(os.environ["OPENEIGHT_OUTAGE_SECONDS"])
    interval = float(os.getenv("OPENEIGHT_OUTAGE_PROBE_INTERVAL", "60"))
    assert duration > 0 and interval > 0
    evidence = os.getenv("OPENEIGHT_OUTAGE_EVIDENCE")
    state = {"unavailable": False, "decisions": [], "telemetry": []}
    lock = threading.Lock()

    def report(event, **values):
        record = {
            "event": event,
            "utc": datetime.now(timezone.utc).isoformat(),
            **values,
        }
        encoded = json.dumps(record, sort_keys=True)
        print(encoded, flush=True)
        if evidence:
            with Path(evidence).open("a") as stream:
                stream.write(encoded + "\n")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            kind = "decisions" if self.path.endswith("/Decide") else "telemetry"
            with lock:
                unavailable = state["unavailable"]
                state[kind].append((time.monotonic(), unavailable, body))
            if unavailable:
                status, payload = 503, {"code": "unavailable", "message": ""}
            elif kind == "decisions":
                status, payload = (
                    200,
                    {
                        "decision_id": body["decision_id"],
                        "run_id": body["run_id"],
                        "verdict": "allow",
                        "reason": "allowed",
                        "replayed": False,
                    },
                )
            else:
                status, payload = (
                    200,
                    {
                        key: [
                            {"id": item["id"], "replayed": False} for item in body[key]
                        ]
                        for key in ("runs", "events")
                    },
                )
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    worker = None
    failed_effects = []
    probes = 0
    try:
        with Client(endpoint, api_key="ctk_test", agent_name="test") as client:
            client._adapter_owned_send = True

            async def probe(execution, blocked):
                nonlocal probes
                owner, _, _, effects = make()
                owner.client.decide = client.decide
                try:
                    call = owner.invoke if execution == "sync" else owner.ainvoke
                    if blocked:
                        with pytest.raises(APIError) as error:
                            result = call({"messages": [("user", "execute")]})
                            if execution == "async":
                                await result
                        assert error.value.status_code == 503
                        assert effects == []
                        failed_effects.append(effects)
                        probes += 1
                    else:
                        result = call({"messages": [("user", "execute")]})
                        if execution == "async":
                            result = await result
                        assert result["messages"][-1].content == "done"
                        assert effects == ["hello"]
                finally:
                    assert owner.close(2).closed

            for execution in ("sync", "async"):
                await probe(execution, False)
            with lock:
                state["unavailable"] = True
            began = time.monotonic()
            report("outage_started", duration_seconds=duration, endpoint=endpoint)
            worker = DeliveryWorker(client, DeliveryConfig(), "complete", None)
            run_id = uuid7()
            job = worker.enqueue(
                "run",
                run_id,
                {"id": run_id, "status": "running", "started_at": utc_timestamp()},
            )
            assert job is not None
            while True:
                for execution in ("sync", "async"):
                    await probe(execution, True)
                elapsed = time.monotonic() - began
                status = worker.status()
                assert status.pending_records == 1
                assert status.failed_records == status.dropped_records == 0
                with lock:
                    decision_requests = sum(item[1] for item in state["decisions"])
                    retries = sum(item[1] for item in state["telemetry"])
                assert decision_requests == probes
                report(
                    "outage_progress",
                    elapsed_seconds=round(elapsed, 3),
                    blocked_attempts=probes,
                    decision_http_requests=decision_requests,
                    telemetry_http_requests=retries,
                    pending_records=status.pending_records,
                    tool_effects=0,
                )
                if elapsed >= duration:
                    break
                await asyncio.sleep(min(interval, duration - elapsed))
            with lock:
                state["unavailable"] = False
                retry_times = [item[0] for item in state["telemetry"] if item[1]]
            assert time.monotonic() - began >= duration
            report("outage_ended", elapsed_seconds=round(time.monotonic() - began, 3))
            for execution in ("sync", "async"):
                await probe(execution, False)
            status = await asyncio.to_thread(worker.flush, 40)
            assert (
                status.pending_records
                == status.failed_records
                == status.dropped_records
                == 0
            )
            assert job.acknowledged
            assert all(effects == [] for effects in failed_effects)
            if duration >= 3:
                assert len(retry_times) >= 2
            gaps = [b - a for a, b in zip(retry_times, retry_times[1:])]
            for index, gap in enumerate(gaps):
                nominal = min(30, 2 ** min(index, 5))
                assert gap >= nominal * 0.9
            report(
                "passed",
                outage_seconds=round(elapsed, 3),
                blocked_attempts=probes,
                telemetry_attempts_during_outage=len(retry_times),
                first_retry_gaps_seconds=[round(gap, 3) for gap in gaps[:8]],
                recovered_tool_effects=2,
                outage_tool_effects=0,
                telemetry_acknowledged=True,
            )
            worker.close(2)
            worker = None
    finally:
        if worker is not None:
            worker.close(0.1)
        server.shutdown()
        thread.join(2)
        server.server_close()
