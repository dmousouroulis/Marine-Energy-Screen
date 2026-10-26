"""
Read-only check of the Henry Hub 52-week high shown in the Marine Energy Screen.

Fetches the raw daily Henry Hub spot series (EIA series RNGWHHD, $/MMBtu) straight from the
EIA API with the same request the Power BI model uses, applies the model's 52W rule
(highest price in the 365 days up to the latest published day), and prints:
  - the latest observation, and the 52W high with its date
  - the five highest days in the window
  - the days around the high, so you can see whether it is a sustained spike or a lone outlier

Nothing is changed in Power BI or at EIA.

Usage (same environment as the deploy script, from the repository root):
    python scripts/verify_henry_hub.py --eia-key YOUR_EIA_KEY
"""
import argparse
import datetime as dt
import sys

URL = "https://api.eia.gov/v2/natural-gas/pri/fut/data/"


def fetch(key: str, start: str) -> list:
    import requests
    rows, offset = [], 0
    while True:
        r = requests.get(URL, params={
            "api_key": key, "frequency": "daily", "data[0]": "value",
            "facets[series][]": "RNGWHHD", "start": start,
            "sort[0][column]": "period", "sort[0][direction]": "asc",
            "offset": offset, "length": 5000}, timeout=60)
        if r.status_code != 200:
            sys.exit(f"EIA returned {r.status_code}: {r.text[:300]}")
        resp = r.json()["response"]
        rows += resp.get("data", [])
        total = int(resp.get("total", len(rows)))
        offset += 5000
        if offset >= total:
            return rows


def analyse(raw: list) -> None:
    obs = []
    for row in raw:
        try:
            obs.append((dt.date.fromisoformat(row["period"]), float(row["value"])))
        except (KeyError, TypeError, ValueError):
            continue  # same as the model: rows without a usable date or value are skipped
    if not obs:
        sys.exit("EIA returned no Henry Hub observations.")
    obs.sort()
    latest_day, latest_val = obs[-1]
    window = [(d, v) for d, v in obs if latest_day - dt.timedelta(days=365) < d <= latest_day]
    hi_day, hi_val = max(window, key=lambda o: o[1])
    lo_day, lo_val = min(window, key=lambda o: o[1])
    vals = sorted(v for _, v in window)
    median = vals[len(vals) // 2]

    print(f"\nEIA series RNGWHHD, Henry Hub natural gas spot price ($/MMBtu)")
    print(f"Latest observation:  {latest_day:%d %b %Y}  {latest_val:.2f}")
    print(f"52-week window:      {window[0][0]:%d %b %Y} to {latest_day:%d %b %Y}  ({len(window)} published days)")
    print(f"52-week high:        {hi_val:.2f}  on {hi_day:%a %d %b %Y}")
    print(f"52-week low:         {lo_val:.2f}  on {lo_day:%a %d %b %Y}")
    print(f"Median in window:    {median:.2f}")

    print("\nFive highest days in the window:")
    for d, v in sorted(window, key=lambda o: o[1], reverse=True)[:5]:
        print(f"   {d:%a %d %b %Y}   {v:7.2f}")

    i = obs.index((hi_day, hi_val))
    print("\nPublished days around the high:")
    for d, v in obs[max(0, i - 5): i + 6]:
        mark = "  <-- 52W high" if d == hi_day else ""
        print(f"   {d:%a %d %b %Y}   {v:7.2f}{mark}")

    above = sum(1 for _, v in window if v > 3 * median)
    print(f"\nDays in the window above 3x the median: {above}")
    if abs(hi_val - 30.72) < 0.005:
        print("The report's 52W high (30.72) matches EIA's published observation exactly.")
    else:
        print(f"Note: the report showed 30.72; EIA's current data gives {hi_val:.2f} "
              "(EIA may have revised the series, or new days have been published since the last refresh).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--eia-key", required=True)
    a = ap.parse_args()
    start = (dt.date.today() - dt.timedelta(days=420)).isoformat()
    analyse(fetch(a.eia_key.strip(), start))
