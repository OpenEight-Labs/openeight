# Customer Support · LangChain

A customer reports a damaged, missing or unwanted order. One support agent uses
order, shipment and payment lookup tools to investigate, asks for a remedy choice,
waits for structured customer confirmation, and invokes refund, replacement, return
or cancellation tools. These are actions within Customer Support, rather than
separate order-status and refund agents. The CLI preserves business receipts and durable recovery.

The same agent handles reported duplicate charges through payment review and a
human handoff. Specialist delegation is demonstrated by
[Incident Response](../incident-response/).

The stateful runtime uses POSIX file locks; run it on macOS, Linux or WSL.

| Task | Tools |
| --- | --- |
| Understand the complaint and guidance | `get_case`, `search_support_knowledge` |
| Check order, shipment and payment facts | `get_order`, `get_shipment`, `get_payment`, `get_return_context` |
| Execute a confirmed remedy | `issue_refund`, `create_replacement`, `create_return`, `cancel_order` |
| Track progress, resolve or hand off | `get_resolution_status`, `resolve_case`, `add_case_note`, `escalate_case` |

Conversation uses ordinary assistant responses, saved in the local transcript.
It has no message-sending tool or approval gate. `resolve_case` is a separate
business Write: it validates completed remedy/settlement facts before recording
case closure, under your active OpenEight policy. Handoff remains distinct from
resolution. See [conversation and resolution](docs/conversation-and-resolution.md).

## Run the local sample

From the cookbook root, with Python 3.11–3.14:

```bash
python cookbook.py sample customer-support
python cookbook.py sample customer-support -- --scenario billing
```

The default walkthrough saves an explicitly synthetic customer confirmation,
executes the whole-balance refund and explicitly resolves the case.
The billing walkthrough compares two gateway captures against the order's payment
record and records a handoff, with no refund. Both use a fresh temporary database
and Python's standard library. They do not run a model or exercise OpenEight
authorization. The integrated agent requires the user to save structured confirmation.

Refunds here settle the confirmed whole outstanding balance. For a minimal
integration supporting exact partial amounts, see the
[governed-refund pattern](../../patterns/governed-refund/).

## Run with OpenEight

```bash
python cookbook.py setup customer-support
cp agents/customer-support/.env.example agents/customer-support/.env
# Fill in the placeholders; shell settings override .env values.
python cookbook.py check customer-support
python cookbook.py run customer-support -- --state .local/support setup --deployment local-support --mode integrated --apply
```

Setup initializes a fresh schema-v4 deployment and pins its endpoint, Agent name
and key. Existing schema-v3 state requires its original runtime; use a new state
directory and separately configured Agent for this workflow.
Explicit `--endpoint` and `--agent-name` flags override their environment values
for a new deployment. Existing deployments keep their saved binding and key.

Start a conversation from the terminal:

```bash
python cookbook.py run customer-support -- --state .local/support run --case case_damaged --message 'My order arrived damaged.'
```

The first invocation publishes the native tools. Review their classification and
activate the intended policies in the OpenEight Console. Missing readiness blocks
protected effects; an unsuccessful setup run is not a completed remedy.
Use the printed thread ID with `status`, save structured confirmation using
`control`, then explicitly `continue`. Use `resume` for a pending approval or
interrupted command. See [CLI controls and recovery](docs/operations.md#cli-controls-and-recovery).
Customer consent, OpenEight approval and completed business effects are separate
steps. The CLI does not approve OpenEight Decisions.

See [common setup](../../guides/setup.md) for interpreter selection, provider
adapters and protected env files. Set an explicit `AGENT_MODEL=provider:model` and
`AGENT_MODEL_API_KEY`. Direct CLI and installed `customer-support` commands read
these exported variables too; only the cookbook launcher loads `.env`. These are
the supported model-setting names for both launch paths.

## Read the code

| File | Responsibility |
| --- | --- |
| [agent.py](agent.py) | Model, standalone/integrated factories, prompt and tool availability |
| [tools.py](tools.py) | Native business tool schemas and bindings |
| [refresh.py](refresh.py), [retry_policy.py](retry_policy.py) | LangChain middleware and framework-independent retry eligibility |
| [retailer.py](retailer.py) | Scoped consent, remedy execution and atomic business receipts |
| [money.py](money.py), [fixtures.py](fixtures.py) | Currency representation and synthetic retailer setup |
| [service.py](service.py) | Command admission, dispatch and application failures |
| [storage.py](storage.py) | Transactional commands, attempts, transcripts and telemetry outbox |
| [journal_schema.py](journal_schema.py), [journal_validation.py](journal_validation.py) | Journal DDL and frozen-payload settlement validation |
| [standalone.py](standalone.py) | Native execution and receipt-backed recovery without external governance |
| [configuration.py](configuration.py), [presentation.py](presentation.py) | Pinned deployment settings and CLI status output |
| [cli.py](cli.py), [initialization.py](initialization.py) | Command interface and explicit deployment setup |

The service owns checkpointer and SDK-owner lifetimes. Agent factories construct
the graph; middleware uses retry evaluations without owning business effects.
Remedy execution helpers run inside the transaction opened by `mutate()`, which also
records the receipt. Journal validation runs before writes inside the existing
settlement transaction, so attempt results and outbox records remain atomic.

## Connect real services

Replace the retailer adapter with authenticated customer-service, order, warehouse and payment adapters. Preserve case/customer/order scope and explicit customer confirmation. Persist destination receipts and retain recovery identity for pending approvals. Physical delivery, return inspection and payments need their own real service evidence; a tool request does not complete those workflows.

## Verify

```bash
python cookbook.py test customer-support -- -q
```

Tests use local data and doubles for external services. Provider behavior, real
business effects and manual console acceptance need separate verification.
[Advanced operations](docs/operations.md) covers detailed setup, evidence and
recovery where supported. [License](LICENSE).
