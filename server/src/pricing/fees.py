"""Mandatory fees: one Strategy per ``basis``, selected from a registry."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import ClassVar

from src.model.hotel_config import FeeSpec
from src.pricing.context import QuoteContext
from src.pricing.money import Cents
from src.pricing.money import pct_of

FEE_REGISTRY: dict[str, type["Fee"]] = {}


def register_fee(basis: str):
    def decorate(cls: type["Fee"]) -> type["Fee"]:
        cls.basis = basis
        FEE_REGISTRY[basis] = cls
        return cls

    return decorate


class Fee(ABC):
    """Computes one charge added after discounts and before tax."""

    basis: ClassVar[str] = ""

    def __init__(self, spec: FeeSpec) -> None:
        self.spec = spec

    @abstractmethod
    def compute(self, ctx: QuoteContext) -> Cents:
        """Amount in cents; 0 means the fee does not appear on the quote."""

    def label(self, ctx: QuoteContext) -> str:
        return self.spec.name


@register_fee("per_room_night")
class PerRoomNightFee(Fee):
    """A flat amount per occupied room per night (resort/destination fee)."""

    def compute(self, ctx: QuoteContext) -> Cents:
        from src.pricing.money import to_cents

        return to_cents(self.spec.value) * ctx.room_nights()

    def label(self, ctx: QuoteContext) -> str:
        return f"{self.spec.name} ({ctx.room_nights()} room-nights)"


@register_fee("pct_of_category")
class PctOfCategoryFee(Fee):
    """A percentage of one category's post-discount total (service charge on F&B)."""

    def compute(self, ctx: QuoteContext) -> Cents:
        base = ctx.category_total(self.spec.category or "")
        return pct_of(base, self.spec.value)

    def label(self, ctx: QuoteContext) -> str:
        return f"{self.spec.name} — {self.spec.value:g}%"


@register_fee("pct_of_subtotal")
class PctOfSubtotalFee(Fee):
    """A percentage of the whole post-discount subtotal."""

    def compute(self, ctx: QuoteContext) -> Cents:
        return pct_of(ctx.net_subtotal, self.spec.value)

    def label(self, ctx: QuoteContext) -> str:
        return f"{self.spec.name} — {self.spec.value:g}%"


def build_fee(spec: FeeSpec) -> Fee:
    fee_cls = FEE_REGISTRY.get(spec.basis)
    if fee_cls is None:  # pragma: no cover - the model enum blocks this
        raise ValueError(f"no fee registered for basis '{spec.basis}'")
    return fee_cls(spec)


_SPEC_BASES = {"per_room_night", "pct_of_category", "pct_of_subtotal"}
assert set(FEE_REGISTRY) == _SPEC_BASES, (
    "fee registry does not match the FeeSpec 'basis' enum — "
    "update model/hotel_config.py and fees.py together"
)
