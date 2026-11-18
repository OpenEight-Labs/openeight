"""Exercise the sample refund business logic without a model or OpenEight."""

import json
from decimal import Decimal

from refund_assistant.business import RefundRequest

request = RefundRequest("ORD-1041", Decimal("10.00"))
print(
    json.dumps(
        {
            "mode": "local_business_sample",
            "governance": "not_exercised",
            "customer_request": request.prompt,
            "before": request.lookup_order("ORD-1041"),
            "receipt": request.issue_refund("ORD-1041", 10, "USD"),
            "after": request.lookup_order("ORD-1041"),
        },
        indent=2,
    )
)
