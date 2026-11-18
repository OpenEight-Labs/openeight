# OTEL observation pattern

Add observation to an existing callable without changing its result or exception.
This pattern uses the standard OpenTelemetry exporter, with no OpenEight SDK,
LangChain, model provider or business simulator. It demonstrates a synthetic
`6 * 7` operation rather than maintaining another order-support application.
Observation does not authorize execution.

## Run

From this directory, with CPython 3.11–3.14:

```bash
python sample.py
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in a dedicated OTEL Agent's settings.
python agent.py --env-file .env --scenario success
python agent.py --env-file .env --scenario error
python -m unittest discover -s tests -v
```

`sample.py` needs only the standard library and does not emit spans. The integrated
demo emits a root span and a `sample_tool` child span. `success` returns 42;
`error` records a synthetic operation failure and exits nonzero. Both report
operation outcome separately from export acknowledgment. Partial OTLP rejection
is an export failure even when the operation returned successfully.

## Instrument your existing tool

[telemetry.py](telemetry.py) contains `observe_call` and the checked exporter:

```python
from telemetry import observe_call

# Use your configured tracer and existing operation.
result = observe_call(tracer, "lookup_inventory", lambda: lookup_inventory(sku))
```

The callable's value and exception are preserved. Choose safe span attributes;
do not attach credentials or raw business payloads. No model usage or costs are
fabricated. Use the [migration guide](../../guides/migrate-existing-agents.md) when
you also need managed SDK governance.

[Transport and console checks](docs/operations.md). [License](LICENSE).
