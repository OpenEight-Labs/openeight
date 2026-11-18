import httpx
import pytest

from openeight import Client, ProtocolError


@pytest.mark.parametrize("field", ["fully_registered", "fully_policy_ready"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_aggregate_readiness_rejects_non_boolean_flags(field, value):
    readiness = {
        "tools": [],
        "fully_registered": False,
        "registration_reason": "",
        "fully_policy_ready": False,
        "policy_reason": "",
    }
    readiness[field] = value
    with Client(
        "https://example.com",
        api_key="ctk_synthetic_example",
        agent_name="example",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"readiness": readiness})
        ),
    ) as client:
        with pytest.raises(ProtocolError):
            client.get_tool_readiness([])


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("policy_ready", [False, True])
def test_aggregate_readiness_preserves_boolean_flags(registered, policy_ready):
    with Client(
        "https://example.com",
        api_key="ctk_synthetic_example",
        agent_name="example",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "readiness": {
                        "tools": [],
                        "fully_registered": registered,
                        "registration_reason": "",
                        "fully_policy_ready": policy_ready,
                        "policy_reason": "",
                    }
                },
            )
        ),
    ) as client:
        result = client.get_tool_readiness([])
        assert result.fully_registered is registered
        assert result.fully_policy_ready is policy_ready
