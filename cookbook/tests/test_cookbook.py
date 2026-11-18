"""Launcher behavior and observable sample outcomes without SDK or credentials."""

from contextlib import redirect_stdout
from io import StringIO
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cookbook", ROOT / "cookbook.py")
cookbook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cookbook)


class ConfigurationTests(unittest.TestCase):
    def test_literal_values_and_shell_precedence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                'OPENEIGHT_API_KEY=file-key\nAGENT_MODEL="chosen:model"\nLITERAL="$(touch /tmp/never-execute)"\n'
            )
            with patch.dict(os.environ, {"OPENEIGHT_API_KEY": "shell-key"}, clear=True):
                values = cookbook.environment(root)
        self.assertEqual(values["OPENEIGHT_API_KEY"], "shell-key")
        self.assertEqual(values["AGENT_MODEL"], "chosen:model")
        self.assertEqual(values["LITERAL"], "$(touch /tmp/never-execute)")

    def test_malformed_assignment_omits_its_value(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("SECRET KEY=private-value\n")
            with self.assertRaises(ValueError) as caught:
                cookbook.environment(root)
            self.assertNotIn("private-value", str(caught.exception))

    def test_placeholder_and_bad_model_are_incomplete(self):
        recipe = cookbook.recipes()["governed-refund"]
        values = {
            "OPENEIGHT_ENDPOINT": "https://api.example",
            "OPENEIGHT_API_KEY": "private-key",
            "OPENEIGHT_AGENT_NAME": "<your-agent>",
            "AGENT_MODEL": "missing-provider",
            "AGENT_MODEL_API_KEY": "private-provider-key",
        }
        issues = cookbook.configuration_issues(recipe, values)
        self.assertIn("Set OPENEIGHT_AGENT_NAME.", issues)
        self.assertIn("Set AGENT_MODEL to an explicit provider:model.", issues)
        self.assertNotIn("private-key", " ".join(issues))

    def test_check_never_claims_live_readiness(self):
        output = StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            status = cookbook.main(["check", "order-lookup"])
        result = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(result["live_connectivity"], "not_checked")
        self.assertEqual(result["governance_readiness"], "not_checked")

    def test_run_forwards_arguments_in_recipe_directory(self):
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(cookbook, "environment", return_value={}),
            patch.object(cookbook, "execute", return_value=3) as execute,
        ):
            status = cookbook.main(
                ["run", "governed-refund", "--", "run", "ORD-1042", "--amount", "100"]
            )
        self.assertEqual(status, 3)
        command = execute.call_args.args[0]
        self.assertEqual(
            command[1:], ["langchain_agent.py", "run", "ORD-1042", "--amount", "100"]
        )
        self.assertEqual(
            execute.call_args.kwargs["directory"],
            ROOT / "patterns/governed-refund",
        )

    def test_manifest_points_to_owned_source_and_safe_templates(self):
        for name, recipe in cookbook.recipes().items():
            with self.subTest(agent=name):
                directory = ROOT / recipe["path"]
                self.assertTrue((directory / "README.md").is_file())
                self.assertTrue((directory / "sample.py").is_file())
                self.assertTrue((directory / recipe["requirements"]).is_file())
                self.assertIn(
                    "<your-agent-api-key>", (directory / ".env.example").read_text()
                )


class BusinessSamplesTests(unittest.TestCase):
    def sample(self, name):
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "cookbook.py"), "sample", name],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith(
                    ("OPENEIGHT_", "AGENT_", "INCIDENT_PROVIDER_")
                )
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["governance"], "not_exercised")
        return payload

    def test_partial_refund_preserves_remaining_balance(self):
        result = self.sample("governed-refund")
        self.assertEqual(result["receipt"]["amount"], 10)
        self.assertTrue(result["receipt"]["simulated"])
        self.assertEqual(result["after"]["refundable_amount"], 15)

    def test_customer_refund_follows_saved_confirmation_and_closes_case(self):
        result = self.sample("customer-support")
        self.assertEqual(result["case"]["id"], "case_damaged")
        self.assertEqual(result["receipts_before_confirmation"], [])
        self.assertIsNotNone(result["customer_confirmation"])
        self.assertEqual(result["after"]["status"], "resolved")
        self.assertEqual(result["payment_after"]["refunded_amount"], 180)
        self.assertIn("closure", [r["kind"] for r in result["receipts"]])
        self.assertNotIn("reply", [r["kind"] for r in result["receipts"]])
        self.assertTrue(result["assistant_response"])

    def test_rollback_has_a_receipt_and_improves_fixture_metrics(self):
        result = self.sample("incident-response")
        self.assertEqual(result["receipt"]["mutation"]["operation"], "rollback_release")
        self.assertLess(
            result["metrics_after"]["error_rate"],
            result["metrics_before"]["error_rate"],
        )

    def test_delayed_order_produces_actionable_reply(self):
        result = self.sample("order-lookup")
        self.assertEqual(result["result"]["action"], "investigate_delayed_delivery")
        self.assertIn("ORDER-1002", result["result"]["reply"])


class DomainTests(unittest.TestCase):
    def test_catalog_starts_with_business_agents_and_patterns_are_opt_in(self):
        main, all_recipes = StringIO(), StringIO()
        with redirect_stdout(main):
            cookbook.main(["list"])
        with redirect_stdout(all_recipes):
            cookbook.main(["list", "--all"])
        self.assertEqual(len(main.getvalue().splitlines()), 2)
        self.assertNotIn("governed-refund", main.getvalue())
        self.assertIn("governed-refund", all_recipes.getvalue())
        self.assertIn("order-lookup", all_recipes.getvalue())

    def test_business_domains_and_primary_frameworks(self):
        recipes = cookbook.recipes()
        self.assertEqual(
            set(recipes),
            {
                "governed-refund",
                "order-lookup",
                "customer-support",
                "incident-response",
            },
        )
        self.assertEqual(recipes["order-lookup"]["framework"], "custom-sdk")
        self.assertEqual(recipes["governed-refund"]["framework"], "langchain")
        self.assertEqual(recipes["customer-support"]["framework"], "langchain")
        for name, recipe in recipes.items():
            category = {
                "agent": "agents",
                "workflow": "workflows",
                "pattern": "patterns",
            }[recipe["kind"]]
            self.assertTrue(recipe["path"].startswith(category + "/"))
            if recipe["kind"] == "agent":
                self.assertEqual(recipe["path"], "agents/" + name)
        self.assertEqual(
            {name for name, recipe in recipes.items() if recipe["kind"] == "agent"},
            {"customer-support", "incident-response"},
        )

    def test_billing_case_hands_off_without_refund(self):
        process = subprocess.run(
            [
                sys.executable,
                "cookbook.py",
                "sample",
                "customer-support",
                "--",
                "--scenario",
                "billing",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        result = json.loads(process.stdout)
        self.assertEqual(
            result["payment"]["review_reason"], "possible_duplicate_charge"
        )
        self.assertEqual(result["after"]["status"], "handed_off")
        self.assertEqual(result["payment_after"]["refunded_amount"], 0)
        self.assertNotIn("refund", [row["kind"] for row in result["receipts"]])
