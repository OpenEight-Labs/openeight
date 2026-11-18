"""Export a synthetic operation trace without an agent framework or OpenEight SDK."""

import argparse
import json
import logging
import os
from pathlib import Path
import shlex
from urllib.parse import urlsplit

from telemetry import CheckedExporter, DeliverySession, observe_call

import requests
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult
from opentelemetry.trace import Status, StatusCode


def load_env(path):
    """Read literal assignments; never execute shell configuration."""
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        parts = shlex.split(value, comments=True)
        if len(parts) > 1:
            raise ValueError("invalid_env_assignment")
        os.environ[key.strip()] = parts[0] if parts else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--ca-file", type=Path, help="Trusted CA bundle for local QC HTTPS"
    )
    parser.add_argument("--scenario", choices=("success", "error"), default="success")
    args = parser.parse_args()
    if args.env_file:
        load_env(args.env_file)
    endpoint = os.environ["OPENEIGHT_ENDPOINT"].rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("expected_https_server_origin")
    name = os.environ["OPENEIGHT_AGENT_NAME"]
    key = os.environ["OPENEIGHT_API_KEY"]
    if not name or not key:
        raise ValueError("missing_agent_credentials")
    logging.getLogger(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    ).disabled = True
    session = DeliverySession()
    exporter = CheckedExporter(
        endpoint=endpoint + "/v1/traces",
        headers={
            "Authorization": "ApiKey " + key,
            "X-OpenEight-Agent-Integration": "otel",
            "X-OpenEight-Agent-Name": name,
        },
        certificate_file=str(args.ca_file.resolve()) if args.ca_file else True,
        session=session,
        timeout=10,
    )
    provider = TracerProvider(
        resource=Resource.create({"service.name": "otel-observation-demo"})
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    tracer = provider.get_tracer("otel-observation-demo")
    try:
        result, operation_error = None, None
        with tracer.start_as_current_span("observation.demo") as root:
            trace_id = format(root.get_span_context().trace_id, "032x")

            def operation():
                if args.scenario == "error":
                    raise ValueError("synthetic_failure")
                return 6 * 7

            try:
                result = observe_call(tracer, "sample_tool", operation)
            except ValueError:
                operation_error = "synthetic_failure"
            root.set_status(
                Status(StatusCode.ERROR if operation_error else StatusCode.OK)
            )
        flushed = provider.force_flush(timeout_millis=15000)
        accepted = (
            flushed
            and exporter.results
            and all(r == SpanExportResult.SUCCESS for r in exporter.results)
            and session.responses > 0
            and session.rejected_spans == 0
        )
        print(
            json.dumps(
                {
                    "agent": name,
                    "trace_id": trace_id,
                    "scenario": args.scenario,
                    "result": result,
                    "operation_error": operation_error,
                    "simulated": True,
                    "spans": 2,
                    "export": "acknowledged" if accepted else "failed",
                    "rejected_spans": session.rejected_spans,
                }
            )
        )
        return 0 if accepted and operation_error is None else 1
    finally:
        provider.shutdown()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, requests.RequestException):
        raise SystemExit(
            "Agent failed: check configuration, TLS trust and QC connectivity."
        ) from None
