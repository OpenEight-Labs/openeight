"""CLI status projections; execution state remains owned by the service/store."""

import json
from urllib.parse import quote
from local import LocalError


def snapshot(store, retailer_for_case, thread_id, integration_status, console_origin, receipts):
    thread = store.load(thread_id)
    if thread is None:
        raise LocalError("not_found")
    retailer = retailer_for_case(thread.case_id)
    c = retailer.context
    case = retailer.read("get_case", {"case_id": c.case_id})
    order = retailer.read("get_order", {"order_id": c.order_id})
    payment = retailer.read("get_payment", {"order_id": c.order_id})
    shipment = retailer.read("get_shipment", {"order_id": c.order_id})
    labels = {
        "get_case": "Checking the case",
        "get_order": "Checking the order",
        "get_payment": "Checking payment",
        "get_shipment": "Checking delivery",
        "search_support_knowledge": "Checking support guidance",
        "get_return_context": "Checking delivery context",
        "get_resolution_status": "Checking remedy progress",
        "issue_refund": "Refund",
        "create_replacement": "Replacement",
        "create_return": "Return request",
        "cancel_order": "Cancellation",
        "resolve_case": "Case resolution",
        "add_case_note": "Case note",
        "escalate_case": "Human handoff",
    }
    activity = []
    for a in (
        store.attempts(thread_id)
        if store.deployment.mode == "integrated"
        else store.local_attempts(thread_id)
    ):
        selected = json.loads(a.native_call_json)["args"]
        item = {
            "label": labels.get(a.tool_name, "Support action"),
            "status": a.status,
            "reason": a.reason_code,
        }
        if a.tool_name == "issue_refund":
            item["amount"] = selected.get("amount")
            item["currency"] = selected.get("currency")
        if a.status == "waiting" and console_origin and store.deployment.workspace_id:
            item["approval_url"] = (
                console_origin
                + "/w/"
                + quote(store.deployment.workspace_id, safe="")
                + "/approvals/"
                + quote(a.decision_id, safe="")
            )
        activity.append(item)
    running = False
    try:
        with store.acquire(thread_id):
            pass
    except LocalError as error:
        if error.code != "execution_busy":
            raise
        running = True
    accepted = any(
        c.thread_id == thread_id for c in store.commands_for_dispatch()
    )
    interrupted = bool(thread.active_command_id) and not running
    return {
        "thread_id": thread.id,
        "mode": store.deployment.mode,
        "integration_status": integration_status,
        "run_id": thread.run_id,
        "status": thread.status,
        "interrupted": interrupted,
        "running": running or accepted,
        "case": {
            "case_id": c.case_id,
            "subject": case["customer_report"],
            "status": case["status"],
            "order_id": c.order_id,
            "total_amount": order["total_amount"],
            "currency": order["currency"],
            "refundable_amount": payment["amount"],
            "fulfilment_status": shipment["fulfilment_status"],
            "facts_revision": order["facts_revision"],
            "verified_issue": case["verified_issue"],
            "product_category": order["product_category"],
            "actions": case["actions"],
        },
        "turns": [
            {"speaker": r["speaker"], "body": r["body"]}
            for r in store.transcript(thread_id)
        ],
        "activity": activity,
        "receipts": receipts(thread_id),
        "observation_incomplete": bool(thread.observation_incomplete),
        "can_reply": thread.status == "waiting_customer"
        and not interrupted
        and not running
        and not accepted,
        "can_resume": (
            thread.status in {"waiting", "reconciliation_required"} or interrupted
        )
        and not running
        and not accepted,
        "can_continue": thread.status == "waiting_customer"
        and not interrupted
        and not running
        and not accepted,
        "run_url": console_origin
        + "/w/"
        + quote(store.deployment.workspace_id, safe="")
        + "/observatory/runs/"
        + quote(thread.run_id, safe="")
        if console_origin and store.deployment.workspace_id and thread.run_id
        else None,
    }
