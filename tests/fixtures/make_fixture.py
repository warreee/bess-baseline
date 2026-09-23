"""Builds the synthetic Fluvius export fixtures (two EANs, spans the October DST switch)."""
from pathlib import Path
import numpy as np, pandas as pd
here = Path(__file__).parent
idx = pd.date_range("2025-10-24", "2025-10-28", freq="15min", tz="Europe/Brussels", inclusive="left")
rng = np.random.default_rng(1)
for ean, reg, base in (("541400000000000001", "Afname", 5.0), ("541400000000000002", "Injectie", 1.0)):
    stop = idx + pd.Timedelta(minutes=15)
    vol = base + rng.random(len(idx))
    out = pd.DataFrame({"Van (datum)": idx.strftime("%d-%m-%Y"), "Van (tijdstip)": idx.strftime("%H:%M:%S"),
                        "Tot (datum)": stop.strftime("%d-%m-%Y"), "Tot (tijdstip)": stop.strftime("%H:%M:%S"),
                        "EAN-code": f'="{ean}"', "Meter": "1SAG0000000000", "Metertype": "Digitale meter",
                        "Register": reg, "Volume": [f"{v:.3f}".replace(".", ",") for v in vol], "Eenheid": "kWh",
                        "Validatiestatus": "Uitgelezen", "Omschrijving": ""})
    with open(here / f"fluvius_{reg.lower()}.csv", "w", encoding="utf-8-sig", newline="") as fh:
        out.to_csv(fh, sep=";", index=False, lineterminator="\r\n")
    (here / f"fluvius_{reg.lower()}.sum").write_text(f"{np.round(vol, 3).sum():.6f}")
