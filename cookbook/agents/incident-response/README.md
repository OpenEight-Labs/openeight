# Incident response

Checkout errors increased after a release. The coordinator delegates diagnosis, performs the requested rollback/restart/scale operation through governed tools, then asks a reporting agent to produce a report backed by actual local effect receipts.

## Run the local sample

From the cookbook root, with Python 3.11–3.14:

```bash
python cookbook.py sample incident-response
```

The sample reads checkout logs/metrics, rolls the local service from v42 back to v41, and shows the fixture error rate dropping from 18% to 1%. It contacts no cluster. This uses Python's standard library only. It does not run a model or
exercise OpenEight authorization.

## Run with OpenEight

```bash
python cookbook.py setup incident-response
cp agents/incident-response/.env.example agents/incident-response/.env
# Fill in the placeholders; shell settings override .env values.
python cookbook.py check incident-response
python cookbook.py run incident-response -- catalog --mode scripted
# Configure every published child tool in the console.
python cookbook.py run incident-response -- run rollback --mode scripted
```

Classify diagnostics and deployment reads as Read. Review native child tool names before configuring Write policies for remediation. A delegation is not permission to mutate. Configure each actual tool binding, then inspect the Decision, report and receipt. Scripted mode drives real native framework/SDK code with deterministic test models; use `--mode provider` and explicit model credentials for a model-driven run.

See [common setup](../../guides/setup.md) for interpreter selection, provider
adapters and protected env files. No model credential is needed for the default command above.

## Read the code

| File | Responsibility |
| --- | --- |
| [simulator.py](simulator.py) | Service state, revision checks and effect receipts |
| [tools.py](tools.py) | Role-specific native tool bindings |
| [prompts.py](prompts.py) | Coordinator and specialist instructions |
| [agent.py](agent.py) | Deep Agents/OpenEight composition |
| [cli.py](cli.py) | Direct entry point: `python cli.py --help` |
| [runner.py](runner.py) | One invocation and report checks |
| [durable_cli.py](durable_cli.py) | Advanced persistent approval/recovery workflow |

## Connect real services

Replace `IncidentSimulator` reads and `apply` with your authenticated observability/deployment service adapter. Keep service identity, expected revision, action ID, conditional mutation and destination effect receipts. Preserve receipt-based reconciliation after timeouts. Never translate a model request directly into an unrestricted shell command.

## Verify

```bash
python cookbook.py test incident-response -- -q
```

Tests use local data and doubles for external services. Provider behavior, real
business effects and manual console acceptance need separate verification.
[Advanced operations](docs/operations.md) covers detailed setup, evidence and
recovery where supported. [License](LICENSE).
