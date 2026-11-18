import base64

import httpx
import pytest

from openeight import APIError, Client, ConfigurationError


@pytest.mark.parametrize('integration', [None, 'otel', 'langchain', 'deepagents', 'other'])
def test_first_read_and_telemetry_carry_only_explicit_integration(integration):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={'state': 'pending'})
    with Client('https://control.example', api_key='ctk_secret', agent_name='worker',
                integration=integration, transport=httpx.MockTransport(respond)) as client:
        client.approval_state('018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b11')
        assert seen[0].headers.get('X-OpenEight-Agent-Integration') == integration
        assert client._machine_headers().get('X-OpenEight-Agent-Integration') == integration


@pytest.mark.parametrize('value', ['', 'unknown', ' OTEL ', 1, [], {}])
def test_invalid_integration_rejected_before_transport(value):
    with pytest.raises(ConfigurationError) as error:
        Client('https://control.example', api_key='ctk_secret', agent_name='worker', integration=value)
    assert error.value.reason == 'invalid_agent_integration'


@pytest.mark.parametrize('reason,code', [('invalid_agent_integration','invalid_argument'),
                                        ('agent_integration_mismatch','failed_precondition')])
def test_identity_connect_reason_survives_sanitization(reason, code):
    domain = 'openeight.identity'
    raw = bytes([10, len(reason)]) + reason.encode() + bytes([18, len(domain)]) + domain.encode()
    body = {'code': code, 'message':'discarded', 'details':[{'type':'google.rpc.ErrorInfo',
            'value':base64.b64encode(raw).decode()}]}
    with Client('https://control.example', api_key='ctk_secret', agent_name='worker',
                transport=httpx.MockTransport(lambda _:httpx.Response(400,json=body))) as client:
        with pytest.raises(APIError) as error:
            client.approval_state('018f0b7b-6b5b-7abc-8f31-8f0c2e9f3b11')
    assert error.value.code == reason
    assert error.value.message == 'request failed'
