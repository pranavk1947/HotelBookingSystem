"""Bookability checks.

Violations are hard — the quote returns without totals. Warnings are soft — the
quote stands and the agent should say something. A new check is one class plus
one line in ``build_constraints``.
"""

from __future__ import annotations

import difflib
from abc import ABC
from abc import abstractmethod

from src.model.quote import Violation
from src.model.quote import Warning
from src.pricing.context import QuoteContext
from src.pricing.money import fmt
from src.pricing.money import to_cents


class Constraint(ABC):
    """Checks one bookability condition against the resolved lines."""

    @abstractmethod
    def check(self, ctx: QuoteContext) -> list[Violation | Warning]:
        """Return any problems found; an empty list means all good."""


class AvailableQtyConstraint(Constraint):
    """You cannot sell 60 rooms out of a 40-room block."""

    def check(self, ctx: QuoteContext) -> list[Violation | Warning]:
        found: list[Violation | Warning] = []
        for line in ctx.lines:
            item = line.item
            if line.qty > item.available_qty:
                found.append(
                    Violation(
                        code="qty_unavailable",
                        item_id=item.id,
                        message=(
                            f"Only {item.available_qty} × {item.name} are available "
                            f"for these dates; {line.qty} were requested."
                        ),
                        suggestion=f"Reduce {item.id} to {item.available_qty} or fewer.",
                    )
                )
            if item.max_qty is not None and line.qty > item.max_qty:
                found.append(
                    Violation(
                        code="qty_above_max",
                        item_id=item.id,
                        message=f"{item.name} is limited to {item.max_qty} per booking.",
                        suggestion=f"Reduce {item.id} to {item.max_qty}.",
                    )
                )
            if item.min_qty is not None and 0 < line.qty < item.min_qty:
                found.append(
                    Violation(
                        code="qty_below_min",
                        item_id=item.id,
                        message=f"{item.name} has a minimum of {item.min_qty} per booking.",
                        suggestion=f"Increase {item.id} to {item.min_qty}.",
                    )
                )
        return found


class CapacityConstraint(Constraint):
    """Attendees must physically fit in the meeting space that was selected."""

    def check(self, ctx: QuoteContext) -> list[Violation | Warning]:
        found: list[Violation | Warning] = []
        attendees = ctx.request.attendees
        if attendees <= 0:
            return found
        for line in ctx.lines:
            item = line.item
            if item.capacity is None or item.category != "meeting_space" or line.qty == 0:
                continue
            seats = item.capacity * line.qty
            if attendees > seats:
                found.append(
                    Violation(
                        code="capacity_exceeded",
                        item_id=item.id,
                        message=(
                            f"{item.name} seats {item.capacity}"
                            f"{f' × {line.qty} = {seats}' if line.qty > 1 else ''}, "
                            f"but {attendees} attendees are expected."
                        ),
                        suggestion=(
                            "Pick a larger space, add another unit of it, or split the "
                            "group across spaces."
                        ),
                    )
                )
        return found


class RoomOccupancyConstraint(Constraint):
    """Warn if the room block cannot sleep the group."""

    def check(self, ctx: QuoteContext) -> list[Violation | Warning]:
        attendees = ctx.request.attendees
        rooms = [line for line in ctx.lines if line.item.category == "room" and line.qty]
        if attendees <= 0 or not rooms:
            return []
        beds = sum(line.qty * (line.item.capacity or 1) for line in rooms)
        if beds < attendees:
            return [
                Warning(
                    code="rooms_below_headcount",
                    message=(
                        f"The room block sleeps {beds} but {attendees} attendees are "
                        "expected — confirm double occupancy or add rooms."
                    ),
                )
            ]
        return []


class MinNightsConstraint(Constraint):
    def check(self, ctx: QuoteContext) -> list[Violation | Warning]:
        minimum = ctx.config.policies.min_nights
        has_rooms = any(line.item.category == "room" and line.qty for line in ctx.lines)
        if has_rooms and 0 < ctx.nights < minimum:
            return [
                Violation(
                    code="below_min_nights",
                    message=f"{ctx.config.name} has a {minimum}-night minimum stay.",
                    suggestion=f"Quote at least {minimum} nights.",
                )
            ]
        return []


class MaxAttendeesConstraint(Constraint):
    def check(self, ctx: QuoteContext) -> list[Violation | Warning]:
        cap = ctx.config.policies.max_attendees
        if cap is not None and ctx.request.attendees > cap:
            return [
                Violation(
                    code="above_max_attendees",
                    message=(
                        f"{ctx.config.name} can host up to {cap} attendees; "
                        f"{ctx.request.attendees} were requested."
                    ),
                    suggestion="Reduce the group size or split across dates.",
                )
            ]
        return []


class MinLeadDaysConstraint(Constraint):
    """Short notice is a warning, not a block: quote the date, flag the sign-off."""

    def check(self, ctx: QuoteContext) -> list[Violation | Warning]:
        required = ctx.config.policies.min_lead_days
        if required <= 0:
            return []
        lead = (ctx.arrival - ctx.today).days
        if lead < required:
            return [
                Warning(
                    code="short_lead_time",
                    message=(
                        f"Arrival is {lead} day(s) out; group bookings normally need "
                        f"{required} days' lead time, so these dates need "
                        "confirmation from the events desk."
                    ),
                )
            ]
        return []


class FnbMinimumConstraint(Constraint):
    """F&B minimum spend, triggered by booking a category (usually meeting space)."""

    def check(self, ctx: QuoteContext) -> list[Violation | Warning]:
        spec = ctx.config.fnb_minimum
        if spec is None:
            return []
        triggered = any(
            line.item.category == spec.when_category_booked and line.qty
            for line in ctx.lines
        )
        if not triggered:
            return []
        required = to_cents(spec.amount)
        booked = ctx.category_total("fnb")
        if booked >= required:
            return []
        currency = ctx.config.currency
        shortfall = required - booked
        note = f" {spec.note}" if spec.note else ""
        return [
            Warning(
                code="fnb_minimum_shortfall",
                message=(
                    f"Food & beverage minimum of {fmt(required, currency)} applies; "
                    f"currently at {fmt(booked, currency)}, "
                    f"{fmt(shortfall, currency)} short.{note}"
                ),
            )
        ]


def suggest_item(item_id: str, known: list[str]) -> str | None:
    """Fuzzy-match an unknown id so the model can self-correct in-loop."""
    matches = difflib.get_close_matches(item_id, known, n=1, cutoff=0.5)
    if matches:
        return f"Did you mean '{matches[0]}'?"
    if known:
        return "Valid item ids: " + ", ".join(sorted(known))
    return None


def build_constraints() -> list[Constraint]:
    """The check set every quote runs through, in reporting order."""
    return [
        AvailableQtyConstraint(),
        CapacityConstraint(),
        MinNightsConstraint(),
        MaxAttendeesConstraint(),
        RoomOccupancyConstraint(),
        MinLeadDaysConstraint(),
        FnbMinimumConstraint(),
    ]
