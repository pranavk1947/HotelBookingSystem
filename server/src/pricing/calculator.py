"""The quote pipeline: the only place money is calculated.

Order of operations:

    1. resolve lines      qty × unit price × derived duration (seasonal rate wins)
    2. constraints        hard violations stop here, before any total exists
    3. subtotal
    4. rules              config order, each a labelled negative adjustment
    5. net subtotal
    6. fees               per-room-night, % of a category, % of subtotal
    7. tax                on (net subtotal + fees)
    8. total              and the deposit due on signing
"""

from __future__ import annotations

from datetime import date

from src.model.hotel_config import PER_PERSON_UNITS
from src.model.hotel_config import HotelConfig
from src.model.hotel_config import InventoryItem
from src.model.quote import Adjustment
from src.model.quote import FeeLine
from src.model.quote import Quote
from src.model.quote import QuoteLine
from src.model.quote import QuoteRequest
from src.model.quote import Selection
from src.model.quote import TaxLine
from src.model.quote import Violation
from src.model.quote import Warning
from src.pricing.constraints import build_constraints
from src.pricing.constraints import suggest_item
from src.pricing.context import QuoteContext
from src.pricing.context import ResolvedLine
from src.pricing.fees import build_fee
from src.pricing.money import Cents
from src.pricing.money import pct_of
from src.pricing.money import to_amount
from src.pricing.money import to_cents
from src.pricing.renderer import QuoteMarkdownRenderer
from src.pricing.rules import build_rule
from src.pricing.seasons import season_matches

UNIT_LABELS = {
    "per_night": "night",
    "per_day": "day",
    "per_person": "person",
    "per_person_per_day": "person / day",
    "flat": "flat",
}


class QuoteCalculator:
    """Builds one quote for one config. Construct per request; holds no state."""

    def __init__(self, config: HotelConfig, today: date | None = None) -> None:
        self.config = config
        self.today = today or date.today()
        self.renderer = QuoteMarkdownRenderer()

    def build(self, request: QuoteRequest) -> Quote:
        ctx = QuoteContext(
            config=self.config,
            request=request,
            today=self.today,
            arrival=date.fromisoformat(request.arrival_date),
        )

        self._resolve_lines(ctx)
        self._check_constraints(ctx)
        if ctx.has_hard_violations:
            return self._to_quote(ctx, priced=False)

        self._compute_subtotal(ctx)
        self._apply_rules(ctx)
        self._apply_fees(ctx)
        self._apply_tax(ctx)
        self._finalize(ctx)
        return self._to_quote(ctx, priced=True)

    # --- stages ---------------------------------------------------------

    def _resolve_lines(self, ctx: QuoteContext) -> None:
        known = [item.id for item in self.config.inventory]
        for selection in self._merge_duplicates(ctx.request.selections):
            item = self.config.item(selection.item_id)
            if item is None:
                ctx.violations.append(
                    Violation(
                        code="unknown_item",
                        item_id=selection.item_id,
                        message=f"'{selection.item_id}' is not in this hotel's catalog.",
                        suggestion=suggest_item(selection.item_id, known),
                    )
                )
                continue
            if selection.qty <= 0:
                continue

            unit_price = self._resolve_unit_price(item, ctx.arrival)
            duration = QuoteContext.duration_for(item, ctx.nights)
            qty = self._effective_qty(item, selection.qty, ctx)
            if qty != selection.qty:
                ctx.warnings.append(
                    Warning(
                        code="qty_expanded_to_headcount",
                        item_id=item.id,
                        message=(
                            f"{item.name} is priced per person, so it was quoted "
                            f"for all {ctx.request.attendees} attendees."
                        ),
                    )
                )
            note = ""
            if selection.duration is not None and selection.duration != duration:
                ctx.warnings.append(
                    Warning(
                        code="duration_overridden",
                        item_id=item.id,
                        message=(
                            f"{item.name} is billed {item.unit.replace('_', ' ')}, so "
                            f"duration {duration} was used instead of "
                            f"{selection.duration}."
                        ),
                    )
                )
            if unit_price != to_cents(item.unit_price):
                note = "seasonal rate"

            ctx.lines.append(
                ResolvedLine(
                    item=item,
                    qty=qty,
                    unit_price=unit_price,
                    duration=duration,
                    subtotal=unit_price * qty * duration,
                    note=note,
                )
            )

        if not ctx.lines and not ctx.violations:
            ctx.warnings.append(
                Warning(code="empty_quote", message="No billable items were selected.")
            )

    @staticmethod
    def _merge_duplicates(selections: list[Selection]) -> list[Selection]:
        """Sum repeats of the same item so a rule threshold sees one total."""
        merged: dict[str, Selection] = {}
        for selection in selections:
            existing = merged.get(selection.item_id)
            if existing is None:
                merged[selection.item_id] = selection.model_copy()
            else:
                existing.qty += selection.qty
        return list(merged.values())

    def _effective_qty(self, item: InventoryItem, qty: int, ctx: QuoteContext) -> int:
        """Per-person items bill per head: qty 1 means one each, not one total."""
        if item.unit in PER_PERSON_UNITS and qty <= 1 and ctx.request.attendees > 0:
            return ctx.request.attendees
        return qty

    def _resolve_unit_price(self, item: InventoryItem, arrival: date) -> Cents:
        """First seasonal window containing the arrival date wins."""
        for season in item.seasonal_rates:
            if season_matches(arrival, season.start, season.end):
                return to_cents(season.unit_price)
        return to_cents(item.unit_price)

    def _check_constraints(self, ctx: QuoteContext) -> None:
        for constraint in build_constraints():
            for finding in constraint.check(ctx):
                if isinstance(finding, Violation):
                    ctx.violations.append(finding)
                else:
                    ctx.warnings.append(finding)

    def _compute_subtotal(self, ctx: QuoteContext) -> None:
        ctx.subtotal = sum(line.subtotal for line in ctx.lines)

    def _apply_rules(self, ctx: QuoteContext) -> None:
        for spec in self.config.rules:
            rule = build_rule(spec)
            if not rule.applies(ctx):
                continue
            adjustment = rule.apply(ctx)
            if adjustment is not None:
                ctx.adjustments.append(adjustment)
        ctx.net_subtotal = ctx.subtotal + sum(a.amount for a in ctx.adjustments)

    def _apply_fees(self, ctx: QuoteContext) -> None:
        for spec in self.config.fees:
            fee = build_fee(spec)
            amount = fee.compute(ctx)
            if amount > 0:
                ctx.fees.append((fee.label(ctx), amount))

    def _apply_tax(self, ctx: QuoteContext) -> None:
        tax_pct = self.config.policies.tax_pct
        if tax_pct <= 0:
            ctx.tax = 0
            return
        base = ctx.net_subtotal + sum(amount for _, amount in ctx.fees)
        ctx.tax = pct_of(base, tax_pct)

    def _finalize(self, ctx: QuoteContext) -> None:
        ctx.total = ctx.net_subtotal + sum(a for _, a in ctx.fees) + ctx.tax

    # --- assembly -------------------------------------------------------

    def _to_quote(self, ctx: QuoteContext, priced: bool) -> Quote:
        currency = self.config.currency
        policies = self.config.policies
        has_rooms = any(line.item.category == "room" for line in ctx.lines)
        quote = Quote(
            currency=currency,
            hotel_name=self.config.name,
            arrival_date=ctx.request.arrival_date,
            nights=ctx.nights,
            attendees=ctx.request.attendees,
            lines=[self._to_line(line, currency, has_rooms) for line in ctx.lines],
            violations=ctx.violations,
            warnings=ctx.warnings,
        )
        if not priced:
            return quote

        quote.subtotal = to_amount(ctx.subtotal)
        quote.adjustments = [
            Adjustment(label=a.label, amount=to_amount(a.amount)) for a in ctx.adjustments
        ]
        quote.net_subtotal = to_amount(ctx.net_subtotal)
        quote.fees = [FeeLine(label=label, amount=to_amount(a)) for label, a in ctx.fees]
        if ctx.tax:
            quote.tax = TaxLine(
                label=f"Tax — {policies.tax_pct:g}%", amount=to_amount(ctx.tax)
            )
        quote.total = to_amount(ctx.total)
        if policies.deposit_pct > 0:
            deposit = pct_of(ctx.total, policies.deposit_pct)
            quote.deposit_due = to_amount(deposit)
            quote.deposit_note = (
                f"{policies.deposit_pct:g}% deposit due on signing to hold the block."
            )
        quote.cancellation_note = policies.cancellation_note
        quote.markdown_table = self.renderer.render(quote)
        return quote

    def _to_line(self, line: ResolvedLine, currency: str, has_rooms: bool) -> QuoteLine:
        item = line.item
        unit_word = UNIT_LABELS[item.unit]
        from src.pricing.money import fmt

        unit_label = (
            fmt(line.unit_price, currency)
            if item.unit == "flat"
            else f"{fmt(line.unit_price, currency)} / {unit_word}"
        )
        if line.duration > 1:
            noun = "nights" if item.unit == "per_night" else "days"
            duration_label = f"{line.duration} {noun}"
        elif item.unit == "per_night" or (item.unit == "per_person" and has_rooms):
            duration_label = "1 night"
        else:
            duration_label = "—"

        return QuoteLine(
            item_id=item.id,
            name=item.name,
            qty=line.qty,
            unit_price=to_amount(line.unit_price),
            unit_label=unit_label,
            duration=line.duration,
            duration_label=duration_label,
            subtotal=to_amount(line.subtotal),
            note=line.note,
        )
