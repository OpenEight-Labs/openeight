# OpenEight

OpenEight helps applications govern agent tool calls and observe their execution.
This repository contains the Python and TypeScript SDKs, runnable business agents
and focused integration examples. Use OpenEight to check policy before a tool
runs, pause sensitive actions for human approval, and record what happened.
The hosted control plane is maintained separately; its server implementation and
self-hosting deployment tooling are not included in this repository.

## Start here

| Your goal | Where to start |
| --- | --- |
| Try a business agent with local sample data | [Cookbook](cookbook/README.md): Customer Support or Incident Response |
| Add OpenEight to an existing agent | [Integration walkthrough](cookbook/guides/build-or-integrate.md#run-native-then-add-governance) |
| Use Python | [Python SDK](sdk/python/README.md) |
| Use TypeScript | [TypeScript SDK](sdk/typescript/README.md) |
| Develop or test an SDK | [SDK development](sdk/README.md) and [contributing](CONTRIBUTING.md) |

With Python 3.11–3.14, run a credential-free sample from this checkout:

```sh
cd cookbook
python cookbook.py list
python cookbook.py sample customer-support
```

The sample uses synthetic business data and local operations. It does not call a
model, contact real business services or exercise OpenEight governance. Follow the
recipe guide for an integrated run with explicit credentials, provider settings
and policy readiness.

The default sample prints JSON including these fields (excerpt):

```json
{
  "assistant_response": "Your confirmed whole-order refund is complete.",
  "mode": "local_business_sample",
  "governance": "not_exercised",
  "simulated": true,
  "scenario": "refund"
}
```

## See governance in action

The [refund walkthrough](cookbook/guides/build-or-integrate.md) starts with a native
LangChain agent and adds OpenEight while keeping its business tools. The constructor
change is small; configuration and outcome handling are part of the full example:

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

The local samples and offline fixture tests need no hosted account. Integrated
governance needs access to an OpenEight deployment with a configured Agent, bound
API key and active tool policies. For the hosted service, start with the
[OpenEight setup guide](https://openeight.ai/docs/connect-agent).
Model-driven examples also need an explicitly selected model and its provider
credentials. The native example uses a model provider without OpenEight governance.
See [setup](cookbook/guides/setup.md) to install and configure a cookbook recipe.

## SDK installation

The Python SDK is prepared for publication to PyPI; registry acceptance is pending.
The TypeScript package remains private and is not enabled for npm publication.
See the [Python installation guide](sdk/python/README.md#installation) and
[TypeScript installation guide](sdk/typescript/README.md#installation) for details.
Cookbook setup uses the published Python SDK and requires its release to be available.
Public assistant skills are maintained independently in
[OpenEight-Labs/skills](https://github.com/OpenEight-Labs/skills); installation
acceptance is pending. This repository contains no generated skills mirror.

## Repository layout

- `sdk/python/` and `sdk/typescript/`: independently versioned clients.
- `sdk/contracts/`: shared public contracts and error mappings.
- `cookbook/`: business agents, focused patterns, guides and launcher.
- `scripts/` and `.github/`: repository checks and CI.

For questions, bugs and feature proposals, see [support](SUPPORT.md).
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
