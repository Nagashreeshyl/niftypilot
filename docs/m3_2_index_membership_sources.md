# M3.2: Point-in-time Nifty 50 membership sources

## What NiftyPilot needs (table `constituent_history`, loader `load-constituents`)

| Field | Required | Notes |
|---|---|---|
| Constituent trading symbol | yes | as of the membership period; renames handled through `instrument_aliases` |
| Instrument identifier | yes | ISIN, in instrument metadata |
| Effective start date | yes | first trading day as a member (inclusion effective date) |
| Effective end date | yes, if excluded | last trading day as a member (the day before the exclusion takes effect) |
| Inclusion / exclusion dates | yes | as stated in the announcement; announcement date and effective date are different things |
| Source document | yes | NSE Indices press release or NSE circular reference |
| Verification status | yes | `verified` only after the document has been read |

Note: the current CSV loader stores symbol and dates only. The **source document and
verification status are not yet columns**. Until they exist, they must be kept in a sidecar
evidence file, with one row per change citing the document (see the acquisition decision).

## Sources

**Official (primary)**
- **NSE Indices press releases** (niftyindices.com, "Press Release" section). Index
  reconstitutions (periodic reviews) and ad-hoc replacements (mergers, delistings, suspensions)
  are announced here with effective dates. Example official URLs found:
  [ind_prs19032020.pdf](https://niftyindices.com/Press_Release/ind_prs19032020.pdf), a
  replacement announced in March 2020; the content was not read in full.
- **NSE circulars** announcing index constituent changes (they matter for F&O). Example found:
  [FAOP34945.pdf](https://nsearchives.nseindia.com/content/circulars/FAOP34945.pdf),
  referring to a Nifty 50 exclusion effective 26 May 2017 (not read in full).
- **niftyindices.com historical reports / archives.** Forum users say constituent and
  index-change archives go back to around April 2013. Unverified; must be checked on the site.

**Secondary (cross-check only, never the sole source):** news coverage of reshuffles,
broker blogs, community-maintained circular mirrors.

**Not found:** an official, paid "constituent history" product with pricing. A forum answer
says vendors sell it; not confirmed. NSE Indices could be asked directly.

## Construction procedure

1. Get the official list of Nifty 50 constituents on the **first day of the backtest period**
   (an archived constituent file or the index factsheet for that date).
2. Collect every press release / circular announcing a Nifty 50 change between the start and
   end of the period: periodic reviews plus ad-hoc replacements.
3. For each change, record the excluded and included symbols, ISIN, effective date and
   document reference.
4. Replay the changes forward. At **every** date the member count must be exactly 50, and a
   test should assert this.
5. Cross-check against at least one later official snapshot (for example a monthly or
   semi-annual constituent file). Any mismatch means a change was missed.
6. Mark rows `verified` only when the underlying document has been read.

## Why survivorship bias matters here

Using today's constituents for 2020-2026 would:
- **drop stocks that left the index**, often after falling, being merged or being
  suspended, and with them their losing days and the gaps and crashes the ORB strategy would
  have traded or avoided;
- **add stocks before they qualified**, often after strong runs (that run is often why they
  were added), importing future success into the past;
- distort the liquidity scanner, which would rank stocks that were not liquid enough, or not
  members, at the time.

The direction of the bias is usually favourable to the backtest, so results look better than
reality. NiftyPilot guards against this in three ways:
- the quality report marks missing membership as **critical**;
- `member_without_data` warnings flag members with no candles;
- the universe is computed per day.

These guards only work if delisted members' candles are actually present in the data. That
is why provider question 6 (delisted instruments) is decisive.

Example to test any provider with: a constituent that left because of a merger, for example
HDFC Ltd, which merged into HDFC Bank in 2023 (general knowledge; confirm the exact dates
from the NSE Indices announcement). A provider that cannot supply its pre-merger 5m candles
cannot support a survivorship-free 2020-2023 backtest.
