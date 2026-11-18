> Component guide: run commands below from `cookbook/`. Repository policies also apply from the root.

# OpenEight cookbook

Start with a complete business agent, run its local sample, then connect it to
OpenEight. Order lookup, shipment tracking and refunds are tools used by Customer
Support. They do not require separate agents.

For a small runnable walkthrough, choose your path:

- [Build a new agent with OpenEight](guides/build-or-integrate.md#build-with-openeight).
- [Add governance to an existing agent](guides/build-or-integrate.md#run-native-then-add-governance).

Both use the same two-tool refund example and synthetic data. The complete
applications below demonstrate broader business workflows.

| Domain | Agent | Implementation | Business outcome |
| --- | --- | --- | --- |
| Customer support | [Customer Support](agents/customer-support/) | LangChain | Investigate an order issue, obtain customer confirmation and complete the appropriate remedy |
| Operations | [Incident Response](agents/incident-response/) | Deep Agents | Investigate a checkout incident, apply a governed remediation and report actual receipts |

## Start with local data

Use CPython 3.11–3.14. From the cookbook root:

```bash
python cookbook.py list
python cookbook.py sample customer-support
python cookbook.py sample customer-support -- --scenario billing
python cookbook.py sample incident-response
```

Samples use local business operations without a model, SDK installation,
credentials or OpenEight authorization. Payments, infrastructure changes and
support tickets are simulations. Use the integrated run to exercise actual
framework and SDK behavior.

## Run Customer Support

```bash
python cookbook.py setup customer-support
cp agents/customer-support/.env.example agents/customer-support/.env
# Fill in Agent settings and an explicit provider:model and provider key.
python cookbook.py check customer-support
python cookbook.py run customer-support -- --state .local/support setup --deployment local-support --mode integrated --apply
python cookbook.py run customer-support -- --state .local/support run --case case_damaged --message 'My order arrived damaged.'
```

Review the published tools, classify reads and writes, and activate the intended
policies in OpenEight. Missing readiness blocks protected effects. Use the CLI
`status`, `control` and `continue` commands to review facts, save structured customer
confirmation and continue the conversation. Customer consent,
OpenEight authorization and a business receipt are separate requirements.
Conversation uses ordinary assistant responses; `resolve_case` is a separate
governed Write after business completion. Use fresh schema-v4 state for this workflow.
[Customer Support setup and recovery](agents/customer-support/).

## Focused integration patterns

Customer Support owns billing review and human handoff as well as order remedies.
Incident Response demonstrates specialist delegation where investigation and
remediation have different tool access. Frameworks do not create additional
versions of the same business agent.

[Integration patterns](patterns/) isolate specific SDK techniques:

- [Order lookup](patterns/order-lookup/): custom Python and direct SDK catalog,
  readiness and telemetry calls around a read-only support tool.
- [Governed refund](patterns/governed-refund/): LangChain tool dispatch, exact
  refund arguments and approval behavior; an optional Deep Agents comparison.
- [OTEL observation](patterns/otel-observation/): observe-only OTLP integration.
- [Checkpoint recovery](patterns/checkpoint-recovery/): application-owned
  checkpoint authorization.

Runnable agents and tool patterns share `sample`, `setup`, `check`,
`run` and `test` commands. `list` shows the two business agents;
`list --all` includes focused patterns and identifies their category and framework. The two
building-block patterns, OTEL observation and checkpoint recovery, have their own
instructions. Frameworks are metadata; they do not define business categories.

## Repository structure

```text
agents/
  customer-support/                      Agent code, tools and local business state
  incident-response/                    Operational specialist collaboration
patterns/
  order-lookup/                         Custom SDK read-only tool integration
  governed-refund/                      Managed refund tool authorization
  otel-observation/                     OpenTelemetry integration
  checkpoint-recovery/                  Checkpoint authorization adapter
tests/                                  Launcher checks and shared test helpers
guides/                                 Setup, integration, development, migration
cookbook.py                             Setup and process launcher
recipes.toml                            Business agents and runnable patterns
```

Read [setup](guides/setup.md), [integration](guides/integration.md),
[development](guides/development.md).
For your own application, see [Migrate an existing agent](guides/migrate-existing-agents.md).

## Verification

```bash
python -m unittest discover -s tests -v
python cookbook.py test customer-support -- -q
python cookbook.py test incident-response -- -q
```

CI selects cookbook checks for cookbook, Python SDK and shared repository changes;
TypeScript-only changes skip this lane. Selected checks run local samples, public
API boundary checks, package builds, OTEL tests, both agents and both runnable
tool-pattern suites. Managed integration jobs build the SDK from the checked-out
source and verify that recipes use that exact candidate wheel. Ordinary cookbook
setup installs the published SDK declared by each recipe. Scripted
models and mocked HTTP responses replace external services; the actual SDK and
native frameworks execute. Missing SDK dependencies fail this lane. These checks
do not certify live providers or real business services.
[Verification details](guides/development.md#verification).

## License

All OpenEight cookbook examples and documentation use [Apache-2.0](LICENSE),
the same license as the SDKs and repository root. Individual examples include
license copies for separate distribution. Customer Support retains attribution
for its adapted SDK SQLite journal code. Third-party dependencies retain their
own licenses.
