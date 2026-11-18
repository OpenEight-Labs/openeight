# Contributing

Contributions to the SDKs, examples, tests and documentation are welcome. Small
fixes can go straight to a pull request. For a substantial API or workflow change,
open a proposal first so maintainers can discuss scope and compatibility before
you invest in implementation. See [support](SUPPORT.md) for the right reporting
route and follow our [code of conduct](CODE_OF_CONDUCT.md).

## Your first pull request

1. Fork this repository on GitHub. Replace `YOUR-USERNAME` below with your GitHub
   username, then clone your fork and create a focused branch:

   ```sh
   git clone https://github.com/YOUR-USERNAME/openeight.git
   cd openeight
   git switch -c docs/clarify-setup
   ```

   Use a `docs/`, `fix/`, `feat/` or `chore/` branch appropriate to your change.
2. Choose the setup below for your contribution. You do not need both SDKs for
   documentation or cookbook work.
3. Make a focused change and run the applicable checks. Include a regression test
   for a behavior fix; documentation corrections do not need artificial tests.
4. Run `git diff --check`, review your diff and inspect new files. Commit only
   intended files, push your branch to your fork, and open a PR against `main`.
   Use the PR template to explain the problem, resulting behavior and verification,
   including checks you could not run and why.

### After opening a PR

GitHub Actions runs the checks selected for your changes. Workflows from outside
contributors may wait for maintainer approval before running. If checks fail,
inspect their logs, make corrections and push to the same branch. Respond to
review feedback in the PR so the discussion and decisions stay together.

The project currently has one maintainer, who decides when a PR is ready to merge.
Community reviews are welcome but do not grant merge or publication authority.
Reviews happen as capacity permits; see [maintainers and decisions](MAINTAINERS.md)
for the review policy. Merging a PR does not publish an SDK release.

## Development and verification

Choose the path matching your change. Unless a block explicitly changes directory,
run commands from the repository root. Use Python 3.12 for the current qualification
lane and Node 24 for TypeScript. Cookbook local samples support Python 3.11–3.14;
Python core and managed-framework support differ, as described in [SDK setup](sdk/README.md).

### Documentation

Check links, heading anchors, command paths and `git diff --check`. Run changed
examples where practical using their documented setup. Prose-only changes do not
require installing Python or Node locally. Reproducible bug reports and clearer
examples are useful contributions too.

### Python environment

For Python SDK or cookbook work, create an isolated environment from the repository
root, then follow the relevant section below:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python --version
```

On Windows PowerShell, use `py -3.12 -m venv .venv` and activate with
`.venv\Scripts\Activate.ps1`. Customer Support requires macOS, Linux or WSL for
its POSIX state locks.

### Python SDK

Install the SDK and development tools with the qualified constraints, then run
lint, typing checks and tests:

```sh
python -m pip install -c sdk/python/constraints/qualified.txt -e 'sdk/python[dev,deepagents-durable,mcp,checkpoints-postgres]'
python -m ruff check sdk/python/openeight sdk/python/typing_tests
python -m mypy --config-file sdk/python/pyproject.toml sdk/python/typing_tests
python -m pytest sdk/python/tests -q
python scripts/check.py
```

See [SDK development](sdk/CONTRIBUTING.md) for generated contracts and
[test environments](sdk/python/constraints/README.md) for qualification details.
Set `OPENEIGHT_COOKBOOK_ROOT` to this checkout's absolute `cookbook/` path when
running cross-component SDK qualification. Tests requiring those inputs may skip
without them; report skips with your results.

### Cookbook

Install Ruff into your active environment before linting. Local samples and
launcher checks do not need model or OpenEight credentials:

```sh
python -m pip install 'ruff==0.12.12'
python -m ruff check cookbook
python scripts/check.py
```

Follow [cookbook development](cookbook/CONTRIBUTING.md) for recipe conventions and
[recipe setup](cookbook/guides/setup.md) for each recipe's separate environment.
Managed recipe tests require setup with the published SDK; they use deterministic
external-service responses and do not require live credentials. Local checks may
skip SDK-dependent tests until dependencies are installed. Report those skips;
they do not establish managed-integration coverage.

### TypeScript SDK

With Node 24 installed, run:

```sh
node --version
cd sdk/typescript
npm ci
npm run format:check
npm run typecheck
npm run build
npm test
cd ../..
```

Use `npm ci` with the committed lockfile. See [SDK development](sdk/CONTRIBUTING.md)
for shared contract checks.

### Checks by changed area

For code or tooling changes, also run `python scripts/check.py` from the repository
root using Python 3.12. TypeScript-only contributors can use the Python environment
instructions above for this check. It checks generated error mappings, framework
declarations, the public SDK surface, SDK tooling and selected dependency-free
cookbook behavior. It does not run the full SDK, TypeScript or managed-recipe suites;
SDK-dependent tests can skip without an installed SDK.

| Changed area | Additional verification |
| --- | --- |
| Python SDK | Focused tests and the SDK suite in [SDK development](sdk/README.md#development); use the qualified constraints for reproducible qualification |
| TypeScript SDK | From `sdk/typescript/`: `npm ci`, `npm run format:check`, `npm run typecheck`, `npm run build`, `npm test` |
| Cookbook recipe or launcher | Launcher/sample tests and affected recipe tests in [cookbook development](cookbook/CONTRIBUTING.md) |
| Public contracts or generated mappings | Root structural checks, regeneration from `sdk/`, and affected Python/TypeScript contract tests |
| CI selection or release workflow | Install `PyYAML==6.0.3`, then run `python -m unittest discover -s scripts -p 'test_*.py' -v`; validate workflow syntax with actionlint |
| Package metadata, exports or release tooling | Installed-distribution and external-consumer checks in [RELEASING.md](RELEASING.md) |
| Documentation only | Check links, command paths and whitespace; execute changed local examples where practical |

SDK changes and affected recipes belong in the same PR. When a change affects
executable instructions maintained in [OpenEight-Labs/skills](https://github.com/OpenEight-Labs/skills),
identify the affected instructions and coordinate with the maintainer; link a
corresponding skills PR if one is opened. Unrelated changes need no skills PR.
Python and TypeScript releases remain independent; cookbook changes do not
publish SDK packages.

Record runtime and dependency versions, explain skipped checks, and distinguish
local tests from backend, live-provider and published-package acceptance.

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
