"""Pre-backtest data-quality report. A dataset with any CRITICAL finding may not be backtested.

CRITICAL: actual data without declared provenance; synthetic instruments inside an actual
  dataset; ingestion rejected rows; candles on holidays/weekends; invalid stored candles;
  overnight gaps beyond `gap_threshold` when price adjustment is unknown (possible
  unadjusted split/bonus); no point-in-time index membership covering the period;
  overlapping (contradictory) membership records; ACTUAL data only: a calendar year in the
  period with no holiday records, membership rows without a source document.
WARNING: missing candles or whole sessions (those sessions become DATA_INVALID in the
  strategy, never filled); index members without data (possible rename); zero volume
  (stocks only); outliers; no holiday calendar loaded;
  overnight gaps in datasets declared adjusted/unadjusted; candles in documented
  non-regular sessions (accepted, never traded); unverified session/corporate-action/
  membership records; gaps explained by a recorded corporate action.
"""

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from niftypilot.market_data.calendar import load_calendar
from niftypilot.market_data.corporate_actions import actions_by_symbol
from niftypilot.market_data.ingest import (
    active_membership,
    constituents_on,
    holidays,
    load_candles,
    membership_conflicts,
)
from niftypilot.market_data.session import IST
from niftypilot.market_data.validation import validate_candles
from niftypilot.models import Candle, ConstituentHistory, Dataset, IngestionRun, Instrument


@dataclass
class Finding:
    kind: str
    detail: str
    instrument: str | None = None
    count: int = 1
    examples: list[str] = field(default_factory=list)


@dataclass
class QualityReport:
    dataset_id: int
    start: str
    end: str
    status: str  # passed | warnings | failed
    critical: list[Finding]
    warnings: list[Finding]
    instruments_checked: int

    def to_dict(self) -> dict:
        return asdict(self)


def utc_range(start: date, end: date) -> tuple[datetime, datetime]:
    """[start 00:00 IST, end+1 00:00 IST) in UTC."""
    a = datetime.combine(start, time(0), IST).astimezone(UTC)
    return a, datetime.combine(end + timedelta(days=1), time(0), IST).astimezone(UTC)


def check_dataset(
    db: Session,
    dataset: Dataset,
    start: date,
    end: date,
    index_name: str | None = None,
    gap_threshold: float = 0.20,
) -> QualityReport:
    crit: list[Finding] = []
    warn: list[Finding] = []
    actual = dataset.data_kind == "actual"
    cal = load_calendar(db, start, end)
    hol = cal.holidays
    if not hol:
        warn.append(
            Finding(
                "no_holiday_calendar",
                "no NSE holidays loaded for the period; weekday calendar used",
            )
        )
    if actual:  # never assume every weekday traded: each year needs the official circular
        # shortcut: "a year has >= 1 holiday row" stands in for "that year's circular loaded";
        # upgrade to a per-year calendar declaration if partial loads become a problem.
        span = holidays(db, date(start.year, 1, 1), date(end.year, 12, 31))
        years = sorted(set(range(start.year, end.year + 1)) - {d.year for d in span})
        if years:
            crit.append(
                Finding(
                    "calendar_year_missing",
                    "actual data: no NSE holiday records for these years",
                    count=len(years),
                    examples=[str(y) for y in years[:3]],
                )
            )
    unverified = sorted(
        str(d) for d, ws in cal.special.items() if any(w.verification != "verified" for w in ws)
    )
    if unverified:
        warn.append(
            Finding(
                "unverified_session_record",
                "session record not verified",
                None,
                len(unverified),
                unverified[:3],
            )
        )

    rejected = db.scalars(
        select(IngestionRun).where(
            IngestionRun.dataset_id == dataset.id, IngestionRun.rows_rejected > 0
        )
    ).all()
    for r in rejected:
        crit.append(
            Finding(
                "ingestion_rejected_rows",
                f"ingestion run {r.id} rejected {r.rows_rejected} rows",
                count=r.rows_rejected,
                examples=[f"{e['kind']}@{e['timestamp']}" for e in r.errors[:3]],
            )
        )

    if index_name and not constituents_on(db, index_name, start):
        crit.append(
            Finding(
                "no_point_in_time_membership",
                f"no {index_name} membership rows cover {start}: universe would "
                "suffer survivorship bias",
            )
        )
    if index_name:
        for a, b in membership_conflicts(db, index_name):
            crit.append(
                Finding(
                    "conflicting_membership",
                    f"records {a.id} and {b.id} overlap; supersede one explicitly",
                    db.get(Instrument, a.instrument_id).trading_symbol,
                )
            )
        rows = db.scalars(
            select(ConstituentHistory).where(
                active_membership(index_name),
                ConstituentHistory.effective_from <= end,
                (ConstituentHistory.effective_until.is_(None))
                | (ConstituentHistory.effective_until >= start),
            )
        ).all()
        no_src = [r for r in rows if not (r.source_document or "").strip()]
        if no_src:
            (crit if actual else warn).append(
                Finding(
                    "membership_missing_source",
                    "membership rows without a source document",
                    count=len(no_src),
                    examples=[str(r.id) for r in no_src[:3]],
                )
            )
        unver = [r for r in rows if r.verification_status != "verified" and r not in no_src]
        if unver:
            warn.append(
                Finding(
                    "unverified_membership",
                    "membership rows not verified",
                    count=len(unver),
                    examples=[str(r.id) for r in unver[:3]],
                )
            )

    a, b = utc_range(start, end)
    inst_ids = db.scalars(
        select(Candle.instrument_id)
        .distinct()
        .where(
            Candle.dataset_id == dataset.id,
            Candle.interval == "5m",
            Candle.timestamp_utc >= a,
            Candle.timestamp_utc < b,
        )
    ).all()
    weekdays = {start + timedelta(days=i) for i in range((end - start).days + 1)}
    trading_days = {d for d in weekdays if cal.is_regular(d)}
    actions = actions_by_symbol(db)

    if dataset.data_kind == "actual" and dataset.provenance_status != "declared":
        crit.append(Finding("missing_provenance", "actual data without declared provenance"))
    have_days: dict[str, set] = {}
    for iid in sorted(inst_ids):
        inst = db.get(Instrument, iid)
        sym = inst.trading_symbol
        if dataset.data_kind == "actual" and inst.exchange == "SYNTH":
            crit.append(Finding("synthetic_instrument_in_actual_dataset", "SYNTH exchange", sym))
        df = load_candles(db, dataset.id, iid, "5m", a, b)
        days = set(df.timestamp_utc.dt.tz_convert(IST).dt.date)
        have_days[sym] = days
        bad_days = sorted(d for d in days if not cal.windows(d))
        if bad_days:
            crit.append(
                Finding(
                    "candles_on_non_trading_day",
                    "candles on holiday/weekend with no documented session",
                    sym,
                    len(bad_days),
                    [str(d) for d in bad_days[:3]],
                )
            )
        missing_days = sorted(
            d for d in trading_days if min(days) <= d <= max(days) and d not in days
        )
        if missing_days:
            warn.append(
                Finding(
                    "missing_session",
                    "trading day without candles",
                    sym,
                    len(missing_days),
                    [str(d) for d in missing_days[:3]],
                )
            )

        sp_days = sorted(d for d in days if d in cal.special)
        if sp_days:
            warn.append(
                Finding(
                    "special_session_candles",
                    "candles in documented non-regular sessions (validated against their "
                    "windows; excluded from trading and history)",
                    sym,
                    len(sp_days),
                    [f"{d} {cal.special[d][0].kind}" for d in sp_days[:3]],
                )
            )
        _, issues = validate_candles(df, calendar=cal)
        counts = Counter(
            i.kind for i in issues if not (i.kind == "zero_volume" and inst.segment == "INDEX")
        )
        for i in issues:
            if i.rejected:
                crit.append(Finding("invalid_stored_candle", i.detail, sym, 1, [str(i.timestamp)]))
        for kind, n in sorted(counts.items()):
            if not any(i.rejected for i in issues if i.kind == kind):
                ex = [str(i.timestamp) for i in issues if i.kind == kind][:3]
                warn.append(Finding(kind, f"{n} flagged", sym, n, ex))

        daily = df.groupby(df.timestamp_utc.dt.tz_convert(IST).dt.date).agg(
            open=("open", "first"), close=("close", "last")
        )
        gap = (daily.open / daily.close.shift(1) - 1).abs()
        big = gap[gap > gap_threshold]
        explained = {a.ex_date for a in actions.get(sym, []) if a.policy != "no_price_effect"}
        if big.index.isin(list(explained)).any():
            ok = big[big.index.isin(list(explained))]
            warn.append(
                Finding(
                    "corporate_action_gap",
                    "gap on a recorded corporate-action ex-date",
                    sym,
                    len(ok),
                    [f"{d}: {g:.1%}" for d, g in ok.head(3).items()],
                )
            )
            big = big[~big.index.isin(list(explained))]
        acts = [a for a in actions.get(sym, []) if start <= a.ex_date <= end]
        if any(a.verification != "verified" for a in acts):
            warn.append(
                Finding(
                    "unverified_corporate_action",
                    "corporate action not verified",
                    sym,
                    len(acts),
                    [f"{a.ex_date} {a.action_type}" for a in acts[:3]],
                )
            )
        if len(big):
            f = Finding(
                "possible_corporate_action",
                f"overnight gap > {gap_threshold:.0%}; price_adjustment={dataset.price_adjustment}",
                sym,
                len(big),
                [f"{d}: {g:.1%}" for d, g in big.head(3).items()],
            )
            (crit if dataset.price_adjustment == "unknown" else warn).append(f)

    if index_name:  # members with no data: unmapped rename, suspension or vendor gap
        gaps: dict[str, list[date]] = {}
        for d in sorted(trading_days):
            for inst in constituents_on(db, index_name, d):
                if d not in have_days.get(inst.trading_symbol, set()):
                    gaps.setdefault(inst.trading_symbol, []).append(d)
        for sym, ds in sorted(gaps.items()):
            warn.append(
                Finding(
                    "member_without_data",
                    "index member has no candles on trading days (rename? suspension? vendor gap?)",
                    sym,
                    len(ds),
                    [str(x) for x in ds[:3]],
                )
            )

    status = "failed" if crit else ("warnings" if warn else "passed")
    report = QualityReport(dataset.id, str(start), str(end), status, crit, warn, len(inst_ids))
    dataset.quality_status = status
    dataset.quality_report = report.to_dict()
    dataset.quality_checked_at = datetime.now(UTC)
    db.commit()
    return report
