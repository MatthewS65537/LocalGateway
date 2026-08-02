"""Time-based routing logic — pure, no I/O.

Provides helpers to match the current time against per-model TimeSlot rules
and filter backend candidates accordingly.
"""
from __future__ import annotations

import datetime as _dt

from .config import TimeSlot, TimeRoutingConfig


def current_time_in_tz(tz_name: str) -> _dt.datetime:
    """Return the current wall-clock time in *tz_name* (IANA string)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except (ImportError, KeyError):
        # Fallback: unknown tz → treat as UTC
        tz = _dt.timezone.utc
    return _dt.datetime.now(tz=tz)


def _hour_in_range(hour: int, start: int, end: int) -> bool:
    """Check whether *hour* falls within [start, end] (wrapping at midnight)."""
    if start <= end:
        return start <= hour <= end
    # Overnight span, e.g. start=22 end=6 → hours 22,23,0,1,2,3,4,5,6
    return hour >= start or hour <= end


def slot_matches(slot: TimeSlot, now: _dt.datetime) -> bool:
    """Return ``True`` if *now* falls within the slot's active window."""
    if not slot.enabled:
        return False
    if slot.days_of_week and now.weekday() not in slot.days_of_week:
        return False
    return _hour_in_range(now.hour, slot.start_hour, slot.end_hour)


def _to_tz(now: _dt.datetime, tz_name: str) -> _dt.datetime:
    """Convert *now* to *tz_name*.  If *now* is naive it is assumed UTC."""
    try:
        from zoneinfo import ZoneInfo
        target = ZoneInfo(tz_name)
    except (ImportError, KeyError):
        target = _dt.timezone.utc
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    return now.astimezone(target)


def get_active_slot(
    slots: list[TimeSlot],
    tz_name: str,
    now: _dt.datetime | None = None,
) -> TimeSlot | None:
    """Return the first matching **enabled** slot, or ``None``.

    *now* (if provided) is converted to *tz_name* before checking.
    """
    if now is None:
        now = current_time_in_tz(tz_name)
    else:
        now = _to_tz(now, tz_name)
    for slot in slots:
        if slot_matches(slot, now):
            return slot
    return None


def filter_backends_for_slot(
    backends: list[tuple[str, str]],  # list of (provider_id, model) tuples
    slot: TimeSlot,
) -> list[tuple[str, str]]:
    """Keep only backends whose ``provider:model`` key is in the slot's
    ``active_providers`` whitelist.  An empty whitelist means all pass."""
    if not slot.active_providers:
        return list(backends)
    allowed = set(slot.active_providers)
    return [b for b in backends if f"{b[0]}:{b[1]}" in allowed]
