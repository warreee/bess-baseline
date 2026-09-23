import numpy as np
import pandas as pd
import pytest

from bess_baseline import Battery, SiteLimits, Tariffs, evaluate, optimum, simulate_dayahead


def make_df(days=10, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-06-01", periods=days * 96, freq="15min", tz="Europe/Brussels").tz_convert("UTC")
    hour = idx.tz_convert("Europe/Brussels").hour.values
    p = 80 + 60 * np.sin((hour - 9) / 24 * 2 * np.pi) + rng.normal(0, 5, len(idx))   # cheap midday, dear evening
    n = 20 + 10 * rng.random(len(idx)) - np.where((hour >= 10) & (hour <= 15), 40, 0)  # PV surplus at noon
    return pd.DataFrame({"n": n, "p": p}, index=idx)


def test_optimum_beats_dayahead_and_both_beat_nothing():
    df = make_df()
    batt, tar, lim = Battery(50, 100, 0.9), Tariffs(comp=30, peak_tariff=60), SiteLimits(max_offtake_kw=80)
    eo = evaluate(optimum(df, batt, tar, lim), batt, tar)
    ed = evaluate(simulate_dayahead(df, batt, tar, lim, forecast="median7"), batt, tar)
    assert eo["benefit_site"] >= ed["benefit_site"] > 0


def test_soc_and_power_limits_respected():
    df = make_df(days=5)
    batt, tar, lim = Battery(50, 100, 0.9), Tariffs(), SiteLimits()
    d = optimum(df, batt, tar, lim)
    assert d.soc.min() >= -1e-6 and d.soc.max() <= 100 + 1e-6
    assert (d.c + d.d).max() <= 12.5 + 1e-6
    d2 = simulate_dayahead(df, batt, tar, lim)
    assert d2.soc.min() >= -1e-6 and d2.soc.max() <= 100 + 1e-6


def test_pv_only_never_charges_from_grid():
    df = make_df(days=5)
    batt, tar, lim = Battery(50, 100, 0.9), Tariffs(comp=30), SiteLimits(pv_only=True)
    for d in (optimum(df, batt, tar, lim), simulate_dayahead(df, batt, tar, lim)):
        assert np.all(d.c <= np.maximum(-d.n, 0) + 1e-6)


def test_offtake_cap_holds_in_dayahead():
    df = make_df(days=5)
    batt, tar, lim = Battery(50, 100, 0.9), Tariffs(comp=30, peak_tariff=60), SiteLimits(max_offtake_kw=40)
    d = simulate_dayahead(df, batt, tar, lim)
    over = np.maximum(d.n, 0) > 10  # quarters where the site alone already exceeds 40 kW
    assert np.all(d.n_batt[~over] <= 10 + 1e-6)


def test_two_quarter_arbitrage_analytic():
    # one cheap quarter (0 EUR/MWh) then one dear quarter (100 EUR/MWh), no site load
    idx = pd.date_range("2025-06-02", periods=8, freq="15min", tz="UTC")
    p = np.array([0, 0, 0, 0, 100, 100, 100, 100], float)
    df = pd.DataFrame({"n": 0.0, "p": p}, index=idx)
    batt = Battery(power_kw=4, capacity_kwh=1, roundtrip=1.0)
    e = evaluate(optimum(df, batt, Tariffs(), SiteLimits()), batt, Tariffs())
    assert e["benefit_spot"] == pytest.approx(0.1, abs=1e-6)  # 1 kWh * 100 EUR/MWh


def test_progress_callback_reports_start_and_end():
    df = make_df(days=3)
    seen = []
    simulate_dayahead(df, Battery(50, 100, 0.9), Tariffs(), SiteLimits(), progress=lambda done, total: seen.append((done, total)))
    assert seen[0] == (0, len(df)) and seen[-1] == (len(df), len(df))
    assert all(a <= b for (a, _), (b, _) in zip(seen, seen[1:]))
