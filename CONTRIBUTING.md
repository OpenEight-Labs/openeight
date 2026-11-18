# Contributing

Contributions to the SDKs, examples, tests and documentation are welcome. Small
fixes can go straight to a pull request. For a substantial API or workflow change,
open a proposal first so maintainers can discuss scope and compatibility before
you invest in implementation. See [support](SUPPORT.md) for the right reporting
route and follow our [code of conduct](CODE_OF_CONDUCT.md).

## Your first pull request

1. Fork this repository on GitHub and clone your fork. From the checkout root,
   create a focused branch such as `git switch -c docs/clarify-setup`.
2. With Python 3.12 installed, create and activate an isolated environment:

   ```sh
   python3.12 -m venv .venv
   . .venv/bin/activate
   python --version
   python scripts/check.py
   ```

   On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell.
   This initial check does not need model or OpenEight credentials. SDK integration
   tests may skip until you install the SDK dependencies below.
3. Make a focused change and run the checks for its area in the table below.
   Include a regression test for a behavior fix; documentation corrections do not
   need artificial tests. Follow [SDK development](sdk/CONTRIBUTING.md) or
   [cookbook development](cookbook/CONTRIBUTING.md) for component conventions.
4. Check `git diff --check` and review new files too. Commit only your intended
   files, push the branch to your fork, and open a PR against `main`.
5. Explain the problem, resulting behavior and exact verification. Respond to
   review comments and update the same PR. A maintainer decides when it is ready
   to merge; see [maintainers and decisions](MAINTAINERS.md).

For Python SDK work, install from the repository root:

```sh
python -m pip install -c sdk/python/constraints/qualified.txt -e 'sdk/python[dev,deepagents-durable,mcp,checkpoints-postgres]'
```

For TypeScript work, use Node 24 and run `npm ci` from `sdk/typescript/`.
You can contribute documentation or local sample improvements without setting up
both SDKs. Reproducible bug reports and clearer examples are useful contributions
on their own.

SDK changes and affected recipes belong in the same PR. Link a corresponding
OpenEight-Labs/skills PR when executable public instructions change, or explain
why none is needed. Python and TypeScript releases remain independent; cookbook
changes do not publish SDK packages. Publication follows [release gates](RELEASING.md).

## Development and verification

Use Python 3.12 for the current qualification lane and Node 24 for TypeScript.
Cookbook local samples support Python 3.11–3.14. Python core and managed-framework
support differ; see [SDK setup](sdk/README.md) and the
[qualified dependency environment](sdk/python/constraints/README.md).
Check `python --version` and `node --version` before running commands. Use an
isolated Python environment and `npm ci` with the committed TypeScript lockfile.

For Python SDK changes, also run `python -m ruff check sdk/python/openeight sdk/python/typing_tests`
and `python -m mypy --config-file sdk/python/pyproject.toml sdk/python/typing_tests`
from the root after installing the SDK development dependencies. For cookbook
changes, run `python -m ruff check cookbook`.

Start with `python scripts/check.py` from the repository root. It checks generated
error mappings, framework declarations, the public SDK surface, SDK tooling and
selected dependency-free cookbook behavior. It does not run the full SDK,
TypeScript or managed-recipe suites; SDK-dependent tests can skip without an
installed SDK.

| Changed area | Additional verification |
| --- | --- |
| Python SDK | Focused tests and the SDK suite in [SDK development](sdk/README.md#development); use the qualified constraints for reproducible qualification |
| TypeScript SDK | From `sdk/typescript/`: `npm ci`, `npm run format:check`, `npm run typecheck`, `npm run build`, `npm test` |
| Cookbook recipe or launcher | Launcher/sample tests and affected recipe tests in [cookbook development](cookbook/CONTRIBUTING.md) |
| Public contracts or generated mappings | Root structural checks, regeneration from `sdk/`, and affected Python/TypeScript contract tests |
| CI selection or release workflow | Install `PyYAML==6.0.3`, then run `python -m unittest discover -s scripts -p 'test_*.py' -v`; validate workflow syntax with actionlint |
| Package metadata, exports or release tooling | Installed-distribution and external-consumer checks in [RELEASING.md](RELEASING.md) |
| Documentation only | Check links, command paths and whitespace; execute changed local examples where practical |

Use the [SDK guide](sdk/CONTRIBUTING.md) and [cookbook guide](cookbook/CONTRIBUTING.md)
for component details. Set `OPENEIGHT_COOKBOOK_ROOT` to this checkout's absolute
`cookbook/` directory for cross-component SDK qualification. Use
`git diff --check` and inspect untracked files separately. Record runtime and
dependency versions, explain skipped checks, and distinguish local tests from
backend, live-provider and published-package acceptance.

Preserve unrelated work and durable runtime state. Security-sensitive behavior,
public contracts and release changes follow the
[solo-maintainer review policy](MAINTAINERS.md#solo-maintainer-review).
See [releasing](RELEASING.md) for release qualification requirements.

## Reports and examples

For ordinary issues, include versions, the command, safe error codes, expected
behavior and synthetic reproduction data. Remove tokens, cookies, private request
bodies, customer content, production endpoints and identifying logs from reports
and attachments. Never publish credentials, runtime state, private inventories or
operational records. Follow [security reporting](SECURITY.md) for suspected
vulnerabilities.

## Licenses and attribution

Contributions to this repository are under Apache-2.0, consistent with section 5
of the [license](LICENSE), unless explicitly stated otherwise and agreed by
maintainers. Retain copyright and third-party notices, identify copied or
generated material, and submit only work you have the right to contribute.
Third-party dependencies retain their own licenses. Do not change license terms
as part of unrelated work.
