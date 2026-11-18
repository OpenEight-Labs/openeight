# Setup

Use CPython 3.11–3.14. Customer Support needs macOS, Linux or WSL for its
POSIX state locks. Start from the cookbook root; every command names the recipe
explicitly. `python cookbook.py sample <recipe>` needs no installation or credentials.

`python cookbook.py setup <recipe>` creates `.venv` in the recipe's directory and installs its
requirements and editable recipe package where applicable. Setup installs the
published OpenEight SDK from PyPI at the version declared by the recipe; no SDK
source checkout is needed. Choose an interpreter with
`setup <recipe> --python /absolute/path/to/python`. A failed install is a failed setup;
the launcher does not substitute SDK implementations or silently change versions.
The qualified requirements files declare direct runtime dependencies. All recipes
target OpenEight SDK 0.0.1; its framework extras constrain compatible native
versions. These are dependency declarations, not a complete transitive lockfile.

Copy the recipe's `.env.example` to `.env` and fill in the placeholders. Files are
literal assignments, not shell scripts. Quotes and comments are supported; variable
expansion, command substitution and multiline values are not. The launcher never
prints the values. Existing shell values win, so unset stale exported credentials
before switching deployments. Use `run <recipe> --env-file /private/path/config.env`
for a separately protected file. Keep files containing credentials private.

All managed agents use these settings:

| Setting | Meaning |
| --- | --- |
| `OPENEIGHT_ENDPOINT` | Your OpenEight API origin |
| `OPENEIGHT_AGENT_NAME` | Immutable name of the selected Agent |
| `OPENEIGHT_API_KEY` | That Agent's own bound machine key |
| `AGENT_MODEL` | Explicit `provider:model` when a model is required |
| `AGENT_MODEL_API_KEY` | Credential for that model provider |

Install the LangChain provider adapter for the provider you choose in the recipe's
virtual environment. A dependency on an adapter does not select a provider or model.
The refund pattern and Customer Support include the OpenAI adapter; other
providers require an explicit adapter install. The refund pattern uses LangChain by default.
Its optional Deep Agents comparison needs
`patterns/governed-refund/.venv/bin/python -m pip install -e 'patterns/governed-refund[deepagents]'`
from the cookbook root. Incident response's OpenAI provider mode needs its `openai`
extra or `requirements-openai.txt`; the default scripted runtime needs neither.
Incident response can use scripted native models with OpenEight for deterministic
integration checks; `--mode provider` uses your explicit provider settings instead.
The order-lookup pattern needs only OpenEight settings and the SDK, with Agent integration Other.
The OTEL-only integration is a separate pattern with its own setup instructions.

`python cookbook.py check <recipe>` checks values and virtual-environment presence;
it performs no network requests and does not prove authentication or governance
readiness. Add `--provider` when checking incident provider mode.

For local HTTPS, use the transport's supported CA configuration and retain TLS
verification. The OTEL pattern accepts `--ca-file /absolute/path/to/ca.pem`. Do not
assume every SDK transport consumes ambient TLS or proxy environment variables.
The launcher does not modify transport security settings.

Direct Python/module commands still work from a recipe directory after activating
its `.venv`. The launcher maps the common model settings to the established runtime
names for Incident Response. Customer Support and Governed Refund read `AGENT_MODEL`
and `AGENT_MODEL_API_KEY` directly. For direct Incident Response invocation, follow
its operations guide for `INCIDENT_PROVIDER_*` settings.
