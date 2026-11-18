# Develop a recipe

An agent owns a complete customer or operational task. Keep its runtime modules
directly in `agents/<agent>/`, as in `agents/customer-support/` and
`agents/incident-response/`. Record the business domain in `recipes.toml`;
it does not need another directory level. Actions such as order lookup and refund execution
are tools of that agent. Do not create an agent recipe for every tool. Specialist
coordination stays with its owning business agent; a focused
SDK or tool integration belongs under `patterns/<pattern>/`.

Declare recipes in the corresponding `agents` or `patterns` section
of `recipes.toml`. Launcher names are unique across both sections. Declare
the implementation framework as metadata, rather than a directory category. Each
README starts with the user's problem and an observable result.

Keep the first run short: one standard-library business sample, one environment
file, one dependency setup, one clear command. Show expected output and the next
human action. Keep provider choices explicit. Explain when a run is waiting, blocked
or incomplete instead of treating a fluent model answer as success.

Keep code boundaries visible:

- Business operations own validated inputs, local data and effect receipts.
- Native tools own schemas and bind those operations to the model.
- Agent composition owns prompts, model selection and the OpenEight factory.
- CLI code owns arguments, configuration and output.
- Advanced operations documentation owns recovery, retained state and deployment details.

Use the smallest structure appropriate to the application. Do not add a shared
cookbook runtime or a base agent class just to make every directory look identical.
The launcher owns setup and process invocation only. Each application remains
independently runnable and installable where it declares a package.

Every README follows the same reading order: problem and result, local sample,
integrated run, OpenEight tools/policies, code map, real-service adapter boundary,
and verification. Keep detailed policy examples and recovery procedures under the
agent's `docs/`. Give configuration defaults a concrete use case.

## Verification

Run `python -m unittest discover -s tests -v` from the root. This checks literal
configuration loading, safe diagnostics, command routing, all credential-free
samples and the order-lookup tool outcomes. Run each runnable recipe's tests
with `python cookbook.py test <agent> -- -q` after setup.

Both agents and both runnable tool-pattern suites run on
every pull request. They execute
the real SDK and framework, use deterministic models, and intercept public HTTPX
send methods to return control-service responses without network I/O. SDK request
serialization, response validation, decisions, approval polling and telemetry
settlement still execute. Shared HTTP test helpers live in `tests/support/`; agent
runtime code never imports them. `COOKBOOK_REQUIRE_SDK=1` makes missing SDK imports
fail instead of skipping the lightweight unittest suites. They do not
prove a live provider account, real payments, production infrastructure, backend
persistence or manual console acceptance. Package builds check distributable code;
OTEL smoke checks validate imports and CLI construction, not trace persistence.

Do not import private SDK modules or inspect managed owners in recipes or cookbook
tests. The root suite checks this boundary. Customer Support owns its SQLite journal
and implements the public `DurableExecutionStore` protocol; its standalone graph
captures the native tools at construction. SDK lock, callback and injected-tool
implementation tests belong in `sdk/`. The backend repository owns server behavior
tests. Keep runnable cookbook applications here rather than copying them into
other repositories. Cross-repository qualification can locate this cookbook through
`OPENEIGHT_COOKBOOK_ROOT`; pin reviewed cookbook and SDK revisions for repeatable
checks. Coordinate path changes with consumers. Ordinary cookbook users install
the published SDK and do not need an SDK source checkout.

Each recipe runs in its own virtual environment. Flat source modules use local
names such as `agent` and `tools`; the SDK's incident qualification suite runs in
a separate Python process so independent applications do not share those imports.

Maintain the recipe's existing license. Do not move credentials, local state,
virtual environments or provider output into tracked sample data.
