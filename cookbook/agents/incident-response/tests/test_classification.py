import httpx
import pytest
from acceptance.classification import (
    ExpectedTool,
    HTTPClassificationReader,
    ClassificationError,
    await_classifications,
)

EXPECTED = (ExpectedTool("tool-id", "version-id", "diagnostics.read_logs", "read"),)


@pytest.mark.parametrize(
    "case,code",
    [
        ("ok", None),
        ("pending", "classification_timeout"),
        ("failed", "classification_failed"),
        ("mismatch", "classification_mismatch"),
        ("version", "catalog_changed"),
        ("foreign", "catalog_changed"),
        ("missing", "classification_read_failed"),
        ("unknown", "classification_protocol_error"),
    ],
)
async def test_classification_is_separate_from_readiness(case, code):
    def handler(request):
        assert request.headers["authorization"] == "Bearer human-fixture"
        assert request.url.path == "/api/v1/tools/tool-id"
        data = dict(
            id="tool-id",
            agent_id="agent-id",
            name="diagnostics.read_logs",
            current_version_id="version-id",
            capability="read",
            classification=dict(status="classified", error_code=None),
            governed=False,
        )
        if case == "pending":
            data["classification"]["status"] = "running"
        if case == "failed":
            data["classification"].update(
                status="failed", error_code="provider_unavailable"
            )
        if case == "mismatch":
            data["capability"] = "write"
        if case == "version":
            data["current_version_id"] = "new-version"
        if case == "foreign":
            data["agent_id"] = "foreign-agent"
        if case == "missing":
            return httpx.Response(404)
        if case == "unknown":
            data["classification"]["status"] = "new-status"
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(
        base_url="https://fixture.invalid",
        headers={"Authorization": "Bearer human-fixture"},
        transport=httpx.MockTransport(handler),
    ) as client:
        reader = HTTPClassificationReader(
            client=client, agent_id="agent-id", expected=EXPECTED
        )
        if code:
            with pytest.raises(ClassificationError) as failure:
                await await_classifications(
                    reader=reader,
                    expected=EXPECTED,
                    timeout_seconds=0.03,
                    poll_seconds=0.005,
                )
            assert failure.value.code == code
        else:
            result = await await_classifications(reader=reader, expected=EXPECTED)
            assert result[0].actual == "read"


@pytest.mark.parametrize("case", ["missing", "duplicate", "blocked"])
async def test_classification_polling_rejects_incomplete_records_and_cancels_io(case):
    import asyncio
    from acceptance.classification import ClassificationObservation

    class Reader:
        async def read(self, tool_ids):
            if case == "blocked":
                await asyncio.sleep(10)
            if case == "missing":
                return ()
            observation = ClassificationObservation(
                "tool-id",
                "version-id",
                "diagnostics.read_logs",
                "read",
                "read",
                "classified",
                None,
            )
            return (observation, observation)

    with pytest.raises(ClassificationError) as failure:
        await await_classifications(
            reader=Reader(), expected=EXPECTED, timeout_seconds=0.02, poll_seconds=0.005
        )
    assert failure.value.code == (
        "classification_timeout"
        if case == "blocked"
        else "classification_records_missing"
    )
