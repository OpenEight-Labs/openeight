"""Complete a synthetic remedy or billing handoff without a model or SDK."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from local import Deployment, now
from retailer import RetailerRepository
from fixtures import seed_retailer


def walkthrough(scenario="refund"):
    with TemporaryDirectory(prefix="cookbook-support-") as directory:
        state = Path(directory) / "retailer"
        deployment = Deployment(
            str(uuid4()), None, None, None, None, None, "standalone", 4, now()
        )
        seed_retailer(state, deployment)
        case_id = "case_billing" if scenario == "billing" else "case_damaged"
        retailer = RetailerRepository.bind(state, deployment, case_id)
        order_id = retailer.context.order_id
        case = retailer.read("get_case", {"case_id": case_id})
        order = retailer.read("get_order", {"order_id": order_id})
        payment = retailer.read("get_payment", {"order_id": order_id})
        before = retailer.receipts()
        confirmation = None
        if scenario == "refund":
            confirmation = retailer.control(
                {
                    "command_id": str(uuid4()),
                    "thread_id": str(uuid4()),
                    "case_id": case_id,
                    "facts_revision": order["facts_revision"],
                    "kind": "confirmation",
                    "payload": {
                        "remedy": "refund",
                        "reason": "Synthetic customer confirms the displayed whole balance",
                        "amount": payment["amount"],
                        "currency": payment["currency"],
                    },
                }
            )
            action_id = confirmation["action_id"]
            receipt = retailer.mutate(
                "issue_refund", retailer.execution_arguments(action_id), str(uuid4())
            )
            reply = "Your confirmed whole-order refund is complete."
        else:
            action_id = None
            receipt = retailer.mutate(
                "escalate_case",
                {"case_id": case_id, "reason": payment["review_reason"]},
                str(uuid4()),
            )
            reply = (
                "Your case has been handed to billing for review; no refund was made."
            )
        if scenario == "refund":
            current = retailer.read("get_order", {"order_id": order_id})
            retailer.mutate(
                "resolve_case",
                {"case_id": case_id, "action_id": action_id,
                 "facts_revision": current["facts_revision"]},
                str(uuid4()),
            )
        return {
            "assistant_response": reply,
            "mode": "local_business_sample",
            "governance": "not_exercised",
            "simulated": True,
            "scenario": scenario,
            "case": case,
            "order": order,
            "payment": payment,
            "receipts_before_confirmation": before,
            "customer_confirmation": confirmation,
            "receipt": receipt,
            "after": retailer.read("get_case", {"case_id": case_id}),
            "payment_after": retailer.read("get_payment", {"order_id": order_id}),
            "receipts": retailer.receipts(),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("refund", "billing"), default="refund")
    print(json.dumps(walkthrough(parser.parse_args().scenario), indent=2))
