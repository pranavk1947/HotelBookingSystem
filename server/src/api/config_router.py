"""Config CRUD for the config page."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Response
from fastapi import status

from src.api.dependencies import ConfigRepoDep
from src.model.hotel_config import ConfigSummary
from src.repository.config_repository import ConfigValidationError
from src.repository.config_repository import parse_config

router = APIRouter(prefix="/api/configs", tags=["configs"])


@router.get("")
async def list_configs(repo: ConfigRepoDep) -> list[ConfigSummary]:
    return repo.list_summaries()


@router.post("/validate")
async def validate_config(payload: dict) -> dict:
    """Dry run. Always HTTP 200: an invalid draft is a normal editing state."""
    try:
        config = parse_config(payload)
    except ConfigValidationError as exc:
        return {"valid": False, "errors": exc.errors}
    return {
        "valid": True,
        "errors": [],
        "summary": {
            "id": config.id,
            "name": config.name,
            "inventory_count": len(config.inventory),
            "rule_count": len(config.rules),
            "fee_count": len(config.fees),
        },
    }


@router.get("/{config_id}")
async def get_config(config_id: str, repo: ConfigRepoDep) -> dict:
    """Return the file as stored, so a config that fails validation still opens."""
    return repo.raw(config_id)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_config(payload: dict, repo: ConfigRepoDep) -> dict:
    config = parse_config(payload)
    repo.create(config)
    return config.model_dump(mode="json")


@router.put("/{config_id}")
async def update_config(config_id: str, payload: dict, repo: ConfigRepoDep) -> dict:
    config = parse_config(payload)
    repo.update(config_id, config)
    return config.model_dump(mode="json")


@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_config(config_id: str, repo: ConfigRepoDep) -> Response:
    repo.delete(config_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
