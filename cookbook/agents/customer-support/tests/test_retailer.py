"""Money, consent, atomic effects, isolation and permanent allocation."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from uuid import uuid4

import pytest

from local import Deployment, LocalError, now, connection, transaction
from retailer import RetailerRepository
from fixtures import seed_retailer
from money import money, numeric
from helpers_remedies import confirm, event, resolve


@pytest.fixture
def retailer(tmp_path):
    state = tmp_path / "state"
    d = Deployment("test", None, None, None, None, None, "standalone", 4, now())
    seed_retailer(state, d)
    return state, d, RetailerRepository.bind(state, d, "case_damaged")


def execute(r, tool, action):
    return r.mutate(tool, r.execution_arguments(action), str(uuid4()))


def test_billing_review_keeps_gateway_discrepancy_separate_from_order_balance(retailer):
    state, deployment, _ = retailer
    r = RetailerRepository.bind(state, deployment, "case_billing")
    payment = r.read("get_payment", {"order_id": r.context.order_id})
    assert payment["captured_amount"] == payment["amount"] == 90
    assert payment["ledger_captured_amount"] == 180
    assert payment["reconciliation_required"] is True
    assert payment["review_reason"] == "possible_duplicate_charge"
    assert len({row["id"] for row in payment["captures"]}) == 2
    assert not r.receipts()
    assert "captures" not in retailer[2].read(
        "get_payment", {"order_id": retailer[2].context.order_id}
    )


@pytest.mark.parametrize(
    "tool,kind",
    [
        ("issue_refund", "refund"),
        ("create_replacement", "replacement"),
        ("create_return", "return"),
        ("cancel_order", "cancellation"),
    ],
)
def test_confirmed_whole_order_effect_and_exact_replay(retailer, tool, kind):
    r = retailer[2]
    action = confirm(r, kind)
    args, key = r.execution_arguments(action), str(uuid4())
    result = r.mutate(tool, args, key)
    assert r.mutate(tool, args, key) == result
    assert len([x for x in r.receipts() if x["kind"] == kind]) == 1
    assert (
        r.read("get_order", {"order_id": r.context.order_id})["allocated_action_id"]
        == action
    )
    with pytest.raises(LocalError):
        r.mutate(tool, {**args, "facts_revision": args["facts_revision"] + 1}, key)


@pytest.mark.parametrize(
    "point,committed",
    [("before_receipt_commit", False), ("after_receipt_commit", True)],
)
def test_receipt_balance_and_allocation_crash_atomicity(retailer, point, committed):
    state, d, r = retailer
    action = confirm(r)
    args, key = r.execution_arguments(action), str(uuid4())
    r.fault = (
        lambda p: (_ for _ in ()).throw(RuntimeError("crash")) if p == point else None
    )
    with pytest.raises(RuntimeError):
        r.mutate("issue_refund", args, key)
    reopened = RetailerRepository.bind(state, d, r.context.case_id)
    assert (reopened.lookup_receipt(key) is not None) == committed
    payment = reopened.read("get_payment", {"order_id": r.context.order_id})
    assert payment["refunded_amount"] == (180 if committed else 0)
    assert reopened.mutate("issue_refund", args, key)["action_status"] == "completed"
    assert len([x for x in reopened.receipts() if x["kind"] == "refund"]) == 1


@pytest.mark.parametrize(
    "kind,tool,changes",
    [
        ("refund", "issue_refund", {"amount": 179}),
        ("refund", "issue_refund", {"currency": "JPY"}),
        ("replacement", "create_replacement", {"verified_issue": "lost"}),
        ("replacement", "create_replacement", {"product_category": "perishable"}),
        ("return", "create_return", {"age_days": 1}),
        ("cancellation", "cancel_order", {"fulfilment_status": "packing"}),
    ],
)
def test_fabricated_schema_valid_facts_fail_without_effect(
    retailer, kind, tool, changes
):
    r = retailer[2]
    action = confirm(r, kind)
    with pytest.raises(LocalError, match="stale_facts"):
        r.mutate(tool, {**r.execution_arguments(action), **changes}, str(uuid4()))
    assert all(x["kind"] == "confirmation" for x in r.receipts())
    assert (
        r.read("get_order", {"order_id": r.context.order_id})["allocated_action_id"]
        is None
    )


@pytest.mark.parametrize(
    "label,kind,tool",
    [
        ("return31", "return", "create_return"),
        ("perishable", "replacement", "create_replacement"),
        ("billing", "replacement", "create_replacement"),
        ("missing", "cancellation", "cancel_order"),
        ("damaged", "cancellation", "cancel_order"),
    ],
)
def test_retailer_does_not_copy_external_policy_restrictions(
    retailer, label, kind, tool
):
    state, d, _ = retailer
    r = RetailerRepository.bind(state, d, "case_" + label)
    assert execute(r, tool, confirm(r, kind))["action_status"] in (
        "requested",
        "awaiting_refund",
    )


@pytest.mark.parametrize(
    "currency,amount",
    [
        ("USD", 50),
        ("USD", 50.0),
        ("USD", 50.01),
        ("JPY", 1250),
        ("KWD", 50.125),
        ("INR", 1250.25),
    ],
)
def test_exact_supported_money(currency, amount):
    d = money(amount, currency, positive=True)
    assert Decimal(str(numeric(str(d), currency))) == d


@pytest.mark.parametrize(
    "currency,amount",
    [
        ("USD", True),
        ("USD", "50"),
        ("USD", float("inf")),
        ("USD", float("nan")),
        ("USD", -1),
        ("USD", 0),
        ("USD", 50.001),
        ("JPY", 1.1),
        ("KWD", 1.0001),
        ("ZZZ", 1),
    ],
)
def test_unsafe_or_unsupported_money(currency, amount):
    with pytest.raises(LocalError):
        money(amount, currency, positive=True)


def test_in_range_unrepresentable_fraction_is_rejected():
    with pytest.raises(LocalError, match="unsupported_amount"):
        money("900719925474099.91", "USD", stored=True)


@pytest.mark.parametrize(
    "label,kind,tool",
    [
        ("returnzero", "return", "create_return"),
        ("cancelzero", "cancellation", "cancel_order"),
    ],
)
def test_zero_balance_completes_without_refund_but_needs_explicit_resolution(
    retailer, label, kind, tool
):
    state, d, _ = retailer
    r = RetailerRepository.bind(state, d, "case_" + label)
    action = confirm(r, kind)
    execute(r, tool, action)
    if kind == "return":
        event(r, action, "receipt")
        accepted = event(r, action, "accept")
        assert accepted["zero_balance"] and accepted["action_status"] == "completed"
    case = r.read("get_case", {"case_id": r.context.case_id})
    assert case["status"] != "resolved" and len(case["actions"]) == 1
    with pytest.raises(LocalError):
        confirm(r, "refund", amount=0)
    resolve(r, action)
    assert r.disposition() == "terminal"


@pytest.mark.parametrize(
    "kind,tool", [("return", "create_return"), ("cancellation", "cancel_order")]
)
def test_positive_balance_requires_separately_confirmed_child_settlement(
    retailer, kind, tool
):
    r = retailer[2]
    primary = confirm(r, kind)
    execute(r, tool, primary)
    if kind == "return":
        event(r, primary, "receipt")
        event(r, primary, "accept")
    with pytest.raises(LocalError, match="resolution_incomplete"):
        resolve(r, primary)
    assert r.disposition() == "waiting_customer"
    child = confirm(r)
    execute(r, "issue_refund", child)
    actions = r.read("get_case", {"case_id": r.context.case_id})["actions"]
    assert len(actions) == 2 and all(a["status"] == "completed" for a in actions)
    assert r.disposition() == "waiting_customer"
    resolve(r, child)
    assert r.disposition() == "terminal"


def test_replacement_physical_ordering_and_handoff_late_delivery(retailer):
    r = retailer[2]
    action = confirm(r, "replacement")
    execute(r, "create_replacement", action)
    with pytest.raises(LocalError, match="invalid_transition"):
        event(r, action, "delivery")
    event(r, action, "dispatch")
    r.mutate(
        "escalate_case",
        {"case_id": r.context.case_id, "reason": "Support owns the remaining journey"},
        str(uuid4()),
    )
    assert r.disposition() == "terminal"
    event(r, action, "delivery")
    assert r.read("get_case", {"case_id": r.context.case_id})["status"] == "handed_off"
    with pytest.raises(LocalError, match="case_terminal"):
        resolve(r, action)
    assert r.disposition() == "terminal"
    assert r.read("get_case", {"case_id": r.context.case_id})["status"] == "handed_off"
    with pytest.raises(LocalError):
        confirm(r)


def test_rejected_inspection_permanently_allocates_order(retailer):
    state, d, r = retailer
    action = confirm(r, "return")
    execute(r, "create_return", action)
    event(r, action, "receipt")
    event(r, action, "reject")
    assert r.read("get_case", {"case_id": r.context.case_id})["status"] == "handed_off"
    with connection(state / "retailer.sqlite") as db, transaction(db):
        db.execute(
            "INSERT INTO cases VALUES (?,?,?,?,?,?,?,?)",
            (
                d.id,
                "case_new",
                now(),
                r.context.order_id,
                "Another issue",
                "damaged",
                "open",
                now(),
            ),
        )
    other = RetailerRepository.bind(state, d, "case_new")
    with pytest.raises(LocalError):
        confirm(other)
    with sqlite3.connect(state / "retailer.sqlite") as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "UPDATE orders SET allocated_action_id=NULL WHERE id=?",
                (r.context.order_id,),
            )


def test_supersession_invalidates_old_pending_execution(retailer):
    r = retailer[2]
    old = confirm(r)
    frozen = r.execution_arguments(old)
    new = confirm(r, "replacement")
    with pytest.raises(LocalError, match="action_not_executable"):
        r.mutate("issue_refund", frozen, str(uuid4()))
    execute(r, "create_replacement", new)
    with pytest.raises(LocalError, match="order_allocated"):
        confirm(r, "return")


def test_concurrent_execution_has_one_receipt_and_effect(retailer):
    state, d, r = retailer
    action = confirm(r)
    args = r.execution_arguments(action)

    def run(_):
        try:
            return RetailerRepository.bind(state, d, r.context.case_id).mutate(
                "issue_refund", args, str(uuid4())
            )
        except LocalError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert len([x for x in r.receipts() if x["kind"] == "refund"]) == 1


def test_foreign_scope_and_unfinished_actions_cannot_resolve(retailer):
    state, d, r = retailer
    with pytest.raises(LocalError):
        r.read("get_order", {"order_id": "order_small"})
    with pytest.raises(LocalError):
        RetailerRepository.bind(state, replace(d, id="other"), r.context.case_id)
    other = RetailerRepository.bind(state, d, "case_small")
    foreign = confirm(other)
    execute(other, "issue_refund", foreign)
    with pytest.raises(LocalError):
        resolve(r, foreign)
    unexecuted = confirm(r)
    with pytest.raises(LocalError, match="resolution_incomplete"):
        resolve(r, unexecuted)
    assert r.disposition() == "waiting_customer"
    assert not any(row["kind"] == "closure" for row in r.receipts())


def test_delivery_age_changes_without_revision(retailer):
    state, d, r = retailer
    fixed = datetime.now(timezone.utc)
    r.clock = lambda: fixed
    action = confirm(r, "return")
    args = r.execution_arguments(action)
    r.clock = lambda: fixed + timedelta(days=1)
    with pytest.raises(LocalError, match="stale_facts"):
        r.mutate("create_return", args, str(uuid4()))


def test_append_only_receipts_and_fresh_schema(retailer):
    state, d, r = retailer
    action = confirm(r)
    execute(r, "issue_refund", action)
    with sqlite3.connect(state / "retailer.sqlite") as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE receipts SET result_json='{}'")
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables == {
            "deployment",
            "customers",
            "orders",
            "cases",
            "actions",
            "receipts",
            "messages",
        }
    seed_retailer(state, d)
    assert r.read("get_payment", {"order_id": r.context.order_id})["amount"] == 0


def test_handoff_is_terminal_without_a_message_write(retailer, monkeypatch):
    import retailer as implementation

    r = retailer[2]
    monkeypatch.setattr(implementation, "now", lambda: "2026-10-13T00:00:00Z")
    r.mutate("escalate_case", {"case_id": r.context.case_id, "reason": "Support handoff"}, str(uuid4()))
    monkeypatch.setattr(implementation, "now", lambda: "2026-10-12T00:00:00Z")
    assert r.disposition() == "terminal"
    assert r.read("get_case", {"case_id": r.context.case_id})["status"] == "handed_off"
    assert [row["kind"] for row in r.receipts()] == ["escalation"]


def test_full_refund_receipt_reports_target_zero_balance(retailer):
    r = retailer[2]
    result = execute(r, "issue_refund", confirm(r))
    assert result["zero_balance"] is True
    assert r.read("get_payment", {"order_id": r.context.order_id})["amount"] == 0


def test_supersession_racing_execution_is_serial_and_exclusive(retailer):
    import threading

    state, d, r = retailer
    action = confirm(r)
    args = r.execution_arguments(action)
    barrier = threading.Barrier(2)

    def run(executing):
        barrier.wait(timeout=5)
        try:
            bound = RetailerRepository.bind(state, d, r.context.case_id)
            return (
                bound.mutate("issue_refund", args, str(uuid4()))
                if executing
                else confirm(bound, "replacement")
            )
        except LocalError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(run, (True, False)))
    facts = r.read("get_order", {"order_id": r.context.order_id})
    actions = r.read("get_case", {"case_id": r.context.case_id})["actions"]
    if facts["allocated_action_id"]:
        assert facts["allocated_action_id"] == action and isinstance(a, dict)
        assert all(x["id"] == action for x in actions)
    else:
        assert not isinstance(a, dict)
        assert all(x["status"] in ("confirmed", "superseded") for x in actions)
    assert len([x for x in r.receipts() if x["kind"] == "refund"]) <= 1


@pytest.mark.parametrize(
    "point,committed",
    [("before_receipt_commit", False), ("after_receipt_commit", True)],
)
def test_child_refund_parent_and_ledger_settle_atomically(retailer, point, committed):
    state, d, r = retailer
    primary = confirm(r, "cancellation")
    execute(r, "cancel_order", primary)
    child = confirm(r)
    args, key = r.execution_arguments(child), str(uuid4())
    r.fault = (
        lambda p: (_ for _ in ()).throw(RuntimeError("crash")) if p == point else None
    )
    with pytest.raises(RuntimeError):
        r.mutate("issue_refund", args, key)
    r = RetailerRepository.bind(state, d, r.context.case_id)
    stages = {
        a["id"]: a["status"]
        for a in r.read("get_case", {"case_id": r.context.case_id})["actions"]
    }
    assert stages[primary] == ("completed" if committed else "awaiting_refund")
    assert stages[child] == ("completed" if committed else "confirmed")
    assert r.read("get_payment", {"order_id": r.context.order_id})["amount"] == (
        0 if committed else 180
    )
    r.mutate("issue_refund", args, key)
    assert all(
        a["status"] == "completed"
        for a in r.read("get_case", {"case_id": r.context.case_id})["actions"]
    )


@pytest.mark.parametrize(
    "kind,point,committed",
    [
        ("return", "before_receipt_commit", False),
        ("return", "after_receipt_commit", True),
    ],
)
def test_zero_balance_acceptance_receipt_and_completion_are_atomic(
    retailer, kind, point, committed
):
    state, d, _ = retailer
    r = RetailerRepository.bind(state, d, "case_returnzero")
    action = confirm(r, kind)
    execute(r, "create_return", action)
    event(r, action, "receipt")
    envelope = {
        "command_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "case_id": r.context.case_id,
        "facts_revision": r.read("get_order", {"order_id": r.context.order_id})[
            "facts_revision"
        ],
        "kind": "physical_event",
        "payload": {"action_id": action, "event": "accept"},
    }
    r.fault = (
        lambda p: (_ for _ in ()).throw(RuntimeError("crash")) if p == point else None
    )
    with pytest.raises(RuntimeError):
        r.control(envelope)
    reopened = RetailerRepository.bind(state, d, r.context.case_id)
    assert reopened.read("get_case", {"case_id": r.context.case_id})["actions"][0][
        "status"
    ] == ("completed" if committed else "received")
    assert bool([x for x in reopened.receipts() if x["kind"] == "accept"]) == committed
    assert reopened.control(envelope)["action_status"] == "completed"
