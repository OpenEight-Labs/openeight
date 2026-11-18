"""Describe the synthetic tracing demo without installing dependencies."""

import json

print(
    json.dumps(
        {
            "mode": "local_sample",
            "governance": "not_exercised",
            "simulated": True,
            "operation": "6 * 7",
            "result": 42,
            "next_step": "Run agent.py with OTEL dependencies to exercise trace export.",
        },
        indent=2,
    )
)
