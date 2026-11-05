"""Exchange session calendar: regular sessions, holidays and documented non-regular sessions.

A day is tradable only if it is a weekday that is not a recorded holiday (regular session,
09:15-15:30 IST), or it has an `exchange_sessions` record (Muhurat / special / extended),
in which case its candles are valid ONLY inside the recorded windows. Nothing is assumed:
an unusual session without a record is never accepted.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from niftypilot.market_data.session import SESSION_CLOSE, SESSION_OPEN, window_slots
from niftypilot.models import SESSION_TYPES, VERIFICATION, ExchangeSession, TradingHoliday

PLACEHOLDERS = {"", "unknown", "n/a", "na", "none", "todo", "tbd", "-", "?"}


@dataclass(frozen=True)
class Window:
    open: time
    close: time
    kind: str  # "regular" or one of SESSION_TYPES
    verification: str = "verified"
    source: str = ""


REGULAR = Window(SESSION_OPEN, SESSION_CLOSE, "regular")


@dataclass
class ExchangeCalendar:
    holidays: set[date] = field(default_factory=set)
    special: dict[date, list[Window]] = field(default_factory=dict)  # documented sessions

    def windows(self, day: date) -> list[Window]:
        """Session windows of `day`; [] means the exchange was closed."""
        if day in self.special:
            return self.special[day]
        if day.weekday() >= 5 or day in self.holidays:
            return []
        return [REGULAR]

    def is_regular(self, day: date) -> bool:
        return self.windows(day) == [REGULAR]

    def slots(self, day: date, interval_minutes: int) -> list[datetime]:
        return [
            s
            for w in self.windows(day)
            for s in window_slots(day, w.open, w.close, interval_minutes)
        ]


def load_calendar(db: Session, start: date, end: date, exchange: str = "NSE") -> ExchangeCalendar:
    hol = set(
        db.scalars(
            select(TradingHoliday.holiday_date).where(
                TradingHoliday.exchange == exchange,
                TradingHoliday.holiday_date >= start,
                TradingHoliday.holiday_date <= end,
            )
        )
    )
    special: dict[date, list[Window]] = {}
    for r in db.scalars(
        select(ExchangeSession)
        .where(
            ExchangeSession.exchange == exchange,
            ExchangeSession.session_date >= start,
            ExchangeSession.session_date <= end,
        )
        .order_by(ExchangeSession.session_date, ExchangeSession.open_time)
    ):
        special.setdefault(r.session_date, []).append(
            Window(r.open_time, r.close_time, r.session_type, r.verification_status, r.source)
        )
    return ExchangeCalendar(hol, special)


def load_sessions_csv(db: Session, path: str | Path, exchange: str = "NSE") -> int:
    """Columns: date,session_type,open_time,close_time,source[,verification_status,notes].

    Times are Asia/Kolkata HH:MM. `source` must cite the NSE circular. Windows on one date
    must not overlap. All rows are validated before any is stored."""
    df = pd.read_csv(path, dtype=str).fillna("")
    rows = []
    for r in df.itertuples():
        kind = r.session_type.strip().lower()
        status = (getattr(r, "verification_status", "") or "unverified").strip().lower()
        if kind not in SESSION_TYPES:
            raise ValueError(
                f"row {r.Index}: session_type {r.session_type!r} not in {SESSION_TYPES}"
            )
        if status not in VERIFICATION:
            raise ValueError(f"row {r.Index}: verification_status {status!r} not in {VERIFICATION}")
        if r.source.strip().lower() in PLACEHOLDERS:
            raise ValueError(f"row {r.Index}: a session needs a source document")
        o, c = time.fromisoformat(r.open_time), time.fromisoformat(r.close_time)
        if c <= o:
            raise ValueError(f"row {r.Index}: close {c} not after open {o}")
        rows.append(
            ExchangeSession(
                exchange=exchange,
                session_date=date.fromisoformat(r.date),
                session_type=kind,
                open_time=o,
                close_time=c,
                source=r.source.strip(),
                verification_status=status,
                notes=getattr(r, "notes", "") or None,
            )
        )
    by_day: dict[date, list] = {}
    for x in [
        *db.scalars(select(ExchangeSession).where(ExchangeSession.exchange == exchange)),
        *rows,
    ]:
        by_day.setdefault(x.session_date, []).append(x)
    for d, ws in by_day.items():
        ws.sort(key=lambda w: w.open_time)
        for a, b in zip(ws, ws[1:], strict=False):
            if b.open_time < a.close_time:
                raise ValueError(
                    f"{d}: overlapping session windows {a.open_time} and {b.open_time}"
                )
    db.add_all(rows)
    db.commit()
    return len(rows)
