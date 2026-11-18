"""Validated refund requests and sample order balances; no real payment effects."""

from dataclasses import dataclass
from decimal import Decimal, localcontext
import json
import re
from threading import Lock
from uuid import uuid4


def _amount_value(amount):
    if type(amount) not in (int, float):
        raise ValueError("invalid_amount")
    value = Decimal(str(amount))
    if not value.is_finite() or value <= 0:
        raise ValueError("invalid_amount")
    return value


def _json_amount(value, *, allow_zero=False):
    if (
        type(value) is not Decimal
        or not value.is_finite()
        or value < 0
        or (value == 0 and not allow_zero)
    ):
        raise ValueError("invalid_order_amount")
    try:
        number = int(value) if value == value.to_integral_value() else float(value)
        encoded = json.dumps(number, allow_nan=False)
        restored = json.loads(encoded, parse_int=Decimal, parse_float=Decimal)
    except (ValueError, OverflowError):
        raise ValueError("amount_not_json_representable") from None
    if restored != value:
        raise ValueError("amount_not_json_representable")
    return number


def _difference(left, right):
    # Align both coefficients without letting ambient Decimal precision round a balance.
    exponent = min(left.as_tuple().exponent, right.as_tuple().exponent)
    with localcontext() as context:
        context.prec = max(left.adjusted(), right.adjusted(), 0) - exponent + 2
        return left - right


def _currency_code(currency):
    if type(currency) is not str or re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise ValueError("invalid_currency")
    return currency


@dataclass(frozen=True)
class Order:
    order_id: str
    amount: Decimal
    currency: str

    def __post_init__(self):
        _json_amount(self.amount)
        _currency_code(self.currency)

    @property
    def json_amount(self):
        return _json_amount(self.amount)


ORDERS = {
    order.order_id: order
    for order in (
        Order("ORD-1041", Decimal("25.00"), "USD"),
        Order("ORD-1042", Decimal("180.00"), "EUR"),
        Order("ORD-1043", Decimal("50000"), "JPY"),
    )
}


class RefundRequest:
    def __init__(self, order_id, requested_amount=None):
        self.order = ORDERS[order_id]
        requested = self.order.amount if requested_amount is None else requested_amount
        if (
            type(requested) is not Decimal
            or not requested.is_finite()
            or requested <= 0
        ):
            raise ValueError("invalid_requested_amount")
        if requested > self.order.amount:
            raise ValueError("refund_exceeds_available")
        _json_amount(requested)
        _json_amount(_difference(self.order.amount, requested), allow_zero=True)
        self._requested_amount = requested
        self._lock = Lock()
        self._receipts = []

    @property
    def requested_amount(self):
        return self._requested_amount

    @property
    def json_requested_amount(self):
        return _json_amount(self.requested_amount)

    @property
    def prompt(self):
        request = (
            "Please refund it."
            if self.requested_amount == self.order.amount
            else f"Please refund {self.requested_amount} {self.order.currency}."
        )
        return f"Order {self.order.order_id} arrived damaged. {request}"

    @property
    def receipts(self):
        with self._lock:
            return [dict(receipt) for receipt in self._receipts]

    def _check_order(self, order_id):
        if type(order_id) is not str or order_id != self.order.order_id:
            raise ValueError("order_not_selected")

    def _balances(self):
        # Callers hold the request lock; a request can have at most one receipt.
        refunded = (
            _amount_value(self._receipts[0]["amount"]) if self._receipts else Decimal(0)
        )
        return refunded, _difference(self.order.amount, refunded)

    def lookup_order(self, order_id: str):
        self._check_order(order_id)
        with self._lock:
            refunded, available = self._balances()
            return {
                "order_id": self.order.order_id,
                "currency": self.order.currency,
                "amount": self.order.json_amount,
                "refunded_amount": _json_amount(refunded, allow_zero=True),
                "refundable_amount": _json_amount(available, allow_zero=True),
                "refunded": available == 0,
            }

    def issue_refund(self, order_id: str, amount: int | float, currency: str):
        self._check_order(order_id)
        if _currency_code(currency) != self.order.currency:
            raise ValueError("currency_mismatch")
        value = _amount_value(amount)
        if value != self.requested_amount:
            raise ValueError("requested_amount_mismatch")
        with self._lock:
            if self._receipts:
                raise ValueError("request_already_recorded")
            _, available = self._balances()
            if value > available:
                raise ValueError("refund_exceeds_available")
            remaining = _difference(available, value)
            _json_amount(remaining, allow_zero=True)
            receipt = {
                "receipt_id": str(uuid4()),
                "order_id": order_id,
                "amount": _json_amount(value),
                "currency": self.order.currency,
                "simulated": True,
            }
            self._receipts.append(receipt)
            return dict(receipt)
