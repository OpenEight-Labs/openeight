"""Explicit provider/model and OpenEight configuration; no credential fallbacks."""

import os

from langchain.chat_models import init_chat_model


def model_client():
    provider, separator, model = os.environ.get("AGENT_MODEL", "").partition(":")
    if not separator or not provider.strip() or not model.strip():
        raise ValueError("explicit_model_required")
    api_key = os.environ.get("AGENT_MODEL_API_KEY", "")
    if not api_key.strip():
        raise ValueError("model_api_key_required")
    return init_chat_model(model, model_provider=provider, api_key=api_key)


def configuration():
    from openeight import OpenEightConfig
    from openeight.langchain import ApprovalConfig

    return OpenEightConfig(
        endpoint=os.environ["OPENEIGHT_ENDPOINT"],
        credential=os.environ["OPENEIGHT_API_KEY"],
        agent_name=os.environ["OPENEIGHT_AGENT_NAME"],
        approval=ApprovalConfig(timeout=3600),
        on_block="raise",
    )
