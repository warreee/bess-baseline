# Method

This document is the standard. Every choice below is deliberate and was tested on real
quarter-hour data of three Flemish sites (a 4 MWh / 2 MW and a 1 MWh / 500 kW battery at a
fruit cooling company, a 50 kWh / 25 kW battery at a fruit grower). Anyone who reproduces
the numbers with this package and a Fluvius export applies the same standard.

## 1. Inputs

- `n_t`: net site position per quarter hour, kWh, positive = grid offtake, negative =
  injection, measured at the connection point **without** the battery.
- `p_t`: Belgian day-ahead price, EUR/MWh. Hourly before 1 October 2025 (repeated per
  quarter), quarter-hourly since.
- Battery: power (kW, symmetric), usable capacity (kWh), AC round-trip efficiency
  (applied as the square root per leg), SOC limits, a cycle cost.
- Site: hard offtake limit (access power), injection limit, PV-only switch.
- Tariffs: `comp` (avoided variable grid costs and levies on offtake), `markup`
  (supplier margin on offtake), `inj_fee` (deduction on injection), `peak_tariff`
  (EUR/kW/year on the highest quarter-hour offtake of each calendar month).

## 2. Valuation

Per quarter hour, with `off = max(n + c - d, 0)` and `inj = max(-(n + c - d), 0)`:

    site_cost = off * (p + comp + markup) - inj * (p - inj_fee)         [EUR, kWh/1000]
    spot_cost = (off - inj) * p

Per calendar month (Europe/Brussels): `peak_cost = max_t(off_t) * 4 * peak_tariff / 12`.

Benefit = cost without battery minus cost with battery, reported as

- **energy part**: site_cost difference,
- **peak part**: peak_cost difference (negative when the strategy raised peaks),
- **spot**: spot_cost difference (gross market revenue, comparable to BSP backtests).

Why split: the energy part is what a day-ahead strategy can capture; the monthly peak is
one quarter hour per month that only hindsight can pin down (§6).

## 3. Optimum (ceiling)

One linear programme over the whole period with variables charge, discharge, offtake,
injection, SOC per quarter and one peak variable per month. Constraints: SOC balance with
efficiency, power limit (charge + discharge ≤ power per quarter), SOC bounds, offtake and
injection limits, PV-only bound on charging, peak variable ≥ 4 × offtake. Objective: site
cost plus cycle cost plus peak cost. Solved with HiGHS via scipy.

This knows everything in hindsight. It is the ceiling, not the baseline.

## 4. Day-ahead (the baseline)

A closed loop, one quarter hour at a time:

1. **Plan.** Every hour, solve the same LP over the horizon of known prices: until the end
   of today, or until the end of tomorrow once it is 13:00 and tomorrow's prices are
   published. Inputs: current SOC, the month's running peak, and a **forecast** of the site
   profile.
2. **Forecast.** Default `median7`: for each quarter of the day, the median of the same
   quarter over the previous seven days. Alternatives shipped for ablation: yesterday's
   profile, same weekday over four weeks (median or mean), perfect (isolates the horizon
   effect), zero (prices only).
3. **Peak expectation.** The planner needs to know roughly how high the month's peak will
   end up, otherwise it refuses to grid-charge early in the month and then loses the
   arbitrage when the load sets the peak anyway. Default: the peak the battery could
   optimally have achieved over the previous 28 days (LP on history, recomputed daily).
   This is robust across large and small batteries; the raw site peak is not (§6).
4. **Execute.** Apply the planned charge and discharge to the **real** quarter, clipped by
   guardrails: SOC bounds, PV-only surplus, offtake never above the allowed line (running
   peak, planned peak, hard limit) with a discharge reflex if the site alone exceeds it,
   injection never above the injection limit.
5. **Cycle cost.** 2 EUR/MWh discharged in the planner. Not a degradation estimate: a
   tie-breaker that removes the LP artefact of charging and discharging in the same quarter
   at negative prices. It changes the result by 0.1 %.

No terminal value on SOC. Re-planning every quarter instead of every hour changes nothing;
planning once a day costs about 6 percentage points.

## 5. What was tested and rejected

- **A fixed "x % of calls are wrong" penalty.** Not reproducible: the number is whatever
  the parameter says.
- **A peak reserve** (a share of capacity only the peak reflex may use). On every site and
  every reserve level it loses more arbitrage than it gains on the peak, because the reflex
  fires on false alarms and a small battery cannot hold the line for the hours a real peak
  lasts.
- **Raw site peak as peak expectation.** Good for a large battery on long peaks, harmful
  for a small battery on short peaks (capture dropped from 64 % to 48 %).
- **Realised peak as expectation.** Self-reinforcing upward drift.

## 6. Results on the reference sites

Capture of the day-ahead baseline against the optimum, energy part / total:

| Site | Battery | Energy part | Total |
|---|---|---|---|
| Fruit cooling, main site, four periods 2023 to 2026 | 4 MWh / 2 MW | 90 to 95 % | 82 to 91 % |
| Fruit cooling, second site | 1 MWh / 500 kW | 95 % | 88 % |
| Fruit grower | 50 kWh / 25 kW | 95 % | 63 % |

Prices only (no site knowledge) captures 40 to 70 %. A perfect one-day profile forecast
captures 95 % (large) to 80 % (small): the remaining gap is the horizon, not the forecast.

## 7. Out of scope

Intraday, imbalance, FCR/aFRR/mFRR, capacity remuneration, PV curtailment at negative
prices, degradation models. A spot-indexed supply contract that passes the quarter-hour
price through on offtake and injection is assumed; with a fixed price there is nothing to
arbitrage.
