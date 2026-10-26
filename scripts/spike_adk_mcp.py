"""Spike 2: an ADK agent calling PayPal's own MCP tools (Agent Toolkit) in the sandbox.

    uv run python -m scripts.spike_adk_mcp
    uv run python -m scripts.spike_adk_mcp "Which invoices are still unpaid, and for how much?"

Read-only on purpose (invoices.list + invoices.get): it proves the wiring
without sending anything. Needs Node on PATH; see SI_PAYPAL_MCP_COMMAND.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import uuid

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

from app.paypal.client import PayPalClient
from app.settings import get_settings

QUESTION = "List my invoices that are not paid yet. Who owes me what, and which are overdue?"


async def main(question: str) -> None:
    s = get_settings()
    s.apply_google_env()
    pp = PayPalClient(s.paypal_client_id, s.paypal_client_secret, s.paypal_base_url)
    token = await pp.access_token()
    await pp.aclose()

    toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=shlex.split(s.paypal_mcp_command)[0],
                args=[*shlex.split(s.paypal_mcp_command)[1:], "--tools=invoices.list,invoices.get"],
                env={
                    "PAYPAL_ACCESS_TOKEN": token,
                    "PAYPAL_ENVIRONMENT": "PRODUCTION" if s.paypal_env == "live" else "SANDBOX",
                    "PATH": os.environ.get("PATH", ""),
                },
            ),
            timeout=60,
        )
    )
    agent = LlmAgent(
        name="chase_spike",
        model=s.gemini_model,
        instruction=(
            "You help a tradesperson see who owes them money. Use the PayPal tools to "
            "look up invoices. Answer in two or three plain sentences with amounts."
        ),
        tools=[toolset],
    )
    sessions = InMemorySessionService()
    runner = Runner(agent=agent, app_name="sayinvoice", session_service=sessions)
    session_id = str(uuid.uuid4())
    await sessions.create_session(app_name="sayinvoice", user_id="spike", session_id=session_id)

    try:
        message = types.Content(role="user", parts=[types.Part(text=question)])
        async for event in runner.run_async(
            user_id="spike", session_id=session_id, new_message=message
        ):
            for call in event.get_function_calls():
                print(f"-> tool call: {call.name}({dict(call.args or {})})")
            for result in event.get_function_responses():
                print(f"<- tool result: {result.name} ({len(str(result.response))} chars)")
            if event.is_final_response() and event.content and event.content.parts:
                print("\n" + "".join(p.text or "" for p in event.content.parts).strip())
    finally:
        await toolset.close()


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or QUESTION))
