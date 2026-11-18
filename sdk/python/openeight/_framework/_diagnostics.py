"""Reject native model-resolution diagnostics that can expose configuration."""

import logging

from openeight.errors import ConfigurationError


def safe_resolution_diagnostics():
    # Qualified native resolvers log model specs and application exceptions here.
    # Never change application logger levels or filters to hide those records.
    if any(
        logging.getLogger(name).isEnabledFor(logging.INFO)
        for name in (
            "deepagents._models",
            "deepagents.profiles.harness.harness_profiles",
            "deepagents.profiles.provider.provider_profiles",
        )
    ):
        raise ConfigurationError("unsafe_framework_diagnostics")
