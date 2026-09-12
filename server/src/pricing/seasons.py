"""Seasonal window matching for MM-DD rate overrides."""

from __future__ import annotations

from datetime import date


def season_matches(day: date, start: str, end: str) -> bool:
    """Is ``day`` inside the inclusive MM-DD window? Windows wrap: 12-20..01-05."""
    key = (day.month, day.day)
    lo = _parse(start)
    hi = _parse(end)
    if lo <= hi:
        return lo <= key <= hi
    return key >= lo or key <= hi


def _parse(value: str) -> tuple[int, int]:
    month, day = value.split("-")
    return int(month), int(day)
