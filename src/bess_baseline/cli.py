"""Command line: bess-baseline run --fluvius offtake.csv [--fluvius injection.csv] --power-kw ... """

from __future__ import annotations

import argparse
import json
import sys

from .core import Battery, SiteLimits, Tariffs, evaluate, monthly_table, optimum, simulate_dayahead, FORECASTS
from .fluvius import read_fluvius, site_frame
from .prices import price_range, quarter_prices


def _fmt(e: dict, name: str) -> str:
    y = e["years"]
    return (f"{name:<12} site {e['benefit_site']/y:>10,.0f} = energy {e['benefit_energy']/y:>10,.0f} "
            f"+ peak {-e['peak_delta']/y:>8,.0f}   spot {e['benefit_spot']/y:>10,.0f}   "
            f"cycles {e['cycles']/y:>5.0f}   charge {e['avg_charge_price']:>6.1f}   discharge {e['avg_discharge_price']:>6.1f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bess-baseline", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prices", help="show the bundled price range")
    r = sub.add_parser("run", help="run the benchmark on Fluvius quarter-hour exports")
    r.add_argument("--fluvius", action="append", required=True, help="Fluvius CSV export (repeat for a second EAN)")
    r.add_argument("--power-kw", type=float, required=True)
    r.add_argument("--capacity-kwh", type=float, required=True, help="usable capacity")
    r.add_argument("--roundtrip", type=float, default=0.88, help="AC round-trip efficiency (default 0.88)")
    r.add_argument("--cycle-cost", type=float, default=2.0, help="EUR/MWh discharged, planner tie-breaker (default 2)")
    r.add_argument("--max-offtake-kw", type=float, default=None, help="hard grid offtake limit (access power)")
    r.add_argument("--max-injection-kw", type=float, default=None)
    r.add_argument("--pv-only", action="store_true", help="charge from own surplus only")
    r.add_argument("--comp", type=float, default=0.0, help="EUR/MWh avoided variable grid costs and levies on offtake")
    r.add_argument("--markup", type=float, default=0.0, help="EUR/MWh supplier markup on offtake")
    r.add_argument("--inj-fee", type=float, default=0.0, help="EUR/MWh deducted on injection")
    r.add_argument("--peak-tariff", type=float, default=0.0, help="EUR/kW/year monthly-peak capacity tariff")
    r.add_argument("--start", default=None, help="local date, inclusive (default: first meter day)")
    r.add_argument("--end", default=None, help="local date, exclusive")
    r.add_argument("--forecast", default="median7", choices=list(FORECASTS))
    r.add_argument("--replan-every", type=int, default=4, help="quarters between re-plans (default 4 = hourly)")
    r.add_argument("--peak-floor-days", type=int, default=28)
    r.add_argument("--peak-floor-source", default="optimal", choices=["site", "realized", "optimal"])
    r.add_argument("--reserve", type=float, default=0.0, help="fraction of capacity held back for the peak (default 0)")
    r.add_argument("--monthly", action="store_true")
    r.add_argument("--json", default=None, help="write results to this file")
    a = ap.parse_args(argv)

    if a.cmd == "prices":
        lo, hi = price_range()
        print(f"bundled Belgian day-ahead prices: {lo} .. {hi} (UTC); quarter-hourly from 2025-10-01")
        return 0

    meter = read_fluvius(*a.fluvius)
    df = site_frame(meter, quarter_prices(), a.start, a.end)
    batt = Battery(a.power_kw, a.capacity_kwh, a.roundtrip, cycle_cost=a.cycle_cost)
    tar = Tariffs(comp=a.comp, markup=a.markup, inj_fee=a.inj_fee, peak_tariff=a.peak_tariff)
    lim = SiteLimits(max_offtake_kw=a.max_offtake_kw, max_injection_kw=a.max_injection_kw, pv_only=a.pv_only)
    print(f"# EANs {meter.attrs.get('eans')}  {df.index.min()} .. {df.index.max()}  quarters {len(df)}  "
          f"missing meter quarters {df.attrs['missing_quarters']}", file=sys.stderr)

    o = optimum(df, batt, tar, lim)
    eo = evaluate(o, batt, tar)
    d = simulate_dayahead(df, batt, tar, lim, forecast=a.forecast, replan_every=a.replan_every,
                          peak_floor_days=a.peak_floor_days, peak_floor_source=a.peak_floor_source,
                          reserve_frac=a.reserve)
    ed = evaluate(d, batt, tar)
    print(_fmt(eo, "optimum")); print(_fmt(ed, "dayahead"))
    cap_e = ed["benefit_energy"] / eo["benefit_energy"] if eo["benefit_energy"] else float("nan")
    cap_s = ed["benefit_site"] / eo["benefit_site"] if eo["benefit_site"] else float("nan")
    print(f"capture energy {cap_e:.0%}   capture total {cap_s:.0%}")
    if a.monthly:
        import pandas as pd
        print(pd.concat({"optimum": monthly_table(o, batt, tar), "dayahead": monthly_table(d, batt, tar)}, axis=1).round(0).to_string())
    if a.json:
        out = {"optimum": eo, "dayahead": ed, "capture_energy": cap_e, "capture_total": cap_s,
               "settings": vars(a)}
        json.dump(out, open(a.json, "w"), indent=1, default=lambda v: v.item() if hasattr(v, "item") else str(v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
