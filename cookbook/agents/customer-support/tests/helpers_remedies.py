"""Structured synthetic confirmations shared by example contract tests."""

from uuid import uuid4


def confirm(retailer, kind="refund", *, thread_id=None, **values):
    order = retailer.read("get_order", {"order_id": retailer.context.order_id})
    payload = {
        "remedy": kind,
        "reason": "Customer confirmed the displayed whole-order remedy",
    }
    if kind == "refund":
        payment = retailer.read("get_payment", {"order_id": retailer.context.order_id})
        payload.update(amount=payment["amount"], currency=payment["currency"])
    if kind == "replacement":
        payload["shipping_method"] = "standard"
    payload.update(values)
    return retailer.control(
        {
            "command_id": str(uuid4()),
            "thread_id": thread_id or str(uuid4()),
            "case_id": retailer.context.case_id,
            "facts_revision": order["facts_revision"],
            "kind": "confirmation",
            "payload": payload,
        }
    )["action_id"]


def event(retailer, action_id, event, **changes):
    envelope = {
        "command_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "case_id": retailer.context.case_id,
        "facts_revision": retailer.read(
            "get_order", {"order_id": retailer.context.order_id}
        )["facts_revision"],
        "kind": "physical_event",
        "payload": {"action_id": action_id, "event": event},
    }
    envelope.update(changes)
    return retailer.control(envelope)


def resolve(retailer, action_id, **changes):
    order = retailer.read("get_order", {"order_id": retailer.context.order_id})
    return retailer.mutate(
        "resolve_case",
        {"case_id": retailer.context.case_id, "action_id": action_id,
         "facts_revision": order["facts_revision"], **changes},
        str(uuid4()),
    )
