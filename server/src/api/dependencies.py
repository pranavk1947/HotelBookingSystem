"""Shared FastAPI dependencies, as ``Annotated`` aliases."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi import Request

from src.agent.agent_service import AgentService
from src.config.settings import Settings
from src.repository.config_repository import HotelConfigRepository


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_config_repo(request: Request) -> HotelConfigRepository:
    return request.app.state.config_repo


def get_agent_service(request: Request) -> AgentService:
    return request.app.state.agent_service


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
ConfigRepoDep = Annotated[HotelConfigRepository, Depends(get_config_repo)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
