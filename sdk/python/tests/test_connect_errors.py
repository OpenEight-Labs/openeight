import base64
import copy
import json
from pathlib import Path

import httpx
import pytest

from openeight import APIError, Client
from openeight._connect_errors import governance_reason

CASES = json.loads(
    (Path(__file__).parents[2] / "contracts/errors.json").read_text()
)["errors"]


def error_info(reason, domain):
    # Independent test encoding of ErrorInfo's two length-delimited strings.
    reason, domain = reason.encode(), domain.encode()
    raw = bytes([10, len(reason)]) + reason + bytes([18, len(domain)]) + domain
    return [{"type": "google.rpc.ErrorInfo", "value": base64.b64encode(raw).decode().rstrip("=")}]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["reason"])
def test_public_error_catalog_decodes_without_server_details(case):
    body = {
        "code": case["connect_code"],
        "message": "private server detail must not escape",
        "details": error_info(case["reason"], case["domain"]),
    }
    with Client(
        "https://control.example",
        api_key="ctk_secret",
        agent_name="worker",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(case["http_status"], json=body)
        ),
    ) as client:
        with pytest.raises(APIError) as raised:
            client.approval_state("018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b11")
    error = raised.value
    assert error.code == case["reason"]
    assert error.category == case["connect_code"]
    assert error.status_code == case["http_status"]
    assert error.message == "request failed"
    assert error.__cause__ is None


def test_known_wire_bytes_decode_independently_of_catalog_generation():
    details = [{"type": "google.rpc.ErrorInfo", "value": "ChFUT09MX05PVF9HT1ZFUk5FRBIQb3BlbmVpZ2h0LmdvdmVybg"}]
    assert governance_reason(400, "failed_precondition", details) == "TOOL_NOT_GOVERNED"


def test_error_details_fail_closed():
    details = error_info("TOOL_NOT_GOVERNED", "openeight.govern")
    encoded = details[0]["value"]
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
    invalid = [
        None, {}, [], details * 2, details * 9,
        [{"type": "other", "value": encoded}],
        [{"type": "google.rpc.ErrorInfo", "value": "?"}],
        [{"type": "google.rpc.ErrorInfo", "value": "a" * 1025}],
        error_info("UNKNOWN_REASON", "openeight.govern"),
        error_info("TOOL_NOT_GOVERNED", "openeight.access"),
    ]
    for data in [
        bytes([10, raw[1] + 3, 239, 187, 191]) + raw[2:],
        raw[:-1], raw + raw, b"\x0a\x80", b"\x0a\x01\xff",
        raw.replace(b"openeight.govern", b"untrusted.domain"),
    ]:
        bad = copy.deepcopy(details)
        bad[0]["value"] = base64.b64encode(data).decode()
        invalid.append(bad)
    for bad in invalid:
        assert governance_reason(400, "failed_precondition", bad) is None
    assert governance_reason(409, "failed_precondition", details) is None
    assert governance_reason(400, "invalid_argument", details) is None
    padded = copy.deepcopy(details)
    padded[0]["value"] += "=" * (-len(encoded) % 4)
    assert governance_reason(400, "failed_precondition", padded) == "TOOL_NOT_GOVERNED"


def test_error_info_metadata_is_not_returned():
    details = error_info("TOOL_NOT_GOVERNED", "openeight.govern")
    raw = base64.b64decode(details[0]["value"] + "=" * (-len(details[0]["value"]) % 4))
    entry = b"\x0a\x03sql\x12\x07private"
    raw += bytes([26, len(entry)]) + entry
    details[0]["value"] = base64.b64encode(raw).decode()
    assert governance_reason(400, "failed_precondition", details) == "TOOL_NOT_GOVERNED"
