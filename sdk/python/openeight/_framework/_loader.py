"""Fixed implementation lookup; applications cannot supply import paths."""

from openeight.errors import ConfigurationError

from ._registry import CompatibilitySelection
from ._types import FrameworkCompatibility


def load_compatibility(selection: CompatibilitySelection) -> FrameworkCompatibility:
    if selection.implementation_revision not in (1, 2):
        raise ConfigurationError("unsupported_framework_version")
    if selection.implementation_id == "langchain-v1":
        from openeight.langchain._frameworks.v1 import LangChainV1

        return LangChainV1()
    if selection.implementation_id == "deepagents-v1":
        from openeight.deepagents._frameworks.v1 import DeepAgentsV1

        return DeepAgentsV1()
    raise ConfigurationError("unsupported_framework_version")
