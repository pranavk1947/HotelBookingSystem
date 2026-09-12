"""Renders a Quote as the markdown table the agent pastes verbatim, so the model
copies one string instead of retyping fourteen numbers."""

from __future__ import annotations

from src.model.quote import Quote
from src.pricing.money import to_cents
from src.pricing.money import fmt


class QuoteMarkdownRenderer:
    def render(self, quote: Quote) -> str:
        currency = quote.currency
        rows: list[str] = [
            "| Line | Qty | Unit price | Duration | Subtotal |",
            "| --- | --- | --- | --- | --- |",
        ]
        for line in quote.lines:
            name = line.name + (f" ({line.note})" if line.note else "")
            rows.append(
                f"| {name} | {line.qty} | {line.unit_label} | "
                f"{line.duration_label} | {fmt(to_cents(line.subtotal), currency)} |"
            )

        rows.append(self._total_row("Subtotal", quote.subtotal, currency))
        for adjustment in quote.adjustments:
            rows.append(self._total_row(adjustment.label, adjustment.amount, currency))
        if quote.adjustments:
            rows.append(self._total_row("Net subtotal", quote.net_subtotal, currency))
        for fee in quote.fees:
            rows.append(self._total_row(fee.label, fee.amount, currency))
        if quote.tax is not None:
            rows.append(self._total_row(quote.tax.label, quote.tax.amount, currency))
        rows.append(self._total_row("**Total**", quote.total, currency, bold=True))
        if quote.deposit_due:
            rows.append(self._total_row("Deposit due on signing", quote.deposit_due, currency))
        return "\n".join(rows)

    @staticmethod
    def _total_row(label: str, amount: float, currency: str, bold: bool = False) -> str:
        rendered = fmt(to_cents(amount), currency)
        if bold:
            rendered = f"**{rendered}**"
        return f"| {label} |  |  |  | {rendered} |"
