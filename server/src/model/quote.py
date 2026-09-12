"""Quote request/response shapes shared by the pricing engine, the tool and the UI."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator


class Selection(BaseModel):
    """One thing the agent wants on the quote."""

    item_id: str
    qty: int = Field(ge=0)
    # Advisory only: the engine derives duration from the item's unit.
    duration: int | None = Field(default=None, ge=0)


class QuoteRequest(BaseModel):
    arrival_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    nights: int = Field(ge=0, le=365)
    attendees: int = Field(ge=0, le=100_000)
    selections: list[Selection] = Field(default_factory=list)

    @field_validator("arrival_date")
    @classmethod
    def _real_calendar_date(cls, value: str) -> str:
        # The pattern accepts 2027-02-30; fromisoformat is what actually runs later.
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(
                "arrival_date must be a real calendar date, YYYY-MM-DD"
            ) from None
        return value


class QuoteLine(BaseModel):
    item_id: str
    name: str
    qty: int
    unit_price: float
    unit_label: str
    duration: int
    duration_label: str
    subtotal: float
    note: str = ""


class Adjustment(BaseModel):
    """A labelled discount (negative) or credit applied before fees."""

    label: str
    amount: float


class FeeLine(BaseModel):
    label: str
    amount: float


class TaxLine(BaseModel):
    label: str
    amount: float


class Violation(BaseModel):
    """Hard failure: the quote cannot be issued as requested."""

    code: str
    message: str
    item_id: str | None = None
    suggestion: str | None = None


class Warning(BaseModel):
    """Soft issue: quote stands, but the agent should say something (or upsell)."""

    code: str
    message: str
    item_id: str | None = None


class Quote(BaseModel):
    currency: str
    hotel_name: str
    arrival_date: str
    nights: int
    attendees: int
    lines: list[QuoteLine] = Field(default_factory=list)
    subtotal: float = 0.0
    adjustments: list[Adjustment] = Field(default_factory=list)
    net_subtotal: float = 0.0
    fees: list[FeeLine] = Field(default_factory=list)
    tax: TaxLine | None = None
    total: float = 0.0
    deposit_due: float = 0.0
    deposit_note: str = ""
    cancellation_note: str = ""
    violations: list[Violation] = Field(default_factory=list)
    warnings: list[Warning] = Field(default_factory=list)
    # Pre-rendered so the model pastes it verbatim instead of retyping numbers.
    markdown_table: str = ""

    @property
    def priced(self) -> bool:
        return not self.violations
