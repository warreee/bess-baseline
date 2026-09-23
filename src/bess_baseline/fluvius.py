"""Reader for Mijn Fluvius "Verbruikshistoriek" CSV exports (quarter-hour totals).

Format (semicolon separated, UTF-8 with BOM, decimal comma, local Brussels time):
    Van (datum);Van (tijdstip);Tot (datum);Tot (tijdstip);EAN-code;Meter;Metertype;Register;
    Volume;Eenheid;Validatiestatus;Omschrijving

Registers starting with "Afname" are summed into offtake, registers starting with "Injectie"
into injection (day/night registers are added together). Other registers are ignored.
An offtake EAN and an injection EAN may come as two separate files; pass both.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pandas as pd

TZ = "Europe/Brussels"


def _read_one(path: str | Path) -> pd.DataFrame:
    raw = Path(path).read_bytes().decode("utf-8-sig")
    df = pd.read_csv(io.StringIO(raw), sep=";", dtype=str)
    needed = {"Van (datum)", "Van (tijdstip)", "Tot (datum)", "Tot (tijdstip)", "Register", "Volume"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"{path}: not a Fluvius export, missing columns {sorted(missing)}")
    df = df.dropna(subset=["Volume"])
    df["volume"] = df["Volume"].str.replace(",", ".", regex=False).astype(float)
    start = pd.to_datetime(df["Van (datum)"] + " " + df["Van (tijdstip)"], format="%d-%m-%Y %H:%M:%S")
    stop = pd.to_datetime(df["Tot (datum)"] + " " + df["Tot (tijdstip)"], format="%d-%m-%Y %H:%M:%S")
    step = (stop - start).dt.total_seconds().median()
    if step != 900:
        raise ValueError(f"{path}: quarter-hour totals required (kwartiertotalen), found {step/60:.0f}-minute rows")
    reg = df["Register"].str.strip().str.lower()
    df["direction"] = np.where(reg.str.startswith("afname"), "offtake",
                       np.where(reg.str.startswith("injectie"), "injection", "other"))
    df = df[df["direction"] != "other"].copy()
    df["start_local"] = start[df.index]
    if "EAN-code" in df.columns:
        df["ean"] = df["EAN-code"].str.replace('="', "", regex=False).str.replace('"', "", regex=False)
    else:
        df["ean"] = ""
    return df[["ean", "direction", "Register", "start_local", "volume"]]


def _localize(group: pd.DataFrame) -> pd.Series:
    """Local naive timestamps to UTC; the repeated hour in October is inferred from order."""
    g = group.sort_values("start_local")
    t = pd.DatetimeIndex(g["start_local"])
    try:
        loc = t.tz_localize(TZ, ambiguous="infer", nonexistent="shift_forward")
    except Exception:
        # fallback: first occurrence of a repeated time is summer time
        dup = t.duplicated(keep="first")
        loc = t.tz_localize(TZ, ambiguous=~dup, nonexistent="shift_forward")
    return pd.Series(loc.tz_convert("UTC"), index=g.index)


def read_fluvius(*paths: str | Path) -> pd.DataFrame:
    """One or more exports -> DataFrame (UTC quarter index) with offtake, injection, n [kWh].

    Missing quarters are left as NaN; use `site_frame` to align with prices and fill.
    """
    parts = [_read_one(p) for p in paths]
    df = pd.concat(parts, ignore_index=True)
    parts_utc = [_localize(g) for _, g in df.groupby(["ean", "direction", "Register"])]
    df["start_utc"] = pd.to_datetime(pd.concat(parts_utc).reindex(df.index), utc=True)
    piv = df.pivot_table(index="start_utc", columns="direction", values="volume", aggfunc="sum")
    for col in ("offtake", "injection"):
        if col not in piv.columns:
            piv[col] = 0.0
    piv = piv[["offtake", "injection"]].sort_index()
    piv["n"] = piv["offtake"].fillna(0.0) - piv["injection"].fillna(0.0)
    piv.attrs["eans"] = sorted(df["ean"].unique().tolist())
    return piv


def site_frame(meter: pd.DataFrame, prices: pd.Series, start=None, end=None) -> pd.DataFrame:
    """Align meter data and quarter prices on a complete quarter grid; returns columns n, p.

    start/end are local Brussels dates (end exclusive). Missing meter quarters count as zero
    and are reported in df.attrs['missing_quarters'].
    """
    t0 = pd.Timestamp(start, tz=TZ).tz_convert("UTC") if start else meter.index.min()
    t1 = pd.Timestamp(end, tz=TZ).tz_convert("UTC") if end else meter.index.max() + pd.Timedelta(minutes=15)
    idx = pd.date_range(t0, t1 - pd.Timedelta(minutes=15), freq="15min")
    df = pd.DataFrame({"n": meter["n"].reindex(idx), "p": prices.reindex(idx)})
    missing_meter = int(df["n"].isna().sum())
    missing_price = int(df["p"].isna().sum())
    if missing_price:
        raise ValueError(f"{missing_price} quarters without a bundled price; bundled range ends "
                         f"{prices.index.max()}. Shorten --end or update the price file.")
    df["n"] = df["n"].fillna(0.0)
    df.attrs["missing_quarters"] = missing_meter
    return df
