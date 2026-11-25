# Governed refund tool · LangChain

A customer received a damaged order and requests a full or partial refund. This
focused pattern uses a small LangChain harness to look up the order and exercise
the exact requested refund through OpenEight. It reports a receipt only after the
tool succeeds. Local sample receipts move no money.

In a complete application, refund is a tool used by the
[Customer Support agent](../../agents/customer-support/).
This pattern isolates tool authorization, partial amounts and approval behavior;
it is not an additional customer-support agent.

The [two-path walkthrough](../../guides/build-or-integrate.md) shows how to build
with OpenEight or migrate an existing native agent. [native_agent.py](native_agent.py)
runs without the SDK using [requirements-native.txt](requirements-native.txt).
It shares the exact business operations, tool schemas and prompt with the governed
runner; no OpenEight credentials or configuration are used in the native path.

## Run the local sample

From the cookbook root, with Python 3.11–3.14:

```bash
python cookbook.py sample governed-refund
```

A 10 USD refund against a 25 USD order records one simulated receipt and leaves 15 USD refundable. This uses Python's standard library only. It does not run a model or
exercise OpenEight authorization.

## Run with OpenEight

```bash
python cookbook.py setup governed-refund
cp patterns/governed-refund/.env.example patterns/governed-refund/.env
# Fill in the placeholders; shell settings override .env values.
python cookbook.py check governed-refund
python cookbook.py run governed-refund -- publish
# Configure tool classifications and policies in the console.
python cookbook.py run governed-refund -- run ORD-1041 --amount 10.00
```

`lookup_order` is Read; `issue_refund` is Write. Select `order_id`, `amount`, and `currency` in its Tool binding so the reviewer can identify the order. Use this intent: “Allow refunds of up to and including 100 USD. Require approval for refunds above 100 USD or in any other currency.” Review the policy and boundary cases in [operations](docs/operations.md#configure-new-tools-once). The 180 EUR example waits for human approval; denial and missing governance execute no refund.

See [common setup](../../guides/setup.md) for interpreter selection, provider
adapters and protected env files. Set an explicit `AGENT_MODEL=provider:model` and `AGENT_MODEL_API_KEY`.

## Read the code

| File | Responsibility |
| --- | --- |
| [refund_assistant/business.py](refund_assistant/business.py) | Exact request, balances and one in-memory receipt |
| [refund_assistant/tools.py](refund_assistant/tools.py) | Native input schemas and tool bindings |
| [langchain_agent.py](langchain_agent.py) | Complete LangChain/OpenEight composition |
| [native_agent.py](native_agent.py) | Native LangChain baseline before migration, without OpenEight |
| [deepagents_agent.py](deepagents_agent.py) | Optional framework comparison; primary recipe is LangChain |
| [refund_assistant/config.py](refund_assistant/config.py) | Explicit model/provider and OpenEight settings |
| [refund_assistant/cli.py](refund_assistant/cli.py) | Explicit model/configuration, sync/async execution and output |

## Connect real services

Replace `RefundRequest.lookup_order` and `RefundRequest.issue_refund` with your order/payment adapters. Preserve the authorized order, original currency, exact customer amount and refundable balance. Use destination idempotency and durable receipts before enabling real payments. The current in-memory receipt protection ends when the process exits.

## Verify

```bash
python cookbook.py test governed-refund -- -q
```

Tests use local data and doubles for external services. Provider behavior, real
business effects and manual console acceptance need separate verification.
[Advanced operations](docs/operations.md) covers detailed setup, evidence and
recovery where supported. [License](LICENSE).

The primary setup installs LangChain only. For the optional `deepagents_agent.py`
comparison, install the `deepagents` extra as described in [setup](../../guides/setup.md).
