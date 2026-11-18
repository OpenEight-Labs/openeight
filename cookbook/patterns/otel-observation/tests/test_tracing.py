"""Verify SDK-free trace delivery and unchanged operation results."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import agent
    from opentelemetry.sdk.trace.export import SpanExportResult
except ModuleNotFoundError:
    agent = None


class Exporter:
    rejected = 0
    spans = []

    def __init__(self, **options):
        self.session = options["session"]
        self.results = []

    def export(self, spans):
        type(self).spans.extend(span.name for span in spans)
        self.results.append(SpanExportResult.SUCCESS)
        self.session.responses += 1
        self.session.rejected_spans += self.rejected
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


@unittest.skipIf(
    agent is None, "Install the recipe's OTEL requirements for tracing tests"
)
class TracingTests(unittest.TestCase):
    def invoke(self, scenario, *, rejected=0):
        Exporter.spans = []
        Exporter.rejected = rejected
        output = StringIO()
        with (
            patch.object(agent, "CheckedExporter", Exporter),
            patch.dict(
                os.environ,
                {
                    "OPENEIGHT_ENDPOINT": "https://control.example",
                    "OPENEIGHT_AGENT_NAME": "test",
                    "OPENEIGHT_API_KEY": "private-test-key",
                },
                clear=True,
            ),
            patch.object(sys, "argv", ["agent.py", "--scenario", scenario]),
            redirect_stdout(output),
        ):
            status = agent.main()
        self.assertNotIn("private-test-key", output.getvalue())
        self.assertEqual(set(Exporter.spans), {"observation.demo", "sample_tool"})
        return status, json.loads(output.getvalue())

    def test_operation_result_is_preserved_by_tracing(self):
        status, result = self.invoke("success")
        self.assertEqual(status, 0)
        self.assertEqual(result["result"], 42)
        self.assertIsNone(result["operation_error"])
        self.assertEqual(result["export"], "acknowledged")

    def test_partial_rejection_does_not_change_operation_result(self):
        status, result = self.invoke("success", rejected=1)
        self.assertEqual(status, 1)
        self.assertEqual(result["export"], "failed")
        self.assertEqual(result["result"], 42)

    def test_operation_failure_is_separate_from_export(self):
        status, result = self.invoke("error")
        self.assertEqual(status, 1)
        self.assertEqual(result["export"], "acknowledged")
        self.assertIsNone(result["result"])
        self.assertEqual(result["operation_error"], "synthetic_failure")
