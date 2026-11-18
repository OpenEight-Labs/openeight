"""Exact currency representation and JSON numeric projection."""

from decimal import Decimal
import json
import math
from local import LocalError, canonical

MAX_NUMBER = 9007199254740991

CURRENCIES = {"USD": 2, "JPY": 0, "KWD": 3, "INR": 2}


def money(value, currency, *, positive=False, stored=False):
    if type(currency) is not str or currency not in CURRENCIES:
        raise LocalError("unsupported_currency")
    if not stored and (
        type(value) not in (int, float)
        or (type(value) is float and not math.isfinite(value))
    ):
        raise LocalError("invalid_amount")
    try:
        amount = Decimal(value if stored else str(value))
        if not amount.is_finite() or amount < 0 or (positive and amount == 0):
            raise LocalError("invalid_amount")
        if amount > MAX_NUMBER or amount.as_tuple().exponent < -CURRENCIES[currency]:
            raise LocalError("unsupported_amount")
        emitted = int(amount) if amount == amount.to_integral_value() else float(amount)
        if Decimal(str(json.loads(canonical(emitted)))) != amount:
            raise LocalError("unsupported_amount")
        return amount
    except (ValueError, ArithmeticError, TypeError):
        raise LocalError("invalid_amount") from None


def json_float(value):
    number = float(value)
    if not math.isfinite(number) or Decimal(value) != Decimal(str(number)):
        raise ValueError("unrepresentable_json_number")
    return number


def decimal_text(amount):
    value = format(amount, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def numeric(value, currency):
    amount = money(value, currency, stored=True)
    return int(amount) if amount == amount.to_integral_value() else float(amount)
