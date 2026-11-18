"""Local binding of an existing native Agent."""

from openeight import ConfigurationError, DeliveryConfig

from openeight.langchain._owner import InstrumentedAgent


def instrument(agent, *, agent_name, endpoint, credential, delivery=None):
    if isinstance(agent, InstrumentedAgent):
        if agent.control is not None or agent.binding != (
            agent_name,
            endpoint,
            credential,
            delivery or DeliveryConfig(),
        ):
            raise ConfigurationError("invalid_integration_configuration")
        agent._ensure_open()
        return agent
    return InstrumentedAgent(
        agent,
        agent_name=agent_name,
        integration="langchain",
        endpoint=endpoint,
        credential=credential,
        delivery=delivery,
    )
