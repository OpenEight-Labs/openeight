"""Checked OTLP delivery; trace acceptance is separate from business execution."""

import requests
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)


class DeliverySession(requests.Session):
    def __init__(self):
        super().__init__()
        self.trust_env = False
        self.rejected_spans = 0
        self.responses = 0

    def request(self, *args, **kwargs):
        kwargs["allow_redirects"] = False
        response = super().request(*args, **kwargs)
        if response.ok:
            result = ExportTraceServiceResponse.FromString(response.content)
            self.rejected_spans += result.partial_success.rejected_spans
            self.responses += 1
        return response


class CheckedExporter(OTLPSpanExporter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.results = []

    def export(self, spans):
        result = super().export(spans)
        self.results.append(result)
        return result


def observe_call(tracer, name, operation):
    """Record an existing callable without changing its return value or failure."""
    with tracer.start_as_current_span(
        name,
        attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name},
    ):
        return operation()
