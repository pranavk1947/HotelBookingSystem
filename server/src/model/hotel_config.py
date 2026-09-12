"""The hotel config schema: one JSON file per hotel, one hotel per agent.

Everything that makes two agents behave differently — catalog, rates, rules,
fees, constraints, upsell pitches, persona — is declared and validated here.
"""

from __future__ import annotations

from typing import Annotated
from typing import Literal
from typing import Union

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

# A config id is used as a filename, so it is a strict slug and re-checked
# before any path join (see FileHotelConfigRepository).
CONFIG_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,48}$"
# Item and fee ids are only referenced inside a config, so a single character
# is fine — no reason to reject an item called `av`.
ITEM_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,48}$"

Category = Literal["room", "meeting_space", "fnb", "av", "service"]

# How an item's price scales. The engine derives duration from this — the LLM
# does not get to decide whether something is billed per night.
#   per_night / per_day / per_person_per_day -> multiplied by nights
#   per_person / flat                        -> charged once
Unit = Literal["per_night", "per_day", "per_person", "per_person_per_day", "flat"]

DURATION_SCALED_UNITS: frozenset[str] = frozenset(
    {"per_night", "per_day", "per_person_per_day"}
)
PER_PERSON_UNITS: frozenset[str] = frozenset({"per_person", "per_person_per_day"})


class StrictModel(BaseModel):
    """Reject unknown keys so a typo in the config page is an error, not a no-op."""

    model_config = ConfigDict(extra="forbid")


class SeasonalRate(StrictModel):
    """An MM-DD window overriding an item's unit price. Year-less, and wraps
    (``12-20`` -> ``01-05``)."""

    label: str = Field(min_length=1, max_length=80)
    start: str = Field(pattern=r"^\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{2}-\d{2}$")
    unit_price: float = Field(ge=0)


class InventoryItem(StrictModel):
    """One sellable line: a room type, a meeting space, a menu, a package."""

    id: str = Field(pattern=ITEM_ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    category: Category
    unit: Unit
    unit_price: float = Field(ge=0)
    available_qty: int = Field(ge=0)
    # Seated/sleeping capacity of ONE unit. Rooms: heads per room. Meeting
    # space: heads in the room. None == not capacity-constrained.
    capacity: int | None = Field(default=None, ge=1)
    min_qty: int | None = Field(default=None, ge=0)
    max_qty: int | None = Field(default=None, ge=1)
    description: str = ""
    # Bullet list shown to the agent, e.g. what a meeting package includes.
    includes_note: list[str] = Field(default_factory=list)
    seasonal_rates: list[SeasonalRate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_qty_bounds(self) -> InventoryItem:
        if self.min_qty is not None and self.max_qty is not None:
            if self.min_qty > self.max_qty:
                raise ValueError("min_qty cannot exceed max_qty")
        return self


class RuleScope(StrictModel):
    """What a rule's discount applies to. Exactly one of category / item_id."""

    category: Category | None = None
    item_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> RuleScope:
        if (self.category is None) == (self.item_id is None):
            raise ValueError("scope needs exactly one of 'category' or 'item_id'")
        return self


class VolumeDiscountSpec(StrictModel):
    """Group-size discount: book at least N units of the scope, take X% off."""

    type: Literal["volume_discount"]
    label: str = Field(min_length=1, max_length=120)
    scope: RuleScope
    min_qty: int = Field(ge=1)
    discount_pct: float = Field(gt=0, le=100)


class LosDiscountSpec(StrictModel):
    """Length-of-stay discount: stay at least N nights, take X% off the scope."""

    type: Literal["los_discount"]
    label: str = Field(min_length=1, max_length=120)
    scope: RuleScope
    min_nights: int = Field(ge=1)
    discount_pct: float = Field(gt=0, le=100)


class CompItemSpec(StrictModel):
    """One complimentary unit per N booked units (the classic comp room)."""

    type: Literal["comp_item"]
    label: str = Field(min_length=1, max_length=120)
    item_id: str
    per_qty: int = Field(ge=1)
    max_comps: int | None = Field(default=None, ge=1)


# Static discriminated union. Deliberately NOT generated from the rule registry:
# a module-level assertion in pricing/rules.py keeps the two in sync instead,
# which costs one line and avoids a dynamic-Pydantic rabbit hole.
RuleSpec = Annotated[
    Union[VolumeDiscountSpec, LosDiscountSpec, CompItemSpec],
    Field(discriminator="type"),
]


class FeeSpec(StrictModel):
    """A mandatory charge added after discounts and before tax; ``basis`` picks
    the calculation."""

    id: str = Field(pattern=ITEM_ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    basis: Literal["per_room_night", "pct_of_category", "pct_of_subtotal"]
    value: float = Field(ge=0)
    category: Category | None = None

    @model_validator(mode="after")
    def _category_required_for_scoped_fee(self) -> FeeSpec:
        if self.basis == "pct_of_category" and self.category is None:
            raise ValueError("basis 'pct_of_category' requires 'category'")
        return self


class FnbMinimum(StrictModel):
    """F&B minimum spend, triggered by booking a category. A shortfall is a
    warning, not a violation — the sales move is to upsell into it."""

    amount: float = Field(ge=0)
    when_category_booked: Category = "meeting_space"
    note: str = ""


class Policies(StrictModel):
    """Hotel-wide commercial terms."""

    tax_pct: float = Field(default=0, ge=0, le=100)
    min_nights: int = Field(default=1, ge=1)
    min_lead_days: int = Field(default=0, ge=0)
    max_attendees: int | None = Field(default=None, ge=1)
    deposit_pct: float = Field(default=0, ge=0, le=100)
    cancellation_note: str = ""


class Upsell(StrictModel):
    """An offer the agent may make, with the words it should use. ``trigger`` is
    the category that makes it relevant; ``always`` means everyone."""

    item_id: str
    trigger: Category | Literal["always"] = "always"
    pitch: str = Field(min_length=1, max_length=400)


class HotelConfig(StrictModel):
    """One hotel == one agent."""

    id: str = Field(pattern=CONFIG_ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    currency: str = Field(default="USD", min_length=1, max_length=8)
    persona: str = Field(default="", max_length=1200)
    sample_opener: str = Field(default="", max_length=600)
    policies: Policies = Field(default_factory=Policies)
    fees: list[FeeSpec] = Field(default_factory=list)
    inventory: list[InventoryItem] = Field(min_length=1)
    rules: list[RuleSpec] = Field(default_factory=list)
    fnb_minimum: FnbMinimum | None = None
    upsells: list[Upsell] = Field(default_factory=list)

    @field_validator("inventory")
    @classmethod
    def _unique_item_ids(cls, items: list[InventoryItem]) -> list[InventoryItem]:
        seen: set[str] = set()
        for item in items:
            if item.id in seen:
                raise ValueError(f"duplicate inventory id '{item.id}'")
            seen.add(item.id)
        return items

    @model_validator(mode="after")
    def _cross_references_resolve(self) -> HotelConfig:
        """Catch dangling item ids at save time, not mid-conversation."""
        known = {item.id for item in self.inventory}
        for rule in self.rules:
            scope = getattr(rule, "scope", None)
            target = getattr(rule, "item_id", None) or getattr(scope, "item_id", None)
            if target is not None and target not in known:
                raise ValueError(
                    f"rule '{rule.label}' references unknown item '{target}'"
                )
        for upsell in self.upsells:
            if upsell.item_id not in known:
                raise ValueError(f"upsell references unknown item '{upsell.item_id}'")
        return self

    def item(self, item_id: str) -> InventoryItem | None:
        for candidate in self.inventory:
            if candidate.id == item_id:
                return candidate
        return None


class ConfigSummary(BaseModel):
    """Lightweight row for the chat page's agent dropdown."""

    id: str
    name: str
    currency: str
    sample_opener: str
