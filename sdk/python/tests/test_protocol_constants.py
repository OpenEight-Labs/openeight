from pathlib import Path

from openeight import _protocol_constants


def test_wire_and_environment_constants_are_exact_and_private():
    assert _protocol_constants.BASE_URL_ENV == "OPENEIGHT_ENDPOINT"
    assert _protocol_constants.API_KEY_ENV == "OPENEIGHT_API_KEY"
    assert _protocol_constants.AGENT_NAME_ENV == "OPENEIGHT_AGENT_NAME"
    assert _protocol_constants.AGENT_NAME_HEADER == "X-OpenEight-Agent-Name"
    assert _protocol_constants.API_KEY_PREFIX == "ctk_"

    import openeight

    assert "_protocol_constants" not in openeight.__all__
    for name in (
        "BASE_URL_ENV",
        "API_KEY_ENV",
        "AGENT_NAME_ENV",
        "AGENT_NAME_HEADER",
        "API_KEY_PREFIX",
    ):
        assert name not in openeight.__all__


def test_entry_points_use_shared_protocol_constants():
    package = Path(__file__).resolve().parents[1] / "openeight"
    active_sources = [
        package / "client.py",
        package / "errors.py",
        package / "__init__.py",
    ]
    literals = (
        "OPENEIGHT_ENDPOINT",
        "OPENEIGHT_API_KEY",
        "OPENEIGHT_AGENT_NAME",
        "X-OpenEight-Agent-Name",
        "ctk_",
    )
    for source in active_sources:
        text = source.read_text()
        for literal in literals:
            assert literal not in text, f"{literal} repeated in {source.name}"
