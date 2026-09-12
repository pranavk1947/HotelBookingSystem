"""Money helpers. Integer cents inside the engine; floats only at the JSON edge."""

from __future__ import annotations

from decimal import ROUND_HALF_UP
from decimal import Decimal

Cents = int


def to_cents(amount: float | int | str) -> Cents:
    """Convert a config/JSON amount to integer cents, rounding half up."""
    quantized = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantized * 100)


def to_amount(cents: Cents) -> float:
    """Back to a JSON-friendly float."""
    return float(Decimal(cents) / Decimal(100))


def pct_of(cents: Cents, pct: float) -> Cents:
    """A percentage of a cent amount, half-up. Every percentage goes through here."""
    value = (Decimal(cents) * Decimal(str(pct)) / Decimal(100)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(value)


def fmt(cents: Cents, currency: str = "USD") -> str:
    """Human money for the markdown table; negatives render as −$2,268.00."""
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(currency, "")
    sign = "−" if cents < 0 else ""
    whole = abs(cents) / 100
    rendered = f"{whole:,.2f}"
    if symbol:
        return f"{sign}{symbol}{rendered}"
    return f"{sign}{rendered} {currency}"
