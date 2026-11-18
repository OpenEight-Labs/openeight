"""Case-bound synthetic retailer with exact money and atomic lifecycle receipts."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
import math
from pathlib import Path
from uuid import UUID, uuid4

from money import MAX_NUMBER, money, decimal_text, numeric
from fixtures import BILLING_CAPTURES

from local import (
    LocalError,
    canonical,
    connection,
    now,
    read_deployment,
    transaction,
)

ISSUES = (
    "damaged",
    "lost",
    "wrong_item",
    "quality",
    "change_of_mind",
    "billing",
    "other",
)
FULFILMENT = ("unfulfilled", "packing", "shipped", "delivered", "cancelled")
KINDS = {
    "issue_refund": "refund",
    "create_replacement": "replacement",
    "create_return": "return",
    "cancel_order": "cancellation",
    "resolve_case": "closure",
    "add_case_note": "note",
    "escalate_case": "escalation",
}
FIELDS = {
    "get_case": ("case_id",),
    "get_order": ("order_id",),
    "get_payment": ("order_id",),
    "get_shipment": ("order_id",),
    "search_support_knowledge": ("query",),
    "get_return_context": ("order_id",),
    "get_resolution_status": ("case_id",),
    "issue_refund": ("action_id", "facts_revision", "amount", "currency"),
    "create_replacement": (
        "action_id",
        "facts_revision",
        "product_category",
        "verified_issue",
    ),
    "create_return": ("action_id", "facts_revision", "age_days"),
    "cancel_order": ("action_id", "facts_revision", "fulfilment_status"),
    "resolve_case": ("case_id", "action_id", "facts_revision"),
    "add_case_note": ("case_id", "body"),
    "escalate_case": ("case_id", "reason"),
}
ACTIONS = {name: name.replace("get_", "").replace("create_", "") for name in FIELDS}


def identifier(value):
    if type(value) is not str or not value or len(value) > 128 or not value.isascii():
        raise LocalError("invalid_input")


def uuid(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise LocalError("invalid_action_key") from None


def text(value, limit):
    if type(value) is not str or not value.strip() or len(value.encode()) > limit:
        raise LocalError("invalid_input")


def validate_arguments(tool, arguments):
    if (
        tool not in FIELDS
        or type(arguments) is not dict
        or set(arguments) != set(FIELDS[tool])
    ):
        raise LocalError("invalid_input")
    enums = {
        "product_category": ("standard", "perishable"),
        "verified_issue": ISSUES,
        "fulfilment_status": FULFILMENT,
    }
    for name, value in arguments.items():
        if name.endswith("_id"):
            identifier(value)
        elif name in ("facts_revision", "age_days"):
            if (
                type(value) is not int
                or not (1 if name == "facts_revision" else 0) <= value <= MAX_NUMBER
            ):
                raise LocalError("invalid_input")
        elif name == "amount":
            if (
                type(value) not in (int, float)
                or (type(value) is float and not math.isfinite(value))
                or not 0 < value <= MAX_NUMBER
            ):
                raise LocalError("invalid_input")
        elif name == "currency":
            if (
                type(value) is not str
                or len(value) != 3
                or not value.isascii()
                or not value.isupper()
                or not value.isalpha()
            ):
                raise LocalError("invalid_input")
        elif name in enums:
            if value not in enums[name]:
                raise LocalError("invalid_input")
        else:
            text(value, {"reason": 500, "body": 8192, "query": 512}[name])


@dataclass(frozen=True)
class CaseContext:
    deployment_id: str
    case_id: str
    customer_id: str
    order_id: str


def insert(db, table, values):
    db.execute(
        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


class RetailerRepository:
    def __init__(self, state, context, *, fault=None, clock=None):
        self.path = Path(state) / "retailer.sqlite"
        self.context = context
        self.fault = fault or (lambda point: None)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def bind(cls, state, deployment, case_id, **kwargs):
        with connection(Path(state) / "retailer.sqlite") as db:
            if read_deployment(db) != deployment:
                raise LocalError("deployment_mismatch")
            row = db.execute(
                "SELECT c.*,o.customer_id FROM cases c JOIN orders o ON o.deployment_id=c.deployment_id AND o.id=c.order_id WHERE c.deployment_id=? AND c.id=?",
                (deployment.id, case_id),
            ).fetchone()
            if row is None:
                raise LocalError("not_found")
            return cls(
                state,
                CaseContext(
                    deployment.id, case_id, row["customer_id"], row["order_id"]
                ),
                **kwargs,
            )

    def _facts(self, db):
        c = self.context
        case = db.execute(
            "SELECT * FROM cases WHERE deployment_id=? AND id=? AND order_id=?",
            (c.deployment_id, c.case_id, c.order_id),
        ).fetchone()
        order = db.execute(
            "SELECT * FROM orders WHERE deployment_id=? AND id=? AND customer_id=?",
            (c.deployment_id, c.order_id, c.customer_id),
        ).fetchone()
        if case is None or order is None:
            raise LocalError("not_found")
        order = dict(order)
        amounts = {
            key: money(
                order[key],
                order["currency"],
                stored=True,
                positive=key == "total_amount",
            )
            for key in (
                "total_amount",
                "captured_amount",
                "refunded_amount",
                "replacement_value",
            )
        }
        if (
            not amounts["refunded_amount"]
            <= amounts["captured_amount"]
            <= amounts["total_amount"]
        ):
            raise LocalError("invalid_balance")
        return dict(case), order

    def _action(self, db, action_id):
        row = db.execute(
            "SELECT * FROM actions WHERE deployment_id=? AND id=? AND case_id=?",
            (self.context.deployment_id, action_id, self.context.case_id),
        ).fetchone()
        if row is None:
            raise LocalError("not_found")
        return dict(row)

    def _actions(self, db):
        return [
            dict(r)
            for r in db.execute(
                "SELECT * FROM actions WHERE deployment_id=? AND case_id=? ORDER BY created_at,id",
                (self.context.deployment_id, self.context.case_id),
            )
        ]

    def age(self, order):
        if order["delivered_at"] is None:
            raise LocalError("delivery_unknown")
        delivered = datetime.fromisoformat(order["delivered_at"].replace("Z", "+00:00"))
        current = self.clock()
        if delivered.tzinfo is None or delivered > current:
            raise LocalError("invalid_delivery_time")
        return int((current - delivered).total_seconds() // 86400)

    def _balance(self, order):
        with localcontext() as context:
            context.prec = 40
            return money(
                decimal_text(
                    Decimal(order["captured_amount"])
                    - Decimal(order["refunded_amount"])
                ),
                order["currency"],
                stored=True,
            )

    def _validate(self, tool, arguments):
        validate_arguments(tool, arguments)
        for selector, expected in (
            ("case_id", self.context.case_id),
            ("order_id", self.context.order_id),
        ):
            if selector in arguments and arguments[selector] != expected:
                raise LocalError("not_found")

    def _project(self, row):
        result = dict(row)
        for field in (
            "amount",
            "total_amount",
            "captured_amount",
            "refunded_amount",
            "replacement_value",
        ):
            if field in result and result[field] is not None:
                result[field] = numeric(result[field], result["currency"])
        return result

    def read(self, tool, arguments):
        self._validate(tool, arguments)
        with connection(self.path) as db:
            db.execute("BEGIN")
            case, order = self._facts(db)
            revision = {"facts_revision": order["revision"]}
            actions = [self._project(a) for a in self._actions(db)]
            if tool == "get_case":
                return {
                    **case,
                    **revision,
                    "customer_id": self.context.customer_id,
                    "actions": actions,
                }
            if tool == "get_order":
                return {**self._project(order), **revision}
            if tool == "get_payment":
                payment = {
                    "amount": numeric(
                        decimal_text(self._balance(order)), order["currency"]
                    ),
                    "currency": order["currency"],
                    "captured_amount": numeric(
                        order["captured_amount"], order["currency"]
                    ),
                    "refunded_amount": numeric(
                        order["refunded_amount"], order["currency"]
                    ),
                    **revision,
                }
                if self.context.case_id == "case_billing":
                    # Gateway facts can disagree with the order record; review before settlement.
                    captures = (
                        [dict(row) for row in BILLING_CAPTURES]
                        if order["currency"] == "USD"
                        and money(order["total_amount"], "USD", stored=True) == 90
                        else []
                    )
                    payment.update(
                        captures=captures,
                        ledger_captured_amount=sum(row["amount"] for row in captures),
                        reconciliation_required=bool(captures)
                        and sum(row["amount"] for row in captures)
                        != payment["captured_amount"],
                        review_reason="possible_duplicate_charge"
                        if captures
                        else "manual_billing_review",
                    )
                return payment
            if tool == "get_shipment":
                return {
                    "fulfilment_status": order["fulfilment_status"],
                    "delivered_at": order["delivered_at"],
                    **revision,
                }
            if tool == "get_return_context":
                return {
                    "delivered_at": order["delivered_at"],
                    "age_days": self.age(order),
                    **revision,
                }
            if tool == "get_resolution_status":
                return {
                    "case_status": case["status"],
                    "actions": actions,
                    "allocated_action_id": order["allocated_action_id"],
                    "settlement_amount": numeric(
                        decimal_text(self._balance(order)), order["currency"]
                    ),
                    "currency": order["currency"],
                    "completion_blockers": [
                        a["status"]
                        for a in actions
                        if a["status"] not in ("superseded", "completed")
                    ],
                    **revision,
                }
            if tool == "search_support_knowledge":
                return [
                    {
                        "body": "Customer confirms the displayed whole-order remedy. Replacement awaits dispatch and delivery. Return awaits receipt and inspection. Positive payment obligations need a separately confirmed refund. Explain only receipted progress; support handoff is not resolution."
                    }
                ]
            raise LocalError("invalid_input")

    def execution_arguments(self, action_id):
        with connection(self.path) as db:
            db.execute("BEGIN")
            case, order = self._facts(db)
            action = self._action(db, action_id)
            result = {"action_id": action_id, "facts_revision": order["revision"]}
            if action["kind"] == "refund":
                result.update(
                    amount=numeric(action["amount"], action["currency"]),
                    currency=action["currency"],
                )
            elif action["kind"] == "replacement":
                if case["verified_issue"] is None:
                    raise LocalError("assessment_unknown")
                result.update(
                    product_category=order["product_category"],
                    verified_issue=case["verified_issue"],
                )
            elif action["kind"] == "return":
                result["age_days"] = self.age(order)
            else:
                result["fulfilment_status"] = order["fulfilment_status"]
            return result

    def lookup_receipt(self, key):
        with connection(self.path) as db:
            self._facts(db)
            row = db.execute(
                "SELECT * FROM receipts WHERE deployment_id=? AND idempotency_key=?",
                (self.context.deployment_id, key),
            ).fetchone()
            if row and row["case_id"] != self.context.case_id:
                raise LocalError("receipt_conflict")
            return dict(row) if row else None

    def receipts(self):
        with connection(self.path) as db:
            self._facts(db)
            return tuple(
                dict(r)
                for r in db.execute(
                    "SELECT * FROM receipts WHERE deployment_id=? AND case_id=? ORDER BY created_at,id",
                    (self.context.deployment_id, self.context.case_id),
                )
            )

    def _prior(self, db, key, request):
        row = db.execute(
            "SELECT * FROM receipts WHERE deployment_id=? AND idempotency_key=?",
            (self.context.deployment_id, key),
        ).fetchone()
        if row:
            if row["case_id"] != self.context.case_id or row["request_json"] != request:
                raise LocalError("receipt_conflict")
            return json.loads(row["result_json"])

    def _receipt(
        self,
        db,
        kind,
        key,
        request,
        result,
        order,
        *,
        action=None,
        amount=None,
        facts=None,
    ):
        result = {"receipt_id": str(uuid4()), "kind": kind, **result}
        case_after, order_after = self._facts(db)
        captured_facts = {
            "revision_before": order["revision"],
            "revision_after": order_after["revision"],
            "order_before": {
                field: order[field]
                for field in (
                    "currency",
                    "captured_amount",
                    "refunded_amount",
                    "fulfilment_status",
                    "product_category",
                    "delivered_at",
                    "allocated_action_id",
                )
            },
            "order_after": {
                field: order_after[field]
                for field in (
                    "currency",
                    "captured_amount",
                    "refunded_amount",
                    "fulfilment_status",
                    "allocated_action_id",
                )
            },
            "case_status_after": case_after["status"],
            "verified_issue": case_after["verified_issue"],
        }
        if action is not None:
            captured_facts["action_status_after"] = self._action(db, action)["status"]
        captured_facts.update(facts or {})
        insert(
            db,
            "receipts",
            {
                "deployment_id": self.context.deployment_id,
                "id": result["receipt_id"],
                "created_at": now(),
                "case_id": self.context.case_id,
                "action_id": action,
                "kind": kind,
                "idempotency_key": key,
                "request_json": request,
                "result_json": canonical(result),
                "facts_json": canonical(captured_facts),
                "amount": decimal_text(amount) if amount is not None else None,
                "currency": order["currency"] if amount is not None else None,
            },
        )
        return result

    def _revise(self, db, order):
        if order["revision"] >= MAX_NUMBER:
            raise LocalError("revision_exhausted")
        db.execute(
            "UPDATE orders SET revision=revision+1,updated_at=? WHERE deployment_id=? AND id=?",
            (now(), self.context.deployment_id, self.context.order_id),
        )

    def _stage(self, db, action_id, status):
        db.execute(
            "UPDATE actions SET status=?,completed_at=?,updated_at=? WHERE deployment_id=? AND id=? AND case_id=?",
            (
                status,
                now() if status == "completed" else None,
                now(),
                self.context.deployment_id,
                action_id,
                self.context.case_id,
            ),
        )

    def _case_stage(self, db, status):
        db.execute(
            "UPDATE cases SET status=?,updated_at=? WHERE deployment_id=? AND id=?",
            (status, now(), self.context.deployment_id, self.context.case_id),
        )

    def control(self, envelope):
        if type(envelope) is not dict or set(envelope) != {
            "command_id",
            "thread_id",
            "case_id",
            "facts_revision",
            "kind",
            "payload",
        }:
            raise LocalError("invalid_input")
        uuid(envelope["command_id"])
        uuid(envelope["thread_id"])
        if envelope["case_id"] != self.context.case_id:
            raise LocalError("not_found")
        revision = envelope["facts_revision"]
        if (
            type(revision) is not int
            or not 1 <= revision <= MAX_NUMBER
            or type(envelope["payload"]) is not dict
        ):
            raise LocalError("invalid_input")
        kind, payload = envelope["kind"], envelope["payload"]
        request, key = canonical(envelope), "control:" + envelope["command_id"]
        with connection(self.path) as db:
            with transaction(db):
                case, order = self._facts(db)
                prior = self._prior(db, key, request)
                if prior:
                    return prior
                if revision != order["revision"]:
                    raise LocalError("stale_facts")
                if kind == "confirmation":
                    if case["status"] in ("resolved", "handed_off"):
                        raise LocalError("case_terminal")
                    result, action_id = self._confirm(
                        db, payload, order, envelope["command_id"]
                    )
                    receipt_kind = "confirmation"
                elif kind == "assessment":
                    if (
                        set(payload) != {"verified_issue"}
                        or payload["verified_issue"] not in ISSUES
                        or case["status"] in ("resolved", "handed_off")
                    ):
                        raise LocalError("invalid_input")
                    db.execute(
                        "UPDATE cases SET verified_issue=?,updated_at=? WHERE deployment_id=? AND id=?",
                        (
                            payload["verified_issue"],
                            now(),
                            self.context.deployment_id,
                            self.context.case_id,
                        ),
                    )
                    result, action_id, receipt_kind = (
                        {"verified_issue": payload["verified_issue"]},
                        None,
                        "assessment",
                    )
                elif kind == "physical_event":
                    if set(payload) != {"action_id", "event"}:
                        raise LocalError("invalid_input")
                    identifier(payload["action_id"])
                    action = self._action(db, payload["action_id"])
                    result = self._physical(db, case, order, action, payload["event"])
                    action_id, receipt_kind = action["id"], payload["event"]
                    if result.get("already_applied"):
                        original_receipt = result.pop("receipt_id")
                        return self._receipt(
                            db,
                            "control_replay",
                            key,
                            request,
                            {**result, "original_receipt_id": original_receipt},
                            order,
                            action=action_id,
                            facts={
                                "revision_before": order["revision"],
                                "revision_after": order["revision"],
                            },
                        )
                else:
                    raise LocalError("invalid_input")
                self._revise(db, order)
                result = self._receipt(
                    db,
                    receipt_kind,
                    key,
                    request,
                    {"status": "saved", **result},
                    order,
                    action=action_id,
                )
                self.fault("before_receipt_commit")
            self.fault("after_receipt_commit")
            return result

    def _confirm(self, db, payload, order, command_id):
        kind = payload.get("remedy")
        expected = {"remedy", "reason"} | (
            {"amount", "currency"}
            if kind == "refund"
            else {"shipping_method"}
            if kind == "replacement"
            else set()
        )
        if (
            kind not in ("refund", "replacement", "return", "cancellation")
            or set(payload) != expected
        ):
            raise LocalError("invalid_input")
        text(payload["reason"], 500)
        parent = None
        if order["allocated_action_id"]:
            parent = self._action(db, order["allocated_action_id"])
            if (
                kind != "refund"
                or parent["kind"] not in ("return", "cancellation")
                or parent["status"] != "awaiting_refund"
            ):
                raise LocalError("order_allocated")
        amount, currency, shipping = None, None, None
        if kind == "refund":
            currency = payload["currency"]
            if currency != order["currency"]:
                raise LocalError("currency_mismatch")
            amount = money(payload["amount"], currency, positive=True)
            if amount != self._balance(order):
                raise LocalError("balance_mismatch")
        if kind == "replacement":
            currency, amount, shipping = (
                order["currency"],
                money(order["replacement_value"], order["currency"], stored=True),
                payload["shipping_method"],
            )
            if shipping not in ("standard", "expedited"):
                raise LocalError("invalid_input")
        actions = self._actions(db)
        for action in actions:
            if action["status"] == "confirmed" and action["parent_action_id"] == (
                parent["id"] if parent else None
            ):
                self._stage(db, action["id"], "superseded")
        action_id, ts = str(uuid4()), now()
        insert(
            db,
            "actions",
            {
                "deployment_id": self.context.deployment_id,
                "id": action_id,
                "created_at": ts,
                "case_id": self.context.case_id,
                "kind": kind,
                "parent_action_id": parent["id"] if parent else None,
                "amount": decimal_text(amount) if amount is not None else None,
                "currency": currency,
                "shipping_method": shipping,
                "reason": payload["reason"],
                "status": "confirmed",
                "confirmation_command_id": command_id,
                "confirmed_at": ts,
                "completed_at": None,
                "updated_at": ts,
            },
        )
        return {"action_id": action_id, "remedy": kind}, action_id

    def _physical(self, db, case, order, action, event):
        if type(event) is not str or event not in (
            "dispatch",
            "delivery",
            "receipt",
            "accept",
            "reject",
        ):
            raise LocalError("invalid_input")
        transitions = {
            ("replacement", "dispatch"): ("requested", "dispatched"),
            ("replacement", "delivery"): ("dispatched", "completed"),
            ("return", "receipt"): ("requested", "received"),
            ("return", "accept"): ("received", "awaiting_refund"),
            ("return", "reject"): ("received", "rejected"),
        }
        transition = transitions.get((action["kind"], event))
        if transition is None or order["allocated_action_id"] != action["id"]:
            raise LocalError("invalid_transition")
        prior = db.execute(
            "SELECT result_json FROM receipts WHERE deployment_id=? AND action_id=? AND kind=?",
            (self.context.deployment_id, action["id"], event),
        ).fetchone()
        if prior:
            return {**json.loads(prior["result_json"]), "already_applied": True}
        before, after = transition
        if action["status"] != before:
            raise LocalError("invalid_transition")
        balance = self._balance(order)
        if event == "accept" and balance == 0:
            after = "completed"
        self._stage(db, action["id"], after)
        if event == "reject":
            self._case_stage(db, "handed_off")
        elif case["status"] not in ("handed_off", "resolved"):
            self._case_stage(
                db,
                "waiting_customer"
                if after in ("completed", "awaiting_refund")
                else "waiting_fulfilment",
            )
        return {
            "action_id": action["id"],
            "action_status": after,
            "zero_balance": balance == 0,
        }

    def mutate(self, tool, arguments, key):
        self._validate(tool, arguments)
        if tool not in KINDS:
            raise LocalError("invalid_input")
        uuid(key)
        c, kind = self.context, KINDS[tool]
        request = canonical({"tool": tool, "context": vars(c), "arguments": arguments})
        with connection(self.path) as db:
            with transaction(db):
                case, order = self._facts(db)
                prior = self._prior(db, key, request)
                if prior:
                    return prior
                action_id = arguments.get("action_id")
                action = self._action(db, action_id) if action_id is not None else None
                if case["status"] == "resolved" or (
                    case["status"] == "handed_off" and kind != "note"
                ):
                    raise LocalError("case_terminal")
                amount = None
                if kind in ("refund", "replacement", "return", "cancellation"):
                    result, amount = self._execute_remedy(
                        db, case, order, action, arguments, kind
                    )
                elif kind == "closure":
                    result = self._resolve_case(db, case, order, action, arguments)
                else:
                    if kind == "escalation":
                        self._case_stage(db, "handed_off")
                    result = {"status": "confirmed", "action_id": action_id}
                self._revise(db, order)
                result = self._receipt(
                    db,
                    kind,
                    key,
                    request,
                    result,
                    order,
                    action=action_id,
                    amount=amount,
                )
                if kind in ("note", "escalation"):
                    insert(
                        db, "messages",
                        {
                            "deployment_id": c.deployment_id,
                            "id": str(uuid4()), "created_at": now(),
                            "case_id": c.case_id, "action_id": None,
                            "kind": kind,
                            "body": arguments.get("body", arguments.get("reason")),
                            "recipient": None, "receipt_id": result["receipt_id"],
                        },
                    )
                self.fault("before_receipt_commit")
            self.fault("after_receipt_commit")
            return result

    def _resolve_case(self, db, case, order, action, arguments):
        if arguments["facts_revision"] != order["revision"]:
            raise LocalError("stale_facts")
        if action is None or action["status"] != "completed":
            raise LocalError("resolution_incomplete")
        primary = (
            self._action(db, action["parent_action_id"])
            if action["parent_action_id"] else action
        )
        if (
            primary["status"] != "completed"
            or order["allocated_action_id"] != primary["id"]
        ):
            raise LocalError("resolution_incomplete")
        self._case_stage(db, "resolved")
        return {"status": "resolved", "action_id": action["id"]}

    def _apply_refund(self, db, order, action, amount):
        c = self.context
        with localcontext() as context:
            context.prec = 40
            refunded = Decimal(order["refunded_amount"]) + amount
        db.execute(
            "UPDATE orders SET refunded_amount=? WHERE deployment_id=? AND id=?",
            (decimal_text(refunded), c.deployment_id, c.order_id),
        )
        if action["parent_action_id"]:
            self._stage(db, action["parent_action_id"], "completed")

    def _apply_cancellation(self, db, order):
        c = self.context
        after = "awaiting_refund"
        if order["fulfilment_status"] == "cancelled":
            raise LocalError("already_cancelled")
        db.execute(
            "UPDATE orders SET fulfilment_status='cancelled' WHERE deployment_id=? AND id=?",
            (c.deployment_id, c.order_id),
        )
        if self._balance(order) == 0:
            after = "completed"
        return after

    def _execute_remedy(self, db, case, order, action, arguments, kind):
        action_id = action["id"] if action is not None else None
        c = self.context
        amount = None
        if (
            action is None
            or action["kind"] != kind
            or action["status"] != "confirmed"
        ):
            raise LocalError("action_not_executable")
        expected = self._arguments(db, case, order, action)
        if any(
            arguments[field] != value for field, value in expected.items()
        ):
            raise LocalError("stale_facts")
        if kind == "refund":
            amount = money(
                arguments["amount"], arguments["currency"], positive=True
            )
            if (
                amount != self._balance(order)
                or arguments["currency"] != order["currency"]
            ):
                raise LocalError("balance_mismatch")
        if action["parent_action_id"]:
            parent = self._action(db, action["parent_action_id"])
            if (
                parent["kind"] not in ("return", "cancellation")
                or parent["status"] != "awaiting_refund"
                or order["allocated_action_id"] != parent["id"]
            ):
                raise LocalError("invalid_parent")
        else:
            changed = db.execute(
                "UPDATE orders SET allocated_action_id=? WHERE deployment_id=? AND id=? AND allocated_action_id IS NULL",
                (action_id, c.deployment_id, c.order_id),
            ).rowcount
            if changed != 1:
                raise LocalError("order_allocated")
        after = {
            "refund": "completed",
            "replacement": "requested",
            "return": "requested",
            "cancellation": "awaiting_refund",
        }[kind]
        if kind == "refund":
            self._apply_refund(db, order, action, amount)
        if kind == "cancellation":
            after = self._apply_cancellation(db, order)
        self._stage(db, action_id, after)
        self._case_stage(
            db,
            "waiting_fulfilment"
            if after == "requested"
            else "waiting_customer",
        )
        result = {
            "action_id": action_id,
            "action_status": after,
            "zero_balance": kind == "refund" or self._balance(order) == 0,
        }
        if amount is not None:
            result.update(
                amount=arguments["amount"], currency=order["currency"]
            )

        return result, amount

    def _arguments(self, db, case, order, action):
        result = {"action_id": action["id"], "facts_revision": order["revision"]}
        if action["kind"] == "refund":
            result.update(
                amount=numeric(action["amount"], action["currency"]),
                currency=action["currency"],
            )
        elif action["kind"] == "replacement":
            if case["verified_issue"] is None:
                raise LocalError("assessment_unknown")
            result.update(
                product_category=order["product_category"],
                verified_issue=case["verified_issue"],
            )
        elif action["kind"] == "return":
            result["age_days"] = self.age(order)
        else:
            result["fulfilment_status"] = order["fulfilment_status"]
        return result

    def disposition(self):
        case = self.read("get_case", {"case_id": self.context.case_id})
        if case["status"] in ("resolved", "handed_off"):
            return "terminal"
        return "waiting_customer"
