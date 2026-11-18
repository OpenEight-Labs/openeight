"""Answer a delayed-delivery request using the local order fixture."""

from datetime import date
import json

from business import lookup_order, recommend_action

order = lookup_order("ORDER-1002")
print(
    json.dumps(
        {
            "mode": "local_business_sample",
            "governance": "not_exercised",
            "order": order,
            "result": recommend_action(order, today=date(2026, 1, 15)),
        },
        indent=2,
    )
)
