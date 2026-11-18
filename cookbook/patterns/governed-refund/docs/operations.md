# Advanced operations

Start with the [agent README](../README.md). Direct commands below run from the
agent directory with its virtual environment active and its explicit runtime
environment configured. Root launcher commands are documented in the README.

## Configure new tools once

Invocation publishes tools automatically. If these tools are already configured,
skip to the three requests. For a new Agent, publish schemas without running the
model so you can configure their policies first:

```bash
python langchain_agent.py publish
```

Expect `catalog_delivery: "acknowledged"` and the native-to-catalog `bindings`.
Publication registers schemas; console policy activation is still required.

In the console, open this Agent and review its catalog:

- Classify `lookup_order` as Read. It emits execution telemetry without a Decision.
- Classify `issue_refund` as Write. Configure a Tool policy selecting the native
  `amount` and `currency` fields. `amount` is a JSON number in
  **the order's currency, including decimal fractions**. `currency` is its required three-letter uppercase
  code, copied exactly from `lookup_order`; there is no default or currency
  whitelist in the handler.
- Set this demonstration intent, optionally using Assist: “Deny nonpositive
  amounts and currencies other than USD, EUR and JPY. For USD, allow positive
  `amount` values at most 50 and require approval above 50 through 200. For EUR,
  allow positive values at most 40 and require approval above 40 through 200.
  For JPY, allow positive values at most 5000 and require approval above 5000
  through 20000. Deny amounts above each currency's approval maximum.” Review,
  test and activate the policy and its exact Tool binding. The application does
  not install policy.

A manually authored [Rego policy](../policies/refund.rego) implements these limits
with default denial. Review it in a draft revision, run the saved policy cases and
activate its exact Tool binding before relying on it. Order identity and refundable
balance checks remain application responsibilities.

These are independent demonstration limits per currency, with no exchange-rate
conversion. Save policy cases for the allowed and approval boundaries, values
on either side, nonpositive amounts and an unlisted currency. For example:

| Currency | Allow | Require approval | Deny |
|---|---|---|---|
| USD | 0.01 and 50 | 50.01 and 200 | 0 and 200.01 |
| EUR | 0.01 and 40 | 40.01 and 200 | 0 and 200.01 |
| JPY | 1 and 5000 | 5001 and 20000 | 0 and 20001 |
| INR | — | — | 25 (not listed in this demonstration policy) |

Confirm readiness before invoking. These policy tests use representative inputs;
the handler separately requires the selected order, original currency and exact
customer-requested amount, no greater than the refundable balance. A different
currency, a changed request or a non-finite number records
no receipt. Orders store amounts as `Decimal` values constructed from strings.
Incoming JSON numbers use their printed decimal value for comparison
without rounding; `500` and `500.00` match `Decimal("500.00")`. Lookup and receipt
amounts remain JSON numbers, and an order amount that cannot round-trip exactly
through that numeric interface is rejected. Precision comes from the stored
order; there is no two-decimal-place or minor-unit assumption. Balance arithmetic
is exact even when the ambient Decimal precision is smaller than the order total.

Tool arguments and receipts share the order, requested amount and currency:

```json
{"order_id": "ORD-1042", "amount": 180, "currency": "EUR"}
```

If this Agent previously published full-order-only tool facts, `amount_minor`,
or a USD-only currency schema,
publish the updated schema before running it. Review the new ToolVersion and
replace the saved currency and amount rules and policy selectors with the
reviewed `amount`/`currency` configuration. Test and activate its new binding.
Existing policies and pending approvals retain their original schema and inputs;
do not reuse them as authorization for the changed contract. Finish or stop
old invocations before switching the example.

## Try full and partial requests

```bash
python langchain_agent.py run ORD-1041
python langchain_agent.py run ORD-1042
python langchain_agent.py run ORD-1043
```

| Order and fixed customer request | Expected result |
|---|---|
| ORD-1041: “Order ORD-1041 arrived damaged. Please refund it.” | Allow `amount: 25`, `currency: "USD"`; `receipt_recorded` and one simulated receipt |
| ORD-1042: “Order ORD-1042 arrived damaged. Please refund it.” | Approval required for `amount: 180`, `currency: "EUR"`; zero effects while pending; one receipt after approval and exact Decision replay |
| ORD-1043: “Order ORD-1043 arrived damaged. Please refund it.” | Deny `amount: 50000`, `currency: "JPY"`; `deny` and no receipt |

For a partial refund, supply the customer's requested amount in the order's currency (decimal fractions supported):

```bash
python langchain_agent.py run ORD-1041 --amount 10.00
```

The agent requests exactly 10 USD for the 25 USD order. A successful receipt has
`amount: 10`. Lookup keeps the original `amount: 25` and returns
`refunded_amount: 10`, `refundable_amount: 15`, and `refunded: false`.
`refunded` means fully refunded; a full request leaves zero refundable.
Omitting `--amount` requests the full amount, preserving the three commands above.
`--amount` is valid only with `run` and supports `--execution async` too.

There is one customer request per scenario and at most one receipt. A remaining
balance does not authorize another tool call: repeats are rejected, and the model
cannot change or split the customer's frozen request to fit a policy limit.
Every new CLI process starts fresh synthetic state, so this is not a way to make
follow-up refunds against a retained balance. The partial and full examples use
the same currency-specific policy limits on the **requested** amount. Refundable
balance and customer-request checks belong to the handler, not policy inputs.
Keep simulation disclosures out of policy intent; express the business rule.

Keep the 180 EUR process running. Find its pending request under this Agent in the
console and approve it. The SDK polls for up to **one hour**, configured as
`ApprovalConfig(timeout=3600)`, and replays the original Decision before dispatch.
A rejection, expiry or local timeout executes no refund. The backend approval
expiry is independent of this local wait. Approval after local waiting stops
does not revive that invocation. Blocked outcomes exit nonzero by design.

A model-only answer is `missing_refund_attempt`, a failed demonstration rather
than proof of denial. Model prose is not a receipt. `delivery` reports telemetry
acknowledgment separately from the local effect; failed/pending delivery does not
undo a recorded receipt. Do not retry a recorded refund to fix telemetry.

## Deep Agents and async execution

Create a **separate Deep Agents Agent/key**, update `OPENEIGHT_AGENT_NAME` and
`OPENEIGHT_API_KEY`, then run the same commands with `deepagents_agent.py`.
Its in-memory `StateBackend` adds native filesystem tools; use the printed
bindings to review their actual catalog names, classifications and write policies
too. The example disables the general-purpose child and adds no subagents or shell.

Sync is the default. Add `--execution async` to either runner's `publish` or `run`
command to use `apublish` or `ainvoke` with the same behavior. The SDK opens
and settles a temporary session for each call, including inline approval waiting.
No explicit managed context or flush is needed for these ordinary calls.

## Inspect and verify

Under the selected Agent, inspect the Run, lookup Event, refund Decision and,
for approval, the human actor and replay. An authorized execution Event links to
its Decision. Compare those records with the printed local receipt. Blocking
without a returned Decision reports a safe state without inventing IDs. Missing
configuration or unavailable authorization must leave receipts empty.

Run the offline checks with `python -m pytest`. These use real SDK/native
framework code and doubles for external model/control services. They do not
qualify your provider account or backend. Usage/spend requires model telemetry
and pricing; unpriced usage is not free. Validate reported token usage against the
selected provider's usage records and configured prices. Inspect exported evidence
separately from local receipts; an audit bundle does not prove a payment occurred.

Receipts and duplicate protection exist only in the current process. Each new
process starts a fresh synthetic demonstration. There is no restart/resume or
cross-process exactly-once guarantee. For recovery requirements, see the
[checkpoint recovery pattern](../../checkpoint-recovery/README.md) and
[Customer Support example](../../../agents/customer-support/README.md). Destination
receipts and reconciliation are still required before retrying uncertain effects.
