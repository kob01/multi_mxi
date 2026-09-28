"""Contract_Agent A2A server entrypoint.

Run:
    python -m app.agents.contract_agent.server     # http://0.0.0.0:9006
"""

from __future__ import annotations

import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore

from app.agents.contract_agent.agent_card import build_agent_card
from app.agents.contract_agent.executor import ContractAgentExecutor


def create_app():
    """Assemble the A2A Starlette application for Contract_Agent."""
    request_handler = DefaultRequestHandler(
        agent_executor=ContractAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )
    server = A2AStarletteApplication(
        agent_card=build_agent_card(),
        http_handler=request_handler,
    )
    return server.build()


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9006)
