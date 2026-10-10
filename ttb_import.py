#!/usr/bin/env python3
"""
Load a Borsa Istanbul BAP daily bulletin "ttb" CSV for a given date into a pandas DataFrame.

Usage:
    python ttb_import.py 08.10.2026
    python ttb_import.py 2026-10-08

Reads:  ~/BorsaIstanbul/BAP/2026-10-08/ttb_2026-10-08.csv
Result: DataFrame `ttb` with only the selected columns.

File layout: ';'-separated, row 1 = Turkish headers (discarded),
row 2 = English headers, then the data.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE_DIR = Path.home() / "BorsaIstanbul" / "BAP"

WANTED_COLUMNS = [
    "TRADEDATE",
    "INSTRUMENT CODE",
    "ISIN/CODE",
    "CURRENCY",
    "CLOSING",
    "TOTAL TRADED VALUE",
]


# --------------------------------------------------------------------------- helpers
def parse_date(date_str: str) -> datetime:
    """Accept DD.MM.YYYY (e.g. 08.10.2026), DD/MM/YYYY or YYYY-MM-DD."""
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date '{date_str}'. Use DD.MM.YYYY, e.g. 08.10.2026")


def ttb_path(date: datetime) -> Path:
    iso = date.strftime("%Y-%m-%d")
    return BASE_DIR / iso / f"ttb_{iso}.csv"


# --------------------------------------------------------------------------- main loader
def load_ttb(date_str: str) -> pd.DataFrame:
    date = parse_date(date_str)
    path = ttb_path(date)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    last_err = None
    for enc in ("utf-8-sig", "cp1254", "iso-8859-9"):
        try:
            ttb = pd.read_csv(
                path,
                sep=";",
                skiprows=1,                       # drop the Turkish header row
                usecols=lambda c: c.strip() in WANTED_COLUMNS,
                dtype={"TRADEDATE": str, "INSTRUMENT CODE": str, "ISIN/CODE": str,
                       "CURRENCY": str},
                encoding=enc,
            )
            break
        except UnicodeDecodeError as exc:
            last_err = exc
    else:
        raise ValueError(f"Could not decode {path}: {last_err}")

    ttb.columns = [c.strip() for c in ttb.columns]
    missing = [c for c in WANTED_COLUMNS if c not in ttb.columns]
    if missing:
        raise KeyError(f"Columns not found in file: {missing}")
    ttb = ttb[WANTED_COLUMNS]

    # Sanity check: trade dates inside the file should match the requested date
    iso = date.strftime("%Y-%m-%d")
    other_dates = set(ttb["TRADEDATE"].dropna().str.strip()) - {iso}
    if other_dates:
        print(f"WARNING: file contains other trade dates: {sorted(other_dates)}",
              file=sys.stderr)

    return ttb


def main() -> int:
    parser = argparse.ArgumentParser(description="Import ttb bulletin for a given date.")
    parser.add_argument("date", help="Date as DD.MM.YYYY, e.g. 08.10.2026")
    args = parser.parse_args()

    global ttb
    try:
        ttb = load_ttb(args.date)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Imported {len(ttb)} rows, {ttb.shape[1]} columns")

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    code = ttb["INSTRUMENT CODE"].fillna("").str.strip()

    # 1) Keep only instrument codes beginning with "TR"
    ttb = ttb[code.str.startswith("TR")].reset_index(drop=True)
    print(f"Codes starting with TR: {len(ttb)} rows")

    # 2) Keep only instrument codes containing "KESN"
    code = ttb["INSTRUMENT CODE"].fillna("").str.strip()
    ttb = ttb[code.str.contains("KESN", regex=False)].reset_index(drop=True)
    print(f"KESN instruments: {len(ttb)} rows")

    # 3) Drop the instrument code column
    ttb = ttb.drop(columns=["INSTRUMENT CODE"])

    # 4) Keep only TRY-denominated rows
    ttb = ttb[ttb["CURRENCY"].fillna("").str.strip().str.upper() == "TRY"].reset_index(drop=True)
    print(f"TRY rows: {len(ttb)} rows")

    # 5) Currency is now constant (TRY) -> drop it
    ttb = ttb.drop(columns=["CURRENCY"])

    print(ttb.head())

    # 6) Save the final table in the current working directory
    out_path = Path.cwd() / "Market_Data.xlsx"
    ttb.to_excel(out_path, index=False, engine="openpyxl")
    print(f"Saved {len(ttb)} rows to {out_path}")

    # ------------------------------------------------------------------
    # Further processing of `ttb` goes here
    # ------------------------------------------------------------------
    return 0


if __name__ == "__main__":
    sys.exit(main())
