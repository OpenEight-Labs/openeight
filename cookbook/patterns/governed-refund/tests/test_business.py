from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from pydantic import ValidationError

from refund_assistant.business import ORDERS, Order, RefundRequest
from refund_assistant.tools import build_tools


def test_refund_schema_reports_amount_units_and_required_currency():
    schema = build_tools(RefundRequest("ORD-1041"))[1].args_schema.model_json_schema()
    assert set(schema["required"]) == {"order_id", "amount", "currency"}
    assert "amount_minor" not in schema["properties"]
    assert "multipleOf" not in schema["properties"]["amount"]
    assert "not cents" in schema["properties"]["amount"]["description"]
    assert "const" not in schema["properties"]["currency"]
    assert "enum" not in schema["properties"]["currency"]


@pytest.mark.parametrize("order_id", ORDERS)
def test_lookup_and_full_refund(order_id):
    scenario = RefundRequest(order_id)
    order = scenario.lookup_order(order_id)
    assert order["currency"] == scenario.order.currency and not order["refunded"]
    assert "amount_minor" not in order
    assert isinstance(scenario.order.amount, Decimal)
    assert scenario.prompt == f"Order {scenario.order.order_id} arrived damaged. Please refund it."
    receipt = build_tools(scenario)[1].invoke(
        {"order_id": order_id, "amount": order["amount"], "currency": order["currency"]}
    )
    assert receipt["simulated"] and scenario.lookup_order(order_id)["refunded"]
    assert scenario.lookup_order(order_id)["refunded_amount"] == order["amount"]
    assert scenario.lookup_order(order_id)["refundable_amount"] == 0
    assert receipt["amount"] == order["amount"] and receipt["currency"] == order["currency"]
    receipt["amount"] = 0
    assert scenario.receipts[0]["amount"] == order["amount"]
    with pytest.raises(ValueError, match="already_recorded"):
        scenario.issue_refund(order_id, order["amount"], order["currency"])


@pytest.mark.parametrize("amount", [25, 25.0])
def test_whole_unit_integer_and_float_inputs(amount):
    scenario = RefundRequest("ORD-1041")
    assert build_tools(scenario)[1].invoke(
        {"order_id": "ORD-1041", "amount": amount, "currency": "USD"}
    )["amount"] == 25


@pytest.mark.parametrize(
    "amount",
    [
        True, "25", 0, -1, 12.5, 180, 2500, 25.001, 25.000000000000004,
        float("nan"), float("inf"), -float("inf"),
    ],
)
def test_invalid_amount_never_records_receipt(amount):
    for invoke in (False, True):
        scenario = RefundRequest("ORD-1041")
        with pytest.raises((ValueError, ValidationError)):
            if invoke:
                build_tools(scenario)[1].invoke(
                    {"order_id": "ORD-1041", "amount": amount, "currency": "USD"}
                )
            else:
                scenario.issue_refund("ORD-1041", amount, "USD")
        assert not scenario.receipts


@pytest.mark.parametrize(
    "amount,currency",
    [(0.29, "USD"), (1250, "JPY"), (1.234, "KWD"), (99.95, "INR")],
)
def test_exact_order_amount_in_original_currency(monkeypatch, amount, currency):
    monkeypatch.setitem(ORDERS, "ORD-MONEY", Order("ORD-MONEY", Decimal(str(amount)), currency))
    scenario = RefundRequest("ORD-MONEY")
    order = scenario.lookup_order("ORD-MONEY")
    assert order["amount"] == amount and order["currency"] == currency
    receipt = build_tools(scenario)[1].invoke(
        {"order_id": "ORD-MONEY", "amount": amount, "currency": currency}
    )
    assert receipt["amount"] == amount and receipt["currency"] == currency


@pytest.mark.parametrize("currency", [None, "usd", "", "US", "USDX", 1])
def test_currency_must_be_explicit_code(currency):
    scenario = RefundRequest("ORD-1041")
    with pytest.raises((ValueError, ValidationError)):
        build_tools(scenario)[1].invoke(
            {"order_id": "ORD-1041", "amount": 25, "currency": currency}
        )
    with pytest.raises(ValueError, match="invalid_currency"):
        scenario.issue_refund("ORD-1041", 25, currency)
    assert not scenario.receipts


@pytest.mark.parametrize("currency", ["EUR", "JPY", "KWD"])
def test_currency_substitution_never_records_receipt(currency):
    scenario = RefundRequest("ORD-1041")
    with pytest.raises(ValueError, match="currency_mismatch"):
        build_tools(scenario)[1].invoke(
            {"order_id": "ORD-1041", "amount": 25, "currency": currency}
        )
    assert not scenario.receipts


def test_old_cents_contract_and_missing_currency_are_rejected():
    scenario = RefundRequest("ORD-1041")
    for args in (
        {"order_id": "ORD-1041", "amount_minor": 2500},
        {"order_id": "ORD-1041", "amount": 25},
    ):
        with pytest.raises(ValidationError):
            build_tools(scenario)[1].invoke(args)
    assert not scenario.receipts


def test_order_binding_and_concurrent_duplicates():
    scenario = RefundRequest("ORD-1041")
    for operation in (
        scenario.lookup_order,
        lambda order: scenario.issue_refund(order, 25, "USD"),
    ):
        with pytest.raises(ValueError, match="order_not_selected"):
            operation("ORD-1042")

    def attempt(_):
        try:
            scenario.issue_refund("ORD-1041", 25, "USD")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(8) as executor:
        assert sum(executor.map(attempt, range(20))) == 1
    assert len(scenario.receipts) == 1


@pytest.mark.parametrize("amount", [500, 500.00])
def test_decimal_order_matches_equivalent_json_amounts(monkeypatch, amount):
    monkeypatch.setitem(ORDERS, "ORD-DECIMAL", Order("ORD-DECIMAL", Decimal("500.00"), "USD"))
    scenario = RefundRequest("ORD-DECIMAL")
    receipt = build_tools(scenario)[1].invoke(
        {"order_id": "ORD-DECIMAL", "amount": amount, "currency": "USD"}
    )
    assert scenario.order.amount.as_tuple().exponent == -2
    assert receipt["amount"] == 500 and receipt["currency"] == "USD"


@pytest.mark.parametrize("amount", [25, 25.0, "25.00", True, Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1")])
def test_order_requires_positive_finite_decimal(amount):
    with pytest.raises(ValueError, match="invalid_order_amount"):
        Order("ORD-INVALID", amount, "USD")


def test_order_rejects_lossy_json_representation():
    with pytest.raises(ValueError, match="amount_not_json_representable"):
        Order("ORD-PRECISE", Decimal("1.234567890123456789"), "KWD")


def test_decimal_order_outputs_remain_json_numbers(monkeypatch):
    import json

    monkeypatch.setitem(ORDERS, "ORD-JSON", Order("ORD-JSON", Decimal("1.234"), "KWD"))
    scenario = RefundRequest("ORD-JSON")
    order = scenario.lookup_order("ORD-JSON")
    receipt = scenario.issue_refund("ORD-JSON", order["amount"], order["currency"])
    result = json.loads(json.dumps({"order": order, "receipt": receipt}), parse_float=Decimal)
    assert result["order"]["amount"] == result["receipt"]["amount"] == Decimal("1.234")


def test_partial_refund_tracks_balances_and_freezes_customer_request():
    scenario = RefundRequest("ORD-1041", Decimal("10.00"))
    before = scenario.lookup_order("ORD-1041")
    assert before["amount"] == before["refundable_amount"] == 25
    assert before["refunded_amount"] == 0 and not before["refunded"]
    assert "refund 10.00 USD" in scenario.prompt
    with pytest.raises(AttributeError):
        scenario.requested_amount = Decimal("5")
    receipt = build_tools(scenario)[1].invoke(
        {"order_id": "ORD-1041", "amount": 10, "currency": "USD"}
    )
    assert receipt["amount"] == 10 and receipt["simulated"]
    after = scenario.lookup_order("ORD-1041")
    assert after["amount"] == 25
    assert after["refunded_amount"] == 10 and after["refundable_amount"] == 15
    assert not after["refunded"]
    with pytest.raises(ValueError, match="already_recorded"):
        scenario.issue_refund("ORD-1041", 10, "USD")
    assert len(scenario.receipts) == 1


@pytest.mark.parametrize("amount", [5, 15, 25, 30])
def test_model_cannot_change_or_split_customer_partial_request(amount):
    scenario = RefundRequest("ORD-1041", Decimal("10"))
    before = scenario.lookup_order("ORD-1041")
    with pytest.raises(ValueError, match="requested_amount_mismatch"):
        scenario.issue_refund("ORD-1041", amount, "USD")
    assert not scenario.receipts
    assert scenario.lookup_order("ORD-1041") == before


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 10, 10.0, "10"])
def test_invalid_customer_request_rejected_at_construction(amount):
    with pytest.raises(ValueError, match="invalid_requested_amount"):
        RefundRequest("ORD-1041", amount)


def test_customer_request_cannot_exceed_refundable_amount():
    with pytest.raises(ValueError, match="refund_exceeds_available"):
        RefundRequest("ORD-1041", Decimal("25.01"))


@pytest.mark.parametrize(
    "total,requested,currency,remaining",
    [("0.29", "0.10", "USD", "0.19"), ("1250", "500", "JPY", "750"),
     ("1.234", "0.234", "KWD", "1"), ("99.95", "9.95", "INR", "90")],
)
def test_partial_refunds_preserve_currency_and_decimal_precision(monkeypatch, total, requested, currency, remaining):
    import json

    monkeypatch.setitem(ORDERS, "ORD-PARTIAL", Order("ORD-PARTIAL", Decimal(total), currency))
    scenario = RefundRequest("ORD-PARTIAL", Decimal(requested))
    scenario.issue_refund("ORD-PARTIAL", scenario.json_requested_amount, currency)
    result = json.loads(json.dumps(scenario.lookup_order("ORD-PARTIAL")), parse_int=Decimal, parse_float=Decimal)
    assert result["refundable_amount"] == Decimal(remaining)
    assert result["refunded_amount"] == Decimal(requested)
    assert result["currency"] == currency


def test_balance_arithmetic_is_independent_of_decimal_context(monkeypatch):
    from decimal import localcontext

    monkeypatch.setitem(ORDERS, "ORD-LARGE", Order("ORD-LARGE", Decimal("123456789012345678901234567890"), "USD"))
    with localcontext() as context:
        context.prec = 3
        scenario = RefundRequest("ORD-LARGE", Decimal("1"))
        scenario.issue_refund("ORD-LARGE", 1, "USD")
        assert scenario.lookup_order("ORD-LARGE")["refundable_amount"] == 123456789012345678901234567889


def test_unrepresentable_remaining_balance_is_rejected_before_execution(monkeypatch):
    monkeypatch.setitem(ORDERS, "ORD-LARGE", Order("ORD-LARGE", Decimal("9007199254740993"), "USD"))
    with pytest.raises(ValueError, match="amount_not_json_representable"):
        RefundRequest("ORD-LARGE", Decimal("0.01"))


def test_concurrent_partial_refund_records_exactly_one_receipt():
    scenario = RefundRequest("ORD-1041", Decimal("10"))

    def attempt(_):
        try:
            scenario.issue_refund("ORD-1041", 10, "USD")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(8) as executor:
        assert sum(executor.map(attempt, range(20))) == 1
    assert len(scenario.receipts) == 1
    assert scenario.lookup_order("ORD-1041")["refundable_amount"] == 15
