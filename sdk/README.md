> Component guide: run commands below from `sdk/`. Repository policies also apply from the root.

# OpenEight SDKs

Official Python and TypeScript SDKs for integrating applications with OpenEight.
Both initial package versions are 0.0.1.

| SDK | Package | Source |
| --- | --- | --- |
| Python | `openeight` | [Python README](python/README.md) |
| TypeScript | `@openeight/sdk` | [TypeScript README](typescript/README.md) |

For application use, install from PyPI or npm using the package installation guides above.

## Contributing and maintenance

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Maintainers
must verify the protections in [MAINTAINERS.md](MAINTAINERS.md) before publication.
Never include credentials, customer data or private operational material in public
issues, commits, workflow logs or artifacts.

## Development

Use standard CPython 3.11–3.14 for managed integrations and Node 24 or newer.
Python package metadata also permits core use on standard CPython 3.15; this does
not qualify the newer managed features on that version.

```sh
python -m pip install -e 'python[dev,deepagents-durable,mcp,checkpoints-postgres]'
python -m pytest python/tests -q
python scripts/check_framework_registry.py
cd typescript
npm ci
npm run build
npm test
```

The current pinned Python integration environment is in
`python/constraints/qualified.txt`; use it for reproducible qualification.
See [test environments](python/constraints/README.md) for the current qualification
lane and [native regression tests](python/tests/native/README.md) for framework
hook and subprocess recovery checks.
The shared public error catalog is `contracts/errors.json`. Both SDKs expose agent operations
only: registration, readiness, Decisions, approval-state polling and telemetry.
`contracts/agent-api.json` inventories those five Connect operations. The full
backend contract and Console/admin clients stay private. No OpenAPI generator or
private backend checkout is required for normal SDK builds and tests. Verify the
publication boundary with `python scripts/check_public_surface.py`. Regenerate error
mappings with `python scripts/generate_error_catalog.py` and verify drift with
`python scripts/generate_error_catalog.py --check`. Generated mappings are packaged
with each SDK; customers do not need the catalog file at runtime.

## Backend qualification

Standalone tests use deterministic external-provider fixtures. Cookbook owns runnable
example applications and synthetic business data. To qualify the SDK against those
applications, set `OPENEIGHT_COOKBOOK_ROOT` to a reviewed cookbook checkout. Without
it, `test_langchain_deployment.py` and the two public checkpoint-example tests are
explicitly skipped. No private backend checkout or copied example is required.
Pin the cookbook commit and SDK revision together for repeatable qualification.
Public durable-state tests use self-contained fixtures to verify rejection of
incompatible state without rewriting it. Real-backend HTTP integration and
live-provider acceptance remain separate release gates; standalone tests do not
replace them.

## Releases

Python and TypeScript are independently versioned, with release tags
`python-v<version>` and `typescript-v<version>`. Follow the repository
[release procedure](../RELEASING.md) for qualification and publication.
Run typecheck, build, tests, pack and external installed-tarball tests before publication.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party
dependencies retain their own licenses. Hosted-service access is governed by
separate service terms.

## Version compatibility

Initial `0.0.x` releases are experimental and may introduce breaking changes.
Pin qualified versions and read the [versioning policy](../docs/versioning.md) before upgrading.
