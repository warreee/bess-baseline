"""Core model: LP optimum, day-ahead closed-loop simulation, valuation.

Sign convention: n_t is the net site position per quarter hour in kWh, positive = grid
offtake, negative = injection. Prices p_t in EUR/MWh (day-ahead). Timestamps are UTC
internally; day and month boundaries follow Europe/Brussels.

Site valuation per quarter:
    offtake * (p + comp + markup) - injection * (p - inj_fee)      [EUR, kWh/1000]
plus, per calendar month, the monthly peak in kW times peak_tariff / 12.
"spot" is the same dispatch valued at p alone (gross market revenue).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

TZ = "Europe/Brussels"
Q = 0.25  # uur per kwartier


@dataclass
class Battery:
    power_kw: float
    capacity_kwh: float
    roundtrip: float = 0.88
    soc_min: float = 0.0        # fractie van capacity_kwh
    soc_max: float = 1.0
    cycle_cost: float = 0.0     # EUR/MWh ontladen (degradatie)

    @property
    def eta(self) -> float:
        return float(np.sqrt(self.roundtrip))

    @property
    def pq(self) -> float:
        return self.power_kw * Q


@dataclass
class Tariffs:
    comp: float = 0.0           # EUR/MWh vermeden variabele kosten bij afname
    markup: float = 0.0         # EUR/MWh leveranciersopslag op afname
    inj_fee: float = 0.0        # EUR/MWh afslag op injectie
    peak_tariff: float = 0.0    # EUR/kW/jaar maandpiek (capaciteitstarief)


@dataclass
class SiteLimits:
    max_offtake_kw: float | None = None     # toegangsvermogen / harde afnamegrens
    max_injection_kw: float | None = None   # injectie-CV
    pv_only: bool = False                   # enkel laden uit eigen overschot


@dataclass
class Dispatch:
    index: pd.DatetimeIndex
    n: np.ndarray          # netto sitepositie zonder batterij (kWh)
    p: np.ndarray          # prijs (EUR/MWh)
    c: np.ndarray          # laden (kWh AC)
    d: np.ndarray          # ontladen (kWh AC)
    soc: np.ndarray        # SOC na kwartier (kWh)
    extra: dict = field(default_factory=dict)

    @property
    def n_batt(self) -> np.ndarray:
        return self.n + self.c - self.d


# ---------------------------------------------------------------- waardering

def month_ids(index: pd.DatetimeIndex) -> tuple[np.ndarray, list[str]]:
    local = index.tz_convert(TZ)
    keys = local.strftime("%Y-%m")
    uniq = list(dict.fromkeys(keys))
    lut = {k: i for i, k in enumerate(uniq)}
    return np.array([lut[k] for k in keys]), uniq


def cost_breakdown(index, n_eff, p, tar: Tariffs) -> dict:
    off = np.maximum(n_eff, 0.0)
    inj = np.maximum(-n_eff, 0.0)
    energy = (off * (p + tar.comp + tar.markup) - inj * (p - tar.inj_fee)).sum() / 1000
    spot = ((off - inj) * p).sum() / 1000
    mid, uniq = month_ids(index)
    peaks = np.zeros(len(uniq))
    np.maximum.at(peaks, mid, off / Q)
    peak_cost = peaks.sum() * tar.peak_tariff / 12
    return {
        "energy_site": energy,
        "energy_spot": spot,
        "peak_cost": peak_cost,
        "total_site": energy + peak_cost,
        "peaks_kw": dict(zip(uniq, peaks)),
        "offtake_kwh": off.sum(),
        "injection_kwh": inj.sum(),
    }


# ---------------------------------------------------------------- LP

def plan(p, n_hat, batt: Battery, tar: Tariffs, lim: SiteLimits, soc0: float,
         mid: np.ndarray, running_peak_kw: np.ndarray, spot_only: bool = False,
         soc_floor_kwh: float = 0.0):
    """LP over T kwartieren. Geeft (c, d, soc, peak_kw per maand) in kWh.

    mid: maandindex per kwartier (0..M-1); running_peak_kw: al gezette piek per maand.
    """
    T = len(p)
    M = int(mid.max()) + 1 if T else 0
    pq, eta = batt.pq, batt.eta
    cmin, cmax = batt.soc_min * batt.capacity_kwh, batt.soc_max * batt.capacity_kwh
    # variabelen: c[0:T], d[T:2T], off[2T:3T], inj[3T:4T], s[4T:5T], m[5T:5T+M]
    ic, id_, io, ii, is_ = (np.arange(T) + k * T for k in range(5))
    im = 5 * T + np.arange(M)
    nv = 5 * T + M

    comp = 0.0 if spot_only else tar.comp
    markup = 0.0 if spot_only else tar.markup
    fee = 0.0 if spot_only else tar.inj_fee
    cost = np.zeros(nv)
    cost[io] = (p + comp + markup) / 1000
    cost[ii] = -(p - fee) / 1000
    cost[id_] = batt.cycle_cost / 1000
    if tar.peak_tariff and not spot_only:
        cost[im] = tar.peak_tariff / 12

    # gelijkheden: balans en SOC
    rows, cols, vals, beq = [], [], [], []
    r = np.arange(T)
    # off - inj - c + d = n_hat
    rows += [r, r, r, r]; cols += [io, ii, ic, id_]; vals += [np.ones(T), -np.ones(T), -np.ones(T), np.ones(T)]
    beq.append(n_hat)
    # s_t - s_{t-1} - eta c_t + d_t/eta = 0  (s_{-1} = soc0)
    r2 = T + r
    rows += [r2, r2, r2]; cols += [is_, ic, id_]; vals += [np.ones(T), -eta * np.ones(T), np.ones(T) / eta]
    rows.append(r2[1:]); cols.append(is_[:-1]); vals.append(-np.ones(T - 1))
    b2 = np.zeros(T); b2[0] = soc0
    beq.append(b2)
    Aeq = coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(2 * T, nv)).tocsr()
    beq = np.concatenate(beq)

    # ongelijkheden: c + d <= pq ; 4*off - m_month <= 0
    rows, cols, vals = [r, r], [ic, id_], [np.ones(T), np.ones(T)]
    bub = [np.full(T, pq)]
    nrow = T
    if M and tar.peak_tariff and not spot_only:
        r3 = nrow + r
        rows += [r3, r3]; cols += [io, im[mid]]; vals += [np.ones(T) / Q, -np.ones(T)]
        bub.append(np.zeros(T)); nrow += T
    Aub = coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(nrow, nv)).tocsr()
    bub = np.concatenate(bub)

    lo, hi = np.zeros(nv), np.full(nv, np.inf)
    hi[ic] = pq; hi[id_] = pq
    if lim.pv_only:
        hi[ic] = np.minimum(pq, np.maximum(-n_hat, 0.0))
    if lim.max_offtake_kw is not None:
        hi[io] = lim.max_offtake_kw * Q
    if lim.max_injection_kw is not None:
        hi[ii] = lim.max_injection_kw * Q
    lo[is_] = cmin; hi[is_] = cmax
    if soc_floor_kwh > cmin:
        # reserve: planner blijft erboven; zit hij eronder, dan een oprit op het laadvermogen
        # dat er echt is (vermogen, PV-only-overschot, ruimte onder de afnamegrens)
        avail = hi[ic].copy()
        if lim.max_offtake_kw is not None:
            avail = np.minimum(avail, np.maximum(0.0, lim.max_offtake_kw * Q - n_hat))
        ramp = soc0 + eta * np.cumsum(avail)
        lo[is_] = np.minimum(max(soc_floor_kwh, cmin), np.maximum(ramp, cmin))
    if M:
        lo[im] = running_peak_kw[:M]
    # infeasible als de site zelf al boven de grens zit: grenzen dan opengooien voor die kwartieren
    if lim.max_offtake_kw is not None:
        hi[io] = np.maximum(hi[io], n_hat)
    if lim.max_injection_kw is not None:
        hi[ii] = np.maximum(hi[ii], -n_hat)

    res = linprog(cost, A_ub=Aub, b_ub=bub, A_eq=Aeq, b_eq=beq, bounds=np.column_stack([lo, hi]), method="highs")
    if res.status != 0:
        raise RuntimeError(f"LP mislukt: {res.message}")
    x = res.x
    return x[ic], x[id_], x[is_], (x[im] if M else np.zeros(0))


# ---------------------------------------------------------------- optimum

def optimum(df: pd.DataFrame, batt, tar, lim, spot_only=False) -> Dispatch:
    mid, _ = month_ids(df.index)
    c, d, soc, _ = plan(df["p"].values, df["n"].values, batt, tar, lim, batt.soc_min * batt.capacity_kwh,
                        mid, np.zeros(mid.max() + 1), spot_only=spot_only)
    return Dispatch(df.index, df["n"].values, df["p"].values, c, d, soc)


# ---------------------------------------------------------------- forecasts

def day_pivot(series: pd.Series) -> pd.DataFrame:
    """Rijen = lokale kalenderdag, kolommen = lokale kloktijd HH:MM."""
    local = series.tz_convert(TZ)
    frame = pd.DataFrame({"v": local.values, "day": local.index.date, "tod": local.index.strftime("%H:%M")})
    piv = frame.pivot_table(index="day", columns="tod", values="v", aggfunc="mean")
    full = pd.date_range(piv.index.min(), piv.index.max(), freq="D").date
    return piv.reindex(full)


FORECASTS = {
    "perfect": None,
    "zero": lambda piv: piv * 0.0,
    "yesterday": lambda piv: piv.shift(1),
    "last_week": lambda piv: piv.shift(7),
    "wd_median4": lambda piv: pd.concat([piv.shift(k) for k in (7, 14, 21, 28)]).groupby(level=0).median(),
    "wd_mean4": lambda piv: pd.concat([piv.shift(k) for k in (7, 14, 21, 28)]).groupby(level=0).mean(),
    "median7": lambda piv: pd.concat([piv.shift(k) for k in range(1, 8)]).groupby(level=0).median(),
}


def forecast_series(n: pd.Series, method: str) -> pd.Series:
    """Verwachte n_t per kwartier voor elke dag, opgebouwd uit eerdere dagen."""
    if method == "perfect":
        return n.copy()
    piv = day_pivot(n)
    fc = FORECASTS[method](piv)
    fc = fc.reindex(piv.index)
    local = n.tz_convert(TZ)
    keys = pd.MultiIndex.from_arrays([local.index.date, local.index.strftime("%H:%M")])
    stacked = fc.stack(future_stack=True)
    vals = stacked.reindex(keys).values
    out = pd.Series(vals, index=n.index)
    # ontbrekende verwachting (eerste weken, DST-kwartieren): terugvallen op het dagelijkse gemiddelde van de historiek
    return out.fillna(out.mean() if np.isfinite(out.mean()) else 0.0)


# ---------------------------------------------------------------- dag-voor-dag simulatie

def simulate_dayahead(df: pd.DataFrame, batt: Battery, tar: Tariffs, lim: SiteLimits,
                      forecast: str = "wd_median4", replan_every: int = 4, publish_hour: int = 13,
                      spot_only: bool = False, peak_floor_days: int = 0,
                      peak_floor_source: str = "site", reserve_frac: float = 0.0,
                      reflex_line: str = "plan") -> Dispatch:
    """Closed-loop: plannen op verwachting, uitvoeren op realiteit.

    Op elk herplanmoment loopt de horizon tot het einde van de lokale dag, of tot het einde
    van morgen als het al publish_hour is en de prijzen van morgen in df zitten.
    peak_floor_days > 0: de planner neemt aan dat de maandpiek minstens de hoogste
    sitepiek van de voorbije N dagen wordt (simpele piekverwachting). peak_floor_source
    "site" = ruwe sitepiek zonder batterij, "realized" = gerealiseerde afname met batterij,
    "optimal" = de piek die de batterij over de voorbije N dagen optimaal had kunnen halen
    (LP op de historiek, eens per dag herrekend).
    reserve_frac: deel van de capaciteit dat de planner niet mag aanspreken; alleen de
    piekreflex (afname boven de toegelaten piek) mag eruit ontladen.
    reflex_line: "plan" = de reflex houdt de lijn van het plan (>= piekverwachting);
    "unavoidable" = de reflex houdt max(lopende maandpiek, sitepiek 28 dagen min batterijvermogen).
    """
    idx = df.index
    p = df["p"].values
    n = df["n"].values
    n_hat = forecast_series(df["n"], forecast).values
    local = idx.tz_convert(TZ)
    day = np.array(local.date)
    hour = np.array(local.hour)
    mid, uniq = month_ids(idx)
    T = len(idx)
    # per kwartier: laatste index van de planhorizon
    day_last = pd.Series(np.arange(T)).groupby(day).transform("max").values
    next_day_last = np.empty(T, dtype=int)
    for t in range(T):
        e = day_last[t]
        next_day_last[t] = day_last[e + 1] if e + 1 < T else e
    horizon_end = np.where(hour >= publish_hour, next_day_last, day_last)

    c = np.zeros(T); d = np.zeros(T); soc_arr = np.zeros(T)
    soc = batt.soc_min * batt.capacity_kwh
    cmin, cmax = batt.soc_min * batt.capacity_kwh, batt.soc_max * batt.capacity_kwh
    pq, eta = batt.pq, batt.eta
    reserve = cmin + reserve_frac * (cmax - cmin)
    running = np.zeros(len(uniq))
    site28 = pd.Series(n / Q).rolling(28 * 96, min_periods=1).max().shift(1).fillna(0.0).values
    if peak_floor_days > 0:
        floor = pd.Series(n / Q).rolling(peak_floor_days * 96, min_periods=1).max().shift(1).fillna(0.0).values
    else:
        floor = np.zeros(T)
    realized_kw = np.zeros(T)
    win = peak_floor_days * 96
    opt_floor, opt_floor_day = 0.0, None
    plan_c = plan_d = None
    plan_peak = {}
    plan_start = 0
    for t in range(T):
        if plan_c is None or (t - plan_start) % replan_every == 0 or t > plan_end:
            e = horizon_end[t]
            sl = slice(t, e + 1)
            m_local = mid[sl] - mid[t]
            fl = floor[t]
            if peak_floor_days > 0 and peak_floor_source == "realized":
                fl = realized_kw[max(0, t - win):t].max() if t > 0 else 0.0
            elif peak_floor_days > 0 and peak_floor_source == "optimal":
                if day[t] != opt_floor_day and t >= 96:
                    h = slice(max(0, t - win), t)
                    hm = mid[h] - mid[h][0]
                    hc, hd, _, _ = plan(p[h], n[h], batt, tar, lim, cmin, hm, np.zeros(hm.max() + 1), spot_only=spot_only)
                    opt_floor = float(((n[h] + hc - hd) / Q).max())
                    opt_floor_day = day[t]
                fl = opt_floor
            rp = np.maximum(running[mid[t]: mid[t] + m_local.max() + 1].copy(), fl)
            pc, pd_, _, pk = plan(p[sl], n_hat[sl], batt, tar, lim, soc, m_local, rp, spot_only=spot_only,
                                  soc_floor_kwh=reserve)
            plan_c, plan_d, plan_start, plan_end = pc, pd_, t, e
            plan_peak = {mid[t] + k: v for k, v in enumerate(pk)}
        ct = plan_c[t - plan_start]; dt = plan_d[t - plan_start]
        # guardrails tegen de realiteit
        ct = min(ct, (cmax - soc) / eta); dt = max(0.0, min(dt, (soc - max(cmin, min(reserve, soc))) * eta))
        if lim.pv_only:
            ct = min(ct, max(-n[t], 0.0))
        allow_kw = max(running[mid[t]], plan_peak.get(mid[t], 0.0))
        if lim.max_offtake_kw is not None:
            allow_kw = min(allow_kw, lim.max_offtake_kw) if tar.peak_tariff and not spot_only else lim.max_offtake_kw
        elif not (tar.peak_tariff and not spot_only):
            allow_kw = np.inf
        allow = allow_kw * Q
        off = n[t] + ct - dt
        if off > allow:
            ct = max(0.0, ct - (off - allow)); off = n[t] + ct - dt
        line = allow
        if reflex_line == "unavoidable" and tar.peak_tariff and not spot_only:
            line = max(running[mid[t]], site28[t] - batt.power_kw) * Q
            if lim.max_offtake_kw is not None:
                line = min(line, lim.max_offtake_kw * Q)
        if off > line and dt < pq:  # piekreflex: ontladen om de piek te drukken, mag in de reserve
            dt = min(pq, dt + (off - line), (soc - cmin) * eta); off = n[t] + ct - dt
        if lim.max_injection_kw is not None:
            inj = -off
            if inj > lim.max_injection_kw * Q:
                dt = max(0.0, dt - (inj - lim.max_injection_kw * Q))
        c[t], d[t] = ct, dt
        soc = soc + eta * ct - dt / eta
        soc_arr[t] = soc
        realized_kw[t] = (n[t] + ct - dt) / Q
        running[mid[t]] = max(running[mid[t]], realized_kw[t])
    return Dispatch(idx, n, p, c, d, soc_arr, {"forecast": forecast, "replan_every": replan_every, "peak_floor_days": peak_floor_days, "peak_floor_source": peak_floor_source, "reserve_frac": reserve_frac})


# ---------------------------------------------------------------- rapportage

def evaluate(disp: Dispatch, batt: Battery, tar: Tariffs) -> dict:
    base = cost_breakdown(disp.index, disp.n, disp.p, tar)
    with_ = cost_breakdown(disp.index, disp.n_batt, disp.p, tar)
    c, d, p, n = disp.c, disp.d, disp.p, disp.n
    years = len(disp.index) * Q / 8760
    both = int(((c > 1e-6) & (d > 1e-6)).sum())
    pv_charge = np.minimum(c, np.maximum(-n, 0.0)).sum()
    dis_to_load = np.minimum(d, np.maximum(n, 0.0)).sum()
    return {
        "years": years,
        "benefit_site": base["total_site"] - with_["total_site"],
        "benefit_energy": base["energy_site"] - with_["energy_site"],
        "benefit_spot": base["energy_spot"] - with_["energy_spot"],
        "peak_delta": with_["peak_cost"] - base["peak_cost"],
        "cycles": d.sum() / batt.capacity_kwh,
        "charge_mwh": c.sum() / 1000,
        "discharge_mwh": d.sum() / 1000,
        "pv_charge_share": pv_charge / c.sum() if c.sum() else np.nan,
        "dis_to_load_share": dis_to_load / d.sum() if d.sum() else np.nan,
        "avg_charge_price": (c * p).sum() / c.sum() if c.sum() else np.nan,
        "avg_discharge_price": (d * p).sum() / d.sum() if d.sum() else np.nan,
        "simultaneous_quarters": both,
        "base_peaks": base["peaks_kw"],
        "with_peaks": with_["peaks_kw"],
    }


def monthly_table(disp: Dispatch, batt: Battery, tar: Tariffs) -> pd.DataFrame:
    """Per kalendermaand (Brussel): sitewaarde, spotwaarde, cycli, gemiddelde laad- en ontlaadprijs."""
    mid, uniq = month_ids(disp.index)
    rows = []
    for k, key in enumerate(uniq):
        m = mid == k
        sub = Dispatch(disp.index[m], disp.n[m], disp.p[m], disp.c[m], disp.d[m], disp.soc[m])
        e = evaluate(sub, batt, tar)
        rows.append({"maand": key, "site": e["benefit_site"], "spot": e["benefit_spot"], "piekdelta": e["peak_delta"],
                     "cycli": e["cycles"], "laadprijs": e["avg_charge_price"], "ontlaadprijs": e["avg_discharge_price"]})
    return pd.DataFrame(rows).set_index("maand")
