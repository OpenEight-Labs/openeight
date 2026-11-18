"""Policy-owned readiness collects native values without a shared alias projection."""

import json
from dataclasses import replace

import pytest

from openeight import FieldSelector, PolicyInputRequirement, ToolReadiness, ToolVersion
from openeight.errors import ConfigurationError
from openeight.tools import collect_selected_values, parse_readiness

TOOL = "00000000-0000-4000-8000-000000000001"
VERSION = "00000000-0000-4000-8000-000000000002"
BINDING = "00000000-0000-4000-8000-000000000003"
POLICY = "00000000-0000-4000-8000-000000000004"
POLICY_VERSION = "00000000-0000-4000-8000-000000000005"


def requirement(fields, **changes):
    values = dict(
        binding_id=BINDING,
        policy_id=POLICY,
        policy_version_id=POLICY_VERSION,
        input_digest="b" * 64,
        field_selectors=tuple(fields),
    )
    values.update(changes)
    return PolicyInputRequirement(**values)


def readiness(*requirements):
    return ToolReadiness(
        None,
        "adjust",
        TOOL,
        VERSION,
        "registered",
        True,
        True,
        True,
        "ready",
        "a" * 64,
        tuple(requirements),
    )


def test_policy_collection_unions_native_paths_and_ignores_conflicting_aliases():
    snapshot = readiness(
        requirement([FieldSelector("quantity", "value")]),
        requirement([FieldSelector("ceiling", "value")], binding_id=POLICY),
    )
    arguments = {
        "quantity": 9007199254740993,
        "ceiling": None,
        "secret": "never upload",
    }
    assert collect_selected_values(arguments, snapshot) == (
        {"path": "ceiling", "value": None},
        {"path": "quantity", "value": 9007199254740993},
    )
    assert arguments["secret"] == "never upload"


def test_policy_collection_collapses_covered_children_and_copies_objects():
    snapshot = readiness(
        requirement([FieldSelector("customer")]),
        requirement([FieldSelector("customer.name")], binding_id=POLICY),
    )
    arguments = {"customer": {"name": "Ada", "tags": ["gold"]}, "unused": 1}
    selected = collect_selected_values(arguments, snapshot)
    arguments["customer"]["tags"].append("changed")
    assert selected == (
        {"path": "customer", "value": {"name": "Ada", "tags": ["gold"]}},
    )


def test_policy_collection_preserves_absent_paths_without_fabricating_null():
    snapshot = readiness(
        requirement([FieldSelector("missing"), FieldSelector("customer name")])
    )
    assert collect_selected_values({"customer name": None}, snapshot) == (
        {"path": "customer name", "value": None},
    )
    assert collect_selected_values({}, readiness(requirement([]))) == ()
    with pytest.raises(ConfigurationError):
        collect_selected_values({}, replace(snapshot, policy_contract_set_digest=None))


def test_readiness_parses_empty_requirements_and_rejects_malformed_references():
    wire = dict(
        name="adjust",
        tool_id=TOOL,
        tool_version_id=VERSION,
        registration_status="registered",
        governed=True,
        policy_ready=True,
        eligible=True,
        reason="ready",
        policy_contract_set_digest="a" * 64,
        policy_input_contracts=[
            dict(
                binding_id=BINDING,
                policy_id=POLICY,
                policy_version_id=POLICY_VERSION,
                input_digest="b" * 64,
                field_selectors=[],
            )
        ],
    )
    parsed = parse_readiness(wire)
    assert parsed.policy_input_contracts == (requirement([]),)
    for field, value in [
        ("input_digest", "sha256:" + "b" * 64),
        ("binding_id", VERSION.upper()),
    ]:
        broken = json.loads(json.dumps(wire))
        broken["policy_input_contracts"][0][field] = (
            value if field != "binding_id" else "foreign"
        )
        with pytest.raises(ConfigurationError):
            parse_readiness(broken)
    broken = dict(wire, policy_contract_set_digest=None)
    with pytest.raises(ConfigurationError):
        parse_readiness(broken)


def test_native_version_has_no_policy_selection_authority():
    version = ToolVersion(
        VERSION,
        TOOL,
        "native",
        "sha256:" + "1" * 64,
        {"type": "object"},
        "sha256:" + "2" * 64,
        "2026-10-17T00:00:00Z",
    )
    assert version.tool_contract_digest == "sha256:" + "2" * 64
    assert not hasattr(version, "select_input")
    assert not hasattr(version, "field_selectors")
