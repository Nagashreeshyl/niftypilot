# M3.1: Execution sensitivity

Status: **the tool is built and tested. The only run so far is on SYNTHETIC random-walk data,
which shows how the mechanics behave. It is not evidence about the strategy.** A real run
needs real data (see m3_1_data_validation.md).

## Method

`backtest/sensitivity.py` runs the unchanged strategy and risk engine under a **fixed,
pre-declared** list of execution assumptions. The list is not tuned: scenarios may be added,
never removed. Only `ExecutionModel` fields vary. Risk limits are untouched. Risk sizing
automatically uses each scenario's spread + slippage, so worse costs also mean smaller or
rejected positions.

| Scenario | Half-spread | Slippage per fill | Entry delay |
|---|---|---|---|
| optimistic | 0.01% | 0.02% | 1 candle |
| baseline (defaults) | 0.025% | 0.05% | 1 |
| wide_spread | 0.05% | 0.05% | 1 |
| very_wide_spread | 0.10% | 0.05% | 1 |
| high_slippage | 0.025% | 0.10% | 1 |
| very_high_slippage | 0.025% | 0.20% | 1 |
| delay_2_bars | 0.025% | 0.05% | 2 |
| delay_3_bars | 0.025% | 0.05% | 3 |
| pessimistic | 0.05% | 0.10% | 2 |

Definitions:
- **Entry delay N:** the order fills at the open of the Nth candle after the signal candle.
  It is unfilled if that open is above the limit (signal entry + 0.1R).
- **Spread cost:** half-spread x reference price x qty, charged on market and stop fills.
  Target limit fills pay none.
- **Slippage cost:** the rest of the adverse fill, including tick rounding.
- **Gross P&L** already contains spread and slippage, because fills are adverse. Net = gross -
  charges.

Every row reports: setups, trades, rejected/unfilled orders, gross, charges, spread, slippage,
net, expectancy (Rs and R), profit factor and max drawdown.

Run (refused if the period touches a sealed OOS):
```bash
uv run python -m niftypilot.cli sensitivity --dataset <name> --start ... --end ... \
    --benchmark "NIFTY 50" --variants A,B,C,D,E --capitals 5000,10000,25000,50000,100000
```

## Synthetic run (mechanics only)

8 synthetic symbols + a synthetic index, 2024-03-01..2024-10-28 (in-sample of the existing
synthetic experiment). Variants A and E, at Rs 1,00,000:

| scenario | var | setups | trades | rejected/unfilled | gross | charges | spread | slippage | net | PF |
|---|---|---|---|---|---|---|---|---|---|---|
| optimistic | A | 199 | 90 | 109 | -4281.20 | 6482.39 | 1050.14 | 2398.14 | -10763.59 | 0.61 |
| optimistic | E | 29 | 24 | 5 | 56.05 | 1747.08 | 281.35 | 639.21 | -1691.03 | 0.75 |
| baseline | A | 199 | 9 | 190 | -1091.00 | 560.22 | 177.36 | 386.75 | -1651.22 | 0.28 |
| baseline | E | 29 | 2 | 27 | -325.75 | 126.45 | 44.47 | 92.77 | -452.20 | 0.01 |
| wide_spread | A | 199 | 1 | 198 | -145.60 | 58.68 | 31.86 | 32.78 | -204.28 | 0.00 |
| delay_2_bars | A | 199 | 8 | 191 | -1533.75 | 497.33 | 166.58 | 362.97 | -2031.08 | 0.08 |
| delay_3_bars | A | 199 | 7 | 192 | -1356.45 | 434.96 | 145.50 | 316.78 | -1791.41 | 0.09 |
| very_wide_spread, high_slippage, very_high_slippage, pessimistic | A, E | 29-199 | 0 | all | 0 | 0 | 0 | 0 | 0 | n/a |

At Rs 5,000 no scenario produced a trade for either variant.

## How to read this

1. **Cost assumptions also decide whether a trade is taken.** The risk engine's net
   reward:risk >= 1 rule is evaluated with the scenario's costs. Higher assumed costs
   therefore reject more setups, which can make *net P&L look better*, because fewer losing
   trades are taken. Compare **expectancy per trade and profit factor alongside the trade
   count**, never net P&L alone.
2. In the optimistic scenario, charges (Rs 6,482) exceeded the gross loss (Rs 4,281) for
   variant A. On this data, transaction costs are the dominant term.
3. On random-walk data every scenario that traded lost money after costs, as expected.
   Nothing about real markets follows from it.

Tests: `tests/test_sensitivity.py`. Spread and slippage split hand-checked on target and stop
trades; delay shifting the fill candle; delay causing a limit miss; fixed scenario list; row
consistency (net = gross - charges); no trades forced at Rs 5,000.
