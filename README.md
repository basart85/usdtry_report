# USD/TRY tail-day report

One command downloads USD/TRY and US Treasury data, tests last week's daily returns against the last year, builds US zero curves around the extreme day, collects related US and Turkey headlines, and writes an HTML report with PNG charts and CSV data.

```bash
pip install -r requirements.txt
python usdtry_report.py                      # last 5 trading days, everything live
```

The output goes to `report_<week end>/`:

| File | What it is |
|---|---|
| `report.html` | Interactive report (open in a browser) |
| `report_body.html` | Same page without `<html>`/`<body>` wrapper, for embedding |
| `week_returns.png`, `returns_histogram.png` | Last week vs last year's band, and the full distribution |
| `zero_curve_<date>.png` / `.csv` | Zero curves for the event day and the previous US trading day, with change in bp |
| `usdtry.csv`, `last_week.csv` | Rates, log returns, percentile ranks, tail flags |
| `news.json` | Ranked headlines with links |
| `run.log` | What was fetched from where, including any fallbacks |

## Data sources (all free, no API keys)

| Data | Primary | Fallback |
|---|---|---|
| USD/TRY daily | ECB reference rates via [Frankfurter](https://frankfurter.dev) | Yahoo Finance chart endpoint (`--fx-source yahoo` to prefer it) |
| US curve | U.S. Treasury daily par yield curve, bootstrapped to zero rates | Fed Gürkaynak–Sack–Wright zero curve (`--curve-source gsw`) |
| News | GDELT DOC 2.0 API, Google News RSS | Yahoo Finance RSS (recent days only) |

## Method

- **Returns.** Daily log returns in percent.
  - "Last week" is the last `--days` observations (default 5).
  - "Last year" is the `--lookback` returns before them (default 252).
  - A day is a tail day if it falls below the `--pct` percentile (default 5) or above 100 − `--pct`.
- **Event day.** If no day is flagged, the most extreme day of the week is used and labelled as a near-miss. Use `--no-fallback` to skip the curves and news in that case.
- **Zero curve.**
  - Bills up to 6M are treated as zero-coupon.
  - The par curve is interpolated (PCHIP) onto a semi-annual grid and discount factors are bootstrapped out to 30Y.
  - Rates are continuously compounded by default; `--comp semi` gives semi-annual rates.
- **News ranking.**
  1. Headlines from the window (event day minus `--news-days-before`, default 4) are classified as US or Turkey and Macro, Political or Markets by keyword.
  2. Off-topic and old items are dropped.
  3. The same story seen in several sources is merged into one item.
  4. Items are scored on topic keywords, closeness to the event day, outlet, and how many sources carried the story.
  5. The top 10 are kept, split 5 US and 5 Turkey when available.
  6. The one-line note is the article's own meta description, when it can be fetched.

## Useful options

```bash
python usdtry_report.py --end 2026-10-06 --pct 5 --days 5
python usdtry_report.py --curve-source gsw --comp semi
python usdtry_report.py --news my_headlines.json        # curated list instead of auto
python usdtry_report.py --no-news
python usdtry_report.py --fx-csv examples/usdtry_ecb.csv --par-csv examples/treasury_par_2026.csv \
        --news examples/news_curated_2026-10-02.json    # fully offline example
python news_fetch.py 2026-09-28 2026-10-02 --focus 2026-10-02   # news only
python ust_zero_curve.py 2026-10-02                     # zero curves only
```

## Relevance filter: up-day and down-day prompts

After the headlines are collected (or loaded with `--news`), each one is compared with a written description of the kind of day it was. These descriptions live in `prompts.json`, which you can edit:

- **up**: USD/TRY rises and the lira weakens. Typical drivers: a strong dollar, a hawkish Fed or US rate hike, rising Treasury yields, an oil or energy surge, Turkish political turmoil, central bank cuts or reserve sales, outflows, downgrades.
- **down**: USD/TRY falls and the lira strengthens. Typical drivers: weak US data, a dovish Fed, falling yields, falling oil or a ceasefire, Turkish central bank hikes, reserves rising, inflows and carry trade, slowing inflation, upgrades.

The day type comes from the event day: above the median return (or above the upper band) means up, otherwise down. The top `--relevant-top` headlines (default 5) lead the report with their score and the terms they matched. The rest are listed under "Show the other headlines". Automatic collection now gathers `--news-top 20` candidates so the filter has something to choose from.

There are two scoring back-ends:

| Backend | How it works | When it is used |
|---|---|---|
| `tfidf` | Finance-aware tokenizer, then TF-IDF vectors, then cosine similarity. No model or extra package needed. | CI and deployed builds, or when Ollama isn't running |
| `ollama` | Embeddings from your local Ollama (`--ollama-model`, default `nomic-embed-text`), then cosine similarity | `--sim-backend ollama`, or the default `auto` when `OLLAMA_HOST` (default localhost:11434) answers |

What the tokenizer does:
- It folds accents (Erdoğan → erdogan) and merges phrases ("Federal Reserve" → fed, "central bank" → centralbank, "Strait of Hormuz" → hormuz).
- It stems words lightly and maps synonyms to shared concepts (raise/tighten → hike, crude/Brent/fuel → energy, payrolls → jobs).
- It adds context tokens per sentence: `@tr_hike` for a Turkish central bank hike (lira-positive) versus `@us_hike` for a Fed hike (lira-negative).

If Ollama is unreachable or the model isn't pulled, scoring falls back to the tokenizer automatically and `run.log` says so.

```bash
ollama pull nomic-embed-text                              # once, for the local embedding backend
python news_relevance.py report_2026-10-06/news.json --direction up --top 5 --show-tokens
python usdtry_report.py --sim-backend tfidf               # force the tokenizer
python usdtry_report.py --relevant-top 0                  # keep every headline, no filtering
```

## Turkish lira zero curve (`try_zero_curve.py`)

This builds the TRY government zero curve from bond-level data: a definitions file (ISIN, type, coupon frequency, maturity, coupon per period) and a day's market prices (ISIN, clean closing price, traded value).

```bash
python try_zero_curve.py --defs definitions.xlsx --market Market_Data.xlsx --us   # trade date read from the file
```

What it does:
- Repairs prices that lost their decimal point on export.
- Averages repeated trades in the same bond, weighted by traded value.
- Keeps only TL fixed-coupon bonds and discounted bills. Floaters, CPI-linked bonds, lease certificates (TRD…) and the gold-denominated bond are excluded and listed in `try_excluded.csv`.

Conventions, checked against Gedik's DİBS bulletin:
- Coupons fall every 182 days, counted back from maturity, and accrued interest builds up over the period.
- Quoted compound yield = (1+r)^2 − 1, where r is the half-yearly rate over whole coupon periods. Bills and bonds in their final period use Act/365.

The curve itself:
- Nelson–Siegel is the default; Svensson is available with `--model nss`. The curve is fitted to dirty prices discounted on actual days.
- `--us` adds the US zero curve for the same day.

The trade date comes from the market file's Trade Date column. If the file holds several dates, choose one with `--date`; if it has no date column, `--date` is required. The outputs are written to `try_curve_<date>/`, and both CSVs start with a `date` column: `try_zero_curve.png`, `try_zero_curve.csv` (zero, par, 1y forward and the USD spread at standard tenors), `try_bonds.csv` (accrued interest, dirty price, yields, fit error per bond) and `try_fit.json` (model parameters and fit statistics).

### Lira curve in the weekly report

After the excess-return days are found, `usdtry_report.py` builds the lira curves itself, for each event day and for the previous Turkish trading day:

1. It runs `tbliste_import.py DD.MM.YYYY` and `ttb_import.py DD.MM.YYYY` inside `try_curve_<date>/inputs/`. They read `~/BorsaIstanbul/BAP/<date>/` and write `definitions.xlsx` and `Market_Data.xlsx` there.
2. It runs `try_zero_curve.py` on those two files, which writes the curve into `try_curve_<date>/`.
3. It compares the two days and adds the lira section to the report: both curves, the daily change in bp, and the TRY − USD spread on the event day, in the same compounding as the US curve. It also writes `try_zero_curve_<date>.png` and `.csv` to the report folder.

The previous day is the latest weekday in the 7 days before the event that has BAP files, so Turkish holidays are skipped. If a day has no BAP files but already has a `try_curve_<date>/` folder, that folder is reused and the log says so. If both are missing, the lira section is left out and `run.log` explains why.

```bash
python usdtry_report.py --end 2026-10-06                      # import scripts in this folder, BAP data in ~/BorsaIstanbul/BAP
python usdtry_report.py --end 2026-10-06 --bap-scripts ~/bist # import scripts somewhere else
python usdtry_report.py --try-source folders                  # don't run the imports; read existing try_curve_* folders
python usdtry_report.py --try-dir curves                      # keep the try_curve_* folders in ./curves
python usdtry_report.py --no-try                              # leave the lira curve out
```

`--try-model nss` and `--try-settle-days 1` are passed on to `try_zero_curve.py`.

## Website: "Currency & Yield Reports"

`build_site.py` turns every `report_*/` folder in this directory into a static site in `site/`:

- `index.html` has the latest report up top (verdict, mini chart, key numbers, headlines), then the full archive.
  - The archive can be searched by date or headline, filtered by year, or limited to tail weeks.
- `reports/<week-end>/index.html` holds one interactive report each.
  - Each has older/newer navigation, chart thumbnails and CSV/JSON downloads.
- It also writes `reports.json`, `404.html`, `robots.txt`, `.nojekyll`, and `sitemap.xml` when `--base-url` is given.

```bash
python usdtry_report.py                 # make this week's report folder
python build_site.py                    # build site/ from all report folders
python build_site.py --serve            # build and preview at http://localhost:8000
python build_site.py --update           # run usdtry_report.py for the latest week, then build
python build_site.py --update -- --pct 10 --curve-source gsw   # options after -- go to usdtry_report.py
python build_site.py --base-url https://<user>.github.io/<repo>/   # sitemap + canonical links
```

All links are relative, so `site/` can be deployed as-is:

- **Netlify:** drag the `site/` folder onto the Netlify dashboard.
- **Cloudflare Pages / S3 / nginx:** upload `site/` as the web root.
- **GitHub Pages, updated weekly and automatically:**
  1. Run `python build_site.py --workflow` to write `.github/workflows/publish-reports.yml`.
  2. Push this folder as a repository and set Settings → Pages → Source to GitHub Actions.
  3. Every Wednesday the workflow makes a new report, commits the `report_*/` folder (the folders are the archive), rebuilds the site and deploys it.
  4. Optionally, set a repository variable `SITE_URL` for the sitemap.

The report folders are the source of truth. Delete one and rebuild to remove a week, or re-run `usdtry_report.py --end <date>` to fill a gap. If two folders cover the same week, the newer one wins.

## Caveats

- **Timing.** The ECB fixing is set around 14:15 CET, before the US session, so a US curve move on the same date can follow the FX move rather than cause it. Yahoo FX is an end-of-day close instead.
- **Friday effect.** USD/TRY Fridays include the weekend's interest carry, so they cluster near the top of the pooled distribution. The same-weekday percentile column in the report adjusts for this.
- **Data lags.**
  - The Treasury curve appears in the evening of each US business day.
  - The GSW curve lags by several days.
  - GDELT asks for about one request every 5 seconds; the script waits, so news collection takes about 30 seconds.
- **Feed stability.** Google News RSS and Yahoo endpoints are public but unofficial, and can change or rate-limit without notice.
  - Every source has a fallback or is optional, and `run.log` records what happened.
  - Google News links are Google redirect URLs, so those items usually have no description.
- **Curated mode.** Automatic ranking is keyword-based. For a report you will share, review `news.json` and rerun with `--news` if you want to edit the list.
