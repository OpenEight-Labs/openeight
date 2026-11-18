# Agent protocol surface

`agent-api.json` lists the five Connect JSON operations supported by these
SDKs. It contains no complete backend API schema, Console route catalog or human
administration contract. Runtime request/response validation is owned by the SDK
source; this inventory does not generate code or confer authorization.

Protocol changes require SDK and backend review, contract tests, and explicit
compatibility qualification. Update the inventory and both implementations
together, then run `python scripts/check_public_surface.py` from the repository
root. The checker verifies operation paths and absence of excluded REST artifacts;
it is not a security scan or proof of semantic backend compatibility.

## Public error mappings

`errors.json` owns the supported agent-facing reasons, ErrorInfo domains, Connect
codes and HTTP statuses. It contains no server messages, metadata, wire payloads
or complete backend error inventory. Change it through reviewed SDK contract work.

Run `python scripts/generate_error_catalog.py` after changes. Commit both generated
language mappings and verify `--check` before release. Neither runtime loads this
JSON file. Tests construct synthetic ErrorInfo responses and retain independent
known-wire and malformed-input cases; generated mappings are not proof that a
backend emits the same protocol. Real-backend compatibility remains separately
verified by the backend's private integration tests.
