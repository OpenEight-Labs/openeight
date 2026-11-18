"""Native schemas expose submitted facts; retailer validation establishes truth."""

from typing import Annotated, Literal

from langchain_core.tools import InjectedToolArg, StructuredTool
from pydantic import ConfigDict, Field, create_model

from local import canonical
from retailer import ACTIONS, FIELDS, KINDS
from money import MAX_NUMBER

DESCRIPTIONS = {
    "get_case": (
        "Retrieve the existing bound support case, stored assessment and confirmed actions. "
        "Returns stored data only; does not modify records, execute actions or send messages."
    ),
    "get_order": (
        "Retrieve existing order facts, monetary amounts, currency and current facts revision. "
        "Returns stored data only; does not modify the order, allocate remedies or send messages."
    ),
    "get_payment": (
        "Retrieve the captured amount, refunded amount and whole outstanding balance in the original currency. "
        "For the synthetic billing case, also return gateway capture facts and any reconciliation discrepancy for billing review. "
        "Returns payment facts only; does not capture payments, issue refunds or modify business records."
    ),
    "get_shipment": (
        "Retrieve existing fulfilment status, recorded delivery time and current facts revision. "
        "Returns stored shipment facts only; does not create, dispatch, cancel or update shipments or orders."
    ),
    "search_support_knowledge": (
        "Retrieve static synthetic support workflow guidance as text. "
        "Does not execute the guidance, invoke external services, send messages or modify business records."
    ),
    "get_return_context": (
        "Retrieve recorded delivery time, completed UTC 24-hour periods since delivery and current facts revision. "
        "Returns delivery facts only; does not open, approve or modify a return or other business records."
    ),
    "get_resolution_status": (
        "Retrieve existing case and remedy progress, outstanding settlement amounts and completion blockers. "
        "Returns current state only; does not execute remedies, advance actions, close cases or send messages."
    ),
    "issue_refund": (
        "Refund the confirmed entire outstanding synthetic balance in its original currency. "
        "Updates the order's refunded balance, completes the refund action and any parent settlement, "
        "and records a payment receipt. Amount is in major currency units, not minor-unit counts. "
        "Decimal amounts are supported within the currency's fractional precision; "
        "this is not an integer-only operation. Does not transfer real money."
    ),
    "create_replacement": (
        "Submit the confirmed whole-order replacement using the order category and stored issue assessment. "
        "Allocates the order to this remedy, records the request and receipt, and updates case progress. "
        "Dispatch and delivery require separate synthetic operator events; this call does not ship goods."
    ),
    "create_return": (
        "Submit the confirmed whole-order return using the recorded age in completed UTC 24-hour periods. "
        "Allocates the order to this remedy, records the request and receipt, and updates case progress. "
        "Receipt, inspection and any refund settlement remain separate actions."
    ),
    "cancel_order": (
        "Execute the confirmed whole-order cancellation using the current recorded fulfilment status. "
        "Marks the synthetic order cancelled, allocates its remedy, and records action progress and a receipt. "
        "A positive outstanding balance requires a separately confirmed refund; this call does not refund payment."
    ),
    "resolve_case": (
        "Resolve the bound synthetic support case after its allocated remedy and any child settlement are complete. "
        "Requires a completed scoped action and the current facts revision. "
        "Updates case status and records a closure receipt. Does not send a customer message or execute a remedy."
    ),
    "add_case_note": (
        "Append a synthetic internal support note and receipt, and advance the stored facts revision. "
        "Modifies the local case history; does not deliver a customer reply or execute a remedy."
    ),
    "escalate_case": (
        "Mark the synthetic case handed off to human support and record the handoff reason and receipt. "
        "Modifies case status and history; does not resolve the case, execute a remedy or contact a real support team."
    ),
}


class ClosedTool(StructuredTool):
    @property
    def tool_call_schema(self):
        schema = super().tool_call_schema
        return create_model(
            schema.__name__,
            __doc__=self.description,
            __config__=ConfigDict(extra="forbid", strict=True),
            **{
                name: (field.annotation, field)
                for name, field in schema.model_fields.items()
            },
        )


def definitions(name):
    result = {}
    for field in FIELDS[name]:
        if field == "amount":
            result[field] = (
                int | float,
                Field(
                    ..., gt=0, le=MAX_NUMBER, allow_inf_nan=False,
                    description=(
                        "Amount in major currency units, not minor-unit counts. "
                        "Decimal amounts are supported within the currency's fractional precision."
                    ),
                ),
            )
        elif field in ("facts_revision", "age_days"):
            result[field] = (
                int,
                Field(..., ge=1 if field == "facts_revision" else 0, le=MAX_NUMBER),
            )
        elif field == "currency":
            result[field] = (str, Field(..., pattern=r"^[A-Z]{3}$"))
        elif field == "product_category":
            result[field] = (Literal["standard", "perishable"], ...)
        elif field == "verified_issue":
            result[field] = (
                Literal[
                    "damaged",
                    "lost",
                    "wrong_item",
                    "quality",
                    "change_of_mind",
                    "billing",
                    "other",
                ],
                ...,
            )
        elif field == "fulfilment_status":
            result[field] = (
                Literal["unfulfilled", "packing", "shipped", "delivered", "cancelled"],
                ...,
            )
        else:
            limit = (
                128
                if field.endswith("_id")
                else {"query": 512, "body": 8192, "reason": 500}[field]
            )
            result[field] = (str, Field(..., min_length=1, max_length=limit))
    if name in KINDS:
        result["idempotency_key"] = (Annotated[str, InjectedToolArg], ...)
    return result


def build_tools(retailer):
    tools = []
    for name in FIELDS:
        schema = create_model(
            name + "Arguments",
            __config__=ConfigDict(extra="forbid", strict=True),
            **definitions(name),
        )

        def handler(tool_name):
            async def execute(**arguments):
                if tool_name in KINDS:
                    key = arguments.pop("idempotency_key")
                    result = retailer.mutate(tool_name, arguments, key)
                else:
                    result = retailer.read(tool_name, arguments)
                return canonical(result)

            return execute

        tools.append(
            ClosedTool(
                name=name,
                description=DESCRIPTIONS[name]
                + f" Connector: retailer. Action: {ACTIONS[name]}.",
                args_schema=schema,
                coroutine=handler(name),
            )
        )
    return tuple(tools)
