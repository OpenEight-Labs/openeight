"""Shared command-line amount validation, independent of governance."""

import argparse
from decimal import Decimal, InvalidOperation


def requested_amount(text):
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("amount must be a decimal number") from None
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError("amount must be positive and finite")
    return value
