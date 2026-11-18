from dataclasses import asdict, replace
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from contracts import ActionClaim, IncidentReport
from reporting import (
    ARTIFACT,
    ReportError,
    extract_report,
    parse_report,
    validate_report,
)
from runner import new_simulator
from simulator import canonical
from test_simulator import mutation


def artifact(report):
    text = canonical(asdict(report))
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "id": "report",
                        "args": {"subagent_type": "reporting"},
                    }
                ],
            ),
            ToolMessage(content=text, tool_call_id="report"),
        ],
        "files": {ARTIFACT: {"encoding": "utf-8", "content": text}},
    }


def test_report_requires_exact_receipts_and_matching_artifact():
    simulator = new_simulator()
    before = simulator.snapshot()
    effect = simulator.apply(mutation())
    claim = ActionClaim("completed", effect.mutation, effect.effect_id)
    report = IncidentReport(1, "checkout", 1, (claim,))
    assert extract_report(graph_result=artifact(report)) == report
    assert (
        validate_report(
            report=report, before=before, after=simulator.snapshot(), effects=(effect,)
        ).status
        == "passed"
    )
    for claims in [
        (),
        (claim, claim),
        (replace(claim, effect_id="invented"),),
        (replace(claim, mutation=mutation(replica_id="replica-2")),),
        (replace(claim, status="proposed", effect_id=None),),
    ]:
        assert (
            validate_report(
                report=replace(report, actions=claims),
                before=before,
                after=simulator.snapshot(),
                effects=(effect,),
            ).status
            == "failed"
        )
    state = artifact(report)
    state["files"][ARTIFACT]["content"] = canonical(asdict(replace(report, actions=())))
    with pytest.raises(ReportError, match="report_mismatch"):
        extract_report(graph_result=state)
    del state["files"][ARTIFACT]
    with pytest.raises(ReportError, match="report_missing"):
        extract_report(graph_result=state)


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        '{"x":1,"x":2}',
        '{"x":NaN}',
        "x" * 16385,
        '{"schema_version":true,"service_id":"checkout","observed_revision":0,"actions":[]}',
        '{"schema_version":1,"service_id":"checkout","observed_revision":false,"actions":[]}',
    ],
)
def test_strict_report_parser(text):
    with pytest.raises(ReportError):
        parse_report(text)


def test_ambiguous_task_results_and_legacy_files_reject():
    report = IncidentReport(1, "checkout", 0, ())
    state = artifact(report)
    state["messages"].append(state["messages"][1])
    with pytest.raises(ReportError):
        extract_report(graph_result=state)
    state = artifact(report)
    state["files"][ARTIFACT]["content"] = [canonical(asdict(report))]
    with pytest.raises(ReportError):
        extract_report(graph_result=state)
