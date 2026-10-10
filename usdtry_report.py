"""
USD/TRY tail-day report — data, charts, news and HTML, from free public sources.

What it does
 1. USD/TRY daily rates      ECB reference rates via Frankfurter (default),
                             falls back to Yahoo Finance chart data.
 2. Tail-day test            last N trading days ("last week", default 5) vs the
                             prior 252 daily log returns ("last year"); flags days
                             below the 5th / above the 95th percentile.
 3. US zero curves           for each flagged day and the previous US trading day:
                             bootstrapped from the U.S. Treasury par curve (default)
                             or the Fed's Gürkaynak-Sack-Wright fit (--curve-source gsw).
                             If nothing is flagged, the week's most extreme day is
                             used and labelled as such (turn off with --no-fallback).
 4. News                     US/Turkey macro & political headlines around the event day(s)
                             from GDELT, Google News RSS and Yahoo RSS (news_fetch.py), or
                             your own list with --news; then scored against the up- or
                             down-day prompt (news_relevance.py, prompts.json) and the
                             top --relevant-top (5) are shown first in the report.
 5. Turkish lira curve       for each event day and the previous Turkish trading day, runs
                             tbliste_import.py and ttb_import.py with that date (they read
                             ~/BorsaIstanbul/BAP/<date>/ and write definitions.xlsx and
                             Market_Data.xlsx into try_curve_<date>/inputs/), fits the curve with
                             try_zero_curve.py into try_curve_<date>/, and adds the lira zero
                             curves, their daily change and the TRY−USD spread to the report.
                             Dates without BAP files fall back to an existing try_curve_<date>/.
 6. Output folder            report.html (interactive charts), PNG charts, CSV data,
                             news.json and a run log.

Usage
    python usdtry_report.py                               # latest week, everything live
    python usdtry_report.py --end 2026-10-06 --pct 5 --days 5
    python usdtry_report.py --curve-source gsw --comp semi
    python usdtry_report.py --news news.json              # curated headlines instead of auto
    python usdtry_report.py --no-news                     # skip the news step
    python usdtry_report.py --fx-csv usdtry.csv --par-csv par.csv   # offline inputs

Requires: pip install -r requirements.txt
"""

import argparse
import json
import re
import subprocess
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("MPLBACKEND", "Agg")      # charts are saved, never shown

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import news_fetch                                # noqa: E402
import news_relevance                            # noqa: E402
import ust_zero_curve as uzc                     # noqa: E402

FX_URL = "https://api.frankfurter.dev/v1/{start}..{end}?base=USD&symbols=TRY"
YAHOO_FX_URL = "https://query1.finance.yahoo.com/v8/finance/chart/TRY=X?period1={p1}&period2={p2}&interval=1d"
STD = ["1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"]

LOG = []


def log(msg):
    print(msg)
    LOG.append(str(msg))


# ============================================================== FX data
def fetch_fx_ecb(start: date, end: date) -> pd.Series:
    """Daily USD/TRY, ECB reference (~14:15 CET), in 60-day chunks (keeps daily resolution)."""
    out, s = {}, start
    while s <= end:
        e = min(s + timedelta(days=60), end)
        r = requests.get(FX_URL.format(start=s, end=e), headers=uzc.HEADERS, timeout=30)
        r.raise_for_status()
        for d, v in r.json().get("rates", {}).items():
            out[d] = v["TRY"]
        s = e + timedelta(days=1)
    ser = pd.Series(out, dtype=float)
    ser.index = pd.to_datetime(ser.index)
    return ser.sort_index().rename("usdtry")


def fetch_fx_yahoo(start: date, end: date) -> pd.Series:
    """Daily USD/TRY close from Yahoo Finance's public chart endpoint (unofficial)."""
    p1 = int(datetime(start.year, start.month, start.day).timestamp())
    p2 = int(datetime(end.year, end.month, end.day).timestamp()) + 86400
    r = requests.get(YAHOO_FX_URL.format(p1=p1, p2=p2), headers=news_fetch.UA, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    # Yahoo stamps FX bars at local (London) midnight; shift by gmtoffset so the date is right
    off = int(res.get("meta", {}).get("gmtoffset", 0))
    idx = pd.to_datetime(np.asarray(res["timestamp"]) + off, unit="s").normalize()
    ser = pd.Series(res["indicators"]["quote"][0]["close"], index=idx, dtype=float).dropna()
    ser = ser[~ser.index.duplicated(keep="last")]
    return ser[ser.index.dayofweek < 5].sort_index().rename("usdtry")


def get_fx(args, start, end):
    if args.fx_csv:
        df = pd.read_csv(args.fx_csv, parse_dates=[0], index_col=0)
        return df.iloc[:, 0].astype(float).sort_index().rename("usdtry"), f"local file {Path(args.fx_csv).name}"
    order = ["ecb", "yahoo"] if args.fx_source == "ecb" else ["yahoo", "ecb"]
    for src in order:
        try:
            s = fetch_fx_ecb(start, end) if src == "ecb" else fetch_fx_yahoo(start, end)
            if len(s) > 50:
                label = ("ECB euro reference rates (≈14:15 CET) via the Frankfurter API" if src == "ecb"
                         else "Yahoo Finance daily close (TRY=X)")
                log(f"FX: {len(s)} days from {src}")
                return s, label
        except Exception as ex:
            log(f"FX source {src} failed: {type(ex).__name__}: {ex}")
    raise SystemExit("Could not download USD/TRY from any source.")


# ============================================================== curves
def read_par_csv(path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["Date"] = pd.to_datetime(df["Date"], format="%m/%d/%Y")
    df = df.set_index("Date").sort_index()
    mats = {c: uzc.tenor_to_years(c) for c in df.columns}
    df = df[[c for c, t in mats.items() if t is not None]]
    df.columns = [mats[c] for c in df.columns]
    return df[sorted(df.columns)]


class CurveSource:
    """Returns zero curves (% continuously compounded, index = maturity in years)."""

    def __init__(self, kind, par_csv=None):
        self.kind, self.par_csv, self._par, self._gsw = kind, par_csv, None, None
        self.label = ("U.S. Treasury par curve (CMT), bootstrapped to zero rates" if kind == "treasury"
                      else "Federal Reserve Gürkaynak–Sack–Wright fitted zero curve")

    def index(self, years):
        if self.kind == "treasury":
            if self._par is None:
                self._par = read_par_csv(self.par_csv) if self.par_csv else uzc.load_treasury(years)
            return self._par.index
        if self._gsw is None:
            self._gsw = uzc.load_gsw()
        return self._gsw.index

    def zero(self, day):
        if self.kind == "treasury":
            return uzc.semi_to_cont(uzc.bootstrap_zero(self._par.loc[day]))
        grid = np.unique(np.r_[list(uzc.STD_TENORS.values()), np.arange(0.25, 30.0001, 0.25)])
        return pd.Series(uzc.nss_zero(self._gsw.loc[day], grid), index=grid)


def zero_pair(src: CurveSource, event: str, comp: str):
    ev = date.fromisoformat(event)
    idx = src.index({ev.year, ev.year - 1} if ev.month == 1 else {ev.year})
    # last curve date on/before the event, and the one before it; refuse stale data
    pos = idx.searchsorted(pd.Timestamp(ev), side="right") - 1
    if pos < 1 or (pd.Timestamp(ev) - idx[pos]).days > 4:
        raise ValueError(f"no curve data within 4 days of {ev} (data covers {idx[0].date()}..{idx[-1].date()})")
    day_a, day_b = idx[pos - 1], idx[pos]
    conv = (lambda z: z) if comp == "cont" else uzc.cont_to_semi
    za, zb = conv(src.zero(day_a)), conv(src.zero(day_b))
    std_t = [uzc.STD_TENORS[k] for k in STD]
    sa = np.interp(std_t, za.index, za.values)
    sb = np.interp(std_t, zb.index, zb.values)
    return dict(
        event=event, prev=day_a.strftime("%Y-%m-%d"), day=day_b.strftime("%Y-%m-%d"),
        shifted=day_b.date() != ev,
        curve_prev=[[float(t), float(v)] for t, v in za.items()],
        curve_day=[[float(t), float(v)] for t, v in zb.items()],
        tenors=STD, std_t=std_t, std_prev=sa.round(4).tolist(), std_day=sb.round(4).tolist(),
        chg_bp=((sb - sa) * 100).round(2).tolist(),
    )


# ============================================================== TRY curves (from try_zero_curve.py output)
TRY_CSV = "try_zero_curve.csv"


def _try_folders(try_dir: Path):
    """{date: folder} for every try_curve_YYYY-MM-DD (or try_curve_YYYY_MM_DD) folder with a try_zero_curve.csv."""
    found = {}
    for f in Path(try_dir).glob("try_curve_*"):
        m = re.fullmatch(r"try_curve_(\d{4})[-_](\d{2})[-_](\d{2})", f.name)
        if f.is_dir() and m and (f / TRY_CSV).exists():
            found[date(int(m[1]), int(m[2]), int(m[3]))] = f
    return found


def _read_try_csv(path: Path) -> pd.DataFrame:
    """Read try_zero_curve.csv as written by try_zero_curve.py, or after Excel re-saved it
    (semicolons, decimal commas, renamed headers)."""
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    first = raw.splitlines()[0] if raw.strip() else ""
    sep = ";" if first.count(";") > first.count(",") else ("\t" if "\t" in first else ",")
    df = pd.read_csv(path, sep=sep, encoding="utf-8-sig", dtype=str)
    df.columns = [re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_") for c in df.columns]

    def num(col):
        return pd.to_numeric(df[col].astype(str).str.strip().str.replace(",", ".", regex=False), errors="coerce")

    def find(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    tenor, years = find("tenor", "vade"), find("years", "yil", "maturity_years")
    cc = find("zero_cc_pct", "zero_cc", "zero_continuous_pct")
    ann = find("zero_annual_pct", "zero_annual", "zero_rate_pct", "zero_pct", "zero")
    if not (tenor and years and (cc or ann)):
        raise ValueError(f"{path} doesn't look like try_zero_curve.py output. Columns found: "
                         + ", ".join(df.columns) + ". Needed: tenor, years and zero_cc_pct or zero_annual_pct.")
    out = pd.DataFrame({"tenor": df[tenor].str.strip(), "years": num(years)})
    a = num(ann) if ann else np.expm1(num(cc) / 100) * 100          # % annual compounding
    out["zero_annual_pct"] = a
    out["zero_cc_pct"] = num(cc) if cc else np.log1p(a / 100) * 100  # % continuous compounding
    ext = find("extrapolated")
    out["extrapolated"] = (df[ext].astype(str).str.strip().str.lower().isin(["true", "1", "doğru", "dogru", "yes"])
                           if ext else False)
    out = out.dropna(subset=["years", "zero_annual_pct"])
    if out.empty:
        raise ValueError(f"{path}: no numeric zero rates could be read")
    return out.sort_values("years").reset_index(drop=True)


TRY_COMP_LABEL = {"annual": "annually compounded", "cont": "continuously compounded",
                  "semi": "semi-annual (bond-equivalent)"}


def convert_rate(pct, src: str, dst: str):
    """Convert % zero rates between 'annual', 'cont' and 'semi' compounding."""
    r = np.asarray(pct, float) / 100
    cc = {"annual": np.log1p(r), "cont": r, "semi": 2 * np.log1p(r / 2)}[src]
    out = {"annual": np.expm1(cc), "cont": cc, "semi": 2 * np.expm1(cc / 2)}[dst]
    return out * 100


def _read_try(folder: Path, comp: str):
    """comp 'annual' (default) reads zero_annual_pct as is; 'cont' reads zero_cc_pct; 'semi' converts."""
    df = _read_try_csv(folder / TRY_CSV)
    if comp == "annual":
        vals = df["zero_annual_pct"]
    elif comp == "cont":
        vals = df["zero_cc_pct"]
    else:                                                  # annual -> semi-annual (bond-equivalent)
        vals = 200 * ((1 + df["zero_annual_pct"] / 100) ** 0.5 - 1)
    return df["tenor"].tolist(), df["years"].tolist(), vals.astype(float).tolist(), df["extrapolated"].astype(bool).tolist()


def try_pair(c: dict, try_dir: Path, comp: str = "annual", max_gap_days=7, prev: date = None, us_comp: str = "cont"):
    """Lira zero curves for the USD/TRY event day and the previous day (given, or the latest earlier
    folder), plus the change and the TRY−USD spread on the event day (same compounding as the US curve)."""
    folders = _try_folders(try_dir)
    ev = date.fromisoformat(c["event"])
    if ev not in folders:
        raise FileNotFoundError(f"no try_curve_{ev} folder with {TRY_CSV} in {Path(try_dir).resolve()}")
    if prev is None:
        earlier = [d for d in folders if ev - timedelta(days=max_gap_days) <= d < ev]
        if not earlier:
            raise FileNotFoundError(f"no try_curve folder in the {max_gap_days} days before {ev}")
        prev = max(earlier)
    elif prev not in folders:
        raise FileNotFoundError(f"no try_curve_{prev} folder with {TRY_CSV} in {Path(try_dir).resolve()}")
    tn, yrs, v_day, ext = _read_try(folders[ev], comp)
    tn_p, yrs_p, v_prev, _ = _read_try(folders[prev], comp)
    v_prev = np.interp(yrs, yrs_p, v_prev).tolist()        # align tenors if the two files differ
    us_day = convert_rate(np.interp(yrs, c["std_t"], c["std_day"]), us_comp, comp)   # US event day, TRY's compounding
    synthetic = any((folders[d] / "SYNTHETIC_README.txt").exists() for d in (ev, prev))
    return dict(
        prev=prev.isoformat(), day=ev.isoformat(), tenors=tn, years=yrs,
        prev_vals=np.round(v_prev, 4).tolist(), day_vals=np.round(v_day, 4).tolist(),
        chg_bp=np.round((np.array(v_day) - np.array(v_prev)) * 100, 2).tolist(),
        spread_pts=np.round(np.array(v_day) - us_day, 3).tolist(), extrapolated=ext,
        folders=[folders[prev].name, folders[ev].name], synthetic=synthetic,
        comp=comp, comp_label=TRY_COMP_LABEL[comp],
    )


# ============================================================== TRY curves built from Borsa Istanbul BAP files
# For each date: tbliste_import.py DD.MM.YYYY -> definitions.xlsx, ttb_import.py DD.MM.YYYY -> Market_Data.xlsx
# (both run inside try_curve_<date>/inputs/), then try_zero_curve.py fits the curve into try_curve_<date>/.
def _load_script_module(scripts_dir: Path, name: str):
    import importlib.util
    path = Path(scripts_dir) / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found (set --bap-scripts to the folder with {name}.py)")
    spec = importlib.util.spec_from_file_location(f"_bap_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bap_files(scripts_dir: Path, d: date):
    """Paths of the ttb and tbliste files the import scripts would read for date d."""
    dt = datetime(d.year, d.month, d.day)
    ttb = _load_script_module(scripts_dir, "ttb_import").ttb_path(dt)
    tbl = _load_script_module(scripts_dir, "tbliste_import").tbliste_path(dt)
    return ttb, tbl


def bap_available(scripts_dir: Path, d: date) -> bool:
    return all(p.exists() for p in bap_files(scripts_dir, d))


def _run(cmd, cwd, what):
    r = subprocess.run([str(x) for x in cmd], cwd=str(cwd), capture_output=True, text=True)
    out = (r.stdout or "").strip().splitlines()
    err = (r.stderr or "").strip().splitlines()
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed: " + (err[-1] if err else out[-1] if out else f"exit code {r.returncode}"))
    for line in err:
        if "WARNING" in line:
            log(f"    {what}: {line.strip()}")
    return out


def build_try_curve(d: date, try_dir: Path, scripts_dir: Path, model="ns", settle_days=0) -> Path:
    """Run the two import scripts for date d, then fit the lira curve into try_curve_<d>/."""
    folder = Path(try_dir) / f"try_curve_{d.isoformat()}"
    inputs = folder / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    for f in ("definitions.xlsx", "Market_Data.xlsx"):          # never fit on a previous day's files
        (inputs / f).unlink(missing_ok=True)
    ds = d.strftime("%d.%m.%Y")
    for script in ("tbliste_import.py", "ttb_import.py"):
        out = _run([sys.executable, Path(scripts_dir) / script, ds], inputs, f"{script} {ds}")
        saved = [l for l in out if l.startswith("Saved")]
        log(f"    {script} {ds}: {saved[-1] if saved else 'done'}")
    for f in ("definitions.xlsx", "Market_Data.xlsx"):
        if not (inputs / f).exists():
            raise RuntimeError(f"{f} was not created for {ds}")
    (folder / "SYNTHETIC_README.txt").unlink(missing_ok=True)   # replaced by real data
    out = _run([sys.executable, HERE / "try_zero_curve.py", "--defs", inputs / "definitions.xlsx",
                "--market", inputs / "Market_Data.xlsx", "--date", d.isoformat(),
                "--settle-days", settle_days, "--model", model, "--out-dir", folder], HERE, f"try_zero_curve.py {ds}")
    fit = [l.strip() for l in out if "fit:" in l or "bonds used" in l]
    log(f"    try_zero_curve.py {ds}: " + " | ".join(fit))
    return folder


def ensure_try_curves(ev: date, try_dir: Path, scripts_dir: Path, model="ns", settle_days=0, max_gap_days=7):
    """Event day + previous Turkish trading day: rebuild each from BAP files when they exist,
    otherwise reuse an existing try_curve_<date>/ folder. Returns the previous day used."""
    def ok(d):
        if bap_available(scripts_dir, d):
            log(f"  TRY {d}: building from Borsa Istanbul files")
            build_try_curve(d, try_dir, scripts_dir, model, settle_days)
            return True
        if d in _try_folders(try_dir):
            log(f"  TRY {d}: no BAP files found ({', '.join(str(p) for p in bap_files(scripts_dir, d))}); "
                f"reusing existing {_try_folders(try_dir)[d].name}")
            return True
        return False

    if not ok(ev):
        ttb, tbl = bap_files(scripts_dir, ev)
        raise FileNotFoundError(f"no BAP files for {ev} ({ttb}, {tbl}) and no try_curve_{ev} folder")
    for k in range(1, max_gap_days + 1):                      # previous day with data (skips weekends/holidays)
        d = ev - timedelta(days=k)
        if d.weekday() < 5 and ok(d):
            return d
    raise FileNotFoundError(f"no BAP files or try_curve folder in the {max_gap_days} days before {ev}")


# ============================================================== analysis
def analyse(px: pd.Series, end, n_days: int, lookback: int, pct: float):
    if end:
        px = px[px.index <= pd.Timestamp(end)]
    r = np.log(px).diff().dropna() * 100
    week = r.iloc[-n_days:]
    hist = r.iloc[:-n_days].iloc[-lookback:]
    if len(hist) < lookback * 0.9:
        raise SystemExit(f"Only {len(hist)} history days; need ~{lookback}. Extend the download window.")
    lo, hi = np.percentile(hist, [pct, 100 - pct])
    rows = []
    for d, v in week.items():
        same_wd = hist[hist.index.dayofweek == d.dayofweek]
        rows.append(dict(date=d.strftime("%Y-%m-%d"), dow=d.strftime("%a"), px=float(px.loc[d]), ret=float(v),
                         rank=float((hist <= v).mean() * 100),
                         rank_wd=float((same_wd <= v).mean() * 100), n_wd=int(len(same_wd)),
                         flag="low" if v < lo else "high" if v > hi else ""))
    return px, r, week, hist, lo, hi, rows


# ============================================================== PNG charts
def png_week(rows, lo, hi, pct, path):
    fig, ax = plt.subplots(figsize=(9, 4))
    x = np.arange(len(rows))
    vals = [r["ret"] for r in rows]
    ax.axhspan(lo, hi, color="#2a78d6", alpha=0.08, label=f"{pct:g}th–{100-pct:g}th pct, last year")
    for y in (lo, hi):
        ax.axhline(y, ls="--", lw=1, color="#7a8191")
    ax.bar(x, vals, color=["#d95926" if r["flag"] else "#2a78d6" for r in rows], width=0.55)
    for i, r in enumerate(rows):
        ax.annotate(f"{r['ret']:+.3f}%", (i, r["ret"]), xytext=(0, 4 if r["ret"] >= 0 else -12),
                    textcoords="offset points", ha="center", fontsize=8)
        if r["flag"]:
            ax.annotate("TAIL", (i, r["ret"]), xytext=(0, 16), textcoords="offset points",
                        ha="center", fontsize=8, color="#d95926", weight="bold")
    top = max(hi, *vals); bot = min(lo, *vals, 0)
    ax.set_ylim(bot - 0.15 * (top - bot), top + 0.35 * (top - bot))     # room for labels + legend
    ax.set_xticks(x, [f"{r['dow']}\n{r['date'][5:]}" for r in rows])
    ax.set_ylabel("daily log return (%)")
    ax.set_title("USD/TRY daily returns, last week vs last year's band")
    ax.legend(loc="upper right", fontsize=8, frameon=False)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout(); fig.savefig(path, dpi=150); plt.close(fig)


def png_hist(hist, rows, lo, hi, path):
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.hist(hist, bins=40, color="#9ec5f4")
    for y, t in ((lo, "p-low"), (hi, "p-high")):
        ax.axvline(y, ls="--", lw=1, color="#7a8191")
    top = ax.get_ylim()[1]
    for r in rows:
        ax.plot(r["ret"], top * 0.95, "o", color="#d95926" if r["flag"] else "#2a78d6", ms=7)
    ax.set_xlabel("daily log return (%)"); ax.set_ylabel("days")
    ax.set_title("Last year's daily returns, last week's days marked")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout(); fig.savefig(path, dpi=150); plt.close(fig)


def png_curve(c, comp_label, path):
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1.3]})
    for key, col, lbl in (("curve_prev", "#9aa3b2", c["prev"]), ("curve_day", "#2a78d6", c["day"])):
        t, v = zip(*c[key]); a1.plot(t, v, color=col, lw=2, label=lbl)
    a1.plot(c["std_t"], c["std_day"], "o", color="#2a78d6", ms=4)
    a1.set_ylabel(f"zero rate (%, {comp_label})"); a1.legend(frameon=False); a1.grid(alpha=0.3)
    a1.set_title(f"US Treasury zero curve: {c['prev']} vs {c['day']}")
    xs = np.array(c["std_t"]); k = 1.12
    a2.bar(xs / k, c["chg_bp"], width=xs * k - xs / k, align="edge",
           color=["#d95926" if v >= 0 else "#256abf" for v in c["chg_bp"]])
    a2.axhline(0, color="black", lw=0.8); a2.set_ylabel("Δ bp"); a2.grid(axis="y", alpha=0.3)
    a2.set_xscale("log"); a2.set_xticks(xs, c["tenors"]); a2.minorticks_off()
    fig.tight_layout(); fig.savefig(path, dpi=150); plt.close(fig)


def png_try_curve(c, comp_label, path):
    t_ = c["try"]
    x = np.arange(len(t_["tenors"]))
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1.3]})
    a1.plot(x, t_["prev_vals"], "o-", color="#9aa3b2", lw=2, label=t_["prev"])
    a1.plot(x, t_["day_vals"], "o-", color="#c0392b", lw=2, label=t_["day"])
    a1.set_ylabel(f"zero rate (%, {comp_label})"); a1.legend(frameon=False); a1.grid(alpha=0.3)
    a1.set_title(f"Turkish lira government zero curve: {t_['prev']} vs {t_['day']}"
                 + ("  [SYNTHETIC TEST DATA]" if t_["synthetic"] else ""))
    a2.bar(x, t_["chg_bp"], color=["#d95926" if v >= 0 else "#256abf" for v in t_["chg_bp"]], width=0.55)
    a2.axhline(0, color="black", lw=0.8); a2.set_ylabel("Δ bp"); a2.grid(axis="y", alpha=0.3)
    a2.set_xticks(x, [tn + ("*" if e else "") for tn, e in zip(t_["tenors"], t_["extrapolated"])])
    if any(t_["extrapolated"]):
        a2.set_xlabel("* outside the maturity range of the bonds (extrapolated)")
    fig.tight_layout(); fig.savefig(path, dpi=150); plt.close(fig)


# ============================================================== main
def main():
    p = argparse.ArgumentParser(description="USD/TRY tail-day report with UST zero curves and news.")
    p.add_argument("--end", help="last day of the week to inspect (YYYY-MM-DD); default: latest data")
    p.add_argument("--days", type=int, default=5, help="trading days in 'last week' (default 5)")
    p.add_argument("--lookback", type=int, default=252, help="history window in trading days")
    p.add_argument("--pct", type=float, default=5, help="tail percentile (5 -> 5th/95th)")
    p.add_argument("--fx-source", choices=["ecb", "yahoo"], default="ecb", help="preferred FX source")
    p.add_argument("--curve-source", choices=["treasury", "gsw"], default="treasury")
    p.add_argument("--comp", choices=["cont", "semi"], default="cont", help="zero-rate compounding")
    p.add_argument("--no-fallback", action="store_true", help="no curves/news if no tail day")
    p.add_argument("--news", help="curated headlines JSON (skips automatic news collection)")
    p.add_argument("--no-news", action="store_true", help="skip the news section")
    p.add_argument("--news-days-before", type=int, default=4, help="news window starts this many days before the event")
    p.add_argument("--news-top", type=int, default=20, help="candidate headlines to collect before relevance filtering")
    p.add_argument("--relevant-top", type=int, default=5, help="headlines kept in the report after relevance scoring (0 = keep all)")
    p.add_argument("--sim-backend", choices=["auto", "tfidf", "ollama"], default="auto",
                   help="relevance scoring: tokenizer TF-IDF, local Ollama embeddings, or auto (Ollama if running)")
    p.add_argument("--ollama-model", default="nomic-embed-text", help="Ollama embedding model")
    p.add_argument("--prompts", help="up/down day prompts JSON (default: prompts.json)")
    p.add_argument("--news-sources", default="gdelt,google,yahoo")
    p.add_argument("--fx-csv", help="local USD/TRY file (date,rate) instead of downloading")
    p.add_argument("--par-csv", help="local Treasury par-curve CSV (Treasury's format) instead of downloading")
    p.add_argument("--try-dir", default=".", help="folder where try_curve_<date>/ folders are written and read")
    p.add_argument("--try-source", choices=["bap", "folders"], default="bap",
                   help="bap (default): run tbliste_import.py + ttb_import.py + try_zero_curve.py for the event day "
                        "and the previous day; folders: only read existing try_curve_<date>/ folders")
    p.add_argument("--bap-scripts", default=str(HERE), help="folder containing ttb_import.py and tbliste_import.py")
    p.add_argument("--try-model", choices=["ns", "nss"], default="ns", help="lira curve model for try_zero_curve.py")
    p.add_argument("--try-settle-days", type=int, default=0, help="settlement lag for the lira bonds")
    p.add_argument("--try-comp", choices=["annual", "cont", "semi"], default="annual",
                   help="compounding of the lira rates shown: annual (default, zero_annual_pct), cont "
                        "(zero_cc_pct) or semi; the US curve is converted to match for the TRY−USD spread")
    p.add_argument("--no-try", action="store_true", help="leave the Turkish lira curve out of the report")
    p.add_argument("--out-dir", help="output folder (default: report_<week end>)")
    a = p.parse_args()

    end = date.fromisoformat(a.end) if a.end else None
    ref_end = end or date.today()

    # 1) FX
    px, fx_label = get_fx(a, ref_end - timedelta(days=int(a.lookback * 1.6) + 30), ref_end)
    px, r, week, hist, lo, hi, rows = analyse(px, end, a.days, a.lookback, a.pct)
    log(f"Week {rows[0]['date']} .. {rows[-1]['date']} | history {hist.index[0].date()} .. {hist.index[-1].date()} "
        f"(n={len(hist)}) | p{a.pct:g}={lo:+.4f}%  p{100-a.pct:g}={hi:+.4f}%")
    for x in rows:
        log(f"  {x['date']} {x['dow']}  {x['ret']:+.4f}%  pct {x['rank']:5.1f}  {x['flag'] or '-'}")

    out = Path(a.out_dir or f"report_{rows[-1]['date']}")
    out.mkdir(parents=True, exist_ok=True)

    # 2) events
    flagged = [x for x in rows if x["flag"]]
    fallback = False
    if flagged:
        events = [x["date"] for x in flagged]
    elif a.no_fallback:
        events = []
    else:
        fallback = True
        events = [max(rows, key=lambda x: abs(x["rank"] - 50))["date"]]
        log(f"No tail day; using the most extreme day {events[0]} instead.")

    # 3) curves
    src = CurveSource(a.curve_source, a.par_csv)
    curves = []
    for e in events:
        try:
            curves.append(zero_pair(src, e, a.comp))
        except Exception as ex:
            log(f"Zero curve for {e} failed: {type(ex).__name__}: {ex}")
            if a.curve_source == "treasury" and not a.par_csv:
                log("  retrying with the Fed GSW curve")
                src = CurveSource("gsw")
                try:
                    curves.append(zero_pair(src, e, a.comp))
                except Exception as ex2:
                    log(f"  GSW also failed: {ex2}")
    for c in curves:
        log(f"Zero curves {c['prev']} vs {c['day']}: Δbp " + ", ".join(f"{t} {v:+.1f}" for t, v in zip(c["tenors"], c["chg_bp"])))
    if not a.no_try:
        Path(a.try_dir).mkdir(parents=True, exist_ok=True)
        for c in curves:
            try:
                prev_try = None
                if a.try_source == "bap":
                    log(f"TRY curves for event day {c['event']}:")
                    prev_try = ensure_try_curves(date.fromisoformat(c["event"]), Path(a.try_dir), Path(a.bap_scripts),
                                                 a.try_model, a.try_settle_days)
                c["try"] = try_pair(c, Path(a.try_dir), a.try_comp, prev=prev_try, us_comp=a.comp)
                t_ = c["try"]
                log(f"TRY zero curves {t_['prev']} vs {t_['day']} from {', '.join(t_['folders'])}"
                    + (" (SYNTHETIC test data)" if t_["synthetic"] else "") + ": Δbp "
                    + ", ".join(f"{t} {v:+.1f}" for t, v in zip(t_["tenors"], t_["chg_bp"])))
            except Exception as ex:
                log(f"TRY curve for {c['event']} skipped: {ex}")

    # 4) news
    news = None
    if a.news:
        news = json.loads(Path(a.news).read_text(encoding="utf-8"))
    elif not a.no_news and events:
        ev_dates = [date.fromisoformat(e) for e in events]
        try:
            news = news_fetch.collect(min(ev_dates) - timedelta(days=a.news_days_before), max(ev_dates),
                                      focus_days=ev_dates, top=a.news_top,
                                      sources=tuple(a.news_sources.split(",")), log=log)
            if not news["items"]:
                log("News: no matching headlines found; section omitted.")
                news = None
        except Exception as ex:
            log(f"News collection failed: {type(ex).__name__}: {ex}")

    # 4b) relevance: compare headlines with the up- or down-day prompt, keep the top ones
    if news and news.get("items") and a.relevant_top > 0:
        ev_rows = [x for x in rows if x["date"] in events] or rows
        main_ev = max(ev_rows, key=lambda x: abs(x["rank"] - 50))
        direction = "up" if (main_ev["flag"] == "high" or (not main_ev["flag"] and main_ev["rank"] >= 50)) else "down"
        news = news_relevance.score_news(news, direction, a.relevant_top, a.sim_backend, a.ollama_model, a.prompts, log=log)
        news["relevance"]["event"] = main_ev["date"]
        log(f"Relevance ({direction} day {main_ev['date']}): top {a.relevant_top} of {len(news['items'])}")
        for it in news["items"][: a.relevant_top]:
            log(f"  {it['relevance']:.3f}  {it['title'][:90]}")

    # 5) outputs
    comp_label = "continuously compounded" if a.comp == "cont" else "semi-annual (bond-equivalent)"
    counts, edges = np.histogram(hist, bins=40)
    payload = dict(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        week_start=rows[0]["date"], week_end=rows[-1]["date"],
        hist_start=hist.index[0].strftime("%Y-%m-%d"), hist_end=hist.index[-1].strftime("%Y-%m-%d"),
        n_hist=int(len(hist)), pct=a.pct, lo=float(lo), hi=float(hi),
        hist_mean=float(hist.mean()), hist_sd=float(hist.std()), week_cum=float(week.sum()), rows=rows,
        hist_bins=[[float((edges[i] + edges[i + 1]) / 2), int(c)] for i, c in enumerate(counts)],
        bin_w=float(edges[1] - edges[0]), flagged=[x["date"] for x in flagged], fallback=fallback,
        curves=curves, comp=comp_label, fx_label=fx_label, curve_label=src.label, news=news,
    )
    tpl = (HERE / "report_template.html").read_text(encoding="utf-8")
    body = tpl.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    (out / "report_body.html").write_text(body, encoding="utf-8")      # for embedding / publishing
    (out / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")  # read by build_site.py
    (out / "report.html").write_text(
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>\n'
        + body + "\n</body></html>", encoding="utf-8")

    png_week(rows, lo, hi, a.pct, out / "week_returns.png")
    png_hist(hist, rows, lo, hi, out / "returns_histogram.png")
    for c in curves:
        png_curve(c, comp_label, out / f"zero_curve_{c['day']}.png")
        if c.get("try"):
            png_try_curve(c, c["try"]["comp_label"], out / f"try_zero_curve_{c['try']['day']}.png")

    pd.DataFrame({"usdtry": px, "log_return_pct": r}).to_csv(out / "usdtry.csv", index_label="date")
    pd.DataFrame(rows).to_csv(out / "last_week.csv", index=False)
    for c in curves:
        pd.DataFrame({"tenor": c["tenors"], "years": c["std_t"], c["prev"]: c["std_prev"],
                      c["day"]: c["std_day"], "change_bp": c["chg_bp"]}).to_csv(
            out / f"zero_curve_{c['day']}.csv", index=False)
        if c.get("try"):
            t_ = c["try"]
            pd.DataFrame({"tenor": t_["tenors"], "years": t_["years"], t_["prev"]: t_["prev_vals"], t_["day"]: t_["day_vals"],
                          "change_bp": t_["chg_bp"], "try_minus_usd_pts": t_["spread_pts"],
                          "extrapolated": t_["extrapolated"]}).to_csv(out / f"try_zero_curve_{t_['day']}.csv", index=False)
    if news:
        (out / "news.json").write_text(json.dumps(news, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Report and data written to {out.resolve()}")
    (out / "run.log").write_text("\n".join(LOG), encoding="utf-8")


if __name__ == "__main__":
    main()
