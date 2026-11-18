# Connect the business tools to OpenEight

For a small runnable example of both journeys, see
[Build a new agent or govern an existing one](build-or-integrate.md).

Already have an agent? Follow
[Migrate an existing agent](migrate-existing-agents.md) for LangChain and Deep Agents
constructor changes, policy setup and compatibility limits.

Customer Support is one agent with order, shipment, payment and remedy tools.
Its `tools.py` binds the retailer operations; its stage middleware
controls the tools available at each conversation stage. Customer confirmation
remains application-owned, while OpenEight governs execution.

For a small managed tool integration, start with the
[governed-refund pattern](../patterns/governed-refund/). Read its
`refund_assistant/business.py`, then `tools.py`, then `langchain_agent.py`.
The business methods enforce the exact customer request and balance; native tool
schemas describe those methods. The OpenEight factory wraps their dispatch:

```python
from openeight.langchain import create_agent

agent = create_agent(
    model=model,
    tools=tools,
    system_prompt=system_prompt,
    openeight=config,
)
```

For Deep Agents, the corresponding factory is `openeight.deepagents.create_deep_agent`.
The agent entry point shows the complete composition without hiding it in a shared
cookbook framework. Order status uses the public Client, tool readiness and Event APIs directly.
The separate OTEL pattern reports activity without using the SDK or enforcing policy.

## First connection

Create an Agent with the matching integration and save its own bound machine key.
Fill in its endpoint, immutable name and key. Publish its actual native schemas
using the recipe command. Review the resulting catalog in the console, classify
reads and writes, and configure the intended policy inputs. Test and activate the
policies and bindings. Publication acknowledgement alone is not readiness.

Invoke only after configuration is ready. A governed write must remain blocked
when authorization is unavailable or uncertain. Keep three different checks visible:
customer consent, OpenEight authorization, and a receipt from the business service.
Neither model prose nor a Decision proves an external effect occurred.

## Move from fixtures to your service

Each recipe's “Connect real services” section identifies the adapter methods to
replace and the application guarantees they currently enforce. Keep model and tool
composition intact while replacing the sample backend. Preserve authenticated
customer/service scope, exact arguments, destination idempotency, and receipt-based
outcomes. A timeout may mean the destination committed an effect; reconcile it
instead of blindly retrying. Configure production secrets outside source control.

The cookbook does not include a payment gateway, carrier integration, production
cluster client or helpdesk connector. Those are explicit application-owned adapter
boundaries, not configuration switches that silently enable real effects.
