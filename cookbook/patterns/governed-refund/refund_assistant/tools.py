"""Native tool schemas and bindings for the refund business operations."""

from langchain_core.tools import StructuredTool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)

from .business import _amount_value


class LookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_id: StrictStr


class RefundInput(LookupInput):
    amount: StrictInt | StrictFloat = Field(
        description=(
            "Requested partial or full refund amount in the order's currency, "
            "not cents or minor units. Decimal fractions are supported "
            "(10.50 means 10.50 in that currency). "
            "Must be finite, positive, "
            "match the customer request, and not exceed the refundable amount."
        ),
    )
    currency: StrictStr = Field(
        pattern=r"^[A-Z]{3}$",
        description="Three-letter currency code exactly as returned by lookup_order. Required; never inferred or converted.",
    )

    @field_validator("amount")
    @classmethod
    def valid_amount(cls, value):
        _amount_value(value)
        return value


def build_tools(request):
    return [
        StructuredTool.from_function(
            request.lookup_order,
            name="lookup_order",
            description=(
                "Read the selected synthetic order's ID, amount in its "
                "currency (decimal fractions supported), currency, refunded amount, refundable "
                "amount, and fully-refunded status. Only retrieves "
                "information; does not issue a refund or change order state."
            ),
            args_schema=LookupInput,
        ),
        StructuredTool.from_function(
            request.issue_refund,
            name="issue_refund",
            description=(
                "Record the customer's requested partial or full refund in "
                "the order's original currency, including decimal fractions. "
                "Execution validates the exact customer request, remaining "
                "refundable balance, and absence of a prior receipt for this "
                "request. No currency conversion or rounding."
            ),
            args_schema=RefundInput,
        ),
    ]
