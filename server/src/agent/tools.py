"""Tools the agent may call. There is exactly one: ``build_quote`` is the only
path to a number. A second tool is one class plus one registry entry."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from typing import Protocol

from pydantic import ValidationError

from src.model.hotel_config import HotelConfig
from src.model.quote import Quote
from src.model.quote import QuoteRequest
from src.service.quote_service import QuoteService

logger = logging.getLogger(__name__)


def _violation(code: str, message: str) -> dict:
    """A tool result the model can read and correct from."""
    return {"violations": [{"code": code, "message": message}], "warnings": []}


class Tool(Protocol):
    name: str

    def schema(self) -> dict: ...
    def execute(self, payload: dict) -> dict: ...


class BuildQuoteTool:
    """Prices a selection against the config. The engine decides, not the model."""

    name = "build_quote"

    def __init__(self, config: HotelConfig, today: date | None = None) -> None:
        self.config = config
        self.today = today or date.today()
        self.service = QuoteService()
        self.last_quote: Quote | None = None

    def schema(self) -> dict:
        item_ids = ", ".join(item.id for item in self.config.inventory)
        return {
            "name": self.name,
            "description": (
                "Price a proposal against this hotel's live inventory and pricing "
                "rules. This is the ONLY way to obtain a price: never state, "
                "estimate, add or discount a number yourself. Returns priced line "
                "items, discounts, fees, tax, totals and a ready-made markdown "
                "table, or a list of violations to correct and re-call with."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "arrival_date": {
                        "type": "string",
                        "description": "Arrival date, YYYY-MM-DD. Resolve relative dates against today's date given in your instructions.",
                    },
                    "nights": {
                        "type": "integer",
                        "description": "Number of nights. For a 3-day conference this is normally 3 (or 2 if they arrive the morning of day 1).",
                    },
                    "attendees": {
                        "type": "integer",
                        "description": "Total head count. Drives per-person pricing and capacity checks.",
                    },
                    "selections": {
                        "type": "array",
                        "description": "Everything to put on the quote.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {
                                    "type": "string",
                                    "description": f"An id from this hotel's catalog. Valid ids: {item_ids}",
                                },
                                "qty": {
                                    "type": "integer",
                                    "description": "How many units. For per-person items pass the head count.",
                                },
                            },
                            "required": ["item_id", "qty"],
                        },
                    },
                },
                "required": ["arrival_date", "nights", "attendees", "selections"],
            },
        }

    def execute(self, payload: dict) -> dict:
        try:
            request = QuoteRequest.model_validate(payload)
        except ValidationError as exc:
            return _violation(
                "bad_tool_input",
                "; ".join(
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors()
                ),
            )
        try:
            quote = self.service.build(self.config, request, today=self.today)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
            logger.exception("Pricing failed for %s", self.config.id)
            return _violation("pricing_failed", str(exc))
        if quote.priced:
            self.last_quote = quote
        return quote.model_dump(mode="json")


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def anthropic_tools(self) -> list[dict]:
        return [tool.schema() for tool in self._tools.values()]

    def dispatch(self, name: str, payload: dict) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return {"violations": [{"code": "unknown_tool", "message": f"No tool '{name}'."}]}
        return tool.execute(payload)

    def get(self, name: str) -> Any:
        return self._tools.get(name)
