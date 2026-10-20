"""
Build a static, deploy-ready website from the report folders made by usdtry_report.py.

    site/
      index.html                 "Currency & Yield Reports": latest report + full archive
      reports/<week-end>/        one page per report, with prev/next navigation,
        index.html               the interactive report, PNG charts and CSV/JSON downloads
        *.png *.csv *.json
      reports.json               machine-readable index of all reports
      404.html, robots.txt, .nojekyll, sitemap.xml (when --base-url is given)

All links are relative, so the folder works from any host or sub-path
(GitHub Pages, Netlify, Cloudflare Pages, S3, nginx, or just opened locally).

Usage (run in the same directory as usdtry_report.py):
    python build_site.py                          # build site/ from every report_*/ folder
    python build_site.py --update                 # run usdtry_report.py for the latest week first
    python build_site.py --update -- --pct 10     # pass options through to usdtry_report.py
    python build_site.py --base-url https://me.github.io/fx-reports/   # adds sitemap + canonical links
    python build_site.py --serve                  # build, then preview at http://localhost:8000
    python build_site.py --workflow               # write a GitHub Actions file that rebuilds weekly
"""

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKER = ".built-by-build_site"
COPY_EXT = {".png", ".csv", ".json"}
SKIP_FILES = {"summary.json"}
FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:'
         'wght@500;600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">')
E = html.escape


# ======================================================================= helpers
def fmt_day(s, year=False):
    d = date.fromisoformat(s)
    out = f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"
    return out + (f" {d.year}" if year else "")


def fmt_range(a, b):
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    left = f"{da.day} {da.strftime('%b')}" + (f" {da.year}" if da.year != db.year else "")
    return f"{left} – {db.day} {db.strftime('%b')} {db.year}"


def pc(x, d=3):
    return ("+" if x >= 0 else "−") + f"{abs(x):.{d}f}%"


def bp(x):
    return "–" if x is None else (("+" if x > 0 else "−" if x < 0 else "±") + f"{abs(x):.1f}")


def ordinal(n):
    n = int(round(n))
    suf = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def human_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ======================================================================= loading
def load_payload(folder: Path):
    """Report data: summary.json if present, else the JSON embedded in report_body.html."""
    summ = folder / "summary.json"
    if summ.exists():
        return json.loads(summ.read_text(encoding="utf-8"))
    body = folder / "report_body.html"
    if body.exists():
        m = re.search(r"const P = (\{.*?\});\s*\n", body.read_text(encoding="utf-8"), re.S)
        if m:
            return json.loads(m.group(1))
    return None


def top_headlines(news, k, balance=True):
    """First k headlines; alternating US and Turkey unless the list is already relevance-ranked."""
    if not balance:
        return [{"title": n["title"], "url": n["url"], "region": n.get("region", ""), "date": n.get("date", "")} for n in news[:k]]
    us = [n for n in news if n.get("region") == "US"]
    tr = [n for n in news if n.get("region") != "US"]
    out = []
    while len(out) < k and (us or tr):
        for side in (us, tr):
            if side and len(out) < k:
                out.append(side.pop(0))
    return [{"title": n["title"], "url": n["url"], "region": n.get("region", ""), "date": n.get("date", "")} for n in out]


def derive(P):
    """Everything the archive needs, computed once per report."""
    rows = P["rows"]
    ext = max(rows, key=lambda r: abs(r["rank"] - 50))
    flagged = [r for r in rows if r.get("flag")]
    c = P["curves"][0] if P.get("curves") else None

    def chg(tenor):
        if not c or tenor not in c["tenors"]:
            return None
        return c["chg_bp"][c["tenors"].index(tenor)]

    lo_p, hi_p = P["pct"], 100 - P["pct"]
    if flagged:
        days = ", ".join(f"{fmt_day(r['date'])} ({pc(r['ret'])})" for r in flagged)
        verdict = (f"{len(flagged)} of {len(rows)} days fell outside last year's "
                   f"{ordinal(lo_p)}–{ordinal(hi_p)} percentile band: {days}.")
    else:
        verdict = (f"No tail days. The largest move was {fmt_day(ext['date'])} at {pc(ext['ret'])}, "
                   f"the {ordinal(ext['rank'])} percentile, against a {ordinal(hi_p)} percentile of {pc(P['hi'])}.")
    news = (P.get("news") or {}).get("items") or []
    return dict(
        slug=P["week_end"], week_start=P["week_start"], week_end=P["week_end"], year=P["week_end"][:4],
        n_days=len(rows), n_tail=len(flagged), tail_dates=[r["date"] for r in flagged],
        ext_date=ext["date"], ext_ret=ext["ret"], ext_rank=ext["rank"], ext_flag=ext.get("flag", ""),
        week_cum=P["week_cum"], px_start=rows[0]["px"], px_end=rows[-1]["px"],
        lo=P["lo"], hi=P["hi"], pct=P["pct"], fallback=P.get("fallback", False),
        curve_day=c["day"] if c else None, curve_prev=c["prev"] if c else None,
        chg_2y=chg("2Y"), chg_10y=chg("10Y"), chg_30y=chg("30Y"),
        n_news=len(news),
        headlines=top_headlines([n for n in news if n.get("selected")] or news, 3, balance=not any(n.get("selected") for n in news)),
        direction=((P.get("news") or {}).get("relevance") or {}).get("direction", ""),
        all_titles=[n["title"] for n in news],
        verdict=verdict, generated=P.get("generated", ""),
        rows=[{"date": r["date"], "ret": r["ret"], "flag": r.get("flag", "")} for r in rows],
    )


def find_reports(src: Path, pattern: str):
    found = {}
    for folder in sorted(src.glob(pattern)):
        if not folder.is_dir():
            continue
        P = load_payload(folder)
        if not P or not (folder / "report_body.html").exists():
            print(f"  skip {folder.name}: no report_body.html / report data")
            continue
        d = derive(P)
        prev = found.get(d["slug"])
        if prev and prev["meta"]["generated"] >= d["generated"]:
            print(f"  skip {folder.name}: older duplicate of week {d['slug']}")
            continue
        found[d["slug"]] = {"folder": folder, "P": P, "meta": d}
    return sorted(found.values(), key=lambda r: r["meta"]["slug"], reverse=True)


# ======================================================================= small SVG chart
def mini_chart(rows, lo, hi, w=132, h=40, label=True):
    """Bars for the week's daily returns over last year's percentile band (theme-aware via CSS)."""
    vals = [r["ret"] for r in rows]
    top, bot = max(hi, *vals), min(lo, *vals, 0.0)
    pad = (top - bot) * 0.08 or 0.01
    top, bot = top + pad, bot - pad
    y = lambda v: h - (v - bot) / (top - bot) * h
    n = len(rows)
    slot = w / n
    bw = max(3.0, slot * 0.56)
    parts = [f'<rect class="mc-band" x="0" y="{y(hi):.1f}" width="{w}" height="{y(lo) - y(hi):.1f}" rx="2"/>',
             f'<line class="mc-zero" x1="0" x2="{w}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>']
    for i, r in enumerate(rows):
        y0, y1 = y(max(r["ret"], 0)), y(min(r["ret"], 0))
        cls = "mc-bar tail" if r.get("flag") else "mc-bar"
        parts.append(f'<rect class="{cls}" x="{i * slot + (slot - bw) / 2:.1f}" y="{y0:.1f}" '
                     f'width="{bw:.1f}" height="{max(1.0, y1 - y0):.1f}" rx="1.5"/>')
    title = ("Daily returns: " + ", ".join(f"{fmt_day(r['date'])} {pc(r['ret'])}" for r in rows)) if label else ""
    return (f'<svg class="mini" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{E(title)}">'
            f'<title>{E(title)}</title>{"".join(parts)}</svg>')


# ======================================================================= shared CSS
TOKENS = """
:root {
  --bg: #f6f7f9; --panel: #ffffff; --ink: #14181f; --ink-2: #4a5160; --ink-3: #7a8191;
  --rule: #e1e4ea; --grid: #eceef2; --band: rgba(42,120,214,.10); --s-main: #2a78d6; --up: #d95926; --down: #256abf;
  --pill-bg: #fdeee7; --pill-ink: #a8401a; --pill2-bg: #eef1f5; --pill2-ink: #4a5160;
  --f-display: "IBM Plex Sans Condensed", "Arial Narrow", system-ui, sans-serif;
  --f-body: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --f-mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #111418; --panel: #191d23; --ink: #eef1f5; --ink-2: #b4bbc8; --ink-3: #868e9c; --rule: #2a3039; --grid: #232830;
  --band: rgba(57,135,229,.16); --s-main: #4f95ea; --up: #ef7a46; --down: #5598e7;
  --pill-bg: #3a2218; --pill-ink: #f3a37f; --pill2-bg: #252b33; --pill2-ink: #b4bbc8; color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #111418; --panel: #191d23; --ink: #eef1f5; --ink-2: #b4bbc8; --ink-3: #868e9c; --rule: #2a3039; --grid: #232830;
  --band: rgba(57,135,229,.16); --s-main: #4f95ea; --up: #ef7a46; --down: #5598e7;
  --pill-bg: #3a2218; --pill-ink: #f3a37f; --pill2-bg: #252b33; --pill2-ink: #b4bbc8; color-scheme: dark; }
"""

MINI_CSS = """
.mini { display: block; overflow: visible; }
.mc-band { fill: var(--band); }
.mc-zero { stroke: var(--ink-3); stroke-width: .6; stroke-dasharray: 2 2; }
.mc-bar { fill: var(--s-main); }
.mc-bar.tail { fill: var(--up); }
"""

INDEX_CSS = TOKENS + MINI_CSS + """
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--ink); font-family: var(--f-body); font-size: 15px; line-height: 1.55; }
a { color: inherit; }
.wrap { max-width: 1040px; margin: 0 auto; padding-inline: 20px; padding-block: 40px 64px; display: grid; gap: 44px; }
header.top { display: grid; gap: 10px; }
.eyebrow { font-family: var(--f-mono); font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); }
h1 { font-family: var(--f-display); font-weight: 600; font-size: clamp(32px, 6vw, 48px); line-height: 1.05; margin: 0; text-wrap: balance; }
h2 { font-family: var(--f-display); font-weight: 600; font-size: 22px; margin: 0; }
.lede { color: var(--ink-2); max-width: 66ch; margin: 0; }
.latest { background: var(--panel); border: 1px solid var(--rule); border-radius: 10px; padding: 22px; display: grid; gap: 18px; }
.latest-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 8px 16px; }
.latest-head .when { font-family: var(--f-display); font-size: 26px; font-weight: 600; line-height: 1.15; }
.latest-body { display: grid; gap: 18px; grid-template-columns: 1fr; }
@media (min-width: 760px) { .latest-body { grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); align-items: start; } }
.verdict { margin: 0; font-size: 16px; max-width: 60ch; }
.kv { display: flex; flex-wrap: wrap; gap: 1px; background: var(--rule); border: 1px solid var(--rule); border-radius: 8px; overflow: hidden; }
.kv div { flex: 1 1 130px; background: var(--panel); padding: 10px 12px; display: grid; gap: 1px; min-width: 0; }
.kv .k { font-size: 12px; color: var(--ink-3); }
.kv .v { font-family: var(--f-mono); font-size: 17px; font-variant-numeric: tabular-nums; }
.heads { margin: 0; padding: 0; list-style: none; display: grid; gap: 8px; }
.heads li { display: grid; grid-template-columns: auto 1fr; gap: 8px; align-items: baseline; font-size: 14px; }
.heads a { text-decoration: none; border-bottom: 1px solid var(--rule); }
.heads a:hover, .heads a:focus-visible { border-bottom-color: var(--s-main); outline: none; }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 8px; background: var(--s-main); color: #fff;
       text-decoration: none; font-weight: 500; font-size: 14px; }
.btn:hover, .btn:focus-visible { filter: brightness(1.08); outline: 2px solid var(--s-main); outline-offset: 2px; }
.tag { font-size: 11px; font-weight: 500; padding: 0 7px; border-radius: 999px; border: 1px solid var(--rule); color: var(--ink-2); white-space: nowrap; }
.tag.us { border-color: var(--s-main); color: var(--s-main); }
.tag.tr { border-color: var(--up); color: var(--up); }
.pill { display: inline-block; font-size: 12px; font-weight: 500; padding: 1px 8px; border-radius: 999px; background: var(--pill2-bg); color: var(--pill2-ink); white-space: nowrap; }
.pill.hot { background: var(--pill-bg); color: var(--pill-ink); }
.controls { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center; }
.controls input[type=search], .controls select { font: inherit; font-size: 14px; padding: 7px 10px; border-radius: 8px;
  border: 1px solid var(--rule); background: var(--panel); color: var(--ink); min-width: 0; }
.controls input[type=search] { flex: 1 1 220px; }
.controls label { display: inline-flex; gap: 6px; align-items: center; font-size: 14px; color: var(--ink-2); }
.count { font-size: 13px; color: var(--ink-3); margin-left: auto; }
.archive { list-style: none; margin: 0; padding: 0; background: var(--panel); border: 1px solid var(--rule); border-radius: 10px; }
.archive li { border-bottom: 1px solid var(--rule); }
.archive li:last-child { border-bottom: 0; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 16px; padding: 14px 18px; text-decoration: none; color: inherit; }
.row > .cell:first-child { grid-column: 1 / -1; }
.row > .arrow { display: none; }
.row:hover, .row:focus-visible { background: color-mix(in srgb, var(--s-main) 5%, var(--panel)); outline: none; }
@media (min-width: 820px) {
  .row { grid-template-columns: 190px 150px 130px 150px minmax(0, 1fr) 16px; align-items: center; gap: 16px; }
  .row > .cell:first-child { grid-column: auto; }
  .row > .arrow { display: block; }
  .archive-head { display: grid !important; }
}
.archive-head { display: none; grid-template-columns: 190px 150px 130px 150px minmax(0, 1fr) 16px; gap: 16px;
  padding: 10px 18px; font-size: 12px; color: var(--ink-3); border-bottom: 1px solid var(--rule); }
.row .week { font-weight: 600; }
.row .sub { font-size: 12px; color: var(--ink-3); }
.row .num { font-family: var(--f-mono); font-variant-numeric: tabular-nums; font-size: 14px; }
.row .cell { display: grid; gap: 2px; min-width: 0; }
.row .cell > .pill, .row .cell > .mini { justify-self: start; }
.row .lbl { font-size: 11px; color: var(--ink-3); }
@media (min-width: 820px) { .row .lbl { display: none; } }
.row .arrow { color: var(--ink-3); }
.up { color: var(--up); } .down { color: var(--down); }
.empty { padding: 28px 18px; color: var(--ink-2); }
footer { font-size: 13px; color: var(--ink-3); display: grid; gap: 4px; max-width: 80ch; }
footer p { margin: 0; }
"""

NAV_CSS = """
.site-nav { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 10; background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: blur(6px); border-bottom: 1px solid var(--rule); }
.site-nav .in { max-width: 960px; margin: 0 auto; padding-inline: 20px; padding-block: 10px; display: flex; flex-wrap: wrap;
  gap: 6px 16px; align-items: center; font-size: 14px; }
.site-nav a { color: var(--ink-2); text-decoration: none; }
.site-nav a:hover, .site-nav a:focus-visible { color: var(--ink); text-decoration: underline; outline: none; }
.site-nav .home { font-weight: 600; color: var(--ink); }
.site-nav .pn { margin-left: auto; display: flex; gap: 14px; }
.site-nav .off { color: var(--ink-3); opacity: .5; }
.dl { max-width: 960px; margin: 0 auto; padding-inline: 20px; padding-block: 0 56px; display: grid; gap: 12px; }
.dl h2 { font-family: var(--f-display); font-weight: 600; font-size: 17px; margin: 0; }
.dl ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 1px; background: var(--rule);
  border: 1px solid var(--rule); border-radius: 8px; overflow: hidden; }
.dl li { background: var(--panel); padding: 9px 14px; display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: baseline; font-size: 14px; }
.dl li a { color: var(--ink); font-family: var(--f-mono); font-size: 13px; overflow-wrap: anywhere; }
.dl li span { color: var(--ink-3); font-size: 12px; }
.dl .thumbs { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.dl .thumbs a { display: block; background: var(--panel); border: 1px solid var(--rule); border-radius: 8px; padding: 6px; }
.dl .thumbs img { display: block; width: 100%; height: auto; border-radius: 4px; background: #fff; }
"""

FILE_DESC = {
    "week_returns.png": "Chart: last week's returns vs last year's band",
    "returns_histogram.png": "Chart: distribution of last year's daily returns",
    "usdtry.csv": "USD/TRY rates and daily log returns",
    "last_week.csv": "Last week: returns, percentile ranks, tail flags",
    "news.json": "Headlines with links, as shown in the report",
}


def describe(name):
    if name in FILE_DESC:
        return FILE_DESC[name]
    m = re.match(r"zero_curve_(\d{4}-\d{2}-\d{2})\.(png|csv)", name)
    if m:
        return f"{'Chart' if m.group(2) == 'png' else 'Data'}: US zero curve on {m.group(1)} vs previous day"
    return ""


# ======================================================================= pages
def split_report_body(body: str):
    """Move <title>, <link> and <style> of the report fragment into <head>."""
    head = []
    for pat in (r"<link[^>]*>", r"<style>.*?</style>"):
        for m in re.finditer(pat, body[: body.find('<div class="wrap">')], re.S):
            head.append(m.group(0))
    cut = body.find('<div class="wrap">')
    return "\n".join(head), body[cut:]


# ======================================================================= link-preview image
PREVIEW = "preview.png"            # 1200x630, the size Open Graph / X / LinkedIn / WhatsApp expect


PREVIEW_SOURCE = "week_returns.png"   # chart made by usdtry_report.py, used as the share-card image


def preview_from_week_chart(src: Path, path: Path, size=(1200, 630)):
    """Fit week_returns.png onto a white 1200x630 canvas (the share-card size) without cropping."""
    from PIL import Image
    W, H = size
    with Image.open(src) as im:
        im = im.convert("RGB")
        scale = min(W / im.width, H / im.height)
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        canvas = Image.new("RGB", (W, H), "white")
        canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
        canvas.save(path, optimize=True)


def make_preview(rep, out_dir: Path, site_title: str):
    """Share-card image for a report: its week_returns.png; older folders without one get the
    generated zero-curve preview instead. Returns which source was used."""
    src = rep["folder"] / PREVIEW_SOURCE
    if src.exists():
        preview_from_week_chart(src, out_dir / PREVIEW)
        return "week"
    preview_image(rep["P"], rep["meta"], out_dir / PREVIEW, site_title)
    return "curve" if rep["meta"]["curve_day"] else "week"


def preview_image(P, m, path: Path, site_title: str):
    """Share-card image: the event day's zero curves vs the previous day, with the change in bp.
    Weeks without curve data get the week's returns against last year's band instead."""
    import os
    os.environ.setdefault("MPLBACKEND", "Agg")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    ink, ink2, ink3, rule = "#14181f", "#4a5160", "#7a8191", "#e1e4ea"
    main, prev_c, up, down, band = "#2a78d6", "#9aa3b2", "#d95926", "#256abf", "#e3edfa"
    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.edgecolor": rule, "axes.labelcolor": ink2,
                         "xtick.color": ink2, "ytick.color": ink2, "axes.spines.top": False,
                         "axes.spines.right": False})
    fig = plt.figure(figsize=(12, 6.3), dpi=100, facecolor="#f6f7f9")
    we = date.fromisoformat(m["week_end"])
    fig.text(0.045, 0.925, f"{site_title.upper()}  ·  USD/TRY & US TREASURY ZERO CURVE",
             fontsize=11, color=ink3, weight="bold")
    c = (P.get("curves") or [None])[0]

    if c:
        fig.text(0.045, 0.835, f"Zero curve {fmt_day(c['prev'])} → {fmt_day(c['day'], year=True)}",
                 fontsize=27, color=ink, weight="bold")
        tail = "tail day" if m["n_tail"] else "near-miss, most extreme day"
        fig.text(0.045, 0.775, f"USD/TRY {pc(m['ext_ret'])} on {fmt_day(m['ext_date'])} "
                 f"({ordinal(m['ext_rank'])} percentile, {tail})  ·  week to {we.day} {we.strftime('%b %Y')}",
                 fontsize=13.5, color=ink2)
        a1 = fig.add_axes([0.06, 0.12, 0.52, 0.58], facecolor="#ffffff")
        for key, col, lbl, lw in (("curve_prev", prev_c, c["prev"], 2.6), ("curve_day", main, c["day"], 3.2)):
            t, v = zip(*c[key])
            a1.plot(t, v, color=col, lw=lw, label=lbl, solid_capstyle="round")
        a1.plot(c["std_t"], c["std_day"], "o", color=main, ms=5)
        a1.set_xscale("log")
        a1.set_xticks(c["std_t"], c["tenors"], fontsize=10)
        a1.minorticks_off()
        a1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.2f}%"))
        a1.tick_params(axis="y", labelsize=10)
        a1.grid(alpha=0.35, color=rule)
        a1.legend(frameon=False, fontsize=11, loc="lower right")

        a2 = fig.add_axes([0.65, 0.12, 0.31, 0.58], facecolor="#ffffff")
        y = np.arange(len(c["tenors"]))
        vals = np.array(c["chg_bp"])
        a2.barh(y, vals, color=[up if v >= 0 else down for v in vals], height=0.62)
        a2.axvline(0, color=ink3, lw=1)
        a2.set_yticks(y, c["tenors"], fontsize=10)
        a2.invert_yaxis()
        lim = max(1.0, np.abs(vals).max()) * 1.45
        a2.set_xlim(-lim, lim)
        for yi, v in zip(y, vals):
            a2.text(v + (0.04 * lim if v >= 0 else -0.04 * lim), yi, f"{v:+.1f}", va="center",
                    ha="left" if v >= 0 else "right", fontsize=9.5, color=ink)
        a2.set_title("Daily change, bp", fontsize=12, color=ink2, loc="left")
        a2.tick_params(axis="x", labelsize=9)
        a2.grid(axis="x", alpha=0.35, color=rule)
    else:                                               # no curve: show the week vs the band
        fig.text(0.045, 0.835, f"USD/TRY week to {we.day} {we.strftime('%b %Y')}",
                 fontsize=27, color=ink, weight="bold")
        fig.text(0.045, 0.775, m["verdict"][:120], fontsize=13.5, color=ink2)
        a = fig.add_axes([0.06, 0.12, 0.9, 0.58], facecolor="#ffffff")
        rows = m["rows"]
        x = np.arange(len(rows))
        a.axhspan(m["lo"], m["hi"], color=band)
        a.bar(x, [r["ret"] for r in rows], color=[up if r["flag"] else main for r in rows], width=0.5)
        a.set_xticks(x, [fmt_day(r["date"]) for r in rows], fontsize=11)
        a.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.2f}%"))
        a.grid(axis="y", alpha=0.35, color=rule)
    fig.savefig(path, dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)


def share_tags(title, desc, page_url, image_rel, image_alt, base_url):
    """Open Graph + X/Twitter card tags. Platforms need absolute image URLs, hence --base-url."""
    img = (base_url + image_rel) if base_url else image_rel
    tags = [f'<meta property="og:type" content="article">',
            f'<meta property="og:title" content="{E(title)}">',
            f'<meta property="og:description" content="{desc}">',
            f'<meta property="og:image" content="{E(img)}">',
            '<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">',
            f'<meta property="og:image:alt" content="{E(image_alt)}">',
            '<meta name="twitter:card" content="summary_large_image">',
            f'<meta name="twitter:title" content="{E(title)}">',
            f'<meta name="twitter:description" content="{desc}">',
            f'<meta name="twitter:image" content="{E(img)}">']
    if base_url:
        tags.insert(1, f'<meta property="og:url" content="{E(base_url + page_url)}">')
    return "\n".join(tags)


def report_page(rep, prev_rep, next_rep, files, site_title, base_url):
    m = rep["meta"]
    body = (rep["folder"] / "report_body.html").read_text(encoding="utf-8")
    head_extra, content = split_report_body(body)
    we = date.fromisoformat(m['week_end'])
    title = f"USD/TRY week to {we.day} {we.strftime('%b %Y')} · {site_title}"
    canon = f'<link rel="canonical" href="{E(base_url)}reports/{m["slug"]}/">' if base_url else ""
    desc = E(m["verdict"])
    alt = (f"US Treasury zero curves {m['curve_prev']} vs {m['curve_day']} with daily change in basis points"
           if rep.get("preview_kind") == "curve"
           else f"USD/TRY daily returns for the week to {m['week_end']} against last year's percentile band")
    has_preview = bool(files) and (files[0].parent / PREVIEW).exists()
    share = (share_tags(title, desc, f"reports/{m['slug']}/", f"reports/{m['slug']}/{PREVIEW}", alt, base_url)
             if has_preview else f'<meta property="og:title" content="{E(title)}">')

    def pn(r, label, arrow_first):
        if not r:
            return f'<span class="off">{label}</span>'
        txt = fmt_range(r["meta"]["week_start"], r["meta"]["week_end"])
        inner = f"← {label}: {txt}" if arrow_first else f"{label}: {txt} →"
        return f'<a href="../{r["meta"]["slug"]}/index.html" title="{E(txt)}">{E(inner)}</a>'

    nav = (f'<nav class="site-nav" aria-label="Site"><div class="in">'
           f'<a class="home" href="../../index.html">{E(site_title)}</a><a href="../../index.html#archive">All reports</a>'
           f'<span class="pn">{pn(next_rep, "Older", True)}{pn(prev_rep, "Newer", False)}</span></div></nav>')

    pngs = [f for f in files if f.suffix == ".png"]
    others = [f for f in files if f.suffix != ".png"]
    thumbs = "".join(f'<a href="{E(f.name)}" aria-label="{E(describe(f.name) or f.name)}">'
                     f'<img src="{E(f.name)}" alt="{E(describe(f.name) or f.name)}"></a>' for f in pngs)
    items = "".join(f'<li><a href="{E(f.name)}" download>{E(f.name)}</a><span>{E(describe(f.name))} · '
                    f'{human_size(f.stat().st_size)}</span></li>' for f in pngs + others)
    downloads = (f'<section class="dl" aria-labelledby="dl-h"><h2 id="dl-h">Charts and data</h2>'
                 f'{"<div class=thumbs>" + thumbs + "</div>" if thumbs else ""}<ul>{items}</ul></section>') if files else ""

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{E(title)}</title>
<meta name="description" content="{desc}">
{share}
{canon}
{head_extra}
<style>{NAV_CSS}</style>
</head><body>
{nav}
{content}
{downloads}
</body></html>
"""


def index_page(reports, site_title, base_url, built):
    metas = [r["meta"] for r in reports]
    canon = f'<link rel="canonical" href="{E(base_url)}">' if base_url else ""
    home_share = (share_tags(site_title, "Weekly USD/TRY tail-day reports with US Treasury zero curves and US/Turkey headlines.",
                             "", f"reports/{metas[0]['slug']}/{PREVIEW}", "Latest report: USD/TRY daily returns against last year's band", base_url)
                  if metas else f'<meta property="og:title" content="{E(site_title)}">')
    years = sorted({m["year"] for m in metas}, reverse=True)
    n_tail_weeks = sum(1 for m in metas if m["n_tail"])

    latest_html = ""
    if metas:
        L = metas[0]
        heads = "".join(
            f'<li><span class="tag {"us" if h["region"] == "US" else "tr"}">{E(h["region"] or "News")}</span>'
            f'<a href="{E(h["url"])}" target="_blank" rel="noopener">{E(h["title"])}</a></li>' for h in L["headlines"])
        curve_txt = (f'{bp(L["chg_2y"])} / {bp(L["chg_10y"])}' if L["curve_day"] else "–")
        latest_html = f"""
  <section class="latest" aria-labelledby="latest-h">
    <div class="latest-head">
      <div><div class="eyebrow" id="latest-h">Latest report</div><div class="when">{E(fmt_range(L["week_start"], L["week_end"]))}</div></div>
      <a class="btn" href="reports/{L["slug"]}/index.html">Read the report →</a>
    </div>
    <div class="latest-body">
      <div style="display:grid;gap:14px;min-width:0">
        <p class="verdict">{E(L["verdict"])}</p>
        {mini_chart(L["rows"], L["lo"], L["hi"], w=300, h=84)}
        <div class="kv">
          <div><span class="k">Week's move</span><span class="v">{pc(L["week_cum"], 2)}</span></div>
          <div><span class="k">USD/TRY</span><span class="v">{L["px_end"]:.3f}</span></div>
          <div><span class="k">Tail days</span><span class="v">{L["n_tail"]} / {L["n_days"]}</span></div>
          <div><span class="k">2Y / 10Y zero, bp{(" on " + fmt_day(L["curve_day"])) if L["curve_day"] else ""}</span><span class="v">{curve_txt}</span></div>
        </div>
      </div>
      <div style="display:grid;gap:10px;min-width:0">
        <div class="eyebrow">{"Most relevant headlines" if L.get("direction") else "Headlines"} around {E(fmt_day(L["ext_date"] if not L["tail_dates"] else L["tail_dates"][0]))}{(" · " + L["direction"] + " day") if L.get("direction") else ""}</div>
        {"<ul class='heads'>" + heads + "</ul>" if heads else "<p class='lede' style='font-size:14px'>No headlines were collected for this week.</p>"}
      </div>
    </div>
  </section>"""

    def row(m):
        tail = (f'<span class="pill hot">{m["n_tail"]} tail day{"s" if m["n_tail"] > 1 else ""}</span>' if m["n_tail"]
                else '<span class="pill">None</span>')
        move_cls = "up" if m["ext_ret"] >= 0 else "down"
        curve = (f'<span class="num"><span class="{"up" if (m["chg_2y"] or 0) > 0 else "down"}">{bp(m["chg_2y"])}</span>'
                 f' / <span class="{"up" if (m["chg_10y"] or 0) > 0 else "down"}">{bp(m["chg_10y"])}</span></span>'
                 f'<span class="sub">{E(fmt_day(m["curve_day"]))}{" · near-miss" if m["fallback"] else ""}</span>'
                 if m["curve_day"] else '<span class="sub">no curve</span>')
        h = m["headlines"][0]["title"] if m["headlines"] else ""
        search = " ".join([m["week_start"], m["week_end"], fmt_range(m["week_start"], m["week_end"]), m["verdict"]]
                          + m.get("all_titles", [])).lower()
        return f"""<li data-year="{m['year']}" data-tail="{1 if m['n_tail'] else 0}" data-search="{E(search)}">
  <a class="row" href="reports/{m['slug']}/index.html">
    <span class="cell"><span class="week">{E(fmt_range(m['week_start'], m['week_end']))}</span>
      <span class="sub">USD/TRY {m['px_start']:.3f} → {m['px_end']:.3f} ({pc(m['week_cum'], 2)})</span></span>
    <span class="cell"><span class="lbl">Daily returns vs band</span>{mini_chart(m['rows'], m['lo'], m['hi'])}</span>
    <span class="cell"><span class="lbl">Tail days</span>{tail}</span>
    <span class="cell"><span class="lbl">Largest move</span><span class="num {move_cls}">{pc(m['ext_ret'])}</span>
      <span class="sub">{E(fmt_day(m['ext_date']))} · {ordinal(m['ext_rank'])} pct</span></span>
    <span class="cell"><span class="lbl">2Y / 10Y zero change, bp</span>{curve}
      {f'<span class="sub" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{E(h)}</span>' if h else ''}</span>
    <span class="arrow" aria-hidden="true">→</span>
  </a></li>"""

    rows_html = "\n".join(row(m) for m in metas) or '<li class="empty">No reports yet. Run usdtry_report.py, then build again.</li>'
    year_opts = "".join(f'<option value="{y}">{y}</option>' for y in years)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{E(site_title)}</title>
<meta name="description" content="Weekly USD/TRY tail-day reports with US Treasury zero curves and US/Turkey headlines.">
{home_share}
{canon}
{FONTS}
<style>{INDEX_CSS}</style>
</head><body>
<div class="wrap">
  <header class="top">
    <div class="eyebrow">Weekly · USD/TRY · US Treasury zero curve</div>
    <h1>{E(site_title)}</h1>
    <p class="lede">Each report ranks a week of USD/TRY daily returns against the year before it, flags days in the tails,
    compares the US Treasury zero curve on the most extreme day with the day before, and lists the US and Turkey headlines around it.</p>
  </header>
{latest_html}
  <section id="archive" aria-labelledby="arch-h" style="display:grid;gap:14px">
    <h2 id="arch-h">All reports</h2>
    <div class="controls">
      <input type="search" id="q" placeholder="Search dates or headlines" aria-label="Search reports">
      <select id="yr" aria-label="Year"><option value="">All years</option>{year_opts}</select>
      <label><input type="checkbox" id="tailOnly"> Tail weeks only</label>
      <span class="count" id="count">{len(metas)} reports · {n_tail_weeks} with tail days</span>
    </div>
    <ul class="archive" id="list">
      <li class="archive-head" aria-hidden="true"><span>Week</span><span>Daily returns vs band</span><span>Tail days</span><span>Largest move</span><span>2Y / 10Y zero Δ bp, event day</span><span></span></li>
{rows_html}
    </ul>
    <p class="empty" id="none" hidden>No reports match these filters.</p>
  </section>
  <footer>
    <p>Bars show each day's USD/TRY log return; the shaded band is last year's {ordinal(metas[0]["pct"] if metas else 5)}–{ordinal(100 - (metas[0]["pct"] if metas else 5))} percentile range; orange bars are tail days.
    "Near-miss" means no day was in the tails and the week's most extreme day was used for the curve.</p>
    <p>Data: ECB reference rates (Frankfurter) or Yahoo Finance for USD/TRY; U.S. Treasury par curve or Federal Reserve GSW curve for rates; GDELT, Google News and Yahoo for headlines. Built {E(built)}. <a href="reports.json">reports.json</a></p>
  </footer>
</div>
<script>
(() => {{
  const q = document.getElementById("q"), yr = document.getElementById("yr"), tail = document.getElementById("tailOnly");
  const items = [...document.querySelectorAll("#list li[data-year]")], none = document.getElementById("none"), count = document.getElementById("count");
  const total = items.length;
  function apply() {{
    const s = q.value.trim().toLowerCase(); let n = 0;
    items.forEach(li => {{
      const ok = (!s || li.dataset.search.includes(s)) && (!yr.value || li.dataset.year === yr.value) && (!tail.checked || li.dataset.tail === "1");
      li.hidden = !ok; if (ok) n++;
    }});
    none.hidden = n > 0 || total === 0;
    count.textContent = n === total ? count.dataset.all : n + " of " + total + " reports";
  }}
  count.dataset.all = count.textContent;
  [q, yr, tail].forEach(el => el.addEventListener("input", apply));
}})();
</script>
</body></html>
"""


def page_404(site_title, base_url=""):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Page not found · {E(site_title)}</title>
{FONTS}<style>{TOKENS}
body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: var(--f-body); }}
main {{ max-width: 560px; margin: 0 auto; padding-inline: 20px; padding-block: 18vh 40px; display: grid; gap: 12px; }}
h1 {{ font-family: var(--f-display); margin: 0; }} a {{ color: var(--s-main); }}
</style></head><body><main><h1>Page not found</h1>
<p>That report doesn't exist or has moved. All reports are listed on the <a href="{E(base_url or '/')}">home page</a>.</p></main></body></html>
"""


WORKFLOW = """name: Publish currency & yield reports
on:
  schedule:
    - cron: "17 6 * * 3"        # Wednesdays 06:17 UTC: report for the week to Tuesday
  workflow_dispatch: {}
permissions:
  contents: write
  pages: write
  id-token: write
concurrency: { group: pages, cancel-in-progress: true }
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - name: New report + rebuild site
        run: python build_site.py --update --base-url "${{ vars.SITE_URL }}"
      - name: Keep report folders in the repo (they are the archive)
        run: |
          git config user.name "report-bot"; git config user.email "report-bot@users.noreply.github.com"
          git add report_*/ && git commit -m "Report $(date -u +%F)" || echo "nothing new"
          git push
      - uses: actions/upload-pages-artifact@v3
        with: { path: site }
  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment: { name: github-pages, url: "${{ steps.d.outputs.page_url }}" }
    steps:
      - id: d
        uses: actions/deploy-pages@v4
"""


# ======================================================================= build
def build(src: Path, out: Path, pattern: str, site_title: str, base_url: str):
    base_url = (base_url.rstrip("/") + "/") if base_url else ""
    reports = find_reports(src, pattern)
    print(f"Found {len(reports)} report(s) in {src.resolve()}")

    if out.exists():
        if not (out / MARKER).exists() and any(out.iterdir()):
            sys.exit(f"{out} exists and was not created by build_site.py; choose another --out or empty it.")
        shutil.rmtree(out)
    (out / "reports").mkdir(parents=True)
    (out / MARKER).write_text("generated; safe to delete\n")
    built = datetime.now().strftime("%Y-%m-%d %H:%M")

    for i, rep in enumerate(reports):
        d = out / "reports" / rep["meta"]["slug"]
        d.mkdir(parents=True)
        files = []
        for f in sorted(rep["folder"].iterdir()):
            if f.suffix in COPY_EXT and f.name not in SKIP_FILES:
                shutil.copy2(f, d / f.name)
                files.append(d / f.name)
        try:
            rep["preview_kind"] = make_preview(rep, d, site_title)
        except Exception as ex:                       # a missing preview must never break the build
            print(f"  preview image failed for {rep['meta']['slug']}: {type(ex).__name__}: {ex}")
        newer = reports[i - 1] if i > 0 else None
        older = reports[i + 1] if i + 1 < len(reports) else None
        (d / "index.html").write_text(report_page(rep, newer, older, files, site_title, base_url), encoding="utf-8")
        print(f"  reports/{rep['meta']['slug']}/  ({len(files)} files)  from {rep['folder'].name}")

    (out / "index.html").write_text(index_page(reports, site_title, base_url, built), encoding="utf-8")
    (out / "404.html").write_text(page_404(site_title, base_url), encoding="utf-8")
    (out / ".nojekyll").write_text("")
    (out / "reports.json").write_text(json.dumps(
        {"title": site_title, "built": built,
         "reports": [{k: v for k, v in r["meta"].items() if k not in ("rows", "all_titles")} | {"url": f"reports/{r['meta']['slug']}/index.html"}
                     for r in reports]}, ensure_ascii=False, indent=1), encoding="utf-8")
    robots = "User-agent: *\nAllow: /\n"
    if base_url:
        urls = [base_url] + [f"{base_url}reports/{r['meta']['slug']}/" for r in reports]
        (out / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "".join(f"  <url><loc>{E(u)}</loc></url>\n" for u in urls) + "</urlset>\n", encoding="utf-8")
        robots += f"Sitemap: {base_url}sitemap.xml\n"
    (out / "robots.txt").write_text(robots)
    print(f"Site written to {out.resolve()}  ({len(reports)} report pages)")


def main():
    ap = argparse.ArgumentParser(description="Build a static site from usdtry_report.py output folders.",
                                 epilog="Arguments after -- are passed to usdtry_report.py when --update is used.")
    ap.add_argument("--src", default=".", help="folder containing the report_* folders (default: current)")
    ap.add_argument("--pattern", default="report_*", help="glob for report folders")
    ap.add_argument("--out", default="site", help="output folder (default: site)")
    ap.add_argument("--title", default="Currency & Yield Reports")
    ap.add_argument("--base-url", default="", help="public URL of the site, for sitemap and canonical links")
    ap.add_argument("--update", action="store_true", help="run usdtry_report.py for the latest week first")
    ap.add_argument("--serve", action="store_true", help="serve the built site at http://localhost:PORT")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--workflow", action="store_true", help="write .github/workflows/publish-reports.yml")
    argv = sys.argv[1:]
    passthrough = argv[argv.index("--") + 1:] if "--" in argv else []
    a = ap.parse_args(argv[: argv.index("--")] if "--" in argv else argv)
    src = Path(a.src)

    if a.workflow:
        wf = src / ".github" / "workflows" / "publish-reports.yml"
        wf.parent.mkdir(parents=True, exist_ok=True)
        wf.write_text(WORKFLOW)
        print(f"Wrote {wf}. In GitHub: Settings → Pages → Source: GitHub Actions; "
              "optionally set a repository variable SITE_URL.")

    if a.update:
        cmd = [sys.executable, str(HERE / "usdtry_report.py"), *passthrough]
        print("Running:", " ".join(cmd))
        r = subprocess.run(cmd, cwd=src)
        if r.returncode != 0:
            print("usdtry_report.py failed; building the site from existing reports.")

    build(src, Path(a.out), a.pattern, a.title, a.base_url)

    if a.serve:
        import functools
        import http.server
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(Path(a.out).resolve()))
        print(f"Serving on http://localhost:{a.port}  (Ctrl+C to stop)")
        http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler).serve_forever()


if __name__ == "__main__":
    main()
