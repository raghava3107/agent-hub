"""Timezone-aware display helpers. Everything the UI shows goes through here
so we never leak seconds or UTC-looking strings to the user."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # 3.8 fallback if someone downgrades
    ZoneInfo = None  # type: ignore


DEFAULT_TZ = os.environ.get("HUB_TIMEZONE", "Asia/Kolkata")


def get_display_tz():
    """Return the tzinfo we'll render times in."""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(DEFAULT_TZ)
        except Exception:  # noqa: BLE001
            pass
    # Best-effort fallback: fixed +05:30 for India.
    return timezone(timedelta(hours=5, minutes=30), name="IST")


DISPLAY_TZ = get_display_tz()


def _parse_iso_utc(s: str) -> Optional[datetime]:
    """Parse the ISO strings we store (`YYYY-MM-DDTHH:MM:SSZ` — see db.now())."""
    if not s:
        return None
    try:
        # Accept both trailing Z and offset-less strings (treat as UTC).
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1])
        else:
            dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_aware(s: Optional[object]) -> Optional[datetime]:
    """Coerce str/datetime → tz-aware UTC datetime."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    return _parse_iso_utc(str(s))


def _12h(dt: datetime) -> str:
    """12-hour '2:20 PM' with no leading zero on the hour."""
    # macOS/Linux support %-I to strip the leading zero. Fall back to lstrip
    # so we work on Windows too.
    try:
        return dt.strftime("%-I:%M %p")
    except ValueError:
        return dt.strftime("%I:%M %p").lstrip("0")


def fmt_ist(s: Optional[object]) -> str:
    """Return e.g. 'Jul 12, 2:20 PM IST'. Handles ISO strings and datetimes.
    No seconds ever. Empty on falsy input."""
    dt = _to_aware(s)
    if dt is None:
        return str(s) if s else ""
    local = dt.astimezone(DISPLAY_TZ)
    return f"{local.strftime('%b %d')}, {_12h(local)} {local.strftime('%Z')}"


def fmt_ist_date(s: Optional[object]) -> str:
    """Just the date. 'Jul 12'."""
    dt = _to_aware(s)
    if dt is None:
        return str(s) if s else ""
    return dt.astimezone(DISPLAY_TZ).strftime("%b %d")


def fmt_ist_time(s: Optional[object]) -> str:
    """Just time in 12-hour format. '2:20 PM'."""
    dt = _to_aware(s)
    if dt is None:
        return str(s) if s else ""
    return _12h(dt.astimezone(DISPLAY_TZ))


def rel_time(s: Optional[object]) -> str:
    """Human-friendly delta vs. now. 'in 25 min', '3 h ago', 'just now'."""
    if not s:
        return ""
    if isinstance(s, datetime):
        dt = s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    else:
        dt = _parse_iso_utc(str(s))
        if dt is None:
            return ""
    now = datetime.now(timezone.utc)
    delta = dt - now
    secs = int(delta.total_seconds())
    if abs(secs) < 45:
        return "just now"
    future = secs > 0
    secs = abs(secs)
    if secs < 60 * 60:
        m = max(1, secs // 60)
        return f"in {m} min" if future else f"{m} min ago"
    if secs < 60 * 60 * 24:
        h = secs // 3600
        mm = (secs % 3600) // 60
        base = f"{h} h" + (f" {mm} min" if mm else "")
        return f"in {base}" if future else f"{base} ago"
    d = secs // (60 * 60 * 24)
    return f"in {d} d" if future else f"{d} d ago"
