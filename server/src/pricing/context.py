"""Mutable state threaded through the pricing pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import date

from src.model.hotel_config import DURATION_SCALED_UNITS
from src.model.hotel_config import HotelConfig
from src.model.hotel_config import InventoryItem
from src.model.quote import QuoteRequest
from src.pricing.money import Cents


@dataclass
class ResolvedLine:
    """One priced line, before any discount."""

    item: InventoryItem
    qty: int
    unit_price: Cents
    duration: int
    subtotal: Cents
    note: str = ""


@dataclass
class AppliedAdjustment:
    """A labelled discount plus the categories it reduced, so scoped fees can
    bill the post-discount total."""

    label: str
    amount: Cents  # negative
    categories: frozenset[str]


@dataclass
class QuoteContext:
    """Everything the pipeline stages read and write."""

    config: HotelConfig
    request: QuoteRequest
    today: date
    arrival: date
    lines: list[ResolvedLine] = field(default_factory=list)
    adjustments: list[AppliedAdjustment] = field(default_factory=list)
    fees: list[tuple[str, Cents]] = field(default_factory=list)
    violations: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    subtotal: Cents = 0
    net_subtotal: Cents = 0
    tax: Cents = 0
    total: Cents = 0

    @property
    def has_hard_violations(self) -> bool:
        return bool(self.violations)

    @property
    def nights(self) -> int:
        return self.request.nights

    def lines_in_scope(
        self, category: str | None, item_id: str | None
    ) -> list[ResolvedLine]:
        """The lines a rule's scope selects."""
        if item_id is not None:
            return [line for line in self.lines if line.item.id == item_id]
        return [line for line in self.lines if line.item.category == category]

    def category_total(self, category: str) -> Cents:
        """Post-discount total for one category — the base for a scoped fee."""
        gross = sum(
            line.subtotal for line in self.lines if line.item.category == category
        )
        discounts = sum(
            adjustment.amount
            for adjustment in self.adjustments
            if category in adjustment.categories
        )
        return max(gross + discounts, 0)

    def room_nights(self) -> int:
        return sum(
            line.qty * self.nights for line in self.lines if line.item.category == "room"
        )

    @staticmethod
    def duration_for(item: InventoryItem, nights: int) -> int:
        """Billed duration, derived from the unit — never taken from the LLM."""
        return nights if item.unit in DURATION_SCALED_UNITS else 1
