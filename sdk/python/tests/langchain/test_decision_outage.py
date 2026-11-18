"""Exercise managed tool admission through real HTTP Decision outages.

Catalog and telemetry use the facade fixtures; Decisions use the production
SDK client against a disposable loopback server. No shared server is stopped.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import test_enforcement_facade

from openeight import APIError, Client, TransportError

make = test_enforcement_facade.make


@pytest.fixture
def decision_endpoint():
    state = {"mode": "allow", "requests": []}
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, body))
            mode = state["mode"]
            if mode == "timeout":
                release.wait(45)
                self.close_connection = True
                return
            if mode == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            if mode == "unavailable":
                status, payload = 503, {"code": "unavailable", "message": ""}
            else:
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
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    # Bind without listening so refusal uses a reserved port, not a guessed one.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
    server.server_bind()
    thread = None

    def start():
        nonlocal thread
        server.server_activate()
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        thread.start()

    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield endpoint, state, start
    finally:
        release.set()
        if thread is not None:
            server.shutdown()
            thread.join(2)
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", ["sync", "async"])
@pytest.mark.parametrize("failure", ["refused", "disconnect", "timeout", "unavailable"])
async def test_decision_outage_blocks_effects_then_recovers(
    make, decision_endpoint, execution, failure, record_property
):
    endpoint, state, start = decision_endpoint
    state["mode"] = failure
    if failure != "refused":
        start()

    async def invoke(owner):
        if execution == "async":
            return await owner.ainvoke({"messages": [("user", "execute")]})
        return owner.invoke({"messages": [("user", "execute")]})

    with Client(endpoint, api_key="ctk_test", agent_name="test", timeout=0.2) as client:
        # Use the same owned HTTP path as managed adapters, including total deadlines.
        client._adapter_owned_send = True
        owner, _, _, effects = make()
        owner.client.decide = client.decide
        started = time.monotonic()
        expected = APIError if failure == "unavailable" else TransportError
        with pytest.raises(expected) as raised:
            await invoke(owner)
        elapsed = time.monotonic() - started
        assert effects == []
        record_property("outage_elapsed_seconds", round(elapsed, 3))
        # Async facade calls carry their own 30-second total deadline.
        assert elapsed < (35 if execution == "async" else 3)
        if failure == "unavailable":
            assert raised.value.status_code == 503
        if failure == "refused":
            assert state["requests"] == []
            start()
        else:
            assert len(state["requests"]) == 1

        state["mode"] = "allow"
        recovered, _, _, recovered_effects = make()
        recovered.client.decide = client.decide
        result = await invoke(recovered)
        assert result["messages"][-1].content == "done"
        assert recovered_effects == ["hello"]
        assert effects == []
        assert state["requests"][-1][0] == (
            "/api/v1/openeight.govern.v1.GovernService/Decide"
        )
