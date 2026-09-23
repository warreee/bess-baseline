from pathlib import Path
import pandas as pd
from bess_baseline import read_fluvius, site_frame, quarter_prices, price_range

FX = Path(__file__).parent / "fixtures"


def test_read_two_eans_across_dst():
    m = read_fluvius(FX / "fluvius_afname.csv", FX / "fluvius_injectie.csv")
    assert len(m) == 4 * 96 + 4            # 24 to 27 October 2025, one 25-hour day
    assert m.index.is_unique and m.index.is_monotonic_increasing
    assert abs(m["offtake"].sum() - float((FX / "fluvius_afname.sum").read_text())) < 1e-6
    assert abs(m["injection"].sum() - float((FX / "fluvius_injectie.sum").read_text())) < 1e-6
    assert m.attrs["eans"] == ["541400000000000001", "541400000000000002"]


def test_site_frame_aligns_with_bundled_prices():
    m = read_fluvius(FX / "fluvius_afname.csv", FX / "fluvius_injectie.csv")
    df = site_frame(m, quarter_prices(), "2025-10-25", "2025-10-27")
    assert len(df) == 96 + 100
    assert df["p"].notna().all() and df.attrs["missing_quarters"] == 0


def test_prices_quarterly_from_october_2025():
    q = quarter_prices("2025-10-01", "2025-10-02")
    assert len(q) == 96
    lo, hi = price_range()
    assert lo.year <= 2020 and hi >= pd.Timestamp("2026-09-01", tz="UTC")
