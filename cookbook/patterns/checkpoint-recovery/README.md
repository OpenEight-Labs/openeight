# Checkpoint recovery pattern

`application.py` demonstrates the public `CheckpointAccess` protocol with durable
SQLite enrollment/execution sidecars. The native graph may use qualified
`AsyncSqliteSaver` or `AsyncPostgresSaver`; native rows and sidecars are separate.
This is a single-process example: own one adapter instance and saver for the entire
application lifespan, and route every native/SDK writer through its claim. Do not
run another process against these databases. Provision exclusive process ownership
outside this adapter; the in-process guard is not a distributed lock.

Supply a trusted async `authorize(config=..., context=...)` callback. It must verify
your application's authenticated session, authorize the exact thread and namespace,
select the immutable graph revision and enrolled model variant, and return
`CheckpointBinding`. A thread ID or a user-supplied subject string is not authority.
The application selects `new`, `continue` or privileged `adopt` mode after checking
its own policy. For adoption, reconcile historical destination effects first.

```python
from application import ApplicationCheckpoints
from openeight import OpenEightConfig
from openeight.langchain import create_agent

access = ApplicationCheckpoints(sidecar_path, authorize=authorize)
async with create_agent(
    model=model,
    tools=tools,
    checkpointer=native_saver,
    openeight=OpenEightConfig(agent_name=agent_name, checkpoints=access),
) as agent:
    result = await agent.ainvoke(
        request,
        {"configurable": {"thread_id": authorized_thread}},
        context=authenticated_application_context,
    )
```

Use canonical HTTPS OpenEight settings and provider credentials configured by your
application. Follow the [SDK installation and compatibility guidance](../../../sdk/python/README.md)
and [qualification environment](../../../sdk/python/constraints/README.md). Keep both databases
on durable local storage with backups. The adapter uses short synchronous SQLite
transactions; a busy/high-throughput application should provide an async adapter
with the same commit and guard semantics. Claims remain valid through settlement
and are invalidated before release, including inherited stale contexts.

A `running` or `recovery_required` record after failure blocks automatic replay.
Inspect and reconcile the original attempt; this example supplies no forced reset,
business receipt recovery, credential rotation, lease takeover or distributed
writers. Never delete rows to retry an uncertain effect. Native HITL continuation
and explicit quiescent adoption follow the SDK guide; arbitrary tool interrupts
are not durable recovery boundaries.


This is an application integration building block, not a standalone agent. Start
with [customer support](../../agents/customer-support/) for a complete recovery-capable
application. See [integration guidance](../../guides/integration.md) for the SDK boundary.
