# Advanced operations

Start with the [agent README](../README.md). Direct commands below run from the
agent directory with its virtual environment active and its explicit runtime
environment configured. Root launcher commands are documented in the README.

## Scenarios and outcomes

| Scenario | Local success evidence |
|---|---|
| `investigate` | Root/default/declarative reads; valid report; no remediation effects |
| `restart` | Exactly one restart of `replica-1` |
| `rollback` | Exactly one release change from v42 to v41 |
| `scale` | Exactly one change from two replicas to three |
| `stale-revision` | One restart succeeds; a second action with the old revision is rejected (exit 4) |

The reporting child returns a strict JSON document. The coordinator saves it through
`write_file` at `/reports/incident.json` in the returned virtual filesystem. The
runner compares that file with the child's task result and with every simulator
receipt. Missing/false/duplicate/omitted claims fail; a runner-generated summary
cannot substitute for the artifact. JSON stdout includes before/after state,
receipts, report validation, delivery, and local checks. Redirect it to an optional
result file; that file is evidence only, never recovery input.

Policy configuration determines allow, deny or approval. `--approval-timeout 300`
keeps the managed owner alive for up to five minutes to decide in Console. The SDK
polls with its bounded backoff and replays the original Decision before dispatch.
Rejected, expired or timed-out attempts do not execute. A transport/delivery failure
does not erase prior receipts or rerun effects. Controls applied before admission
block new dispatch; they cannot recall an already admitted effect. Ctrl-C cleans
up local ownership and exits 130, without claiming to revoke a server approval.

Exit codes: 0 means local checks passed and delivery drained; 1 is runtime,
assertion or delivery failure; 2 is configuration failure; 3 is block/approval
timeout; 4 is business validation failure; 130 is cancellation. Exit 0 does not
certify the separately pending HTTP, UI, billing, or exported-evidence lanes.

## Durable scripted workflow

Install `openeight` with `pip install openeight` or `uv add openeight`, then
install `'./agents/incident-response[durable]'` with pip (or enable
the example's `durable` extra with uv). This adds the supported saver dependencies.
Use a fresh Agent and its Agent-pinned key in the three OpenEight environment
variables above. Durable mode currently supports the default eleven-tool profile
and the scripted `investigate`, `restart`, `rollback` and `scale` scenarios.

```bash
incident-agent durable initialize --directory /private/incident-release-1 \
  --revision incident-release-1 --scenario rollback --mode scripted
# Configure the returned Tool/ToolVersion IDs and policies in OpenEight, then:
incident-agent durable initialize --directory /private/incident-release-1 \
  --revision incident-release-1 --scenario rollback --mode scripted
incident-agent durable start --directory /private/incident-release-1 \
  --revision incident-release-1 --command-id incident-0042
# Save the returned thread_id. After a human resolves the original approval:
incident-agent durable resume --directory /private/incident-release-1 \
  --revision incident-release-1 --thread-id <returned-thread-id> \
  --command-id approval-0042
incident-agent durable inspect --directory /private/incident-release-1 \
  --revision incident-release-1
```

The first initialize can exit 3 with `needs_configuration` and the catalog map;
repeat it after human configuration. A ready initialize exits 0. Start/resume
exit 3 while waiting or requiring recovery, 2 for incompatible configuration, 1
for runtime/report/delivery failure, and 0 for a completed validated report with
drained delivery. `inspect` is a local business-state read and needs no credentials.
It does not read execution internals or certify server delivery.

Retry a request with its original command ID and identical input. Duplicate start
returns the frozen original execution response, including an original waiting
response after a later resume completed. Its displayed business state/receipts are
current. Retry a completed resume with its original resume command ID to obtain
the same completed response and report. A new resume command is for continuing or
recovering work. The program never installs policies, approves actions, invents
Decision/Run IDs or generates execution idempotency keys.

`execution/` belongs to the SDK and contains the local journal and native
checkpoints. `business/incident.sqlite` belongs to this example and contains
immutable scenario/revision configuration, the current synthetic service state,
keyed effect receipts and bounded report candidates. Each synthetic mutation and
its receipt commit in one SQLite transaction; reconciliation validates the exact
key, original arguments and receipt history before confirming an effect. An
inconsistent or unreadable destination stops recovery. Generated todos/files and
delegation recover through SDK native-state receipts. The reporting child writes
the actual report; the coordinator reads `/reports/incident.json` through the
governed native tool, and the CLI checks those bytes against the recorded child
output and destination effects. It never manufactures a replacement report.

Use a dedicated owner-only local POSIX directory. Both stores reject insecure
permissions, symlinks and unknown schemas. Keep the complete stopped directory
for backup; no migration, pruning or multi-host execution is provided. Native
files are virtual StateBackend data and cannot access host files. Persistent
arguments/results are recovery data, independently of optional telemetry capture.

Changing the runtime, executable configuration, revision or credential cannot
adopt this directory. Preserve the old runtime/configuration/key to drain its
pending approvals. Initialize a fresh directory and a separate Agent/key for the
new runtime, configure its policies, then send new work there while the old
runtime drains. A fresh directory alone provides no server-catalog isolation.
Use the SDK's [public API documentation](../../../../sdk/python/README.md#documentation)
for durable execution APIs. Keep the runtime and storage under one application's
ownership; reconcile uncertain destination effects before retrying them.
Provider mode, QC, Console visual acceptance and publication remain
separate qualification or delivery work.

## Explicit provider and content capture

Provider mode supports explicit Anthropic and OpenAI adapters. Set
`INCIDENT_PROVIDER_MODEL=anthropic:<explicit-model-id>` or
`INCIDENT_PROVIDER_MODEL=openai:<explicit-model-id>` and `INCIDENT_PROVIDER_API_KEY`,
then use `--mode provider`. For OpenAI, install the optional adapter with the
supplemental pins from the repository root:

```bash
/tmp/incident-agent-venv/bin/python -m pip install \
  -r agents/incident-response/requirements-openai.txt \
  './agents/incident-response[openai]'
```

The supplemental file includes the existing SDK-qualified framework closure and
pins only the additional OpenAI packages. Native LangChain tool binding handles
[OpenAI function calls](https://developers.openai.com/api/docs/guides/function-calling);
the application still owns tool execution through the governed adapter.
There is no model default, provider fallback or automatic provider retry. Scripted runs do not certify
provider token reporting, model rates or billing. Provider responses use the same
tools, governed boundaries and report checks as scripted runs.

`--capture-content none` is the default. `inputs`, `outputs` and `all` opt into
optional telemetry. `--redaction synthetic-sentinels` replaces the exact synthetic
string `INCIDENT_DEMO_PRIVATE_SENTINEL` in string values with `[REDACTED]`, preserving
`INCIDENT_DEMO_CONTROL` and other content. Selecting the redactor never enables
capture. This demonstrates callback wiring; it does not detect arbitrary secrets.
Native arguments, results and required policy-selected evidence are unchanged.
The HTTP capture fixtures put sentinels in read-only fixture content, outside
selected write-policy values. Nonzero capture-failure counters fail local checks.

## Verification and operator acceptance

From this recipe directory, install the recipe and run its standalone tests:

```bash
python -m pip install -r requirements-qualified.txt '.[test,durable]'
python -m pytest tests -q
```

These tests cover the simulator, reports, configuration, destination storage and
classification fixtures without importing SDK implementation modules or SDK test helpers.
Native graph and durable-engine compatibility tests live under `sdk/python/tests/`
in this repository. Set `OPENEIGHT_COOKBOOK_ROOT` to its `cookbook/`
directory and install the exact candidate SDK wheel before running SDK qualification.
The backend repository owns authenticated HTTP end-to-end tests. Offline tests, HTTP evidence,
live provider behavior and manual Console acceptance are separate checks.

Record these manual checks separately for a fresh Agent:

1. Match all eleven default catalog names, descriptions and Tool IDs to Console.
   Require five reads and six side-effecting classifications, with none pending or
   failed. Record classification status/errors separately from policy readiness.
2. Author and activate policies through Assist; inspect classification/readiness.
3. Run allowed, denied and approval-required remediation. Approve/reject in Console
   while the CLI remains alive; compare receipts with child Decisions and Events.
4. Suspend/revoke before a new invocation; verify no new effect. Inspect previously
   completed effects separately.
5. Trace root Run, child tools, Decisions, approvals and terminal Events. Compare
   enabled/disabled content fields and real-provider usage with configured rates.
6. Export available evidence through the Console and record the export's verification
   result separately from the recipe's local report checks.
   Simulator receipts are separate corroboration, not automatically signed export data.

Manual Console/Assist, real-provider usage and offline exported-bundle acceptance
remain pending until their execution evidence is recorded. No server, database,
public API or Console implementation changes are required by this example.

The human-only `acceptance/classification.py` helper accepts an already scoped,
authenticated HTTP client from an operator harness. It validates Tool/Agent/current
version identities, polls classification for at most 120 seconds at two-second
intervals, and reports stable timeout/failure/mismatch codes. It never installs
policies, changes capability or gives human credentials to the agent. Its mock-HTTP
tests verify parsing and bounded polling; real QC classification remains a separate
acceptance result.
