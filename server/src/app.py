"""FastAPI application: two browser pages and the API behind them.

Boots without any LLM key on purpose, so the config page stays usable.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.agent.agent_service import AgentService
from src.agent.llm_client import AnthropicLLMClient
from src.agent.openai_client import OpenAILLMClient
from src.agent.prompt_builder import SystemPromptBuilder
from src.api.chat_router import router as chat_router
from src.api.config_router import router as config_router
from src.api.errors import register_error_handlers
from src.api.health_router import router as health_router
from src.config.settings import get_settings
from src.repository.config_repository import FileHotelConfigRepository

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
logger = logging.getLogger("hotel-agent")


def _build_llm(settings):
    """Pick the provider. Anthropic is the default; OpenAI is the swap-in."""
    if not settings.has_api_key:
        return None
    if settings.provider == "openai":
        if not settings.openai_api_key.strip():
            logger.warning("llm_provider is 'openai' but OPENAI_API_KEY is empty")
            return None
        return OpenAILLMClient(
            settings.openai_api_key, settings.openai_model, settings.openai_base_url
        )
    if not settings.anthropic_api_key.strip():
        logger.warning("llm_provider is 'anthropic' but ANTHROPIC_API_KEY is empty")
        return None
    return AnthropicLLMClient(settings.anthropic_api_key, settings.anthropic_model)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.config_repo = FileHotelConfigRepository(settings.configs_dir)

    app.state.agent_service = AgentService(
        _build_llm(settings), SystemPromptBuilder(), settings
    )

    port = os.environ.get("PORT", "8000")
    configs = [summary.id for summary in app.state.config_repo.list_summaries()]
    logger.info("Configured agents: %s", ", ".join(configs) or "(none)")
    if not settings.has_api_key:
        logger.warning(
            "No LLM key set — the config page works, chat will not. Add "
            "ANTHROPIC_API_KEY or OPENAI_API_KEY to .env and restart."
        )
    else:
        endpoint = settings.openai_base_url or "default endpoint"
        logger.info(
            "LLM provider: %s (model %s%s)",
            settings.provider,
            settings.active_model,
            f" via {endpoint}" if settings.provider == "openai" and settings.openai_base_url else "",
        )
    logger.info("Chat:   http://127.0.0.1:%s/", port)
    logger.info("Config: http://127.0.0.1:%s/config", port)

    yield


app = FastAPI(
    title="Hotel Group Proposal Agent",
    description="An agent that negotiates and assembles group proposals from a hotel config.",
    version="1.0.0",
    docs_url="/apidocs",
    redoc_url=None,
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(config_router)
app.include_router(chat_router)
register_error_handlers(app)

_settings = get_settings()
CLIENT_DIR = _settings.client_dir


@app.get("/", include_in_schema=False)
async def chat_page() -> FileResponse:
    return _page("index.html")


@app.get("/config", include_in_schema=False)
async def config_page() -> FileResponse:
    return _page("config.html")


def _page(filename: str) -> FileResponse:
    # No-store everywhere: an edited config must never be read from cache.
    return FileResponse(
        CLIENT_DIR / filename, headers={"Cache-Control": "no-store, max-age=0"}
    )


app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")


@app.middleware("http")
async def no_store(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/static", "/api")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

