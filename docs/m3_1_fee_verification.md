# M3.1: Fee verification

Status (2026-11-04): **no fee component is officially verified.** No real Groww contract note
has been supplied, and the official government and exchange documents could not be retrieved
(attempts below). Every backtest therefore reports net P&L as **not authoritative**. This was
not relabelled to look better.

## Components (as implemented in `risk/charges.py`)

Historical and current rates are separate entries with effective dates (`NSE_RATES`,
`BROKER_RATES`). `resolve(trade_date)` picks the entry covering that date.

| Component | Party | Side | Base | Formula | Rate / dates in model | Source | Status | Rounding |
|---|---|---|---|---|---|---|---|---|
| Brokerage | Groww (broker) | buy and sell, per executed order | order value | min(Rs 20, 0.1% x value); if < Rs 5, min(Rs 5, 2.5% x value) | current; start date unknown | Groww pricing and help pages (fetched 2026-11-04) | current only | model: paise; broker rule unverified |
| STT | Govt of India | sell only (intraday) | sell value | 0.025% | current; start date unknown | Groww pricing page | current only | model: paise; whether the official rule rounds to the rupee is **unverified** |
| NSE transaction charge | NSE | buy and sell | order value | 0.00322% until 2024-09-30; 0.00297% from 2024-10-01 | dated | Zerodha bulletin citing NSE circular FA64232 (not read); current rate on Groww page | secondary | model: paise |
| NSE IPFT | NSE | buy and sell | order value | 0.0001% | current; start date unknown | Groww pricing page | current only | model: paise |
| SEBI turnover fee | SEBI | buy and sell | order value | 0.0001% (Rs 10/crore) | current; start date unknown | Groww pricing page | current only | model: paise |
| Stamp duty | State (collected centrally) | buy only | buy value | 0.003% (non-delivery) | from 2020-07-01 | secondary sources (the parliamentary answer and gazette could not be fetched) | secondary | model: paise |
| GST | Govt of India | buy and sell | brokerage + exchange + SEBI + IPFT | 18% | current | Groww pricing page | current only | model: paise |

Status meanings: **verified** = an official document states the rate *and* its effective
dates (no entry qualifies). **Secondary** = the rate and dates come from a reputable
non-official source. **Current only** = a primary broker page confirms today's rate; its
start date is unknown. Only *verified* entries, or *current only* entries for trade dates on
or after 2026-11-04, count as authoritative.

### Known gaps from the 2026-11-04 search (secondary sources, not applied to the model)

- A secondary tracker says SEBI circular `SEBI/HO/MRD/TPD-1/P/CIR/2024/92` made exchange
  charges uniform from 2024-10-01.
- A forum thread says the SEBI fee was Rs 5/crore from 2020-06-01 to 2021-03-31 (a COVID-era
  reduction). If true, the model **overstates** SEBI fees slightly for that window.
- Stamp duty on *delivery* trades is 0.015% per secondary sources. This doesn't apply here
  (intraday only).

## Attempts to reach official documents (2026-11-04)

| Document | URL | Result |
|---|---|---|
| NSE circular FA64232 (transaction charges, Oct 2024) | nsearchives.nseindia.com/content/circulars/FA64232.pdf | timeout |
| NSE investor page on STT/SEBI fees/levies | nseindia.com/invest/first-time-investor-sebi-turnover-fees-stt-other-levies | timeout |
| Income Tax Dept STT rates (section 98) | incometaxindia.gov.in/w/section-98-55 | HTTP 403 |
| Rajya Sabha answer PQ 253 (stamp duty, 9 Mar 2021) | rsdebate.nic.in/.../PQ_253_09032021... | redirected to a generic page |
| SEBI fee structure page | sebi.gov.in/commreport/feestructure.html | reachable, but it is an old consultation proposal, not the current schedule |

## Contract-note reconciliation (built, waiting for input)

- `risk/reconcile.py` recomputes every charge for the orders on a note and compares each
  component the note states. The tolerance is Rs 0.01 per order.
- CLI: `uv run python -m niftypilot.cli reconcile-contract-note note.json`.
- `tests/test_contract_notes.py` reconciles every JSON file in
  `backend/tests/fixtures/contract_notes/`. **With no files it SKIPS and states that fee
  verification is blocked.**
- The other three tests in that file use *self-generated* notes built from our own model.
  They prove the comparison logic and verify no real fee.

What to provide: one or more real Groww **intraday** contract notes, ideally including:
- a small order (to test the 2.5% minimum);
- an order above Rs 20,000 (to test the Rs 20 cap);
- a sell (STT);
- a date before 2024-10-01 if available (old NSE rate).

Transcribe only printed values into the JSON format in `reconcile.py`, and remove your name,
client code, PAN and address. Those files are git-ignored.

Once notes reconcile, the matching rates can be marked as confirmed **for those trade dates**.
Effective *ranges* still need the official circulars. Mark an entry `VERIFIED` only when the
document has actually been read.

Sources: [Groww pricing](https://groww.in/pricing/stocks),
[Groww intraday charges](https://groww.in/help/stocks/sx-pricing/what-are-intraday-charges),
[Zerodha bulletin, 1 Oct 2024](https://zerodha.com/marketintel/bulletin/391488/revision-in-transactions-charges-from-1st-october-2024),
[SEBI fee structure page](https://www.sebi.gov.in/commreport/feestructure.html).
