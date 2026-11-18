import pytest
from concurrent.futures import ThreadPoolExecutor
from collections import UserDict, defaultdict
import json

from openeight import FieldSelector, ToolRegistration, ToolReadiness, ToolVersion
from openeight.errors import ConfigurationError


def test_registration_deep_snapshots_schema():
    schema = {"type": "object", "properties": {"amount": {"type": "integer"}}}
    registration = ToolRegistration(
        name="refund", description="Refund", input_schema=schema
    )
    schema["properties"]["amount"]["type"] = "string"
    assert registration.input_schema["properties"]["amount"]["type"] == "integer"
    with pytest.raises((TypeError, AttributeError)):
        registration.input_schema["properties"]["amount"]["type"] = "string"


@pytest.mark.parametrize(
    "field", ["governed", "connector", "action", "capability", "version", "fields"]
)
def test_machine_registration_rejects_governance_fields(field):
    with pytest.raises(TypeError):
        ToolRegistration(
            name="x", description="x", input_schema={}, **{field: "unsupported"}
        )


@pytest.mark.parametrize(
    "selector",
    [
        lambda: FieldSelector("customer.\ud800name"),
        lambda: FieldSelector("customer.name", "\udfff"),
    ],
)
def test_field_selector_translates_invalid_unicode(selector):
    with pytest.raises(ConfigurationError) as caught:
        selector()

    assert caught.value.reason == "invalid_tool_registration"
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_readiness_normalizes_unpopulated_optional_ids():
    from openeight.tools import parse_readiness

    readiness = parse_readiness(
        {
            "request_index": 0,
            "name": "x",
            "tool_id": "",
            "tool_version_id": "",
            "registration_status": "unavailable",
            "governed": False,
            "policy_ready": True,
            "eligible": True,
            "reason": "observe_only",
        }
    )
    assert isinstance(readiness, ToolReadiness)
    assert readiness.tool_id is None and readiness.tool_version_id is None


def selection_readiness(fields):
    from openeight import PolicyInputRequirement

    return ToolReadiness(
        None,
        "adjust",
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
        "registered",
        True,
        True,
        True,
        "ready",
        "a" * 64,
        (
            PolicyInputRequirement(
                "00000000-0000-4000-8000-000000000003",
                "00000000-0000-4000-8000-000000000004",
                "00000000-0000-4000-8000-000000000005",
                "b" * 64,
                tuple(
                    FieldSelector(field) if isinstance(field, str) else field
                    for field in fields
                ),
            ),
        ),
    )


def collect(fields, arguments):
    from openeight.tools import collect_selected_values

    return collect_selected_values(arguments, selection_readiness(fields))


def test_collection_native_paths_and_independent_snapshots():
    from openeight.tools import collect_selected_values

    fields = selection_readiness(
        ["value", "customer.country", FieldSelector("customer.name", "name")]
    )
    source = {
        "value": [{"items": [1, None]}],
        "customer": {"country": {"code": ["US"]}, "name": ["Ada"]},
    }
    expected = (
        {"path": "customer.country", "value": {"code": ["US"]}},
        {"path": "customer.name", "value": ["Ada"]},
        {"path": "value", "value": [{"items": [1, None]}]},
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        snapshots = list(
            executor.map(
                lambda value: collect_selected_values(value, fields), [source] * 64
            )
        )
    assert all(snapshot == expected for snapshot in snapshots)
    source["customer"].clear()
    source["value"].clear()
    assert all(snapshot == expected for snapshot in snapshots)
    snapshots[0][0]["value"]["code"].append("changed")
    assert all(snapshot == expected for snapshot in snapshots[1:])


def test_collection_empty_missing_null_and_mapping_root():
    assert collect([], {}) == ()
    assert collect(["value"], {"value": None}) == ({"path": "value", "value": None},)
    assert collect(["value"], UserDict(value=1)) == ({"path": "value", "value": 1},)
    defaults = []
    missing = defaultdict(lambda: defaults.append(True))
    assert collect(["value"], missing) == ()
    assert defaults == [] and dict(missing) == {}
    for nested in ([], None, {}, {1: "US"}):
        assert collect(["customer.country"], {"customer": nested}) == ()


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        -float("inf"),
        1 << 63,
        -(1 << 63) - 1,
        "\ud800",
        {"\udfff": 1},
        {1: "x"},
        (1,),
        {1},
        object(),
    ],
)
def test_collection_rejects_non_json(value):
    with pytest.raises(ConfigurationError):
        collect(["value"], {"value": value})


def test_collection_rejects_cycles_and_preserves_integer_precision():
    assert collect(["value"], {"value": [-(1 << 63), (1 << 63) - 1]}) == (
        {"path": "value", "value": [-(1 << 63), (1 << 63) - 1]},
    )
    for value in ([], {}):
        if isinstance(value, list):
            value.append(value)
        else:
            value["cycle"] = value
        with pytest.raises(ConfigurationError):
            collect(["value"], {"value": value})
    with pytest.raises(ConfigurationError):
        collect(["value"], {"value": "x" * 65535})
    with pytest.raises(ConfigurationError):
        collect(["value"], {"value": ["x" * 65000] * 17})


@pytest.mark.parametrize(
    "fields",
    [
        ["customer.country", FieldSelector("value", "customer")],
        [FieldSelector("value", "customer"), "customer.country"],
    ],
)
def test_policy_requirement_rejects_alias_parent_collision(fields):
    with pytest.raises(ConfigurationError):
        selection_readiness(fields)


@pytest.mark.parametrize("revision", ["", "malformed", "00000000-0000-0000-0000-000000000000", "11111111-1111-4111-8111-11111111111A", 0, False])
def test_readiness_rejects_invalid_governance_revision(revision):
    from openeight.tools import parse_readiness

    with pytest.raises(ConfigurationError):
        parse_readiness(dict(name="read", registration_status="registered", governed=False,
                             policy_ready=True, eligible=True, reason="read_only",
                             governance_revision_id=revision))


def test_readiness_preserves_optional_revision_without_inventing_old_server_metadata():
    from openeight.tools import parse_readiness

    response = dict(name="read", registration_status="registered", governed=False,
                    policy_ready=True, eligible=True, reason="read_only")
    assert parse_readiness(response).governance_revision_id is None
    response["governance_revision_id"] = "11111111-1111-4111-8111-111111111111"
    assert parse_readiness(response).governance_revision_id == response["governance_revision_id"]
