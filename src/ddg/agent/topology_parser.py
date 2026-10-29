"""Use Foundry to translate free-form graph descriptions into validated Markdown."""

import asyncio

from pydantic import BaseModel

from .._prompts import load as load_prompt
from ..config import Settings
from ..control import RunControl


class TopologyDocument(BaseModel):
    """Canonical Markdown returned by the topology extraction model."""

    markdown: str


INSTRUCTIONS = load_prompt("topology_parser.txt")


def parse_with_foundry(text: str, settings: Settings, control: RunControl) -> str:
    """Extract canonical Markdown, charging model usage to the caller's run budget."""
    return asyncio.run(_parse(text, settings, control))


async def _parse(text: str, settings: Settings, control: RunControl) -> str:
    from agent_framework import Agent
    from agent_framework.foundry import FoundryChatClient
    from azure.identity.aio import DefaultAzureCredential

    async with control.model_call("topology_parsing"):
        async with DefaultAzureCredential() as credential:
            client = FoundryChatClient(
                project_endpoint=settings.model.project_endpoint,
                model=settings.model.model,
                credential=credential,
            )
            agent = Agent(client, INSTRUCTIONS, name="topology-parser")
            response = await agent.run(
                "Extract the graph from this source document:\n" + text,
                options={"response_format": TopologyDocument},
                client_kwargs={"timeout": max(1.0, control.remaining_seconds())},
            )
        usage = response.usage_details or {}
        control.record_tokens(usage.get("input_token_count") or 0, usage.get("output_token_count") or 0)
        if not isinstance(response.value, TopologyDocument):
            raise ValueError("Foundry topology parsing did not return the required Markdown document")
        return response.value.markdown
