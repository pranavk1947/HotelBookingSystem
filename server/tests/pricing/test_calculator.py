"""Pricing engine tests.

Test 1 is the contract with the brief: the example table must reproduce to the
cent. The rest pin the boundaries where a pricing engine actually goes wrong —
thresholds, seasonal edges, discount stacking, fee scoping and rounding.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.model.hotel_config import HotelConfig
from src.model.quote import QuoteRequest
from src.model.quote import Selection
from src.pricing.calculator import QuoteCalculator
from src.pricing.seasons import season_matches

CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs"
# Fixed "today" so lead-time and seasonal assertions never rot.
TODAY = date(2026, 1, 15)


def load(config_id: str) -> HotelConfig:
    return HotelConfig.model_validate(
        json.loads((CONFIG_DIR / f"{config_id}.json").read_text())
    )


@pytest.fixture
def cascadia() -> HotelConfig:
    return load("grand-cascadia")


@pytest.fixture
def hacienda() -> HotelConfig:
    return load("hacienda-del-sol")


def brief_request() -> QuoteRequest:
    """The brief's worked example: 3-day sales conference in March, 40 attendees."""
    return QuoteRequest(
        arrival_date="2026-03-09",
        nights=3,
        attendees=40,
        selections=[
            Selection(item_id="std-room", qty=40),
            Selection(item_id="grand-ballroom", qty=1),
            Selection(item_id="breakout-a", qty=1),
            Selection(item_id="breakout-b", qty=1),
            Selection(item_id="av-package", qty=1),
            Selection(item_id="plated-dinner", qty=40),
        ],
    )


# --- 1. the contract with the brief ------------------------------------


def test_brief_example_reproduces_exactly(cascadia: HotelConfig) -> None:
    quote = QuoteCalculator(cascadia, today=TODAY).build(brief_request())

    assert quote.violations == []
    by_id = {line.item_id: line for line in quote.lines}
    assert by_id["std-room"].subtotal == 22_680.00      # 40 × 189 × 3
    assert by_id["grand-ballroom"].subtotal == 7_200.00  # 1 × 2400 × 3
    assert by_id["breakout-a"].subtotal == 1_950.00      # 1 × 650 × 3
    assert by_id["breakout-b"].subtotal == 1_950.00
    assert by_id["av-package"].subtotal == 1_200.00      # 1 × 400 × 3
    assert by_id["plated-dinner"].subtotal == 3_000.00   # 40 × 75, once

    assert quote.subtotal == 37_980.00
    assert len(quote.adjustments) == 1
    assert quote.adjustments[0].amount == -2_268.00      # 10% of 22,680
    assert quote.adjustments[0].label == "Volume discount (10% on rooms ≥ 30)"
    assert quote.net_subtotal == 35_712.00

    # Beyond the brief's table, the config's real-world charges apply.
    assert quote.fees[0].amount == 660.00                # 22% service charge on 3,000
    assert quote.tax is not None
    assert quote.tax.amount == pytest.approx(3_455.34)   # 9.5% of 36,372
    assert quote.total == pytest.approx(39_827.34)
    assert quote.deposit_due == pytest.approx(9_956.84)  # 25%

    # The three headline numbers appear verbatim in what the agent pastes.
    assert "$37,980.00" in quote.markdown_table
    assert "−$2,268.00" in quote.markdown_table
    assert "$35,712.00" in quote.markdown_table


def test_duration_and_unit_labels_match_the_brief(cascadia: HotelConfig) -> None:
    quote = QuoteCalculator(cascadia, today=TODAY).build(brief_request())
    by_id = {line.item_id: line for line in quote.lines}
    assert by_id["std-room"].unit_label == "$189.00 / night"
    assert by_id["std-room"].duration_label == "3 nights"
    assert by_id["grand-ballroom"].duration_label == "3 days"
    assert by_id["plated-dinner"].unit_label == "$75.00 / person"
    # The brief labels the one-off dinner "1 night" when there is a room block.
    assert by_id["plated-dinner"].duration_label == "1 night"


# --- 2. threshold boundaries -------------------------------------------


@pytest.mark.parametrize(
    "rooms,expect_discount", [(29, False), (30, True), (31, True)]
)
def test_volume_discount_threshold_is_inclusive(
    cascadia: HotelConfig, rooms: int, expect_discount: bool
) -> None:
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=2,
        attendees=rooms,
        selections=[Selection(item_id="std-room", qty=rooms)],
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)
    assert bool(quote.adjustments) is expect_discount


def test_comp_rooms_do_not_lower_the_discount_threshold() -> None:
    """Thresholds evaluate on BOOKED qty, never post-comp billable qty.

    30 rooms with a comp-per-30 rule still earns the 30-room discount; reading
    the threshold off the billable 29 would silently drop a tier.
    """
    config = HotelConfig.model_validate(
        {
            "id": "comp-test",
            "name": "Comp Test Inn",
            "inventory": [
                {
                    "id": "std-room",
                    "name": "Room",
                    "category": "room",
                    "unit": "per_night",
                    "unit_price": 100.0,
                    "available_qty": 100,
                    "capacity": 2,
                }
            ],
            "rules": [
                {
                    "type": "comp_item",
                    "label": "1 comp room per 30",
                    "item_id": "std-room",
                    "per_qty": 30,
                },
                {
                    "type": "volume_discount",
                    "label": "10% on rooms ≥ 30",
                    "scope": {"category": "room"},
                    "min_qty": 30,
                    "discount_pct": 10.0,
                },
            ],
        }
    )
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=2,
        attendees=30,
        selections=[Selection(item_id="std-room", qty=30)],
    )
    quote = QuoteCalculator(config, today=TODAY).build(request)

    labels = [a.label for a in quote.adjustments]
    assert labels == ["1 comp room per 30", "10% on rooms ≥ 30"]
    assert quote.subtotal == 6_000.00               # 30 × 100 × 2
    assert quote.adjustments[0].amount == -200.00   # one room × 2 nights
    # 10% off the post-comp 5,800 — discounts stack multiplicatively, in order.
    assert quote.adjustments[1].amount == -580.00
    assert quote.net_subtotal == 5_220.00


# --- 3. seasonal windows ------------------------------------------------


def test_seasonal_rate_applies_on_the_boundary(hacienda: HotelConfig) -> None:
    def room_rate(arrival: str) -> float:
        request = QuoteRequest(
            arrival_date=arrival,
            nights=2,
            attendees=2,
            selections=[Selection(item_id="casita-king", qty=1)],
        )
        quote = QuoteCalculator(hacienda, today=date(2026, 1, 1)).build(request)
        return quote.lines[0].unit_price

    assert room_rate("2026-02-28") == 259.00   # day before peak
    assert room_rate("2026-03-01") == 329.00   # first day of peak
    assert room_rate("2026-04-30") == 329.00   # last day of peak
    assert room_rate("2026-05-01") == 259.00   # day after


def test_seasonal_window_wraps_the_year_end() -> None:
    assert season_matches(date(2026, 12, 25), "12-20", "01-05")
    assert season_matches(date(2026, 1, 2), "12-20", "01-05")
    assert not season_matches(date(2026, 6, 1), "12-20", "01-05")


# --- 4. stacking, fees and tax ------------------------------------------


def test_los_discount_stacks_after_volume(hacienda: HotelConfig) -> None:
    request = QuoteRequest(
        arrival_date="2026-06-08",  # outside the peak window
        nights=3,
        attendees=50,
        selections=[Selection(item_id="casita-king", qty=25)],
    )
    quote = QuoteCalculator(hacienda, today=TODAY).build(request)

    assert quote.subtotal == 19_425.00              # 25 × 259 × 3
    volume, los = quote.adjustments                 # comp needs 40 casitas, not 25
    # Rules fire in config order, each on what the previous one left behind.
    assert volume.amount == -1_554.00               # 8% of 19,425
    assert los.amount == -2_144.52                  # 12% of the remaining 17,871
    assert quote.net_subtotal == 15_726.48


def test_service_charge_is_scoped_to_fnb_and_tax_sits_on_top(
    cascadia: HotelConfig,
) -> None:
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=1,
        attendees=10,
        selections=[
            Selection(item_id="breakout-a", qty=1),
            Selection(item_id="plated-dinner", qty=10),
        ],
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)

    assert quote.subtotal == 1_400.00               # 650 + 750
    assert quote.fees[0].amount == 165.00           # 22% of the 750 F&B only
    assert quote.tax is not None
    assert quote.tax.amount == pytest.approx(148.68)  # 9.5% of 1,565
    assert quote.total == pytest.approx(1_713.68)


def test_resort_fee_bills_per_room_night(hacienda: HotelConfig) -> None:
    request = QuoteRequest(
        arrival_date="2026-06-08",
        nights=3,
        attendees=20,
        selections=[Selection(item_id="casita-king", qty=10)],
    )
    quote = QuoteCalculator(hacienda, today=TODAY).build(request)
    resort = next(f for f in quote.fees if "Resort" in f.label)
    assert resort.amount == 1_050.00                # 35 × 10 rooms × 3 nights
    assert "30 room-nights" in resort.label


def test_percentages_round_half_up_to_the_cent() -> None:
    config = HotelConfig.model_validate(
        {
            "id": "rounding-test",
            "name": "Rounding Inn",
            "policies": {"tax_pct": 7.777},
            "inventory": [
                {
                    "id": "std-room",
                    "name": "Room",
                    "category": "room",
                    "unit": "per_night",
                    "unit_price": 33.33,
                    "available_qty": 10,
                    "capacity": 2,
                }
            ],
        }
    )
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=1,
        attendees=2,
        selections=[Selection(item_id="std-room", qty=1)],
    )
    quote = QuoteCalculator(config, today=TODAY).build(request)
    assert quote.subtotal == 33.33
    assert quote.tax is not None
    assert quote.tax.amount == 2.59                 # 2.5922... -> 2.59
    assert quote.total == 35.92


# --- 5. constraints -----------------------------------------------------


def test_capacity_violation_blocks_totals(cascadia: HotelConfig) -> None:
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=1,
        attendees=200,
        selections=[Selection(item_id="breakout-a", qty=1)],
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)

    assert quote.total == 0.0
    assert quote.markdown_table == ""
    codes = [v.code for v in quote.violations]
    assert "capacity_exceeded" in codes


def test_overselling_inventory_is_a_violation(hacienda: HotelConfig) -> None:
    request = QuoteRequest(
        arrival_date="2026-06-08",
        nights=2,
        attendees=80,
        selections=[Selection(item_id="casita-king", qty=90)],
    )
    quote = QuoteCalculator(hacienda, today=TODAY).build(request)
    violation = next(v for v in quote.violations if v.code == "qty_unavailable")
    assert "60" in violation.message
    assert violation.suggestion is not None


def test_below_minimum_stay_is_a_violation(hacienda: HotelConfig) -> None:
    request = QuoteRequest(
        arrival_date="2026-06-08",
        nights=1,
        attendees=4,
        selections=[Selection(item_id="casita-king", qty=2)],
    )
    quote = QuoteCalculator(hacienda, today=TODAY).build(request)
    assert [v.code for v in quote.violations] == ["below_min_nights"]


def test_fnb_minimum_is_a_warning_not_a_violation(cascadia: HotelConfig) -> None:
    """An unmet F&B minimum is an upsell, not a refusal to quote."""
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=1,
        attendees=20,
        selections=[Selection(item_id="breakout-a", qty=1)],
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)

    assert quote.violations == []
    assert quote.total > 0
    warning = next(w for w in quote.warnings if w.code == "fnb_minimum_shortfall")
    assert "$5,000.00" in warning.message


def test_unknown_item_returns_a_suggestion(cascadia: HotelConfig) -> None:
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=2,
        attendees=10,
        selections=[Selection(item_id="std_room", qty=10)],  # underscore, not hyphen
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)
    violation = next(v for v in quote.violations if v.code == "unknown_item")
    assert violation.suggestion == "Did you mean 'std-room'?"


def test_short_lead_time_warns_but_still_quotes(cascadia: HotelConfig) -> None:
    request = QuoteRequest(
        arrival_date="2026-01-20",  # 5 days out, policy wants 14
        nights=2,
        attendees=10,
        selections=[Selection(item_id="std-room", qty=5)],
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)
    assert quote.violations == []
    assert quote.total > 0
    assert any(w.code == "short_lead_time" for w in quote.warnings)


# --- 6. unit semantics --------------------------------------------------


def test_per_person_item_bills_headcount_not_room_count(cascadia: HotelConfig) -> None:
    """A qty of 1 on a per-person dinner means 'one dinner each', not one dinner."""
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=1,
        attendees=40,
        selections=[
            Selection(item_id="breakout-a", qty=1),
            Selection(item_id="plated-dinner", qty=1),
        ],
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)
    dinner = next(line for line in quote.lines if line.item_id == "plated-dinner")
    assert dinner.qty == 40
    assert dinner.subtotal == 3_000.00


def test_duration_supplied_by_the_model_is_ignored(cascadia: HotelConfig) -> None:
    """The engine derives duration from the unit; the tool input is advisory."""
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=3,
        attendees=40,
        selections=[Selection(item_id="plated-dinner", qty=40, duration=3)],
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)
    dinner = quote.lines[0]
    assert dinner.duration == 1
    assert dinner.subtotal == 3_000.00              # not 9,000
    assert any(w.code == "duration_overridden" for w in quote.warnings)


def test_per_person_per_day_multiplies_both(cascadia: HotelConfig) -> None:
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=3,
        attendees=40,
        selections=[Selection(item_id="coffee-break", qty=40)],
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)
    assert quote.lines[0].subtotal == 2_160.00      # 40 × 18 × 3


def test_all_three_room_rules_stack_in_config_order(hacienda: HotelConfig) -> None:
    """40 casitas for 3 nights trips the comp, the volume tier and length of stay."""
    request = QuoteRequest(
        arrival_date="2026-06-08",
        nights=3,
        attendees=80,
        selections=[Selection(item_id="casita-king", qty=40)],
    )
    quote = QuoteCalculator(hacienda, today=TODAY).build(request)

    assert quote.subtotal == 31_080.00              # 40 × 259 × 3
    comp, volume, los = quote.adjustments
    assert comp.amount == -777.00                   # 1 casita × 259 × 3
    assert volume.amount == -2_424.24               # 8% of 30,303
    assert los.amount == -3_345.45                  # 12% of the remaining 27,878.76
    assert quote.net_subtotal == 24_533.31


def test_per_person_item_without_a_room_block_has_no_duration(
    cascadia: HotelConfig,
) -> None:
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=1,
        attendees=20,
        selections=[Selection(item_id="plated-dinner", qty=20)],
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)
    assert quote.lines[0].duration_label == "—"


def test_repeated_selections_of_one_item_are_merged(cascadia: HotelConfig) -> None:
    """Two half-blocks must reach the volume threshold as one 30-room booking."""
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=2,
        attendees=30,
        selections=[
            Selection(item_id="std-room", qty=15),
            Selection(item_id="std-room", qty=15),
        ],
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)

    assert len(quote.lines) == 1
    assert quote.lines[0].qty == 30
    assert quote.adjustments[0].amount == -1_134.00   # 10% of 11,340


def test_expanding_a_per_person_qty_is_stated_not_silent(
    cascadia: HotelConfig,
) -> None:
    request = QuoteRequest(
        arrival_date="2026-03-09",
        nights=1,
        attendees=40,
        selections=[Selection(item_id="plated-dinner", qty=1)],
    )
    quote = QuoteCalculator(cascadia, today=TODAY).build(request)
    warning = next(w for w in quote.warnings if w.code == "qty_expanded_to_headcount")
    assert "all 40 attendees" in warning.message
