"""Pricing rules: Strategy objects behind a registry.

A new discount type is one class here plus one spec in ``model/hotel_config.py``.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import ClassVar

from src.model.hotel_config import CompItemSpec
from src.model.hotel_config import LosDiscountSpec
from src.model.hotel_config import VolumeDiscountSpec
from src.pricing.context import AppliedAdjustment
from src.pricing.context import QuoteContext
from src.pricing.money import Cents
from src.pricing.money import pct_of

RULE_REGISTRY: dict[str, type["PricingRule"]] = {}


def register_rule(type_name: str):
    """Class decorator: make a rule constructible from its config ``type``."""

    def decorate(cls: type["PricingRule"]) -> type["PricingRule"]:
        cls.type = type_name
        RULE_REGISTRY[type_name] = cls
        return cls

    return decorate


class PricingRule(ABC):
    """One discount strategy, built from its config spec."""

    type: ClassVar[str] = ""

    def __init__(self, spec) -> None:
        self.spec = spec

    @abstractmethod
    def applies(self, ctx: QuoteContext) -> bool: ...

    @abstractmethod
    def apply(self, ctx: QuoteContext) -> AppliedAdjustment | None:
        """Produce the labelled adjustment. Called only when ``applies``."""


class _ScopedDiscountRule(PricingRule):
    """Shared machinery for rules that take a percentage off a scope."""

    def _scope_lines(self, ctx: QuoteContext):
        return ctx.lines_in_scope(self.spec.scope.category, self.spec.scope.item_id)

    def _scope_base(self, ctx: QuoteContext) -> Cents:
        """Scope lines less earlier discounts, so rules stack multiplicatively in
        config order and can never sum past 100%."""
        lines = self._scope_lines(ctx)
        gross = sum(line.subtotal for line in lines)
        touched = {line.item.category for line in lines}
        earlier = sum(
            adjustment.amount
            for adjustment in ctx.adjustments
            if adjustment.categories & touched
        )
        return max(gross + earlier, 0)

    def _categories(self, ctx: QuoteContext) -> frozenset[str]:
        return frozenset(line.item.category for line in self._scope_lines(ctx))

    def _booked_units(self, ctx: QuoteContext) -> int:
        """Units booked in scope. Thresholds always use booked qty, never billable."""
        return sum(line.qty for line in self._scope_lines(ctx))


@register_rule("volume_discount")
class VolumeDiscountRule(_ScopedDiscountRule):
    """Take X% off the scope once at least N units are booked."""

    spec: VolumeDiscountSpec

    def applies(self, ctx: QuoteContext) -> bool:
        return self._booked_units(ctx) >= self.spec.min_qty

    def apply(self, ctx: QuoteContext) -> AppliedAdjustment | None:
        base = self._scope_base(ctx)
        if base <= 0:
            return None
        amount = -pct_of(base, self.spec.discount_pct)
        if amount == 0:
            return None
        return AppliedAdjustment(self.spec.label, amount, self._categories(ctx))


@register_rule("los_discount")
class LengthOfStayRule(_ScopedDiscountRule):
    """Take X% off the scope once the stay reaches N nights."""

    spec: LosDiscountSpec

    def applies(self, ctx: QuoteContext) -> bool:
        return ctx.nights >= self.spec.min_nights and bool(self._scope_lines(ctx))

    def apply(self, ctx: QuoteContext) -> AppliedAdjustment | None:
        base = self._scope_base(ctx)
        if base <= 0:
            return None
        amount = -pct_of(base, self.spec.discount_pct)
        if amount == 0:
            return None
        return AppliedAdjustment(self.spec.label, amount, self._categories(ctx))


@register_rule("comp_item")
class CompItemRule(PricingRule):
    """One unit free per N booked, credited at the resolved rate for the full stay."""

    spec: CompItemSpec

    def _line(self, ctx: QuoteContext):
        for line in ctx.lines:
            if line.item.id == self.spec.item_id:
                return line
        return None

    def _comp_count(self, ctx: QuoteContext) -> int:
        line = self._line(ctx)
        if line is None:
            return 0
        count = line.qty // self.spec.per_qty
        if self.spec.max_comps is not None:
            count = min(count, self.spec.max_comps)
        return count

    def applies(self, ctx: QuoteContext) -> bool:
        return self._comp_count(ctx) > 0

    def apply(self, ctx: QuoteContext) -> AppliedAdjustment | None:
        line = self._line(ctx)
        if line is None:
            return None
        count = self._comp_count(ctx)
        amount = -(line.unit_price * line.duration * count)
        if amount == 0:
            return None
        return AppliedAdjustment(
            self.spec.label, amount, frozenset({line.item.category})
        )


def build_rule(spec) -> PricingRule:
    """Instantiate the strategy a config spec names."""
    rule_cls = RULE_REGISTRY.get(spec.type)
    if rule_cls is None:  # pragma: no cover - blocked by the assertion below
        raise ValueError(f"no pricing rule registered for type '{spec.type}'")
    return rule_cls(spec)


# Keep the registry and the Pydantic discriminated union from drifting: adding a
# spec without a strategy (or vice versa) fails at import, not mid-conversation.
_SPEC_TYPES = {"volume_discount", "los_discount", "comp_item"}
assert set(RULE_REGISTRY) == _SPEC_TYPES, (
    f"rule registry {sorted(RULE_REGISTRY)} does not match RuleSpec union "
    f"{sorted(_SPEC_TYPES)} — update model/hotel_config.py and rules.py together"
)
