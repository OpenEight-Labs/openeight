# OTEL transport and acceptance

Create an Agent with integration OTEL and its bound machine key. Set
`OPENEIGHT_ENDPOINT` to its HTTPS server origin, `OPENEIGHT_AGENT_NAME` to its
immutable name, and `OPENEIGHT_API_KEY` to its key. The literal env file is read
without shell execution. Keep secrets outside source control.

The demo exports to `/v1/traces` using the official HTTP/protobuf exporter. For a
local certificate authority, pass `--ca-file /absolute/path/to/ca.pem`. TLS
verification stays enabled; redirects and ambient proxies are disabled.

```bash
python agent.py --env-file .env --scenario success
python agent.py --env-file .env --scenario error
```

The first command reports `result: 42`; the second reports `operation_error:
synthetic_failure`. Each emits `observation.demo` with one `sample_tool` child.
The exporter requires successful delivery and zero reported rejected spans before
printing `export: acknowledged`. An acknowledged export does not make a failed
operation successful, and an export failure does not undo a completed operation.

CI installs only the OTEL requirements and verifies that `openeight` is absent.
Exporter doubles cover success, partial rejection and an operation failure. These
checks do not prove backend persistence. For a live acceptance run, inspect each
printed trace ID in the selected Agent's Runs and Events, including parentage and
the error status in the failure case. Confirm that these are observed operations;
no policy authorization is enforced by this pattern.
