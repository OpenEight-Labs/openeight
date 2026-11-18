# SDK qualification with cookbook examples

Use this checklist for one reviewed SDK candidate and cookbook snapshot. It is a
runbook and evidence template, not a record of completed acceptance. Start a new
record when source, artifact bytes, dependency closure or configuration changes.
All boxes below start unchecked; previous runs do not qualify the current tree.
Follow [RELEASING.md](../RELEASING.md) for the complete release process.

## Record the candidate

- [ ] Record owner, date, OS/architecture, Python/Node versions, SDK version and
  intended supported framework/runtime tuples from
  [managed integration inventory](../sdk/python/constraints/managed-integrations.json)
  and [framework declarations](../sdk/python/openeight/_framework/frameworks.json).
- [ ] Record the combined source commit and clean/dirty status, including reviewed
  untracked files. For a local uncommitted candidate, retain a file-level snapshot
  manifest and digest. Release sign-off requires the final reviewed commit.
- [ ] Use a disposable copy of reviewed public source, isolated virtual environments
  and fresh temporary state. Exclude credentials, caches, environments and durable
  runtime state from the copy. Keep original SDK/cookbook checkouts intact.
- [ ] Record the exact SDK wheel/sdist SHA256 hashes and cookbook snapshot. Use one
  wheel throughout this record; never fall back to editable SDK source or a registry
  package with the same version. Record recipe package hashes where built.
- [ ] Keep logs and manifests outside public source; sanitize evidence before sharing.
  Do not use production credentials or contact live providers during offline checks.

## 1. Structural checks and local business samples

Run from the isolated repository checkout with Python 3.12. These checks need no
provider credentials and do not establish actual governance or provider acceptance.

```sh
python --version
python scripts/check.py
cd cookbook
python cookbook.py list
python cookbook.py list --all
python cookbook.py sample customer-support
python cookbook.py sample customer-support -- --scenario billing
python cookbook.py sample incident-response
python cookbook.py sample governed-refund
python cookbook.py sample order-lookup
```

- [ ] Structural checks pass: generated errors, public SDK surface, framework
  declarations, SDK tooling, launcher/business behavior and order-lookup tests.
- [ ] The default list shows the two business agents; `--all` adds both runnable
  patterns. Every catalog path, entry point and dependency file resolves.
- [ ] Support sample completes a confirmed remedy with a local receipt; billing
  discrepancy hands off without inventing a settled payment/refund result.
- [ ] Incident sample demonstrates a local remediation receipt and resulting state.
- [ ] Refund and order-lookup samples report expected local business outcomes.
  Reports distinguish simulation from real payments, backend and model behavior.

## 2. Build and identify the installed SDK

Use the Python candidate build, distribution inspection, clean install and isolated
pytest commands in [RELEASING.md](../RELEASING.md#build-and-qualify-candidates).

- [ ] Wheel and sdist build and pass strict metadata checks. Inspect their members,
  licenses, NOTICE and required runtime resources; exclude private source/state.
- [ ] Build/install the sdist separately and exercise it; wheel success alone is insufficient.
- [ ] Full installed-wheel SDK suite passes from outside the checkout with isolated
  Python. Set `OPENEIGHT_COOKBOOK_ROOT` to the disposable copy's `cookbook/`.
- [ ] Confirm imported `openeight` resolves inside the qualification environment;
  verify direct installation URL and SHA256 with `scripts/assert_candidate.py`.
- [ ] Record dependencies and `pip check` results. Inventory every skip and decide
  whether it blocks the claimed support tuple; do not report skipped lanes as passed.

## 3. Run every cookbook integration against that wheel

Start in the isolated repository root after building exactly one SDK wheel.
The commands below create per-recipe `.venv` directories, so do not run them over
existing application state or environments. `python` must select Python 3.12.
Set `WHEEL` to the absolute path of that reviewed wheel and `EVIDENCE` to a fresh
absolute evidence directory. Retain both for the duration of qualification.

```sh
export ROOT="$PWD"
export WHEEL="/absolute/path/to/reviewed/openeight.whl"
export EVIDENCE="/absolute/path/to/fresh/qualification-evidence"
mkdir -p "$EVIDENCE"
unset PYTHONPATH
python - <<'PYCODE'
import os
from pathlib import Path
wheel = Path(os.environ["WHEEL"]).resolve(strict=True)
assert wheel.suffix == ".whl"
Path(os.environ["EVIDENCE"], "candidate.txt").write_text(
    "openeight @ " + wheel.as_uri() + "\n"
)
PYCODE
export PIP_CONSTRAINT="$EVIDENCE/candidate.txt"
export COOKBOOK_REQUIRE_SDK=1
export OPENEIGHT_COOKBOOK_ROOT="$ROOT/cookbook"
cd cookbook
for recipe in customer-support incident-response governed-refund order-lookup; do
  case "$recipe" in
    customer-support|incident-response) path="agents/$recipe" ;;
    *) path="patterns/$recipe" ;;
  esac
  python -m venv "$path/.venv" || exit 1
  "$path/.venv/bin/python" -m pip install 'pip==26.3' || exit 1
  python cookbook.py setup "$recipe" || exit 1
  "$path/.venv/bin/python" -I "$ROOT/scripts/assert_candidate.py" "$WHEEL" || exit 1
  "$path/.venv/bin/python" -m pip check || exit 1
  "$path/.venv/bin/python" -m pip freeze > "$EVIDENCE/$recipe-dependencies.txt"
  python cookbook.py test "$recipe" -- -q || exit 1
  "$path/.venv/bin/python" -I "$ROOT/scripts/assert_candidate.py" "$WHEEL" || exit 1
done
```

Capture stdout/stderr and exit codes for each command in the run record. Recipe
packages may be editable for source tests; the SDK must be the installed wheel.
Do not relax dependency locks to turn a failed qualification into a pass.

| Example | Acceptance evidence |
| --- | --- |
| Customer Support / LangChain | Business tests, native and integrated modes, customer confirmation, order/remedy scope, billing handoff, HTTP/UI behavior and durable receipt recovery |
| Incident Response / Deep Agents | Coordinator/child behavior, strict report validation, stale/conflicting request rejection, denied-child zero effects, durable replay and receipt reconciliation |
| Governed refund / LangChain and optional Deep Agents | Exact requested amount/currency, partial balances, native-to-managed equivalence, allow/deny/pending and unavailable-service behavior |
| Order lookup / direct SDK | Registration/readiness/telemetry through the actual client; missing readiness blocks the lookup; delivery failure is distinct from the business result |

- [ ] All four suites pass with the exact wheel installed in each environment.
- [ ] Install governed-refund's optional Deep Agents extra and repeat its full suite,
  retaining the same candidate constraint. Recheck SDK origin/hash afterward:

```sh
# From cookbook/, with the candidate environment variables above still set.
patterns/governed-refund/.venv/bin/python -m pip install -e 'patterns/governed-refund[deepagents,test]'
python cookbook.py test governed-refund -- -q
patterns/governed-refund/.venv/bin/python -I ../scripts/assert_candidate.py "$WHEEL"
```

- [ ] Review collected/parametrized cases and skips: expected native, managed,
  durable and sync/async variants actually ran. A zero exit code alone is insufficient.
- [ ] For each protected effect, demonstrate allow executes the intended operation;
  deny, pending, missing readiness and unavailable governance cause zero effects.
  Use the appropriate existing tests and add focused coverage if a required case is absent.
- [ ] Verify malformed configuration and protocol data fail safely; logs/output do
  not disclose credentials or unrequested model/tool content.
- [ ] Verify shutdown, delivery failures, cancellation, retries and restart behavior
  preserve identity and distinguish incomplete work from successful settlement.
- [ ] Confirm uncertain durable effects stop for reconciliation; never delete state,
  force approval or automatically replay an unresolved effect to complete a test.

## 4. Distribution and companion patterns

- [ ] Build governed-refund, Customer Support and Incident Response packages.
  Install each wheel into its own fresh external environment under the candidate
  constraint; verify SDK origin/hash and package metadata.
- [ ] Outside the source tree, run `customer-support --help` and
  `incident-agent --help`. Exercise installed
  entry points and business behavior rather than relying solely on source tests.
- [ ] Native refund lane runs in a separate SDK-free environment using
  `patterns/governed-refund/requirements-native.txt`; its native tests pass without
  importing/installing OpenEight. Do not use candidate constraints in this lane.
- [ ] OTEL lane runs in a separate environment using
  `patterns/otel-observation/requirements.txt`, with no OpenEight SDK installed.
  Run its unittest suite, sample and `agent.py --help`. Verify callable values and
  exceptions are preserved and export failure/partial rejection is reported independently.
  These checks do not qualify SDK governance or actual backend trace persistence.
- [ ] Checkpoint recovery is qualified through SDK-owned workflow/claim tests and
  cookbook durable applications, not a launcher recipe. Check authorized thread/
  namespace/revision binding, stale claims, new/continue/adopt boundaries and recovery stops.
  Record SQLite and PostgreSQL coverage separately. The pattern README currently
  references private setup material; public reproduction remains a documentation
  gate until a public dependency/setup path is validated.

## 5. Runtime matrix and TypeScript boundary

- [ ] Run the installed-wheel managed integration matrix for Python 3.11–3.14
  using `sdk/scripts/verify_openeight_matrix.py`; pass the explicit cookbook root.
  Use fresh disposable matrix directories: this runner clears its target environments.
  Record exact dependency tuples and results from its JSON output.
- [ ] Repeat cookbook suites for every runtime/framework combination being claimed;
  a Python 3.12 recipe pass or SDK-only matrix pass does not qualify every recipe elsewhere.
- [ ] Run native hook/recovery regressions with all required dependencies and a
  supported POSIX environment. Record actual collection counts and unexpected skips.
  Do not treat passing unsafe-upstream counterexamples as safe integrations.
- [ ] Record PostgreSQL, historical SDK recovery, platform and long-running coverage
  separately; missing infrastructure/history remains an explicit qualification gap.
- [ ] TypeScript: run typecheck, build, tests, pack, external installed-tarball behavior
  and Bundler/NodeNext declaration checks per the release guide. These Python cookbook
  examples do not qualify TypeScript cookbook usage; report that coverage boundary.
  Preserve `private: true` unless publication changes are separately reviewed.

## 6. Governance against local QC — required for integrated qualification

Offline fixtures do not establish actual governance. Qualify integrated SDK/cookbook
behavior against the local QC control plane and Console using the exact candidate
artifact. A blocked or unrun QC gate leaves integrated qualification incomplete,
even when every offline test passes. This is separate from paid/provider acceptance.

Preparing this checklist does not authorize starting/redeploying QC, creating test
resources or changing policies. When execution is authorized, follow the owning
backend and Console operational runbooks and use dedicated test resources and fresh state.
Preserve existing QC services, credentials, data and other users' pending work.

### Pin the integration environment

Record the exact backend and Console revisions, deployed artifacts, configuration
and rollback plan in private qualification evidence. Recheck that environment
before execution; source checkout state and passing health checks do not prove
which software is running. If compatibility requires a server update, obtain
separate authorization and preserve existing state and rollback evidence.

### Decision-free telemetry compatibility

The SDK emits `openeight.tool.governance_revision_id` for managed decision-free
read Events. The selected server must recognize that attribute and validate the
immutable revision. An environment with only one governance snapshot can conceal
a mismatch, so fresh-agent smoke tests alone do not establish compatibility.

- [ ] With the exact SDK artifact and real service transport, capture a read Event,
  change the tool's capability, then deliver the queued Event. Verify that the
  original immutable read revision is accepted and settlement completes.
- [ ] Verify rejection of ambiguous missing stamps, foreign revisions, malformed
  stamps and historical configured-write revisions. Do not assume legacy
  attribute names are aliases.
- [ ] Recheck approval/Decision identity after read/write/read or write/read/write
  transitions: returning to a prior capability must not restore an earlier approval.
- [ ] Revalidate Decision-backed writes, read-only examples and Console projection.
  Internal revision stamps must stay out of ordinary human-facing attributes.

### Establish the environment and identity

- [ ] Record the local QC backend/frontend revisions, configuration, endpoints,
  database/migration version and relevant service status. Confirm the Console
  targets QC; health checks alone do not establish governance acceptance.
- [ ] Record candidate wheel SHA256 and installed origin in each recipe environment,
  cookbook snapshot, dependency closure and deterministic model configuration.
  Use actual SDK/network calls to QC: do not stub Decisions, approval polling,
  readiness or telemetry responses in this lane. No live provider is required.
- [ ] Provision dedicated test organization/workspace/agents through supported APIs
  or Console flows. Record scoped identities without credentials; use disposable
  business data, fresh deployment identities and state directories.
- [ ] Publish/register each recipe's exact tool versions and inspect readiness through
  the public SDK/API. Confirm expected versions, `fully_registered`,
  `fully_policy_ready` and per-tool readiness before governed execution. Catalog
  acknowledgment is insufficient. Check the unconfigured-policy case before
  activating the intended test policies, and retain its blocked/no-effect evidence.

### Exercise the governance contract

Apply these cases to the applicable operations in each recipe. A read-only example
can demonstrate governed reads; it does not prove protected-write behavior.

| Case | Required evidence from QC and the destination adapter |
| --- | --- |
| Allow | Correct scoped tool/version and exact arguments are authorized; the intended effect has one receipt |
| Deny | Backend returns a denial; the handler/effect count remains zero |
| Human approval pending | Approval is visible and pending; no protected handler/effect executes before resolution |
| Human approval granted | An authorized human approves in the normal workflow; continuation executes only the approved request without duplicate effects |
| Human approval rejected or expired, where supported | The request remains blocked and has no protected effect |
| Missing readiness / unconfigured policy | SDK reports the missing prerequisite and blocks governed execution |
| Governance unavailable | A controlled failure isolated to the test client causes a safe failure with no protected effect |
| Scope or identity mismatch | Test-only invalid/revoked credentials and mismatched tool/workspace selectors fail closed with no cross-scope reads or effects |

- [ ] Run Customer Support's confirmed remedy and billing-handoff paths, including
  write allow/deny/pending and customer confirmation. Customer consent must not
  replace an OpenEight approval; distinguish simulated receipts from real payments.
  Use fresh schema-v4 state. Verify ordinary assistant turns persist without a
  tool Decision or case closure; separately qualify `resolve_case` allow, denial,
  pending approval, restart/replay, and incomplete/foreign/stale-action rejection.
- [ ] Run Incident Response's coordinator/child and remediation paths, including a
  denied child with zero effects and validated final reporting.
- [ ] Run governed refund in each claimed framework variant; verify the authorized
  amount, currency and request identity match the effect receipt.
- [ ] Run order lookup through direct SDK registration and exact-version readiness.
  Its tool must be classified as an ungoverned Read with `read_only` readiness;
  no Decision is expected. Verify persisted read telemetry and rejection of a
  mismatched/governed classification. It does not qualify a protected write.
- [ ] Record not-applicable cases with a concrete reason. Use only dedicated test
  scopes for negative authority tests, never another user's tenant records.
- [ ] Record pre/post business state, handler counts, receipt IDs and corresponding
  tool-version, Decision, approval and Run IDs. Fluent output and a blocked SDK
  result alone do not prove zero effects.

### Recovery, persistence and Console evidence

- [ ] Pause and restart the disposable application using its documented recovery
  path. Verify pending approval stays blocked, valid approved continuation preserves
  request identity, and retry/restart does not duplicate a completed business effect.
- [ ] Exercise stale/conflicting requests and uncertain durable outcomes. Require
  reconciliation where indicated; never delete state, reset approvals or migrate
  existing pending work to make the test pass.
- [ ] Through public APIs and Console views, verify persisted tool versions, policy
  bindings, Decisions, approvals, Runs and Events match the recorded scenario.
  Validate denied/pending/approved status, correlation, settlement and delivery failures.
  Do not use SQL/service internals as the integration acceptance oracle.
- [ ] Verify optional content capture remains off unless explicitly enabled for a
  separate synthetic-data case. Retain sanitized Console evidence and effect receipts;
  do not publish tokens, complete request bodies or customer content.
- [ ] Record a QC result for each recipe/framework and governance case, with exact
  commands, service revisions, artifact hashes, observed outcomes and skipped cases.
  Resolve actionable failures before marking integrated candidate qualification passed.

## 7. Provider and additional manual acceptance — separately authorized

Local QC governance with deterministic models does not establish a provider's real
behavior, real payments/infrastructure changes or every operator workflow.

- [ ] Run explicitly selected provider/model paths only after live/paid-run authorization.
  Record provider configuration, outcome and limitations without secrets or raw payloads.
- [ ] Review remaining human usability/operational workflows in the relevant recipe
  guides, separately from the Console governance evidence already required in QC.
- [ ] Distinguish simulated refunds/remediation from real payment/infrastructure evidence.
  Any real-service validation needs separate authorization and destination-owned receipts.
- [ ] For the SDK-free OTEL pattern, verify actual QC ingestion/export rejection and
  Console visibility separately. This is observation acceptance, not governance proof.

## 8. Published-package acceptance and sign-off

- [ ] After separately authorized publication, download the exact production registry
  artifacts and compare hashes with the approved candidates. Record ownership,
  registry URLs and observation time; do not infer availability from an old 404.
- [ ] Repeat install, recipe integration and distribution acceptance in clean
  environments using only the exact registry version. Remove candidate constraints,
  direct wheel URLs, editable SDK installs and local package indexes/overrides.
  Retain the cookbook's supported dependency locks. Verify installed origins anew;
  `assert_candidate.py` checks direct local-wheel origin and is not a registry-origin test.
- [ ] The maintainer checks artifact identity, security negatives, skips,
  compatibility claims and the final source tree. Resolve actionable findings and
  record author review or independent review under the
  [review policy](../MAINTAINERS.md#solo-maintainer-review).
- [ ] Record one decision per claimed package/runtime/framework tuple. Distinguish
  offline candidate qualification, local QC governance, provider/additional manual
  acceptance and published-package acceptance. No single pass substitutes for the others.
- [ ] Keep SDK-dependent public skills off their default installation branch until
  published-package qualification and the skills repository's own acceptance pass.
- [ ] Keep unrelated work and existing runtime state unchanged. Source pushes,
  repository administration and publication require separate authorization.

## Result record

Copy this table into the private run evidence and attach sanitized summaries to review.
Use `not run`, `pass`, `fail`, `blocked` or `not applicable` with a reason and owner.

| Gate / package / runtime / framework | Status | Source and artifact hash | Commands, log references, case counts and skips | Remaining action / owner |
| --- | --- | --- | --- | --- |
| Structural and local samples | not run | | | |
| SDK installed wheel / sdist | not run | | | |
| Customer Support | not run | | | |
| Incident Response | not run | | | |
| Governed refund / framework variants | not run | | | |
| Order lookup | not run | | | |
| Installed recipe packages | not run | | | |
| Native refund / OTEL / checkpoint pattern | not run | | | |
| Runtime matrix / historical / PostgreSQL | not run | | | |
| TypeScript tarball and declarations | not run | | | |
| Local QC governance / recipe / framework / case | not run | | | |
| Provider / additional manual | not run | | | |
| Production registry acceptance | not run | | | |
| Maintainer review and final decision | not run | | | |
