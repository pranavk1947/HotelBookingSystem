"""Chat API shapes. The server is stateless: the client posts the whole history."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic import Field

from src.model.quote import Quote


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)


class ChatRequest(BaseModel):
    config_id: str
    messages: list[ChatMessage] = Field(min_length=1, max_length=200)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    rounds: int = 0


class ChatResponse(BaseModel):
    reply: str
    quote: Quote | None = None
    usage: Usage = Field(default_factory=Usage)
    # Set on a handled failure so the UI styles it as a problem, not agent output.
    error: str | None = None
