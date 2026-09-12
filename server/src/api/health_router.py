from __future__ import annotations

from fastapi import APIRouter

from src.api.dependencies import ConfigRepoDep
from src.api.dependencies import SettingsDep

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(settings: SettingsDep, repo: ConfigRepoDep) -> dict:
    """Liveness, plus the two things that actually stop the app being usable."""
    return {
        "status": "healthy",
        "api_key_configured": settings.has_api_key,
        "provider": settings.provider,
        "model": settings.active_model,
        "configs": [summary.id for summary in repo.list_summaries()],
    }
