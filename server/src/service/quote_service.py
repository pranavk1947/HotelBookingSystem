"""Thin seam so the tool, the tests and any future endpoint price through one path."""

from __future__ import annotations

from datetime import date

from src.model.hotel_config import HotelConfig
from src.model.quote import Quote
from src.model.quote import QuoteRequest
from src.pricing.calculator import QuoteCalculator


class QuoteService:
    def build(
        self, config: HotelConfig, request: QuoteRequest, today: date | None = None
    ) -> Quote:
        return QuoteCalculator(config, today=today).build(request)
