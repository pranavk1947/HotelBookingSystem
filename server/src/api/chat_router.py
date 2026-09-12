"""The conversation endpoint.

Sync on purpose: the SDK call blocks, so FastAPI runs it in a threadpool and the
config page stays responsive. Stateless: the client posts the whole history and
the config is re-read from disk every turn.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.api.dependencies import AgentServiceDep
from src.api.dependencies import ConfigRepoDep
from src.model.chat import ChatRequest
from src.model.chat import ChatResponse

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat")
def chat(
    payload: ChatRequest, repo: ConfigRepoDep, agent: AgentServiceDep
) -> ChatResponse:
    config = repo.get(payload.config_id)
    return agent.run_turn(config, payload.messages)
