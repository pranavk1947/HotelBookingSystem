"""Central domain-exception -> HTTP error rendering.

Routes raise domain errors; the handler registered here renders the
``{error_code, message, errors[]}`` envelope. A new failure mode is one
``ERROR_SPECS`` entry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse

from src.repository.config_repository import ConfigNotFoundError
from src.repository.config_repository import ConfigValidationError
from src.repository.config_repository import DuplicateConfigError
from src.repository.config_repository import InvalidConfigIdError
from src.repository.config_repository import LastConfigError


@dataclass(frozen=True)
class ErrorSpec:
    """How one domain exception renders as an HTTP error response."""

    status_code: int
    error_code: str
    # None -> fall back to str(exc), for messages that embed request detail.
    message: str | None = None
    build_extra: Callable[[Any], dict[str, object]] | None = None


def _validation_extra(exc: ConfigValidationError) -> dict[str, object]:
    return {"errors": exc.errors}


ERROR_SPECS: dict[type[Exception], ErrorSpec] = {
    ConfigNotFoundError: ErrorSpec(
        status.HTTP_404_NOT_FOUND, "CONFIG_NOT_FOUND"
    ),
    DuplicateConfigError: ErrorSpec(
        status.HTTP_409_CONFLICT, "DUPLICATE_CONFIG_ID"
    ),
    InvalidConfigIdError: ErrorSpec(
        status.HTTP_400_BAD_REQUEST, "INVALID_CONFIG_ID"
    ),
    LastConfigError: ErrorSpec(
        status.HTTP_409_CONFLICT,
        "LAST_CONFIG",
        "At least one config must remain so the chat page has an agent to talk to.",
    ),
    ConfigValidationError: ErrorSpec(
        status.HTTP_400_BAD_REQUEST,
        "CONFIG_INVALID",
        "This config did not validate.",
        build_extra=_validation_extra,
    ),
}


def _render(exc: Exception, spec: ErrorSpec) -> JSONResponse:
    content: dict[str, object] = {
        "error_code": spec.error_code,
        "message": spec.message or str(exc),
    }
    if spec.build_extra is not None:
        content.update(spec.build_extra(exc))
    return JSONResponse(status_code=spec.status_code, content=content)


def register_error_handlers(app: FastAPI) -> None:
    async def _handle(_request: Request, exc: Exception) -> JSONResponse:
        return _render(exc, ERROR_SPECS[type(exc)])

    for exc_type in ERROR_SPECS:
        app.add_exception_handler(exc_type, _handle)
