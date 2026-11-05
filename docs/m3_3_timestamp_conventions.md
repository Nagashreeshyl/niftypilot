# M3.3: Candle timestamp conventions

Code: `backend/niftypilot/market_data/provenance.py` (manifest import of actual data).

## Canonical representation

Each stored candle is keyed by its **start time in UTC**, which was already the M1-M3.1
convention. Sessions are interpreted in **Asia/Kolkata**:
- regular session 09:15-15:30, giving 75 five-minute candles labelled 09:15 to 15:25;
- India has no daylight saving, so IST is always UTC+05:30;
- every conversion goes through `zoneinfo`, never a hard-coded offset.

## Manifest fields (required)

| Field | Values | Rule |
|---|---|---|
| `source_timezone` | an IANA zone, such as `Asia/Kolkata` or `UTC` | must exist in tzdata. Placeholders and made-up names are rejected |
| `timestamp_convention` | `start` or `end` | **required, no default**. An undocumented convention cannot be imported |

## Conversion

1. Parse the provider's text with `timestamp_format`.
2. If the file has **no offset**, localise it to `source_timezone`. Ambiguous or nonexistent
   local times (possible only in zones with daylight saving) become NaT and are rejected.
3. If the file **has offsets**, every offset must equal the declared zone's offset at that
   instant. Otherwise the import is refused (`contradicts source_timezone`). Mixed offsets
   are refused too.
4. `end`: subtract one interval, so the candle labelled 09:20 becomes the 09:15 candle.
   Daily candles are not shifted.
5. Convert to UTC.
6. The provider's **original text** is kept in `candles.source_timestamp`. The convention and
   timezone are recorded per file in `datasets.manifest.files[]`.

## Protection against wrong declarations

**Alignment gate (whole file, before anything is inserted):**
- After conversion, the file is validated against the exchange calendar.
- **Any** row outside a documented session, or off the grid, refuses the whole manifest:
  "... Check timestamp_convention, source_timezone and the session calendar. Nothing
  imported."
- This prevents the dangerous case where a wrong declaration shifts every candle by 5
  minutes, still lands on the grid, gets stored, and leaves a second, shifted copy after a
  corrected re-import:
  - END data declared as START puts a candle at 15:30, which is outside the session;
  - START data declared as END puts one at 09:10;
  - IST data declared as UTC lands at 14:45 onward.

**One convention per dataset:**
- A second manifest for the same dataset must match the first's `source_timezone`,
  `timestamp_convention`, `interval` and `price_adjustment`.
- To import differently labelled data, use a new dataset.

**Duplicates (unchanged):**
- the unique key `(dataset, instrument, interval, timestamp_utc)`;
- identical duplicate rows collapse to one;
- conflicting duplicates are all rejected.

Consequence: a vendor file containing pre-open or post-close candles cannot be imported
as-is. That needs a deliberate, documented filter, which has not been built.

## Aggregation

`resample_candles` works on the canonical start-labelled candles, so 15-minute buckets are
identical whatever the source convention (tested). The 09:15 open is on the 15-minute grid.
Special-session windows that start on a 15-minute boundary aggregate the same way.

## Tests

`tests/test_timestamp_conventions.py` covers:
- start input (opening candle 09:15, closing candle 15:25, source text kept);
- end input converted to canonical start;
- a wrong convention refused in both directions, with nothing inserted;
- mixing conventions in one dataset refused;
- identical and conflicting duplicates;
- an end-labelled Muhurat session;
- incorrect timezone input (contradicting offsets, IST declared as UTC, a non-IANA zone,
  agreeing offsets accepted);
- an undeclared convention;
- 15-minute aggregation equal for start and end input.

Existing M3.1 manifest tests now declare `"timestamp_convention": "start"`.
