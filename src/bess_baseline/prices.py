"""Bundled Belgian day-ahead (Belpex / EPEX SPOT BE) prices.

The package ships `data/belpex_da_be.csv` (timestamp_utc, price_eur_mwh) so that a run needs
no external data source or API key. Hourly until 30 September 2025, quarter-hourly from
1 October 2025 (market resolution change). Hourly values are repeated per quarter hour.
The file is refreshed by EnergyBytes from ENTSO-E data; see README for the update path.
"""

from __future__ import annotations

from importlib.resources import files

import pandas as pd

_CACHE: pd.Series | None = None


def load_prices() -> pd.Series:
    """Raw bundled series, UTC index, native resolution."""
    global _CACHE
    if _CACHE is None:
        path = files("bess_baseline").joinpath("data/belpex_da_be.csv")
        with path.open("rb") as fh:
            df = pd.read_csv(fh, parse_dates=["timestamp_utc"])
        s = df.set_index("timestamp_utc")["price_eur_mwh"].astype(float).sort_index()
        s.index = pd.to_datetime(s.index, utc=True)
        _CACHE = s
    return _CACHE


def quarter_prices(start=None, end=None) -> pd.Series:
    """Quarter-hourly UTC series over the bundled range; hourly values repeated per quarter."""
    p = load_prices()
    full = pd.date_range(p.index.min(), p.index.max() + pd.Timedelta(minutes=45), freq="15min", tz="UTC")
    q = p.reindex(full).ffill()
    def _ts(x):
        t = pd.Timestamp(x)
        return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")
    if start is not None:
        q = q[q.index >= _ts(start)]
    if end is not None:
        q = q[q.index < _ts(end)]
    return q


def price_range() -> tuple[pd.Timestamp, pd.Timestamp]:
    p = load_prices()
    return p.index.min(), p.index.max()
