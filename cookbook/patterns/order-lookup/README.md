# Order-lookup tool · custom Python + OpenEight SDK

A customer asks where an order is. This read-only tool integration reads local shipment
facts and returns a truthful next step: confirm delivery, track a shipment,
investigate a late delivery, or ask for a corrected order number. It uses the
OpenEight SDK directly, with no LangChain, Deep Agents or model account.

Order and shipment lookup are tools in the
[Customer Support agent](../../agents/customer-support/). This
pattern isolates direct SDK dispatch and telemetry for developers writing their
own orchestration; it is not a separate business agent.

## Local sample

From the cookbook root, using CPython 3.11–3.14:

```bash
python cookbook.py sample order-lookup
```

ORDER-1002 is past its promised delivery date. The output recommends investigating
with the carrier; it does not claim an investigation ticket was opened. This
walkthrough uses standard-library business logic and does not contact OpenEight.

## Run with the SDK

Create an OpenEight Agent with integration **Other** and its own machine key.

```bash
python cookbook.py setup order-lookup
cp patterns/order-lookup/.env.example patterns/order-lookup/.env
# Fill in endpoint, Agent name and its bound key.
python cookbook.py check order-lookup
python cookbook.py run order-lookup -- publish
# In the console, classify lookup_order as Read.
python cookbook.py run order-lookup -- run ORDER-1002 --as-of 2026-01-15
```

The runtime calls `Client.register_tools`, verifies the exact ToolVersion with
`Client.get_tool_readiness`, then uses `Client.governed_run` and `Event` to record
the read and local recommendation. It refuses to run when the lookup is not ready
as a Read tool. Read-only work needs no Decision; the SDK context instruments the
run and does not automatically authorize arbitrary writes. No raw order inputs or
outputs are included in telemetry. Delivery failure exits unsuccessfully.

## Read the code

- [business.py](business.py): order lookup and recommendation rules.
- [data/orders.json](data/orders.json): delivered and delayed sample shipments.
- [agent.py](agent.py): explicit catalog, readiness, dispatch and telemetry.
- [tests/test_sdk.py](tests/test_sdk.py): actual SDK with an HTTP service double.

## Connect real services

Replace `lookup_order` with a customer-scoped order/tracking adapter. Authenticate
the caller and authorize the selected order before accessing real records. Supply
the application date explicitly for deterministic scenarios. Recommendations are
not effects: adding ticket creation or a refund needs a separate write operation,
selected-input Decide/approval handling and destination receipts. This read-only
recipe does not implement that write lifecycle; [refunds](../governed-refund/) does.

## Verify

```bash
python cookbook.py test order-lookup -- -v
```

SDK tests cover acknowledged telemetry, read-classification blocking and failed
delivery. They use HTTP doubles, not a live deployment. [License](LICENSE).
