"""
Collect and rank US / Turkey macro & political headlines for a date window,
from free sources (no API keys):

  * GDELT DOC 2.0 API     - date-bounded full-text news search (primary)
  * Google News RSS       - search feed with after:/before: date operators
  * Yahoo Finance RSS     - headline feeds; only cover recent days (bonus)

Pipeline: fetch -> keep items inside the window -> classify region (US/Turkey)
and kind (Macro/Political/Markets) by keywords -> drop off-topic items ->
de-duplicate near-identical headlines across sources -> score -> pick the top N
balanced between US and Turkey -> enrich each pick with the article's own meta
description for the one-line note.

Usage (standalone):
    python news_fetch.py 2026-09-28 2026-10-02 --top 10 --out news.json

Notes: be polite with GDELT (it asks for ~1 request / 5 s; this module waits).
Google News RSS and Yahoo feeds are unofficial public feeds - fine for personal
research, but they can change or rate-limit without notice.
"""

import argparse
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 usdtry-report"}

# ---------------------------------------------------------------- query sets
QUERIES = {
    "US": [
        "jobs report payrolls", "Treasury yields", "Federal Reserve rate",
        "US inflation CPI", "ISM manufacturing", "White House economy",
        "Congress budget shutdown", "oil prices Iran",
    ],
    "Turkey": [
        "Turkish lira", "Turkey central bank", "Turkey inflation",
        "Erdogan", "Turkey opposition", "Turkey economy",
    ],
}
# GDELT needs compact boolean queries
GDELT_QUERIES = {
    "US": ['("jobs report" OR payrolls OR "Treasury yields" OR "Federal Reserve" OR "bond yields")',
           '("White House" OR Congress OR "Trump administration") (economy OR tariffs OR budget OR oil)'],
    "Turkey": ['("Turkish lira" OR "Turkey central bank" OR "Turkish central bank" OR "Turkey inflation")',
               '(Erdogan OR "Turkish opposition" OR "Turkey opposition" OR Ankara) (election OR probe OR parliament OR economy)'],
}
YAHOO_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=TRY%3DX,%5ETNX,%5ETYX,%5EIRX&region=US&lang=en-US",
    "https://finance.yahoo.com/news/rssindex",
]

# ---------------------------------------------------------------- classifiers
RX_TR = re.compile(r"\b(turk\w*|türk\w*|lira|erdo[gğ]an|ankara|istanbul|İstanbul|cbrt|tcmb|akp|chp|"
                   r"imamo[gğ]lu|özel|ozel|bist|borsa istanbul|spk)\b", re.I)
RX_US = re.compile(r"\b(u\.s\.|us|usa|american|america|fed|federal reserve|fomc|treasur(y|ies)|payrolls?|"
                   r"jobs report|white house|trump|congress|senate|wall street|s&p|dow|nasdaq|warsh|"
                   r"bessent|ism|bls|10-year|2-year|30-year)\b", re.I)
RX_POL = re.compile(r"\b(election|opposition|parliament|president|minister|court|sanction\w*|congress|"
                    r"senate|white house|trump|erdo[gğ]an|probe|arrest\w*|resign\w*|protest\w*|scandal|"
                    r"government|shutdown|tariff\w*|war|nato|diplomat\w*|jail\w*|party)\b", re.I)
RX_MACRO = re.compile(r"\b(inflation|cpi|ppi|jobs?|payrolls?|unemployment|gdp|growth|rates?|hike\w*|cut\w*|"
                      r"central bank|fed|federal reserve|yields?|bonds?|treasur(y|ies)|reserves?|lira|"
                      r"dollar|currency|fx|pmi|ism|deficit|debt|budget|oil|crude|economy|economic|"
                      r"recession|wages?|claims|current account|rating|moody|fitch|s&p global|fund\w*|"
                      r"liquidat\w*|banks?)\b", re.I)
RX_MKT = re.compile(r"\b(stocks?|shares|dow|nasdaq|s&p 500|equit\w*|bist|index|indexes|indices|rally|"
                    r"sell-?off|futures)\b", re.I)
RX_JUNK = re.compile(r"\b(horoscope|recipe|football|soccer|celebrity|transfer window|tourist|holiday deals|"
                     r"exchange rate today|currency converter|price prediction)\b", re.I)

PREFERRED = {  # small boost for wire services / outlets with solid macro coverage
    "reuters.com": 3, "bloomberg.com": 3, "apnews.com": 2, "ft.com": 3, "wsj.com": 3,
    "finance.yahoo.com": 2, "yahoo.com": 2, "cnbc.com": 2, "aljazeera.com": 1,
    "turkishminute.com": 1, "hurriyetdailynews.com": 1, "dailysabah.com": 1, "bianet.org": 1,
    "al-monitor.com": 2, "economist.com": 2, "marketwatch.com": 1, "investing.com": 1,
}
SOURCE_NAMES = {"finance.yahoo.com": "Yahoo Finance", "reuters.com": "Reuters", "bloomberg.com": "Bloomberg",
                "apnews.com": "AP", "cnbc.com": "CNBC", "aljazeera.com": "Al Jazeera",
                "turkishminute.com": "Turkish Minute", "dailysabah.com": "Daily Sabah",
                "hurriyetdailynews.com": "Hürriyet Daily News", "al-monitor.com": "Al-Monitor"}


# ---------------------------------------------------------------- helpers
def _get(url, timeout=20, **kw):
    r = requests.get(url, headers=UA, timeout=timeout, **kw)
    r.raise_for_status()
    return r


def _domain(url):
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def _clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _norm_tokens(title):
    t = re.sub(r"[^a-z0-9ğüşöçı ]", " ", title.lower())
    stop = {"the", "a", "an", "of", "to", "in", "on", "as", "and", "for", "at", "by", "with", "is", "after", "amid", "s"}
    return {w for w in t.split() if w not in stop and len(w) > 1}


def _item(title, url, when, source, via, desc=""):
    return dict(title=_clean(title), url=url, when=when, date=when.date().isoformat(),
                source=source or SOURCE_NAMES.get(_domain(url), _domain(url)), via=via,
                domain=_domain(url), desc=_clean(desc))


# ---------------------------------------------------------------- sources
def fetch_gdelt(start: date, end: date, log=print):
    out = []
    s = start.strftime("%Y%m%d000000")
    e = (end + timedelta(days=1)).strftime("%Y%m%d000000")
    for region, qs in GDELT_QUERIES.items():
        for q in qs:
            url = ("https://api.gdeltproject.org/api/v2/doc/doc?query="
                   + quote_plus(q + " sourcelang:english")
                   + f"&mode=ArtList&format=json&maxrecords=75&sort=HybridRel&startdatetime={s}&enddatetime={e}")
            try:
                r = _get(url, timeout=30)
                try:
                    arts = r.json().get("articles", [])
                except ValueError:        # GDELT answers errors as plain text
                    log(f"  GDELT: {r.text[:120].strip()}")
                    arts = []
                for a in arts:
                    when = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                    out.append(_item(a.get("title", ""), a.get("url", ""), when, None, "GDELT"))
                log(f"  GDELT [{region}] {q[:50]}… -> {len(arts)}")
            except Exception as ex:
                log(f"  GDELT failed ({type(ex).__name__}: {ex})")
            time.sleep(5.5)                 # GDELT rate limit
    return out


def _parse_rss(xml_text, via):
    out = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = it.findtext("title") or ""
        link = it.findtext("link") or ""
        pub = it.findtext("pubDate") or ""
        src_el = it.find("source")
        source = src_el.text if src_el is not None else None
        try:
            when = parsedate_to_datetime(pub) if "," in pub else datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if via == "Google News" and source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]   # Google appends " - Source"
        desc = it.findtext("description") or ""
        if via == "Google News":
            desc = ""                            # Google's description just repeats the title
        out.append(_item(title, link, when, source, via, desc))
    return out


def fetch_google_news(start: date, end: date, log=print):
    out = []
    for region, qs in QUERIES.items():
        for q in qs:
            qq = f"{q} after:{start - timedelta(days=1)} before:{end + timedelta(days=1)}"
            url = f"https://news.google.com/rss/search?q={quote_plus(qq)}&hl=en-US&gl=US&ceid=US:en"
            try:
                items = _parse_rss(_get(url).text, "Google News")
                out += items
                log(f"  Google News [{region}] {q} -> {len(items)}")
            except Exception as ex:
                log(f"  Google News failed for '{q}' ({type(ex).__name__})")
            time.sleep(1.0)
    return out


def fetch_yahoo(start: date, end: date, log=print):
    out = []
    for url in YAHOO_FEEDS:
        try:
            items = _parse_rss(_get(url).text, "Yahoo")
            out += items
            log(f"  Yahoo feed -> {len(items)}")
        except Exception as ex:
            log(f"  Yahoo feed failed ({type(ex).__name__})")
    return out


# ---------------------------------------------------------------- ranking
def classify(it):
    text = it["title"] + " " + it.get("desc", "")
    tr, us = len(RX_TR.findall(text)), len(RX_US.findall(text))
    region = "Turkey" if tr > us else "US" if us > 0 else ("Turkey" if tr else None)
    pol, mac, mkt = (len(RX_POL.findall(text)), len(RX_MACRO.findall(text)), len(RX_MKT.findall(text)))
    if pol == mac == mkt == 0:
        kind = None
    elif pol > mac and pol >= mkt:
        kind = "Political"
    elif mkt > mac:
        kind = "Markets"
    else:
        kind = "Macro"
    return region, kind, pol + mac


def rank(items, start: date, end: date, focus_days=(), top=10):
    start_dt = datetime.combine(start, datetime.min.time(), timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc)
    pool = []
    for it in items:
        if not it["title"] or not (start_dt <= it["when"] < end_dt) or RX_JUNK.search(it["title"]):
            continue
        region, kind, hits = classify(it)
        if not region or not kind:
            continue
        it.update(region=region, kind=kind, hits=hits)
        pool.append(it)

    # cluster near-duplicate headlines (same story across sources)
    clusters = []
    for it in sorted(pool, key=lambda x: x["when"]):
        tok = _norm_tokens(it["title"])
        for c in clusters:
            inter = len(tok & c["tok"])
            if tok and inter / max(1, min(len(tok), len(c["tok"]))) >= 0.7:
                c["items"].append(it)
                break
        else:
            clusters.append({"tok": tok, "items": [it]})

    focus = {d if isinstance(d, date) else date.fromisoformat(d) for d in focus_days}
    scored = []
    for c in clusters:
        its = c["items"]
        # representative: prefer direct (non-Google redirect) links and preferred outlets
        rep = max(its, key=lambda x: (x["via"] != "Google News", PREFERRED.get(x["domain"], 0), len(x["title"])))
        rep = dict(rep)
        rep["date"] = min(i["date"] for i in its)
        d = date.fromisoformat(rep["date"])
        score = (min(rep["hits"], 6)
                 + (3 if d in focus else 1 if any(abs((d - f).days) <= 1 for f in focus) else 0)
                 + 2 * min(len({i["via"] for i in its}) - 1, 2)          # seen in several sources
                 + min(len(its) - 1, 4) * 0.5                            # widely covered
                 + max(PREFERRED.get(i["domain"], 0) for i in its))
        rep["score"] = round(score, 2)
        rep["vias"] = sorted({i["via"] for i in its})
        scored.append(rep)

    scored.sort(key=lambda x: -x["score"])
    half = top // 2
    us = [x for x in scored if x["region"] == "US"]
    tr = [x for x in scored if x["region"] == "Turkey"]
    pick = us[:half] + tr[:top - half]
    if len(pick) < top:                     # fill from whichever side has more
        rest = [x for x in scored if x not in pick]
        pick += rest[: top - len(pick)]
    return pick


def enrich(items, log=print):
    """Fill the one-line note with the article's own meta description (best effort)."""
    for it in items:
        if it.get("desc") or "news.google.com" in it["url"]:
            continue                         # Google links are JS redirects; skip
        try:
            page = _get(it["url"], timeout=10).text[:200_000]
            m = (re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', page, re.I)
                 or re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', page, re.I))
            if m:
                it["desc"] = _clean(m.group(1))
        except Exception:
            pass
    for it in items:
        d = it.get("desc", "")
        it["why"] = (d[:217].rsplit(" ", 1)[0] + "…") if len(d) > 220 else d
    log(f"  descriptions found for {sum(bool(i['why']) for i in items)}/{len(items)} items")
    return items


def collect(start: date, end: date, focus_days=(), top=10, sources=("gdelt", "google", "yahoo"), log=print):
    log(f"News window {start} .. {end}")
    items = []
    if "gdelt" in sources:
        items += fetch_gdelt(start, end, log)
    if "google" in sources:
        items += fetch_google_news(start, end, log)
    if "yahoo" in sources:
        items += fetch_yahoo(start, end, log)
    picked = enrich(rank(items, start, end, focus_days, top), log)
    counts = {}
    for p in picked:
        counts[p["via"]] = counts.get(p["via"], 0) + 1
    note = ("Collected automatically from " + ", ".join(f"{k} ({v})" for k, v in counts.items())
            + f" out of {len(items)} candidate headlines; ranked by topic keywords, closeness to the event day, "
              "outlet and how many sources carried the story. Notes are the articles' own descriptions.")
    return {
        "window": f"{start} to {end}",
        "summary": "",
        "source_note": note,
        "items": [{k: p[k] for k in ("date", "region", "kind", "source", "via", "title", "url", "why", "score")}
                  for p in picked],
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Collect top US/Turkey macro & political headlines.")
    ap.add_argument("start"), ap.add_argument("end")
    ap.add_argument("--focus", nargs="*", default=[], help="event day(s) YYYY-MM-DD to weight up")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--sources", default="gdelt,google,yahoo")
    ap.add_argument("--out", default="news.json")
    a = ap.parse_args()
    res = collect(date.fromisoformat(a.start), date.fromisoformat(a.end), a.focus, a.top,
                  tuple(a.sources.split(",")), log=lambda m: print(m, file=sys.stderr))
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    for i in res["items"]:
        print(f"{i['date']}  {i['region']:6} {i['kind']:9} {i['score']:5}  {i['title'][:90]}  [{i['via']}]")
