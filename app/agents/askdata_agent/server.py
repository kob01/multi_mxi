"""AskData_Agent A2A server entrypoint.

Run:
    python -m app.agents.askdata_agent.server   # http://0.0.0.0:9007
"""

from __future__ import annotations

import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore

from app.agents.askdata_agent.agent_card import build_agent_card
from app.agents.askdata_agent.executor import AskDataAgentExecutor


def create_app():
    """组装 AskData_Agent 的 A2A Starlette 应用。"""
    request_handler = DefaultRequestHandler(
        agent_executor=AskDataAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )
    server = A2AStarletteApplication(
        agent_card=build_agent_card(),
        http_handler=request_handler,
    )
    return server.build()


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9007)
