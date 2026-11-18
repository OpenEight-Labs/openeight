# Python qualification environment

`qualified.txt` reproduces the current installed-wheel managed integration lane.
`managed-integrations.json` selects its CPython 3.11–3.14 environments and support
IDs. These are test inputs, excluded from wheels and source distributions.
Customer dependencies are declared in `../pyproject.toml`.

The authoritative runtime support declaration is
`../openeight/_framework/frameworks.json`. It retains all supported version tuples,
feature combinations, adapter revisions and checkpoint formats. Passing the
current lane does not repeat historical qualification or establish Python 3.15
managed integration coverage.

From the repository root:

```sh
python sdk/scripts/verify_openeight_matrix.py --wheel /path/to/openeight.whl \
  --environment-root /path/to/test-environments --output /path/to/results.json
```

Historical locks and qualification case mappings are retained in private release
evidence. They are not customer installation requirements.
