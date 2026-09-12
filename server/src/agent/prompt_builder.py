"""Turns a config into the agent's system prompt.

The catalog is rendered as English, not JSON, and rebuilt on every request from
the freshly-read file — that is what makes a config edit change what the agent says.
"""

from __future__ import annotations

from datetime import date

from src.model.hotel_config import HotelConfig
from src.model.hotel_config import InventoryItem

UNIT_PHRASES = {
    "per_night": "per room per night",
    "per_day": "per day",
    "per_person": "per person, charged once",
    "per_person_per_day": "per person per day",
    "flat": "flat, charged once",
}

CATEGORY_HEADINGS = {
    "room": "Guest rooms",
    "meeting_space": "Meeting & event space",
    "fnb": "Food & beverage",
    "av": "Audio-visual",
    "service": "Activities & services",
}


class SystemPromptBuilder:
    def build(self, config: HotelConfig, today: date) -> str:
        sections = [
            self._role(config, today),
            self._catalog(config),
            self._rules(config),
            self._fees(config),
            self._constraints(config),
            self._upsells(config),
            self._policy(),
            self._output_format(),
        ]
        return "\n\n".join(section for section in sections if section)

    # --- sections -------------------------------------------------------

    def _role(self, config: HotelConfig, today: date) -> str:
        persona = config.persona or (
            f"You are a group sales manager at {config.name}. Professional, concise, warm."
        )
        return (
            "# ROLE\n"
            f"{persona}\n\n"
            f"You sell group business at {config.name}. All prices are in "
            f"{config.currency}.\n"
            f"Today is {today.strftime('%A, %d %B %Y')} ({today.isoformat()}). "
            "Resolve every relative date ('in March', 'next month', 'the 12th') "
            "against this date, and always state the arrival date and number of "
            "nights you have assumed."
        )

    def _catalog(self, config: HotelConfig) -> str:
        lines = [
            "# WHAT WE SELL",
            "Quote only from this list, using these exact item ids.",
        ]
        for category, heading in CATEGORY_HEADINGS.items():
            items = [i for i in config.inventory if i.category == category]
            if not items:
                continue
            lines.append(f"\n## {heading}")
            for item in items:
                lines.append(self._item_line(item, config.currency))
        return "\n".join(lines)

    def _item_line(self, item: InventoryItem, currency: str) -> str:
        parts = [
            f"- `{item.id}` — {item.name}: "
            f"{currency} {item.unit_price:,.2f} {UNIT_PHRASES[item.unit]}."
        ]
        if item.capacity is not None:
            noun = "sleeps" if item.category == "room" else "seats"
            parts.append(f"{noun.capitalize()} {item.capacity} per unit.")
        parts.append(f"{item.available_qty} available.")
        if item.description:
            parts.append(item.description)
        if item.includes_note:
            parts.append("Includes: " + ", ".join(item.includes_note) + ".")
        for season in item.seasonal_rates:
            parts.append(
                f"Seasonal: {season.label} ({season.start} to {season.end}) is "
                f"{currency} {season.unit_price:,.2f} — applied automatically."
            )
        return " ".join(parts)

    def _rules(self, config: HotelConfig) -> str:
        if not config.rules:
            return (
                "# PRICING RULES\nNo automatic discounts. Do not invent any or offer "
                "one you were not given."
            )
        lines = [
            "# PRICING RULES",
            "These apply automatically inside build_quote. Mention them as a benefit "
            "when relevant, but you must NOT calculate them yourself:",
        ]
        for rule in config.rules:
            lines.append(f"- {rule.label}")
        return "\n".join(lines)

    def _fees(self, config: HotelConfig) -> str:
        lines = ["# FEES & TAX"]
        if not config.fees and config.policies.tax_pct <= 0:
            lines.append("No additional fees or tax.")
        for fee in config.fees:
            if fee.basis == "per_room_night":
                lines.append(
                    f"- {fee.name}: {config.currency} {fee.value:,.2f} per room per night."
                )
            elif fee.basis == "pct_of_category":
                lines.append(f"- {fee.name}: {fee.value:g}% of the {fee.category} total.")
            else:
                lines.append(f"- {fee.name}: {fee.value:g}% of the subtotal.")
        if config.policies.tax_pct > 0:
            lines.append(
                f"- Tax: {config.policies.tax_pct:g}%, applied after fees."
            )
        if config.policies.deposit_pct > 0:
            lines.append(
                f"- Deposit: {config.policies.deposit_pct:g}% of the total due on "
                "signing to hold the block."
            )
        if config.policies.cancellation_note:
            lines.append(f"- Cancellation: {config.policies.cancellation_note}")
        lines.append(
            "These are added by build_quote and already appear in the table it "
            "returns. Do not add them yourself."
        )
        return "\n".join(lines)

    def _constraints(self, config: HotelConfig) -> str:
        policies = config.policies
        lines = ["# HOUSE RULES & LIMITS"]
        if policies.min_nights > 1:
            lines.append(f"- Minimum stay: {policies.min_nights} nights.")
        if policies.max_attendees is not None:
            lines.append(f"- We can host up to {policies.max_attendees} attendees.")
        if policies.min_lead_days > 0:
            lines.append(
                f"- Group bookings normally need {policies.min_lead_days} days' lead time."
            )
        if config.fnb_minimum is not None:
            note = config.fnb_minimum.note or (
                f"Booking {config.fnb_minimum.when_category_booked.replace('_', ' ')} "
                f"carries a {config.currency} {config.fnb_minimum.amount:,.0f} food "
                "and beverage minimum."
            )
            lines.append(f"- {note}")
            lines.append(
                "  If build_quote warns the minimum is short, say so plainly and "
                "offer food and beverage that closes the gap — that is a better "
                "outcome for the customer than paying the shortfall as a fee."
            )
        lines.append(
            "- Meeting space must physically seat the group. If it does not, "
            "build_quote will refuse; pick a bigger space or add another room."
        )
        return "\n".join(lines)

    def _upsells(self, config: HotelConfig) -> str:
        if not config.upsells:
            return ""
        lines = [
            "# UPSELLS YOU MAY OFFER",
            "Offer only these, at most two at a time, using the substance of the "
            "given pitch in your own voice. Never invent an item or a price.",
        ]
        for upsell in config.upsells:
            when = (
                "always relevant"
                if upsell.trigger == "always"
                else f"relevant when the quote includes {upsell.trigger.replace('_', ' ')}"
            )
            lines.append(f"- `{upsell.item_id}` ({when}): {upsell.pitch}")
        return "\n".join(lines)

    def _policy(self) -> str:
        return """# HOW YOU RUN THE CONVERSATION
1. You are talking to a paying customer, not to an operator. Never ask for
   permission or confirmation to continue. Banned phrasings: "shall I proceed",
   "would you like me to prepare", "let me know and I'll", "just confirm and
   I'll put this together".
2. You get at most TWO clarifying turns in the whole conversation, batching two
   or three questions each. Ask about: arrival date and nights; head count and
   how many rooms; what happens during the day (sessions, breakouts) and any
   meals. After your second clarifying turn you MUST produce a quote.
3. If something is still unknown, assume the sensible default, quote anyway,
   and list the assumption. Defaults: single occupancy (one room per attendee);
   one plenary space for the whole group; two breakouts if the group is 30+ and
   they mentioned sessions; a plated dinner for everyone if they mentioned
   dinner; arrival on the second Monday of the month if they only named a month.
4. Every number you write comes from the most recent build_quote result. Prices
   quoted earlier in this conversation may be stale — if anything changes, or
   if you are unsure, call build_quote again rather than restating an old
   number.
5. If build_quote returns violations, do not show them raw. Choose a compliant
   alternative, call build_quote again, and explain the change in one sentence
   ("Breakout A seats 60, so I've put the plenary in the Ballroom").
6. If build_quote returns warnings, work them into the conversation: a food and
   beverage shortfall is an upsell, a short lead time is a caveat.
7. When the customer accepts, changes or declines something, call build_quote
   again with the new selection and present the updated table."""

    def _output_format(self) -> str:
        return """# HOW YOU REPLY
- Keep prose short. Two or three sentences around the table, not an essay.
- When you have a quote, paste the `markdown_table` from the build_quote result
  VERBATIM. Do not retype it, reorder it, round it or add rows to it.
- Under the table, in one short block: the assumptions you made, then the
  deposit and cancellation terms in a single line each.
- Then offer at most two upsells, as a question a salesperson would actually
  ask.
- Never show item ids, tool names, JSON or internal codes to the customer.
- Never apologise for the price."""
