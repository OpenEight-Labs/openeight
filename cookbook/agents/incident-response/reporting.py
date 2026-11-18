"""Validate the actual reporting task and its root virtual-file artifact."""

from collections.abc import Mapping
from contracts import (
    ActionClaim,
    IncidentReport,
    Mutation,
    ReportValidation,
    ServiceSnapshot,
    EffectRecord,
)
from simulator import (
    BusinessError,
    identifier,
    strict_json,
    validate_mutation,
)

ARTIFACT = "/reports/incident.json"


class ReportError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def parse_report(text):
    try:
        if not isinstance(text, str) or len(text.encode("utf-8")) > 16384:
            raise ValueError()
        doc = strict_json(text)
        if not isinstance(doc, dict) or set(doc) != {
            "schema_version",
            "service_id",
            "observed_revision",
            "actions",
        }:
            raise ValueError()
        if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
            raise ValueError()
        identifier(doc["service_id"])
        if (
            type(doc["observed_revision"]) is not int
            or doc["observed_revision"] < 0
            or type(doc["actions"]) is not list
        ):
            raise ValueError()
        actions = []
        for claim in doc["actions"]:
            if type(claim) is not dict or set(claim) != {
                "status",
                "mutation",
                "effect_id",
            }:
                raise ValueError()
            if claim["status"] not in ("proposed", "completed"):
                raise ValueError()
            mutation = Mutation(**claim["mutation"])
            validate_mutation(mutation)
            if claim["effect_id"] is not None:
                identifier(claim["effect_id"])
            actions.append(ActionClaim(claim["status"], mutation, claim["effect_id"]))
        return IncidentReport(
            1, doc["service_id"], doc["observed_revision"], tuple(actions)
        )
    except (ValueError, TypeError, KeyError, BusinessError):
        raise ReportError("report_malformed") from None


def extract_report(*, graph_result: Mapping[str, object]) -> IncidentReport:
    messages = graph_result.get("messages", ())
    ids = [
        call["id"]
        for message in messages
        for call in getattr(message, "tool_calls", ())
        if call.get("name") == "task"
        and call.get("args", {}).get("subagent_type") == "reporting"
    ]
    if not ids:
        raise ReportError("report_missing")
    if len(ids) != 1:
        raise ReportError("report_mismatch")
    results = [
        message
        for message in messages
        if getattr(message, "tool_call_id", None) == ids[0]
    ]
    if len(results) != 1 or getattr(results[0], "status", "success") != "success":
        raise ReportError("report_mismatch")
    report = parse_report(results[0].content)
    files = graph_result.get("files", {})
    if not isinstance(files, Mapping) or ARTIFACT not in files:
        raise ReportError("report_missing")
    artifact = files[ARTIFACT]
    if not isinstance(artifact, Mapping) or artifact.get("encoding") != "utf-8":
        raise ReportError("report_malformed")
    if parse_report(artifact.get("content")) != report:
        raise ReportError("report_mismatch")
    return report


def validate_report(
    *,
    report: IncidentReport,
    before: ServiceSnapshot,
    after: ServiceSnapshot,
    effects: tuple[EffectRecord, ...],
) -> ReportValidation:
    valid = (
        report.service_id == after.service_id == before.service_id
        and report.observed_revision == after.revision
    )
    receipts = {effect.effect_id: effect for effect in effects}
    seen_actions, seen_effects = set(), set()
    for claim in report.actions:
        mutation = claim.mutation
        valid &= (
            mutation.service_id == after.service_id
            and mutation.action_id not in seen_actions
        )
        seen_actions.add(mutation.action_id)
        try:
            args = validate_mutation(mutation)
            if mutation.operation == "rollback_release":
                valid &= args["target_release"] in ("v41", "v42")
            if mutation.operation == "restart_replica":
                valid &= args["replica_id"] in dict(after.restart_counts)
        except (ValueError, BusinessError):
            valid = False
        if claim.status == "completed":
            receipt = receipts.get(claim.effect_id)
            valid &= (
                receipt is not None
                and receipt.mutation == mutation
                and claim.effect_id not in seen_effects
            )
            seen_effects.add(claim.effect_id)
        else:
            valid &= claim.effect_id is None and all(
                e.mutation.action_id != mutation.action_id for e in effects
            )
    valid &= seen_effects == set(receipts)
    return ReportValidation(
        "passed" if valid else "failed",
        ARTIFACT,
        report,
        None if valid else "report_claim_invalid",
    )
