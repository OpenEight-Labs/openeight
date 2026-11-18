# Advanced operations

Start with the [agent README](../README.md). Direct commands below run from the
agent directory with its virtual environment active and its explicit runtime
environment configured. Root launcher commands are documented in the README.

## Confirmation and completion

Use `status` to inspect the bound case, order, balance, currency, facts revision
and existing actions. Save a closed JSON envelope with `control` to confirm the
exact remedy. Free-text agreement is not consent. A refund confirms the whole
outstanding amount and original currency; replacement includes shipping method.
Return/cancellation include neither monetary nor shipping placeholders. A new
confirmation supersedes only an unexecuted confirmed action; executing the first
primary remedy permanently allocates the order, including after rejection,
completion or handoff.

A saved confirmation creates its action and receipt atomically without invoking
the graph. Run `continue` explicitly afterward. Finished customer turns use an
ordinary SDK reply with a factual continuation message; pending approvals or
interrupted commands use `resume`. Open the approval URL from `status` in
authenticated OpenEight Console to resolve a Decision, then resume through the CLI. Local
controls cannot approve Decisions. Retain each exact control envelope and its
command ID for explicit retries; another control never substitutes for it.

A refund completes with its ledger receipt. Replacement waits for synthetic
operator dispatch and delivery. Return waits for receipt and inspection; rejection
hands off the case. Cancellation records cancellation. An accepted return or
cancellation with a positive balance awaits a separately confirmed, governed child
refund; child and parent complete together. Zero balance completes without a refund
call. These completed actions do not close the case. `resolve_case` separately
validates the bound completed action, allocated completed primary remedy and
current facts revision, then records case closure under the active policy.
Ordinary assistant responses never close cases and do not require a tool Decision.
Handoff is shown as handed off, never resolved; the agent can acknowledge it in
an ordinary final response. Later physical events remain inspectable without
reopening the completed conversation.

The assessment and physical-event controls are synthetic. They save assessment and physical
facts with server UTC time, validate stage/revision and retain immutable receipts.
Replacement stock and real payments/carriers are outside this model. One product
per order, the full quantity, one executed primary remedy per order and one live
settlement child are supported. Partial/split compensation needs support review.

## Tools and money

The four remedy tools require `action_id`, `facts_revision` and ordinary business
arguments: refund `amount`/`currency`, replacement `product_category`/`verified_issue`,
return `age_days`, cancellation `fulfilment_status`. Seven reads expose the bound
case/order, payment/shipment projections, return context, workflow guidance and
resolution progress. Other writes resolve the case, add internal notes or hand
off. `resolve_case` takes required `case_id`, non-null `action_id` and
`facts_revision`; it carries no conversation body. Exact idempotency-key replay
returns the original closure receipt; a new closure after resolution is refused.
There is no outbound messaging or proposal tool.

Amount is a strict JSON int or float in major currency units, not minor-unit counts.
Decimal amounts are supported within currency precision; 50 remains 50 and
50.0 remains 50.0 in frozen arguments. SQLite balances are decimal TEXT and retailer
arithmetic uses Decimal. The example supports USD/INR at two fractional digits,
JPY at zero and KWD at three. These are representation settings, never approval
limits. No default currency, foreign exchange, cents or scale fields are exposed.
Reject unsupported currencies, excess fractional precision, nonfinite/negative
amounts and values that cannot round-trip exactly through JSON numeric projection.
The upper bound is 9007199254740991 whole units, but fractional representability
also matters: `900719925474099.91` cannot become `900719925474099.9`. CLI JSON
controls reject that conversion before recording consent. This is not a claim of
arbitrary-precision transport through providers, the shared SDK, OPA or the editor.

Fixtures include delivery/assessment/fulfilment variations, settled and uncaptured
orders, and JPY/KWD examples. They encode facts, not policy verdicts. Generate actual intent and policy in OpenEight, record any human-supplied
intent, and compare that output with the business requirements separately. Never load baseline Rego in place of generated output and call it a
successful generation. Historical schema-v2 policy examples do not configure this schema-v4 workflow. Backend Assist regression acceptance is a separate operator gate; no private checkout is required for cookbook use.

Set `OPENEIGHT_CONSOLE_ORIGIN` if your OpenEight Console uses a different origin
from `OPENEIGHT_ENDPOINT`. It only supplies approval and Run links in status output;
no API credentials or requests are sent there. Links require the deployment's
optional workspace ID. Without an override, links use the API origin.

## CLI controls and recovery

```sh
customer-support --state .local/remedies-standalone run \
  --case case_damaged --message 'I need help with my order.'
customer-support --state .local/remedies-standalone status --thread '<thread-uuid>'
customer-support --state .local/remedies-standalone receipts --thread '<thread-uuid>'
customer-support --state .local/remedies-standalone control --envelope '<control.json>'
customer-support --state .local/remedies-standalone continue --thread '<thread-uuid>'
customer-support --state .local/remedies-standalone resume --thread '<thread-uuid>'
```

A refund control file contains this closed envelope, using the thread/case and
revision from status. Use a fresh canonical UUID as command_id:

```json
{
  "command_id": "<uuid>",
  "thread_id": "<uuid>",
  "case_id": "case_damaged",
  "facts_revision": 1,
  "kind": "confirmation",
  "payload": {"remedy": "refund", "amount": 180, "currency": "USD", "reason": "I confirm the displayed whole-order refund"}
}
```

Replace the payload with `{"remedy":"return","reason":"Confirmed"}` or
`{"remedy":"cancellation","reason":"Confirmed"}` for those actions; replacement
uses `remedy`, `reason` and `shipping_method`. Assessment uses kind `assessment`
and payload `{"verified_issue":"damaged"}`. Physical controls use kind
`physical_event` and `{"action_id":"<uuid>","event":"dispatch"}`; events are
`dispatch`, `delivery`, `receipt`, `accept`, `reject`.

All controls validate thread-bound
case/customer/order, acquire lifecycle-before-thread locks, then enter the retailer
writer transaction. Exact command/envelope replay returns its receipt even after
revision changes; a differing envelope conflicts. Duplicate physical events bind
the new command without repeating the event or incrementing revision.

Keep both databases and the pinned machine key. Schema-v4 uses a new directory;
old schema-v2/v3 state is refused without being rewritten. Rollback stops this runtime
and opens the preserved old directory with its compatible prior runtime and identity.
Never transplant pending Decisions or action keys, and never rotate credentials
beneath pending work. Files are mode 0600 and state directories 0700. Reset refuses
unsettled work and cannot undo server evidence. Stable lock files remain outside
the state directory.

Receipt-first recovery repairs results and integrated Event/outbox settlement.
Unknown outcomes stop for reconciliation. After a settled known-no-effect failure,
only changed facts for the same executable, unchanged-consent action permit one
refreshed governed attempt per originating start/reply command. Checkpointed tool
calls and command-ID customer messages attribute attempts across restart and
approval resume; missing or ambiguous attribution stops refresh. Unchanged facts,
a second failure, denial or approval wait never create an automatic retry loop.

## Verification

```sh
python -m pip install -e ".[dev]"
python -m pytest tests -q
```

Deterministic retailer/graph tests, SDK compatibility, real OpenEight-generated
intent/policy comparison, authenticated HTTP approval/recovery and manual UI
acceptance are separate evidence. The control plane's
`TestActionScopeReferenceExamples` exercises selected schema-v4 tools through real
HTTP classification, policy activation and human-approval endpoints. It prepares a
completed zero-balance retailer fixture and checks `resolve_case` approval rejection
(zero closures) and approval (one closure), alongside operation scope. For its
installed-SDK lane, set `OPENEIGHT_MANAGED_INSTALLED=true` and point
`OPENEIGHT_PYTHON` to the qualified interpreter. This scoped harness does not cover
the full four-remedy lifecycle or durable CLI recovery; those require the separate
recipe/QC checks above. See the OpenEight LangChain integration guide
(`docs/integrations/langchain.md`) and local QC runbook
(`docs/runbooks/local-qc.md`) for platform verification.

### Execution telemetry

The local recovery store upgrades attempts with nullable `pause_event_json` and
`resume_event_json`, preserving existing approvals, receipts and outbox records.
Pause/resume observations identify their tool and Decision and settle atomically
with attempt transitions. Handler latency excludes approval waiting; receipt-only
recovery uses observation time with no invented duration. Deploy the matching
Observe 000011 server before running this SDK against QC. This does not add an
automatic durable polling loop or change remedy behavior.

Optional telemetry content is excluded by default. Applications constructing the
managed SDK owner can explicitly select `OpenEightConfig(capture_content="inputs" |
"outputs" | "all", redact_content=callback, ...)`; supplying a redactor alone does
not enable capture. The example uses the default `"none"`. Required policy-selected
Decision input still appears in linked tool Events. Native command inputs,
checkpoints, results and business receipts remain unchanged and may contain secrets.

Enabled public content and synchronous application redaction results must each
fit 16 KiB of canonical UTF-8 JSON. Failed capture omits only that optional field;
provider metadata, streaming tokens and tool artifacts remain excluded. The SQLite
store validates optional redacted output independently from the native result while
retaining other settlement checks and atomic result/Event/outbox insertion.
Receipt recovery captures newly produced telemetry under the new invocation policy.
Existing frozen outbox records replay unchanged without invoking the redactor,
even after opt-out. Inventory pending records before rollout; this change does not
purge retained content. Monitor the public delivery status's
`content_capture_failures` counter. Redaction applies to newly captured optional
telemetry, not native checkpoints, business receipts or already queued records;
protect those stores and apply retention only after pending work is settled.

## Policy-owned argument selection

Tool publication contains native schema and configuration only. Before each new
Write attempt the adapter obtains GetToolReadiness for the exact ToolVersion,
collects the minimal union of native paths required by its active Policies and
sends `input={}`, `policy_contract_set_digest` and `selected_values`. Policy aliases
are applied by the server. Unselected arguments are not sent for governance.
Registration summaries do not carry executable selector requirements. The local
catalog refreshes governance digests on an unchanged schema version; schema or
Tool ownership conflicts still block execution. Existing attempts retain their
frozen authorization envelope.

The attempt freezes the complete envelope before Decide. Transport retry, approval
resume and linked terminal telemetry use that snapshot even if active policies
change. A new attempt refreshes readiness; it never rewrites a prior Decision.
Durable storage retains `decision-request-v3`, the contract-set digest and canonical
three-field envelope. Obsolete local attempt formats are refused without conversion
or deletion; preserve the old store with its compatible runtime and use a fresh
separately configured deployment for this release.
