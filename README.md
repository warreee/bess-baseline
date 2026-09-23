# bess-baseline

An open, reproducible day-ahead baseline for battery storage behind the meter.

It answers one question: **what would this battery earn if its owner simply traded it on
the day-ahead market, with no forecasting skill beyond what anyone can do in a
spreadsheet?** That number is the bar a balancing service provider (BSP), aggregator or
optimiser has to clear, net of fees, before their contract adds value.

Two results per run:

| | Knows | Role |
|---|---|---|
| **optimum** | prices and the real site profile for the whole period (hindsight) | ceiling |
| **dayahead** | tomorrow's prices at 13:00, the site's own past, nothing else | the baseline |

Both are split into an **energy part** (arbitrage plus self-consumption, valued at the
day-ahead price plus avoided variable costs) and a **peak part** (monthly-peak capacity
tariff). The capture ratio on the energy part is the number to compare a BSP against; the
peak part is reported separately because a monthly peak cannot be predicted, only
recognised afterwards.

Method, choices and their justification: [METHOD.md](METHOD.md).

## Install

```bash
pip install bess-baseline        # or: uv pip install bess-baseline
```

Python 3.11+, numpy, pandas, scipy (HiGHS). No API keys, no external data: Belgian
day-ahead prices since 2020 ship with the package.

## Run

Export quarter-hour data from Mijn Fluvius ("Verbruikshistoriek, kwartiertotalen"). An
offtake EAN and an injection EAN come as two files; pass both.

```bash
bess-baseline run \
  --fluvius Verbruikshistoriek_elektriciteit_5414..._kwartiertotalen.csv \
  --fluvius Verbruikshistoriek_elektriciteit_5414..._kwartiertotalen.csv \
  --power-kw 2000 --capacity-kwh 4000 --roundtrip 0.87 \
  --max-offtake-kw 800 --peak-tariff 59.86 --comp 32.5 \
  --start 2024-11-01 --end 2025-11-01 --monthly
```

Output (EUR per year):

```
optimum      site    118,739 = energy    112,239 + peak    6,500   spot    113,203   cycles   289 ...
dayahead     site    104,404 = energy    104,211 + peak      194   spot    109,771   cycles   287 ...
capture energy 93%   capture total 88%
```

`spot` is the same dispatch valued at the day-ahead price alone: the gross market revenue a
BSP backtest usually quotes.

Tariff inputs (all per site, from your grid operator's tariff sheet and your supply contract):

| Flag | Meaning | Typical Flanders MV |
|---|---|---|
| `--comp` | EUR/MWh of variable grid costs and levies avoided when a discharged kWh replaces offtake | 30 to 35 |
| `--markup` | EUR/MWh supplier markup on offtake in a spot-indexed contract | 5 to 10 |
| `--inj-fee` | EUR/MWh deducted from the spot price on injection | 0 to 7 |
| `--peak-tariff` | EUR/kW/year monthly-peak capacity tariff | 48 to 60 |
| `--max-offtake-kw` | access power or any hard offtake limit | site specific |

Leave them at zero for a pure spot valuation.

## Python

```python
from bess_baseline import *
meter = read_fluvius("offtake.csv", "injection.csv")
df = site_frame(meter, quarter_prices(), "2024-11-01", "2025-11-01")
batt = Battery(power_kw=2000, capacity_kwh=4000, roundtrip=0.87, cycle_cost=2)
tar = Tariffs(comp=32.5, peak_tariff=59.86)
lim = SiteLimits(max_offtake_kw=800)
evaluate(optimum(df, batt, tar, lim), batt, tar)
evaluate(simulate_dayahead(df, batt, tar, lim), batt, tar)
```

`df` only needs two columns: `n` (net site position per quarter hour in kWh, positive is
offtake) and `p` (EUR/MWh), on a UTC quarter-hour index. Any meter data source works.

## Price data

`bess_baseline/data/belpex_da_be.csv` holds Belgian day-ahead prices (EPEX SPOT BE) from
2020, hourly until 30 September 2025 and quarter-hourly since. Source: ENTSO-E Transparency
Platform; a small number of recent values come from
[energy-charts.info](https://energy-charts.info) (Fraunhofer ISE, CC BY 4.0) when ENTSO-E
was late. EnergyBytes refreshes the file with each release. `bess-baseline prices` prints
the bundled range.

## Scope

In scope: day-ahead arbitrage, self-consumption, monthly-peak guarding, grid charging
under an offtake limit. Out of scope by design: intraday, imbalance, FCR/aFRR/mFRR,
capacity remuneration, PV curtailment, degradation beyond a simple cycle cost. Those are
exactly the things a BSP is paid to add.

## License

MIT. Built by [EnergyBytes](https://energybytes.be).
