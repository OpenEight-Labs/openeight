# Integration patterns

Patterns isolate a tool or integration boundary. Start with
[Customer Support](../agents/customer-support/) or
[Incident Response](../agents/incident-response/) for a complete agent.

| Pattern | Implementation | Purpose |
| --- | --- | --- |
| [Order lookup](order-lookup/) | Custom Python + OpenEight SDK | Publish a read-only support tool, check readiness and record execution |
| [Governed refund](governed-refund/) | LangChain; optional Deep Agents comparison | Exercise exact refund inputs, governance and approval handling |
| [OTEL observation](otel-observation/) | OpenTelemetry, without the OpenEight SDK | Observe execution without enforcing policy |
| [Checkpoint recovery](checkpoint-recovery/) | Application adapter | Authorize access to application-owned checkpoints |

Order lookup and governed refund support the common launcher:

```bash
python cookbook.py sample order-lookup
python cookbook.py sample governed-refund
```

Their integrated runs use the real SDK. They demonstrate tool boundaries and do
not represent separate business agents. OTEL observation and checkpoint recovery
have their own setup instructions.
