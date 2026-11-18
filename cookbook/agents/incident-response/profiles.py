"""Explicit role surfaces and concise catalog descriptions."""

from openeight.deepagents import CatalogDescription, ToolSurface

DEFAULT = {
    (): ("read_file", "write_file", "write_todos", "task"),
    ("diagnostics",): ("read_logs", "read_metrics", "read_deployments"),
    ("remediation",): (
        "read_deployments",
        "restart_replica",
        "rollback_release",
        "set_replica_count",
    ),
    ("reporting",): (),
}
CHILD_DESCRIPTIONS = {
    "diagnostics": "Read simulated checkout logs, metrics and deployment state.",
    "remediation": "Read deployment state; restart replicas, roll back releases or change replica count in the simulated checkout service.",
    "reporting": "Return an incident report from supplied facts and effect receipts; no tools.",
}
DESCRIPTIONS = {
    "read_file": "Read a file from this run's virtual filesystem.",
    "write_file": "Create or replace file contents in this run's virtual filesystem; mutates in-memory file state.",
    "write_todos": "Replace this run's in-memory todo list with the supplied tasks and statuses.",
    "task": "Run a specialist subagent; delegated tools can mutate simulated service state and require separate authorization.",
    "read_logs": "Read simulated checkout logs and the current service revision.",
    "read_metrics": "Read simulated checkout error metrics and the current service revision.",
    "read_deployments": "Read the simulated checkout release, replica count and current revision.",
    "restart_replica": "Restart one simulated checkout replica at the expected revision.",
    "rollback_release": "Change simulated checkout to a known release at the expected revision.",
    "set_replica_count": "Set simulated checkout to 1–10 replicas at the expected revision.",
}


def configuration(profile="default"):
    if profile != "default":
        raise ValueError("unsupported_profile")
    return dict(
        tool_surfaces=tuple(
            ToolSurface(path, names) for path, names in DEFAULT.items()
        ),
        catalog_naming="qualified-v1",
        catalog_descriptions=tuple(
            CatalogDescription(path, name, DESCRIPTIONS[name])
            for path, names in DEFAULT.items()
            for name in names
        ),
        include_general_purpose=False,
    )
