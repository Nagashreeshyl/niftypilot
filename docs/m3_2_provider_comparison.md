# M3.2: Historical data provider comparison

Researched 2026-11-04 via web search and direct page fetches. **Confirmed** = read on the
provider's own site or official docs that day. **Unverified** = secondary sources, forums,
SDK descriptions, or not found. Nothing below has been tested against real data. No account
was opened and no API was called.

## Comparison

| Provider | Historical 5m coverage | 5m data | Nifty 50 index | Bulk / API | Licence clarity for local storage + research | Pricing | Docs | Recommendation | Main unverified questions |
|---|---|---|---|---|---|---|---|---|---|
| **Groww Trade API** (Backtesting API) | Confirmed: equities and indices "available from 2020" | Confirmed (`5minute`) | Confirmed: `NSE-NIFTY` listed | REST; 5m capped at 30 days/request (about 80 requests per instrument for 6.5 yr) | **Low.** Terms IV.2 forbid copying or entering platform information "into a database" except personal non-commercial use; XI.1.1 limits API use to automated trading | Confirmed: Backtesting excluded from free tier; Rs 499/mo + tax or Rs 4,999/yr + tax | Good, official | **Conditional:** shortlist; needs written licence clarification | timezone; start vs end labelling; adjustment; delisted/removed constituents; start month in 2020; rate limits; pre-open in 09:15 candle |
| **Zerodha Kite Connect** | Unverified ("several years" per docs; no date) | Confirmed (`5minute`) | Unverified for history (docs silent) | REST; per-request limit not stated | Unknown (terms not reviewed) | Confirmed: Rs 500/mo per API key incl. historical data (GST not stated); historical add-on removed 2025-02-08 | Good, official | Insufficient evidence | depth; delisted; adjustment; terms; needs a Zerodha account |
| **Upstox API** (Historical Candle V3) | Confirmed: minutes "available from January, 2022" | Confirmed (1-300 min) | Indices supported (index keys shown) | REST; 1-month max per request for 1-15 min intervals | Unknown | Unverified (forum, 2024: "free as of now") | Good, official | Insufficient evidence; coverage < 5 yr | pricing; terms; delisted; adjustment; needs an Upstox account |
| **DhanHQ** | Confirmed (support FAQ): intraday "last 5 years" | Confirmed (1, 5, 15, 25, 60 min) | Not stated | REST; about 90 days/request (unofficial SDK) | Unknown | Confirmed: Data API Rs 499/mo + tax (page doesn't say historical is included; a forum post says it is) | Official FAQ; API reference not read | Insufficient evidence | index coverage; terms; delisted; adjustment; needs a Dhan account |
| **Angel One SmartAPI** | Unverified | Unverified | Unverified | Forum: about 500 candles/request; limits conflict | Unknown | Unverified | Docs not read | Insufficient evidence | everything |
| **ICICI Direct Breeze** | Unverified (SDK claims 10 years incl. 1-second) | Unverified | Unverified | API | Unknown | Unverified (forum: free with an ICICI account) | SDK only | Insufficient evidence | everything; data quality concerns reported on forums |
| **NSE Data & Analytics** (exchange subsidiary) | Unverified (sells historical data; scope by enquiry) | Unverified | NSE Indices data via NSE | Delivered files, by request (marketdata@nse.co.in) | **Expected high** (formal exchange licence), but terms not seen | Unverified: 2026 pricing PDFs exist but timed out | Official; product detail by enquiry | **Conditional:** request a quote as the licence-clean reference | product granularity (5m or 1m/tick to resample); price; licence terms for individuals |
| **TrueData** | Unverified | Unverified (historical REST documented in SDK) | Unverified | REST | Unknown | By quote (own page) | Vendor site | Insufficient evidence | historical licence; depth; price |
| **Global Datafeeds** | Unverified (third-party pages conflict; focus on F&O) | Unverified | Unverified | Daily CSV files (per third-party) | Unknown | By quote | Vendor site not read | Insufficient evidence | cash-equity history depth; licence |

Not recommended and excluded: scraping nseindia.com or charting sites; NSE "undocumented"
endpoints that need session cookies; unofficial SDKs that call undocumented broker endpoints
(for example pyGrowwAPI, whose own disclaimer says it uses undocumented endpoints); free
Kaggle-style datasets without a licence or provenance.

## Confirmed facts and where they came from

- **Groww**
  - [Changelog](https://groww.in/trade-api/docs/curl/changelog): Historical Data APIs, 3 Oct
    2025, "from 2020 onwards", 1 min to 1 month.
  - [Backtesting docs](https://groww.in/trade-api/docs/curl/backtesting):
    `GET /v1/historical/candles`. Up to 5-minute intervals: 30 days per request. Equities,
    indices and FNO from 2020; `NSE-NIFTY` index symbol. Candles are
    [timestamp, open, high, low, close, volume, OI]. Timestamp format "yyyy-MM-dd HH:mm:ss",
    **timezone not stated**. Adjustment and rate limits not stated.
  - [Old historical-data endpoint](https://groww.in/trade-api/docs/curl/historical-data):
    `/v1/historical/candle/range` is **deprecated**, and minutes go back only "last 3 months".
    Do not use it.
  - [Groww blog, 30 Sep 2026](https://groww.in/blog/api-trading-on-groww): the free trial
    excludes Live data and Backtesting APIs. Rs 499/month + taxes, or Rs 4,999/year + taxes.
  - [Terms](https://groww.in/terms-and-conditions/):
    - IV.2: "You shall not copy, reproduce, sell, redistribute, publish, reverse engineer,
      enter into a database, ... any part of any information, content, materials, services
      available from or through the Platform, except that You may download the Platform for
      Your own personal, internal, non-commercial use".
    - XI.1.1: "use API connectivity only for implementing automated trading systems ...".
    - XI.1.2: algo use for yourself or your family only.
    - No clause specifically covering historical market data or backtesting was found.
- **Zerodha**
  - [Historical API docs](https://kite.trade/docs/connect/v3/historical/): 5-minute interval
    exists. The example timestamp `2017-12-15T09:15:00+0530` shows IST and the first candle at
    09:15, which suggests start labelling (inference).
  - [Zerodha support](https://support.zerodha.com/category/trading-and-markets/kite-web-and-mobile/kite-api/articles/how-do-i-sign-up-for-kite-connect):
    Connect costs Rs 500/month per API key and includes historical data.
  - [Forum announcement](https://kite.trade/forum/discussion/14806/historical-data-is-now-free):
    the add-on fee was removed from 8 Feb 2025.
- **Upstox** [Historical Candle V3](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data):
  minutes and hours available from January 2022; days from January 2000. Requests are limited
  to 1 month for 1-15 minute intervals.
- **Dhan** [support FAQ](https://dhan.co/support/platforms/dhanhq-api/what-timeframe-data-is-available-through-dhan-s-historical-data-apis/):
  intraday OHLCV for the last 5 years in 1/5/15/25/60-minute candles.
  [Subscription page](https://dhan.co/support/platforms/dhanhq-api/how-does-the-dhanhq-data-api-subscription-work/):
  Rs 499 + taxes per month.
- **NSE** [Data & Analytics](https://www.nseindia.com/static/nse-data-and-analytics): data
  products, including historical, are sold by NSE Data & Analytics Ltd (NSE subsidiary);
  enquiries to marketdata@nse.co.in.

## Questions to put to any shortlisted provider (in writing)

1. Does the licence permit downloading, storing locally and automatically processing
   historical candles for **personal, non-commercial research and backtesting**? No
   redistribution would take place.
2. What timezone are timestamps in, and is each timestamp the candle **start** or **end**?
3. Are pre-open trades included in the 09:15 candle? Are post-close session trades excluded?
4. Are prices and volumes adjusted for splits and bonuses? Can raw (unadjusted) data be
   supplied?
5. Are intervals with no trades omitted or forward-filled?
6. Can you supply candles for **delisted, merged or renamed** instruments (for example a
   former Nifty 50 constituent that no longer trades), under their historical symbols?
7. What is the exact earliest available date for 5-minute data, per instrument?
8. Rate limits, and terms for re-downloading after the subscription ends.
9. How are Muhurat or special sessions and extended-hours days represented?
