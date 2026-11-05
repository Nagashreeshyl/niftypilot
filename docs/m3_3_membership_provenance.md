# M3.3: Point-in-time index membership provenance

Code: `backend/niftypilot/market_data/ingest.py` (`load_constituents_csv`,
`membership_conflicts`, `supersede_membership`, `constituents_on`), table
`constituent_history`, CLI `load-constituents` and `supersede-membership`.

## Record

| Column | Since | Notes |
|---|---|---|
| `instrument_id`, `index_name` | M1 | |
| `effective_from`, `effective_until` | M1 | effective dates, not announcement dates. `effective_until` is NULL for a current member. CHECK until >= from (named `ck_constituent_dates` since M3.3) |
| `source_document` | M3.3 | NSE Indices press release or circular reference |
| `source_published` | M3.3 | publication date of that document |
| `verification_status` | M3.3 | `unverified` (default), `verified`, or `superseded` |
| `verification_notes` | M3.3 | |

The CSV for `load-constituents` keeps its M1 columns. The new ones are optional columns:
`source_document,source_published,verification_status,verification_notes`.

## Rules

- **History is preserved.** Rows are never deleted. Old membership is never inferred from
  today's constituents: no code derives membership, it only reads loaded rows.
- **Contradictions are rejected.** Two active rows for the same instrument and index with
  overlapping dates:
  - at load time, the whole file is rolled back;
  - inserted any other way, they are a critical quality finding, `conflicting_membership`.
- **Explicit resolution:** `supersede-membership <id> --note "<which document shows it is
  wrong>"` marks the wrong row `superseded`. It stays in the table for audit and is ignored
  by every membership query. The note is required.
- **Missing provenance is identified.** In the checked period, rows without
  `source_document` are flagged as `membership_missing_source`:
  - **critical for actual datasets**;
  - a warning for synthetic ones.
  Rows with a source that aren't `verified` give the warning `unverified_membership`.
- **The universe is chosen on the simulated date.** `load_market(..., index_name)` builds
  `membership(d)` from active rows, and the engine calls it for each trading day (M3,
  unchanged). This is tested across an addition and removal on the same date.

## Not done

- A check that exactly 50 members exist on every date (m3_2_index_membership_sources.md,
  step 4). The test universes are small; add this when real membership is loaded.
- No real membership data was loaded.

## Tests

`tests/test_membership_provenance.py` covers:
- additions and removals, and historical membership with no inference;
- the universe chosen on the simulated date through `load_market`;
- missing and unverified provenance (actual vs synthetic);
- conflicting records rejected at load;
- an injected conflict that stays critical until explicitly superseded.
