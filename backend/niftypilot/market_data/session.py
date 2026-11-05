"""NSE regular cash-equity session: 09:15-15:30 Asia/Kolkata.

Holidays are NOT bundled (we do not invent a calendar); callers pass the official NSE list.
Non-regular sessions (Muhurat, special, extended) live in market_data/calendar.py.
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


def to_ist(ts: datetime) -> datetime:
    return ts.astimezone(IST)


def session_date(ts: datetime) -> date:
    return to_ist(ts).date()


def is_trading_day(day: date, holidays: Iterable[date] = ()) -> bool:
    return day.weekday() < 5 and day not in set(holidays)


def session_slots(day: date, interval_minutes: int = 5) -> list[datetime]:
    """UTC start times of every candle in the regular session for `day`."""
    return window_slots(day, SESSION_OPEN, SESSION_CLOSE, interval_minutes)


def window_slots(day: date, open_: time, close: time, interval_minutes: int) -> list[datetime]:
    """UTC start times of the full candles inside [open_, close) IST on `day`."""
    start = datetime.combine(day, open_, IST)
    end = datetime.combine(day, close, IST)
    step = timedelta(minutes=interval_minutes)
    slots, t = [], start
    while t + step <= end:
        slots.append(t.astimezone(UTC))
        t += step
    return slots
