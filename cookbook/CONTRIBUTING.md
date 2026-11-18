# Cookbook development

Follow the repository-wide [contribution process](../CONTRIBUTING.md).
Run cookbook commands from `cookbook/` and follow the code and README structure in
[development](guides/development.md).

## Recipe structure

Build around a real user task. A recipe should accept meaningful business input,
perform a useful workflow on local sample data, and report a concrete result.
Framework regression scenarios belong in tests or advanced documentation.

Keep agent code directly under `agents/<use-case>/` and record its domain as metadata.
Treat order lookup, refunds and similar actions as tools within the business agent.
Keep specialist coordination with its owning agent and focused integration examples
under `patterns/`. Add each runnable recipe to its matching section of `recipes.toml`
so setup, checks, runs and tests work consistently. Frameworks are metadata.

Every agent needs a short README, placeholder-only `.env.example`, declared
dependencies, sample data/business operations, tests and a documented real-service
adapter boundary. Provide a standard-library sample that never uses credentials
and explicitly states that it does not exercise model calls or governance. Keep
the full governed run available separately, with observable receipts and clear
blocking behavior. Keep provider/model selection explicit.

## Checks

Run launcher/sample tests, affected recipe tests and package builds when applicable.
Use public SDK interfaces in agent code and tests; keep SDK implementation
qualification with the SDK. Use deterministic models and HTTP service doubles for
offline integration tests. Never replace the SDK client to bypass its wire contract,
vendor the SDK or contact real business services in automatic tests.
