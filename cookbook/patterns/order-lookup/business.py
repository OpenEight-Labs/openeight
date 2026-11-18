"""Order lookup and truthful customer guidance, independent of telemetry."""

from datetime import date
import json
from pathlib import Path

DATA = Path(__file__).parent / "data" / "orders.json"


def lookup_order(order_id, *, data=DATA):
    orders = json.loads(data.read_text())
    order = orders.get(order_id)
    return {"order_id": order_id, **order} if order else None


def recommend_action(order, *, today):
    if order is None:
        return {
            "action": "request_order_number",
            "reply": "Please check your order number so I can find the shipment.",
        }
    if order["status"] == "delivered":
        return {
            "action": "confirm_delivery",
            "reply": f"Order {order['order_id']} was delivered on {order['delivered_date']}. If it has not arrived, contact support to investigate.",
        }
    if date.fromisoformat(order["promised_date"]) < today:
        return {
            "action": "investigate_delayed_delivery",
            "reply": f"Order {order['order_id']} is past its promised delivery date. Ask support to investigate with the carrier.",
        }
    return {
        "action": "track_shipment",
        "reply": f"Order {order['order_id']} is in transit and expected by {order['promised_date']}.",
    }
