# Native framework regression tests

These deterministic tests preserve upstream behavior that matters to the SDK.
They use fake models and temporary local storage; no provider credentials or
hosted backend are required. They are collected by `pytest sdk/python/tests`. Missing optional Deep Agents/SQLite
dependencies produce explicit module skips; POSIX-only recovery modules also skip
on other operating systems. Qualified runs must include all 51 cases without skips.

- `deepagents_hooks/` checks middleware ordering, child coverage, human-edited
  arguments, debug disclosure and scoped checkpoint behavior. Some cases prove
  unsafe upstream compositions; a passing counterexample is not a safe integration.
- `deepagents_recovery/` checks interruption, process death, sibling identity,
  generated-state replay and reconciliation across temporary SQLite checkpoints.
  Its helper programs run in separate processes. `http_contract.py` is a helper
  for separate HTTP integration qualification, not an independently collected test.

From the repository root, in the current managed integration environment:

```sh
python -m pytest sdk/python/tests/native -q
```

Recovery probes require POSIX and assert the exact framework tuple in
`test_environment_matches_native_candidate`. Native tests alone do not qualify
SDK registration, authorization, real-backend HTTP behavior or live providers.
The SDK-level tests remain under `sdk/python/tests/langchain/`.

Historical hook and recovery dependency snapshots are retained in private release
evidence. Run these tests with the current qualification environment and report
the actual dependency versions.
`deepagents_hooks/provenance.json` records the upstream source used by the original
hook probes. This is upstream provenance, not SDK release acceptance evidence.
