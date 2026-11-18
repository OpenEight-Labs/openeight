"""LangChain entry point: python langchain_agent.py publish or run ORD-1041."""

from openeight.langchain import create_agent

from refund_assistant.cli import run_cli
from refund_assistant.prompts import SYSTEM_PROMPT
from refund_assistant.tools import build_tools


def build(model, request, config):
    return create_agent(
        model=model,
        tools=build_tools(request),
        system_prompt=SYSTEM_PROMPT,
        openeight=config,
    )


if __name__ == "__main__":
    raise SystemExit(run_cli(build))
