"""
Calendar context module — holiday awareness, day-of-week and hour-of-day profiling.

Not a sidecar. Imported by stl_service.py and available for any Python component.
Configure via environment variables:
  HOLIDAY_COUNTRY       ISO 3166-1 alpha-2 country code (default: IN for India)
  HOLIDAY_SUBDIVISION   Province/state code (optional, e.g. "DL" for Delhi)
"""

import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

try:
    import holidays as _holidays_lib
    _HOLIDAYS_AVAILABLE = True
except ImportError:
    _HOLIDAYS_AVAILABLE = False

# ── Configuration ─────────────────────────────────────────────────────────────

HOLIDAY_COUNTRY      = os.getenv("HOLIDAY_COUNTRY", "IN")
HOLIDAY_SUBDIVISION  = os.getenv("HOLIDAY_SUBDIVISION", None)

# Pre-build calendar covering ±3 years from now (avoids rebuilding per call)
def _build_calendar():
    if not _HOLIDAYS_AVAILABLE:
        return {}
    now = datetime.now(timezone.utc)
    years = list(range(now.year - 1, now.year + 4))
    try:
        if HOLIDAY_SUBDIVISION:
            return _holidays_lib.country_holidays(
                HOLIDAY_COUNTRY, subdiv=HOLIDAY_SUBDIVISION, years=years
            )
        return _holidays_lib.country_holidays(HOLIDAY_COUNTRY, years=years)
    except Exception:
        return {}

_CALENDAR = _build_calendar()


def get_calendar_context(ts: Optional[datetime] = None) -> dict:
    """
    Return a rich calendar context dict for the given timestamp.
    Defaults to current UTC time if ts is None.

    Keys:
      day_of_week          : "Monday" … "Sunday"
      hour_of_day          : 0-23
      is_weekend           : bool
      is_holiday           : bool
      holiday_name         : str or None
      is_day_before_holiday: bool
      is_day_after_holiday : bool
      week_of_month        : 1-5
      next_holiday         : {"name": str, "date": "YYYY-MM-DD", "days_away": int} or None
    """
    if ts is None:
        ts = datetime.now(timezone.utc)

    d = ts.date()

    is_holiday = d in _CALENDAR
    holiday_name = _CALENDAR.get(d)

    tomorrow = d + timedelta(days=1)
    yesterday = d - timedelta(days=1)
    is_day_before_holiday = tomorrow in _CALENDAR
    is_day_after_holiday = yesterday in _CALENDAR

    next_holiday = _find_next_holiday(d)

    return {
        "day_of_week":           ts.strftime("%A"),
        "hour_of_day":           ts.hour,
        "is_weekend":            ts.weekday() >= 5,
        "is_holiday":            is_holiday,
        "holiday_name":          holiday_name,
        "is_day_before_holiday": is_day_before_holiday,
        "is_day_after_holiday":  is_day_after_holiday,
        "week_of_month":         (d.day - 1) // 7 + 1,
        "next_holiday":          next_holiday,
    }


def _find_next_holiday(from_date: date) -> Optional[dict]:
    """Return the next upcoming holiday on or after from_date."""
    if not _CALENDAR:
        return None
    upcoming = sorted(
        (hdate for hdate in _CALENDAR.keys() if hdate >= from_date),
        key=lambda x: x
    )
    if not upcoming:
        return None
    h = upcoming[0]
    return {
        "name":      _CALENDAR[h],
        "date":      h.isoformat(),
        "days_away": (h - from_date).days,
    }


def format_calendar_prompt_section(ctx: dict) -> str:
    """Format calendar context as a Brain 2 prompt section."""
    lines = [
        "## Calendar Context",
        "",
        f"- **Day**: {ctx['day_of_week']}, hour {ctx['hour_of_day']:02d}:00 UTC",
    ]
    if ctx["is_holiday"] and ctx["holiday_name"]:
        lines.append(f"- **Holiday**: {ctx['holiday_name']} — traffic may be below normal")
    elif ctx["is_weekend"]:
        lines.append("- **Weekend**: expect lower traffic than weekday baseline")
    else:
        lines.append("- **Weekday**: normal weekday traffic patterns apply")

    if ctx["is_day_before_holiday"]:
        lines.append("- **Day before holiday**: operators wrapping up; traffic may tail off")
    if ctx["is_day_after_holiday"]:
        lines.append("- **Day after holiday**: return-to-work surge possible")

    if ctx["next_holiday"]:
        nh = ctx["next_holiday"]
        if nh["days_away"] == 0:
            pass  # already covered by is_holiday
        elif nh["days_away"] <= 7:
            lines.append(f"- **Upcoming holiday**: {nh['name']} in {nh['days_away']} day(s) ({nh['date']})")

    lines += [
        "",
        "When contextualizing this anomaly, consider whether the observed deviation",
        "is consistent with the current time window. A metric spike during a known",
        "peak hour (9am weekday) requires stronger deviation to be significant than",
        "the same spike at 3am or on a public holiday.",
        "",
    ]
    return "\n".join(lines)
