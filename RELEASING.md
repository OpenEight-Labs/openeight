# Releasing OpenEight SDKs

Python and TypeScript are independently versioned and released. Cookbook changes
alone do not publish SDKs. This procedure separates local candidate qualification
from authorized publication and acceptance of the published package.
Use the [SDK/cookbook qualification checklist](docs/sdk-cookbook-qualification.md)
to record cross-component execution and acceptance evidence.

## Release a cookbook update

Use a new `cookbook-v<version>` tag when distributing updated recipes. Run the
affected recipe tests against the published SDK version declared by the recipe.
After review and merge, tag the verified commit on main. Keep existing tags in
place so earlier walkthroughs remain reproducible.

For recipes distributed through the OpenEight skill, update its exporter after
the cookbook tag is available. Export into an empty directory and run the
recipe’s setup and offline checks before merging the skill update.

## Prepare the source

Follow the [versioning policy](docs/versioning.md) and use a reviewed release branch. Update the selected package version in
`sdk/python/pyproject.toml` and `sdk/python/openeight/__init__.py`, or `sdk/typescript/package.json` (and its lockfile).
Update `sdk/python/CHANGELOG.md` for Python; include TypeScript changes and migration
notes in its GitHub release description. Explain compatibility changes and required
backend behavior. Retain package licenses and notices.

TypeScript currently has `private: true`. Removing that flag is a separately
reviewed publication change: rebuild and requalify afterward. Do not edit a
qualified tarball or publish an earlier artifact with different metadata.

Record the final source commit and clean-tree status. An uncommitted working-tree
snapshot is useful for local tests, but is not a release revision. Intended tags
are `python-v<version>` and `typescript-v<version>`; never move a release tag.

## Build and qualify candidates

Use Python 3.12 and Node 24 for the current lane. Run commands below from the
repository root in an activated, isolated Python environment. Use a new output
directory for each candidate; retain it until the release evidence is archived.

```sh
ROOT="$PWD"
OUT="$(mktemp -d)"
python -m pip install 'build>=1,<2' twine
python scripts/check.py
```

For Python:

```sh
python -m build sdk/python --outdir "$OUT/python"
python -m twine check --strict "$OUT"/python/*
WHEEL="$(find "$OUT/python" -name '*.whl')"
python -m venv "$OUT/python-test"
"$OUT/python-test/bin/python" -m pip install 'pip==26.3'
"$OUT/python-test/bin/python" -m pip install   -c "$ROOT/sdk/python/constraints/qualified.txt"   "$WHEEL[dev,deepagents-durable,mcp,checkpoints-postgres]"
(cd "$OUT" && "$OUT/python-test/bin/python" -I -c   'import pathlib,sys,openeight; assert pathlib.Path(openeight.__file__).is_relative_to(pathlib.Path(sys.prefix))')
"$OUT/python-test/bin/python" scripts/assert_candidate.py "$WHEEL"
(cd "$OUT" && OPENEIGHT_COOKBOOK_ROOT="$ROOT/cookbook" \
  "$OUT/python-test/bin/python" -I -m pytest "$ROOT/sdk/python/tests" -q)
```

The import probe and suite use isolated Python from outside the checkout, so an
inherited `PYTHONPATH` cannot substitute source for the installed candidate.
Inspect wheel and source-distribution members for expected modules, assets,
metadata, licenses and unintended files. Test the sdist by building and installing
it in another clean environment. Use the framework/runtime matrix procedure in
[sdk/python/constraints](sdk/python/constraints/README.md), running its SDK scripts
from `sdk/`. Record support tuples actually exercised. PostgreSQL, backend and live-provider checks require their own inputs and evidence;
skips do not qualify those lanes. Run affected cookbook integration tests against
the exact wheel, following the candidate constraint and installed-origin checks
in [.github/workflows/cookbook.yml](.github/workflows/cookbook.yml).

For TypeScript:

```sh
mkdir -p "$OUT/typescript" "$OUT/node-test"
(cd sdk/typescript && npm ci && npm run typecheck && npm run build && npm test &&   npm pack --pack-destination "$OUT/typescript")
TARBALL="$(find "$OUT/typescript" -name '*.tgz')"
(cd "$OUT/node-test" && npm init -y && npm install "$TARBALL" &&   node --input-type=module -e   'import {MachineClient} from "@openeight/sdk"; if(typeof MachineClient !== "function") throw Error("missing export")')
```

Inspect the tarball inventory and verify emitted JavaScript, declarations and
exports. Typecheck external consumers with both Bundler and NodeNext resolution;
exercise supported client behavior from the installed tarball. The import smoke
above alone does not establish that coverage.

## Review and authorize publication

Retain one release record containing the source commit, package version, artifact
filenames and SHA256 hashes, build/runtime/dependency versions, distribution
inventory, exact test commands/results, skips, compatibility notes and maintainer
sign-off under the [review policy](MAINTAINERS.md#solo-maintainer-review). Record
whether review was performed by the author or an independent reviewer. Include
dependency/advisory results and any required backend, manual or provider acceptance.
Store sensitive operational evidence privately.

Verify actual repository/tag protections, publisher ownership and protected
environment approvals using [maintainer controls](sdk/MAINTAINERS.md).
CODEOWNERS and this document do not configure those controls. The Python release
workflow uses full-SHA Actions and PyPI Trusted Publishing; registry bindings and
protected-environment settings must be configured and verified before publication.
Untrusted PR jobs never receive publishing authority. TypeScript publication
remains manual and independent.

Publication, tags, source pushes and repository administration each require
explicit authorization. Promote only the reviewed artifact bytes. If any source,
metadata or artifact changes after review, rebuild and repeat qualification.

## Python release workflow

[Python SDK release](.github/workflows/python-sdk-release.yml) runs only through
`workflow_dispatch` on an existing `python-v<version>` tag. It rejects branches,
TypeScript tags and disagreement between the tag, project version and
`openeight.__version__`. Ordinary pushes, cookbook changes and TypeScript changes
never bump or publish a Python version. Version changes are reviewed source edits.

Before the first publication, a maintainer must configure:

- PyPI's `openeight` Trusted Publisher for owner `OpenEight-Labs`, repository
  `openeight`, workflow `python-sdk-release.yml`, environment `pypi`.
- The GitHub `pypi` environment with `@vasu-openeight` as the required reviewer,
  self-approval allowed during solo maintenance, administrator bypass disabled,
  and deployment tags restricted to `python-v*`.
- A tag ruleset protecting `python-v*` creation, updates and deletion, with release
  creation limited to repository administrators and no bypass for updates/deletion.

These settings are external prerequisites, not created by this workflow. See
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

Dispatch with `publish: false` for qualification without registry writes. For an
authorized release, dispatch the same immutable tag with `publish: true`, then
review that run's source SHA, test results and artifact hashes before approving its
`pypi` environment. Each run builds its own artifacts; approval covers that run's
bytes. Only the publish job receives `id-token: write`.

The workflow checks distribution contents, licenses and the typing marker; tests
fresh wheel and sdist installations; runs SDK qualification on Python 3.11–3.14;
and publishes the exact hashed distributions after qualification succeeds. It
then downloads both files from production PyPI, compares their hashes and smoke
tests the downloaded wheel. Tests requiring a live backend, PostgreSQL or an
opt-in outage soak may skip; these remain separate release acceptance evidence.

Regular CI selects Python, TypeScript and cookbook checks independently. Python
changes also qualify cookbook compatibility; shared contracts/tooling select both
SDKs. TypeScript-only changes do not build the Python SDK. Structural checks run
for every change. None of these checks publish packages.

## Publish and verify the registry artifact

After approval, the authorized maintainer publishes the recorded distributions
using the verified registry identity. For a reviewed manual release, the commands
are `python -m twine upload <reviewed-wheel> <reviewed-sdist>` or
`npm publish <reviewed-tarball> --access public`; these are external writes and
are not part of local verification. Tag the approved source commit and attach
release notes and artifact hashes. Do not rebuild silently during publication.

Download the exact version from production PyPI or npm into a clean directory.
For Python, use `python -m pip download --no-deps --only-binary=:all: --index-url
https://pypi.org/simple openeight==<version>` and retrieve the sdist separately.
For npm, use `npm pack @openeight/sdk@<version>
--registry=https://registry.npmjs.org`. Compare downloaded files with the approved
hashes and registry digests, then repeat installed-package acceptance without
local paths, editable installs or candidate constraints. Record the registry URLs
and download time. A successful upload or TestPyPI test is not production registry
acceptance. A registry 404 does not establish namespace ownership.

SDK-dependent guidance in OpenEight-Labs/skills stays on an unmerged candidate
branch until the corresponding published SDK passes qualification. A withheld tag
is insufficient: unpinned installs follow the default branch. The skills maintainer
must complete installer/update/immutable-install and assistant behavior acceptance,
verify the final merge tree matches the tested content, then tag that qualified
skill revision. This repository does not publish or mirror the skill source.

## Failed releases and recovery

If qualification fails before publication, stop promotion, correct the candidate
and rebuild under a new evidence record. If a published version is defective,
notify maintainers, document affected versions and the workaround, and prepare a
new patch version. Yank/deprecate a defective version only through a separately
authorized registry operation; never overwrite artifacts or move tags. Coordinate
security issues through [SECURITY.md](SECURITY.md). Preserve durable state and
review checkpoint compatibility before recommending a downgrade.
