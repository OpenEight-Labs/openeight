"""Existing native LangChain agent: same refund tools, without OpenEight."""

import argparse
import json

from langchain.agents import create_agent

from refund_assistant.business import ORDERS, RefundRequest
from refund_assistant.config import model_client
from refund_assistant.inputs import requested_amount
from refund_assistant.prompts import SYSTEM_PROMPT
from refund_assistant.tools import build_tools


def build(model, request):
    return create_agent(
        model=model,
        tools=build_tools(request),
        system_prompt=SYSTEM_PROMPT,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("order", choices=tuple(ORDERS))
    parser.add_argument("--amount", type=requested_amount)
    args = parser.parse_args(argv)
    request = None
    try:
        request = RefundRequest(args.order, args.amount)
        agent = build(model_client(), request)
        agent.invoke({"messages": [("user", request.prompt)]})
        result = {
            "state": "receipt_recorded"
            if request.receipts
            else "missing_refund_attempt",
            "governance": "not_enabled",
            "receipts": request.receipts,
        }
    except Exception:
        result = {
            "state": "failed",
            "governance": "not_enabled",
            "explanation": "Check model configuration and the selected request.",
            "receipts": request.receipts if request is not None else [],
        }
    print(json.dumps(result))
    return 0 if result["state"] == "receipt_recorded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
