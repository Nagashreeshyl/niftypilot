# M3.2: Fee evidence and contract-note checklist

Current state (from `risk/charges.py` and m3_1_fee_verification.md): **0 of 7 components
verified.** Nothing here changes that. It lists the evidence that would.

## 1. Outstanding fees and the evidence each needs

| Component | Model today | Status | Verifiable from an official published schedule? | Needs a real contract note? | Effective dates needed |
|---|---|---|---|---|---|
| Brokerage (Groww) | min(Rs 20, 0.1%); if < Rs 5: min(Rs 5, 2.5%) | current only | Partly: Groww's pricing page is the broker's own schedule, but has no dates | **Yes.** Minimum rule, per-order vs per-trade aggregation, rounding | Groww change history (a secondary source says the minimum went from Rs 2 to Rs 5 around June 2025; ask Groww) |
| STT (intraday, sell) | 0.025% of sell value | current only | **Yes:** Finance Act / Income Tax Dept STT rate table (incometaxindia.gov.in; returned 403 when fetched) | Yes, for **rounding** (rupee rounding is often stated; unconfirmed) | effective date of the 0.025% rate; any change in the period |
| NSE transaction charge | 0.00322% to 30 Sep 2024; 0.00297% from 1 Oct 2024 | secondary | **Yes:** NSE circular FA64232 (and earlier circulars for any earlier changes) | Yes, for how Groww passes it through and rounds it | full history for the backtest period |
| NSE IPFT | 0.0001% | current only | **Yes:** NSE circular introducing / revising IPFT | Yes, whether it is shown separately or inside transaction charges | start date |
| SEBI turnover fee | 0.0001% (Rs 10/crore) | current only | **Yes:** SEBI (Payment of Fees) Regulations and amendments (a secondary source reports Rs 5/crore from 1 Jun 2020 to 31 Mar 2021) | Yes, for rounding | full history |
| Stamp duty (intraday buy) | 0.003% from 1 Jul 2020 | secondary | **Yes:** Indian Stamp Act amendment (Finance Act 2019), Stamp Rules notification of 10 Dec 2019, deferral notification of 30 Mar 2020 | Yes, for rounding | confirmed 1 Jul 2020 start |
| GST | 18% of brokerage + exchange + SEBI + IPFT | current only | **Yes:** CBIC GST rate notification for stockbroking services | **Yes:** the base (which charges attract GST) and the CGST/SGST vs IGST split | stable over the period? confirm |
| Groww auto square-off charge | not modelled (we exit at 15:15) | n/a | Groww pricing page (Rs 50 + GST) | only if ever incurred | n/a |

**Verification rule (unchanged):** an entry becomes `VERIFIED` only when the official document
has been **read** and states both the rate and its effective dates. A matching contract note
confirms the rate *on that trade date* and the calculation method. It does not establish an
effective range.

## 2. Contract-note collection checklist

Collect **real Groww intraday (MIS) contract notes** for these cases, ideally one note per case:

- [ ] **Small intraday buy**: order value under Rs 1,000 (for example 5 shares of a Rs 150
      stock). Tests the "lower of Rs 5 or 2.5%" minimum.
- [ ] **Buy above Rs 20,000**: tests the Rs 20 brokerage cap (0.1% x 20,000 = Rs 20).
- [ ] **Intraday sell** (the closing leg of a round trip on the same day): tests STT (sell
      side) and the absence of stamp duty on the sell.
- [ ] **A note from before 1 Oct 2024**, if you have one: tests the old NSE rate (0.00322%).
- [ ] Optional: two orders in the same stock on one day (does brokerage apply per order or
      per trade?).
- [ ] Optional: a partial fill (one order, several trades).

### Keep visible (needed for reconciliation)

- Trade date; exchange (NSE); segment (equity / intraday)
- For each order or trade: symbol or ISIN, buy/sell, quantity, price, trade value, trade time
- Every charge line: brokerage, STT, exchange transaction charges, SEBI turnover fees, stamp
  duty, IPFT (if shown), CGST / SGST / IGST, and any other line
- Totals: net amount payable / receivable
- Any printed notes on rates or rounding
- The broker's name (Groww) and the contract-note date

### Remove or black out before sharing

- Your name, address, email, phone
- PAN
- **Client code / UCC / client ID**, demat (DP/BO) ID, bank account details
- Signature, QR codes or barcodes that encode account details
- Contract-note number and order/trade numbers: not needed for reconciliation. Remove them
  in anything shared; keep them only in your private copy.

### Never send or store (in chat, files or the repo)

Passwords, OTPs, PINs, TOTP secrets, API keys or secrets, access tokens, client IDs, login
links.

### How to hand it over

Transcribe the visible values into the JSON format in `backend/niftypilot/risk/reconcile.py`
and save it in `backend/tests/fixtures/contract_notes/` (git-ignored). Then run:

```bash
uv run python -m niftypilot.cli reconcile-contract-note backend/tests/fixtures/contract_notes/<file>.json
uv run pytest tests/test_contract_notes.py -rs
```

A mismatch is a finding: it identifies a wrong rate, base or rounding rule. It must not be
"fixed" by editing the note.
