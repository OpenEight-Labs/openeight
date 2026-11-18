from datetime import date
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business import lookup_order, recommend_action


class OrderSupportTests(unittest.TestCase):
    def test_unknown_order_requests_correction(self):
        self.assertEqual(
            recommend_action(lookup_order("UNKNOWN"), today=date(2026, 1, 15))[
                "action"
            ],
            "request_order_number",
        )

    def test_delivered_order_has_recorded_delivery_date(self):
        result = recommend_action(lookup_order("ORDER-1001"), today=date(2026, 1, 15))
        self.assertEqual(result["action"], "confirm_delivery")
        self.assertIn("2026-01-11", result["reply"])

    def test_transit_before_due_date_does_not_claim_late_delivery(self):
        result = recommend_action(lookup_order("ORDER-1002"), today=date(2026, 1, 11))
        self.assertEqual(result["action"], "track_shipment")

    def test_transit_after_due_date_recommends_investigation(self):
        result = recommend_action(lookup_order("ORDER-1002"), today=date(2026, 1, 15))
        self.assertEqual(result["action"], "investigate_delayed_delivery")
        self.assertNotIn("opened", result["reply"])
