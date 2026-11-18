"""Deep Agents entry point: python deepagents_agent.py publish or run ORD-1041."""

from deepagents.backends import StateBackend
from openeight.deepagents import create_deep_agent

from refund_assistant.cli import run_cli
from refund_assistant.prompts import SYSTEM_PROMPT
from refund_assistant.tools import build_tools


def build(model, request, config):
    return create_deep_agent(
        model=model,
        tools=build_tools(request),
        system_prompt=SYSTEM_PROMPT,
        backend=StateBackend(),
        include_general_purpose=False,
        openeight=config,
    )


if __name__ == "__main__":
    raise SystemExit(run_cli(build))
