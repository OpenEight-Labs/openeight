"""Publication uses frozen preconditions across transport retries and conflicts."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from openeight import ConfigurationError, ToolRegistration
from openeight.langchain._catalog import CatalogPublisher


class Server:
    def __init__(self):
        self.tool, self.version = str(uuid4()), str(uuid4())
        self.reads = 0
        self.requests = []
        self.status = "registered"
        self.fail_once = False

    def get_tool_readiness(self, ids, **kwargs):
        self.reads += 1
        return SimpleNamespace(
            tools=(
                SimpleNamespace(
                    name="refund", tool_id=self.tool, tool_version_id=self.version
                ),
            )
        )

    def register_tools(self, facts, **kwargs):
        self.requests.append(facts)
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError()
        return tuple(
            SimpleNamespace(
                request_index=i,
                status=self.status,
                tool=SimpleNamespace(id=self.tool, name=f.name),
                tool_version=SimpleNamespace(id=self.version, tool_id=self.tool),
            )
            for i, f in enumerate(facts)
        )


def fact(field="amount"):
    return ToolRegistration(
        name="refund",
        description="Refund",
        input_schema={"type": "object", "properties": {field: {"type": "integer"}}},
    )


def test_transport_retry_preserves_original_conditions():
    server = Server()
    publisher = CatalogPublisher(server)
    server.fail_once = True
    with pytest.raises(TimeoutError):
        publisher.register_tools((fact(),))
    expected = server.requests[0][0].expected_current_version_id
    server.version = str(uuid4())
    publisher.register_tools((fact(),))
    assert server.requests[1] == server.requests[0]
    assert server.requests[1][0].expected_current_version_id == expected
    assert server.reads == 1


def test_conflict_never_refreshes_or_retries():
    server = Server()
    publisher = CatalogPublisher(server)
    server.status = "conflict"
    assert publisher.register_tools((fact(),))[0].status == "conflict"
    server.version = str(uuid4())
    for candidate in (fact(), fact("price")):
        with pytest.raises(ConfigurationError) as error:
            publisher.register_tools((candidate,))
        assert error.value.reason == "tool_registration_conflict"
    assert len(server.requests) == 1
    assert server.reads == 1


def test_new_schema_uses_last_acknowledged_pair_and_same_schema_replays():
    server = Server()
    publisher = CatalogPublisher(server)
    publisher.register_tools((fact(),))
    previous = server.version
    server.version = str(
        uuid4()
    )  # Human configuration can produce a same-schema successor.
    publisher.register_tools((fact(),))
    assert server.requests[-1][0].expected_current_version_id == previous
    publisher.register_tools((fact("price"),))
    assert server.requests[-1][0].expected_current_version_id == server.version
    assert server.requests[-1][0].expected_tool_id == server.tool
    assert server.reads == 1


@pytest.mark.parametrize(
    "conditions",
    [
        {"expected_tool_id": str(uuid4())},
        {"expected_tool_id": "", "expected_current_version_id": ""},
    ],
)
def test_registration_rejects_incomplete_or_empty_conditions(conditions):
    with pytest.raises(ConfigurationError):
        ToolRegistration(
            name="refund", description="Refund", input_schema={}, **conditions
        )


def test_stale_generation_cannot_replace_or_poison_current_publication():
    server = Server()
    publisher = CatalogPublisher(server)
    old, new = fact(), fact("price")
    publisher.select((old,))
    publisher.register_tools((old,))
    publisher.select((new,))
    publisher.register_tools((new,))
    with pytest.raises(ConfigurationError) as error:
        publisher.register_tools((old,))
    assert error.value.reason == "tool_binding_changed"
    publisher.register_tools((new,))
    assert len(server.requests) == 3


def test_description_change_is_published_after_acknowledgement():
    server = Server()
    publisher = CatalogPublisher(server)
    original = fact()
    publisher.register_tools((original,))
    changed = ToolRegistration(
        name=original.name,
        description="Updated description",
        input_schema=original.input_schema,
    )
    publisher.register_tools((changed,))
    assert server.requests[-1][0].description == "Updated description"


def test_batches_reserve_conditional_publication_bytes():
    import json
    from openeight.client import _serialize_tool_facts
    from openeight.langchain._catalog import catalog_batches

    facts = tuple(
        ToolRegistration(
            name=f"t{i}",
            description="",
            input_schema={"type": "object", "description": "a" * 261920},
        )
        for i in range(4)
    )
    batches = tuple(catalog_batches(facts))
    assert len(batches) == 2
    for batch in batches:
        prepared = tuple(
            ToolRegistration(
                name=f.name,
                description=f.description,
                input_schema=f.input_schema,
                expected_tool_id=str(uuid4()),
                expected_current_version_id=str(uuid4()),
            )
            for f in batch
        )
        assert (
            len(
                json.dumps(
                    {"tools": [_serialize_tool_facts(f) for f in prepared]},
                    ensure_ascii=False,
                ).encode()
            )
            <= 1048576
        )


def test_pending_transport_retry_can_settle_after_new_generation_selected():
    server = Server()
    publisher = CatalogPublisher(server)
    old, new = fact(), fact("price")
    publisher.select((old,))
    server.fail_once = True
    with pytest.raises(TimeoutError):
        publisher.register_tools((old,))
    publisher.select((new,))
    with pytest.raises(ConfigurationError) as error:
        publisher.register_tools((new,))
    assert error.value.reason == "tool_publication_unsettled"
    publisher.register_tools((old,))
    assert server.requests[0] == server.requests[1]
    publisher.register_tools((new,))
    with pytest.raises(ConfigurationError) as error:
        publisher.register_tools((old,))
    assert error.value.reason == "tool_binding_changed"
    assert len(server.requests) == 3


def test_rejected_batch_does_not_create_unsent_pending_entries():
    server = Server()
    publisher = CatalogPublisher(server)
    server.fail_once = True
    with pytest.raises(TimeoutError):
        publisher.register_tools((fact(),))
    first = ToolRegistration(name="other", description="first", input_schema={})
    with pytest.raises(ConfigurationError):
        publisher.register_tools((first, fact("price")))
    assert "other" not in publisher.entries
    second = ToolRegistration(name="other", description="second", input_schema={})
    publisher.register_tools((second,))
    assert server.requests[-1][0].description == "second"


def test_conflicting_discovered_name_is_not_published_by_list_order(monkeypatch):
    from openeight.langchain import _catalog

    tools = tuple(
        SimpleNamespace(
            name="refund",
            description="Refund",
            tool_call_schema={
                "type": "object",
                "properties": {field: {"type": "integer"}},
            },
        )
        for field in ("amount", "price", "amount")
    )
    monkeypatch.setattr(
        _catalog,
        "inspect_agent",
        lambda *args: SimpleNamespace(
            tools=tools, discovery_completeness="complete", discovery_diagnostic=None
        ),
    )
    catalog = _catalog.BoundToolCatalog(object())
    assert catalog.facts == ()
    assert catalog.completeness == "partial"
    assert catalog.diagnostic == "conflicting_tool_definitions"
