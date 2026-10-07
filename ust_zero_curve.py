"""
US Treasury ZERO-COUPON (spot) yield curve for two consecutive trading days, plotted.

Two sources (both free, no API key):

  --source treasury  (default)
      Downloads the official U.S. Treasury Daily Par Yield Curve (CMT) and
      bootstraps zero rates from it:
        * bill tenors (<= 6M) carry no coupon, so their par yield is already a zero
          rate (bond-equivalent, semi-annual);
        * par yields are interpolated (monotone cubic / PCHIP) onto a semi-annual
          grid 0.5Y..30Y and discount factors are bootstrapped from
              1 = c/2 * sum_{i<n} DF(t_i) + (1 + c/2) * DF(t_n).
      Available the same evening Treasury publishes the curve.

  --source gsw
      Federal Reserve Gürkaynak-Sack-Wright fitted curve (feds200628.csv):
      daily Nelson-Siegel-Svensson parameters -> continuously compounded zero
      yields at any maturity. Smoother, research-standard, but published with a
      lag of several days, and the fit is less reliable below ~1Y.

Usage:
    python ust_zero_curve.py                        # latest two available days
    python ust_zero_curve.py 2026-10-05             # that day + previous trading day
    python ust_zero_curve.py 2026-10-02 2026-10-05  # two explicit days
    python ust_zero_curve.py 2026-10-05 --source gsw --comp semi --save zero.png

Requires: pip install requests pandas numpy matplotlib scipy
"""

import argparse
import io
import re
import sys
from datetime import date, datetime

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

try:
    from scipy.interpolate import PchipInterpolator
except ImportError:          # fall back to linear interpolation of par yields
    PchipInterpolator = None

TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)
GSW_URL = "https://www.federalreserve.gov/data/yield-curve-tables/feds200628.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (zero-curve-script)"}

# Tenors shown as markers / in the change panel and table
STD_TENORS = {"1M": 1/12, "3M": 0.25, "6M": 0.5, "1Y": 1, "2Y": 2, "3Y": 3, "5Y": 5,
              "7Y": 7, "10Y": 10, "20Y": 20, "30Y": 30}


# ----------------------------------------------------------------------------- helpers
def semi_to_cont(z):            # % semi-annual  -> % continuous (keeps Series index)
    return 200 * np.log1p(z / 200)


def cont_to_semi(r):            # % continuous   -> % semi-annual (keeps Series index)
    return 200 * np.expm1(r / 200)


def tenor_to_years(label: str):
    """'1 Mo', '1.5 Month', '10 Yr' -> years."""
    m = re.match(r"\s*([\d.]+)\s*(Mo|Month|Months|Yr|Year|Years)\b", label, re.I)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).lower()
    return n / 12 if unit.startswith("mo") else n


def pick_days(index: pd.DatetimeIndex, d1, d2):
    """Resolve the two trading days to compare."""
    if d1 is None:
        return index[-2], index[-1]
    if d2 is None:
        pos = index.searchsorted(pd.Timestamp(d1), side="right") - 1
        if pos < 1:
            sys.exit(f"Not enough data on/before {d1}.")
        if index[pos].date() != d1:
            print(f"Note: no data for {d1} (holiday/weekend or not yet published); "
                  f"using {index[pos].date()}.")
        return index[pos - 1], index[pos]
    a, b = sorted([pd.Timestamp(d1), pd.Timestamp(d2)])
    for d in (a, b):
        if d not in index:
            sys.exit(f"No data for {d.date()} (weekend/holiday or not yet published).")
    return a, b


# ----------------------------------------------------------------- source 1: Treasury
def fetch_treasury_year(year: int) -> pd.DataFrame:
    r = requests.get(TREASURY_URL.format(year=year), headers=HEADERS, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df["Date"] = pd.to_datetime(df["Date"], format="%m/%d/%Y")
    return df.set_index("Date").sort_index()


def load_treasury(years) -> pd.DataFrame:
    """Par yields, columns renamed to maturity in years (float), sorted."""
    df = pd.concat([fetch_treasury_year(y) for y in sorted(years)]).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    mats = {c: tenor_to_years(c) for c in df.columns}
    df = df[[c for c, t in mats.items() if t is not None]]
    df.columns = [mats[c] for c in df.columns]
    return df[sorted(df.columns)]


def bootstrap_zero(par_row: pd.Series) -> pd.Series:
    """
    Par row (index = maturity in years, values = % BEY) -> zero curve.
    Returns Series indexed by maturity (years), values = % semi-annual zero rate.
    """
    par = par_row.dropna()
    x, y = par.index.to_numpy(float), par.to_numpy(float)
    interp = (PchipInterpolator(x, y) if PchipInterpolator is not None
              else (lambda t: np.interp(t, x, y)))

    zeros = {}
    # Bills (<= 6M): zero-coupon instruments, par yield == zero yield
    for t, v in par.items():
        if t <= 0.5 + 1e-9:
            zeros[t] = v

    # Coupon bootstrap on the semi-annual grid
    grid = np.arange(0.5, x.max() + 1e-9, 0.5)
    c_grid = interp(grid) / 100
    dfs = []
    for t, c in zip(grid, c_grid):
        if np.isclose(t, 0.5):
            df_t = 1 / (1 + c / 2)                    # 6M: single cash flow
        else:
            df_t = (1 - c / 2 * sum(dfs)) / (1 + c / 2)
        dfs.append(df_t)
        zeros[float(t)] = 200 * (df_t ** (-1 / (2 * t)) - 1)   # % semi-annual

    return pd.Series(zeros).sort_index()


def treasury_zero_curves(d1, d2):
    ref = d1 or date.today()
    years = {ref.year, (d2 or ref).year}
    if ref.month == 1:                       # previous trading day may be last year
        years.add(ref.year - 1)
    par = load_treasury(years)
    day_a, day_b = pick_days(par.index, d1, d2)
    za = semi_to_cont(bootstrap_zero(par.loc[day_a]))   # work in continuous internally
    zb = semi_to_cont(bootstrap_zero(par.loc[day_b]))
    return day_a, day_b, za, zb, "U.S. Treasury par curve, bootstrapped"


# ----------------------------------------------------------------------- source 2: GSW
def load_gsw() -> pd.DataFrame:
    r = requests.get(GSW_URL, headers=HEADERS, timeout=120)
    r.raise_for_status()
    lines = r.text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Date"))
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), na_values=["NA", "-999.99"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    return df[["BETA0", "BETA1", "BETA2", "BETA3", "TAU1", "TAU2"]].dropna(
        subset=["BETA0", "BETA1", "BETA2", "TAU1"])


def nss_zero(p: pd.Series, n: np.ndarray) -> np.ndarray:
    """Svensson zero yield, % continuously compounded, maturity n in years."""
    b0, b1, b2, b3, t1, t2 = (p[k] for k in ["BETA0", "BETA1", "BETA2", "BETA3", "TAU1", "TAU2"])
    b3 = 0.0 if pd.isna(b3) else b3
    t2 = 1.0 if pd.isna(t2) else t2
    x1, x2 = n / t1, n / t2
    f1 = (1 - np.exp(-x1)) / x1
    f2 = f1 - np.exp(-x1)
    f3 = (1 - np.exp(-x2)) / x2 - np.exp(-x2)
    return b0 + b1 * f1 + b2 * f2 + b3 * f3


def gsw_zero_curves(d1, d2):
    params = load_gsw()
    day_a, day_b = pick_days(params.index, d1, d2)
    grid = np.unique(np.r_[list(STD_TENORS.values()), np.arange(0.25, 30.0001, 0.25)])
    za = pd.Series(nss_zero(params.loc[day_a], grid), index=grid)
    zb = pd.Series(nss_zero(params.loc[day_b], grid), index=grid)
    return day_a, day_b, za, zb, "Fed Gürkaynak-Sack-Wright (NSS fit)"


# ------------------------------------------------------------------------------- plot
def plot(day_a, day_b, za, zb, source, comp, save=None):
    conv = (lambda s: s) if comp == "cont" else cont_to_semi
    za, zb = conv(za), conv(zb)
    comp_lbl = "continuously compounded" if comp == "cont" else "semi-annual (BEY)"

    # values at the standard tenors (interpolated on the zero curve where needed)
    def at_std(z):
        return pd.Series({k: np.interp(t, z.index, z.values)
                          for k, t in STD_TENORS.items() if z.index.min() - 1e-9 <= t <= z.index.max() + 1e-9})
    sa, sb = at_std(za), at_std(zb)
    common = sa.index.intersection(sb.index)
    chg = (sb[common] - sa[common]) * 100
    xs = np.array([STD_TENORS[k] for k in common])

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1.3]})
    for z, s, day, col in [(za, sa, day_a, "#7a8ca3"), (zb, sb, day_b, "#1f4e8c")]:
        ax1.plot(z.index, z.values, "-", lw=2, color=col, label=f"{day:%a %Y-%m-%d}")
        ax1.plot([STD_TENORS[k] for k in s.index], s.values, "o", color=col, ms=5)
    ax1.set_ylabel(f"Zero rate (%, {comp_lbl})")
    ax1.set_title(f"US Treasury Zero-Coupon Yield Curve — {source}")
    ax1.grid(alpha=0.3)
    ax1.legend()

    k = 1.12                                  # equal bar width on log axis
    ax2.bar(xs / k, chg.values, width=xs * k - xs / k, align="edge",
            color=["#2e8b57" if v < 0 else "#c0392b" for v in chg.values])
    ax2.axhline(0, color="black", lw=0.8)
    for x, v in zip(xs, chg.values):
        ax2.annotate(f"{v:+.1f}", (x, v), textcoords="offset points",
                     xytext=(0, 3 if v >= 0 else -11), ha="center", fontsize=8)
    lo, hi = min(0, chg.min()), max(0, chg.max())
    pad = max(1.0, 0.25 * (hi - lo))
    ax2.set_ylim(lo - pad, hi + pad)
    ax2.set_ylabel("Change (bp)")
    ax2.grid(alpha=0.3, axis="y")

    ax2.set_xscale("log")
    ax2.set_xticks(xs)
    ax2.set_xticklabels(list(common), fontsize=9)
    ax2.minorticks_off()
    ax2.set_xlabel("Maturity")

    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=150)
        print(f"Saved chart to {save}")
    plt.show()

    table = pd.DataFrame({f"{day_a:%Y-%m-%d}": sa[common].round(3),
                          f"{day_b:%Y-%m-%d}": sb[common].round(3),
                          "Δ (bp)": chg.round(1)})
    print(f"\nZero rates, % {comp_lbl} — {source}")
    print(table.to_string())
    return table


def main():
    p = argparse.ArgumentParser(description="Plot US Treasury zero curves for two days.")
    p.add_argument("dates", nargs="*", help="YYYY-MM-DD (0, 1 or 2 dates)")
    p.add_argument("--source", choices=["treasury", "gsw"], default="treasury")
    p.add_argument("--comp", choices=["cont", "semi"], default="cont",
                   help="compounding of displayed zero rates (default: continuous)")
    p.add_argument("--save", help="save chart to this PNG path")
    args = p.parse_args()

    ds = [datetime.strptime(s, "%Y-%m-%d").date() for s in args.dates[:2]]
    d1 = ds[0] if ds else None
    d2 = ds[1] if len(ds) > 1 else None

    fn = treasury_zero_curves if args.source == "treasury" else gsw_zero_curves
    day_a, day_b, za, zb, source = fn(d1, d2)
    plot(day_a, day_b, za, zb, source, args.comp, args.save)


if __name__ == "__main__":
    main()
