"""Server-owned and unshipped selectors forbidden in machine telemetry."""

FORBIDDEN_TELEMETRY_SELECTORS = frozenset(
    {
        "organization_id",
        "workspace_id",
        "agent_id",
        "api_key_id",
        "credential_id",
        "policy_id",
        "policy_version_id",
        "tool_version",
    }
)
