# OpenEight

Apply policies and human approvals to agent tool calls, and record their execution.
OpenEight checks policy before a governed tool runs, pauses sensitive actions for
review, and records the decision and execution outcome.

[Website](https://openeight.ai) · [Python SDK](sdk/python/README.md) · [TypeScript SDK](sdk/typescript/README.md) ·
[Cookbook](cookbook/README.md) · [Documentation](https://openeight.ai/docs) ·
[Contributing](CONTRIBUTING.md)

## When to use OpenEight

- Check policy before an agent issues a refund or changes a business record.
- Require human approval before a sensitive tool call can execute.
- Trace a tool call's policy decision and execution outcome when investigating a run.

## Add governance to an agent

The [refund walkthrough](cookbook/guides/build-or-integrate.md) starts with a native
LangChain agent and adds OpenEight while keeping its business tools. The constructor
diff below is an integration overview, not a standalone script. Follow the
[complete example](cookbook/guides/build-or-integrate.md#build-with-openeight)
for configuration and outcome handling; check [availability](#availability-and-installation)
before running it:

```diff
-from langchain.agents import create_agent
+from openeight.langchain import create_agent

 agent = create_agent(
     model=model,
     tools=build_tools(request),
     system_prompt=SYSTEM_PROMPT,
+    openeight=config,
 )
```

Here, `config` is an `OpenEightConfig` containing your endpoint, Agent name and
bound API key. In the walkthrough, a customer requests a simulated 10.50 USD refund:

| Policy outcome | What the governed example does |
| --- | --- |
| Allow | Executes the tool; reports a receipt only if execution succeeds |
| Deny | Blocks the refund; creates no refund receipt |
| Approval required | Pauses for review; pending, rejected or expired approval does not execute the refund |

## Try a local business sample

You need Git and Python 3.11–3.14. No SDK installation, OpenEight account or
model-provider credentials are needed for this sample:

```sh
git clone https://github.com/OpenEight-Labs/openeight.git
cd openeight/cookbook
python cookbook.py sample customer-support
```

The sample uses synthetic business data and local operations. It does not call a
model, contact real business services or exercise OpenEight governance. It lets
you explore the Customer Support workflow before connecting external services.

The default sample prints JSON including these fields (excerpt):

```json
{
  "governance": "not_exercised",
  "simulated": true
}
```

Explore more scenarios in the [cookbook](cookbook/README.md). To connect a model
and apply real policy decisions, follow the [setup guidance](#availability-and-installation).

## Availability and installation

| Component | Availability | Next step |
| --- | --- | --- |
| Local cookbook samples | Runnable now without credentials or an installed SDK | [Cookbook](cookbook/README.md#start-with-local-data) |
| Python SDK | Source available; first PyPI release pending | [Installation](sdk/python/README.md#installation) after publication; [development setup](CONTRIBUTING.md#development-and-verification) for source work |
| TypeScript SDK | Source available; not published to npm | [Build and test from source](sdk/typescript/README.md#qualification) |
| Governed cookbook examples | Require the published Python SDK, model credentials and OpenEight configuration | [Recipe setup](cookbook/guides/setup.md) after the Python release |

Governed examples need access to an OpenEight deployment, a configured Agent and
its bound API key, an explicitly selected model, and separate model-provider
credentials. Register tools and activate their policies before executing governed
actions. Start with the [OpenEight setup guide](https://openeight.ai/docs/connect-agent),
then follow the [governed refund setup](cookbook/guides/build-or-integrate.md#build-with-openeight).

## How the pieces fit

- **SDKs** connect your agent to OpenEight for policy decisions and execution
  telemetry. Python includes managed LangChain and Deep Agents integrations plus
  a direct client. TypeScript provides direct registration, readiness, decision,
  approval-state and telemetry APIs; it does not include the managed Python
  framework integrations. The SDKs are independently versioned.
- **The hosted control plane** manages policies, human review and execution
  records. Its server implementation and self-hosting deployment tooling are
  maintained separately and are not included in this repository.
- **The cookbook** contains runnable business agents and focused integration
  examples, including local samples you can explore without credentials.

This repository's SDKs, cookbook, tooling and documentation are open source under
[Apache-2.0](LICENSE).

## Support and contributions

For questions, bugs and feature proposals, see [support](SUPPORT.md).
For SDK development and tests, see the [SDK guide](sdk/README.md).
To contribute, follow [CONTRIBUTING.md](CONTRIBUTING.md) and the
[code of conduct](CODE_OF_CONDUCT.md). [Maintainers](MAINTAINERS.md) explain how
decisions and reviews work.
Report suspected vulnerabilities using [SECURITY.md](SECURITY.md).

## Licensing

OpenEight's SDKs, cookbook examples, tooling and documentation are licensed under
[Apache-2.0](LICENSE). Copies of the same license are included in the SDK packages,
[cookbook](cookbook/LICENSE) and individual examples so they can be distributed
separately. Retain applicable copyright and attribution notices when redistributing.
Third-party dependencies retain their own licenses.
