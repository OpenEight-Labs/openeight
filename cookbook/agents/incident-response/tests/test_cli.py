import asyncio
import json

import cli


def test_missing_configuration_has_stable_error_and_no_secret(monkeypatch, capsys):
    monkeypatch.delenv("OPENEIGHT_ENDPOINT", raising=False)
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_do_not_print")
    assert cli.main(["run", "rollback", "--mode", "scripted"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "error_code": "invalid_configuration"
    }


def test_provider_requires_explicit_configuration(monkeypatch, capsys):
    monkeypatch.setenv("OPENEIGHT_ENDPOINT", "http://localhost:7777")
    monkeypatch.setenv("OPENEIGHT_AGENT_NAME", "incident-test")
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_do_not_print")
    monkeypatch.delenv("INCIDENT_PROVIDER_MODEL", raising=False)
    assert cli.main(["run", "rollback", "--mode", "provider"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "error_code": "invalid_configuration"
    }


def test_raw_runtime_error_is_not_printed(monkeypatch, capsys):
    monkeypatch.setenv("OPENEIGHT_ENDPOINT", "http://localhost:7777")
    monkeypatch.setenv("OPENEIGHT_AGENT_NAME", "incident-test")
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_do_not_print")

    async def fail(**kwargs):
        raise RuntimeError("ctk_do_not_print provider raw request")

    monkeypatch.setattr(cli, "run_scenario", fail)
    assert cli.main(["run", "rollback", "--mode", "scripted"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error_code": "execution_failed"}


def test_cli_cancellation_is_not_success(monkeypatch, capsys):
    monkeypatch.setenv("OPENEIGHT_ENDPOINT", "http://localhost:7777")
    monkeypatch.setenv("OPENEIGHT_AGENT_NAME", "incident-test")
    monkeypatch.setenv("OPENEIGHT_API_KEY", "ctk_do_not_print")

    async def cancel(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(cli, "run_scenario", cancel)
    assert cli.main(["run", "rollback", "--mode", "scripted"]) == 130
    assert capsys.readouterr().out == ""
