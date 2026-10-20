#!/usr/bin/env python3
"""
Load a Borsa Istanbul BAP "tbliste" file for a given date into a pandas DataFrame.

Usage:
    python tbliste_import.py 08.10.2026
    python tbliste_import.py 2026-10-08

Reads:  ~/BorsaIstanbul/BAP/2026-10-08/tbliste_2026-10-08.xls
Result: DataFrame `tbliste` with only the selected columns.
"""

import argparse
import sys
import unicodedata
from datetime import datetime
from io import StringIO
from pathlib import Path

import pandas as pd

BASE_DIR = Path.home() / "BorsaIstanbul" / "BAP"

# Columns to keep, exactly as they appear in the file header (two-line TR/EN headers)
WANTED_COLUMNS = [
    "ISIN Kodu\nISIN Code",
    "İhraçcı Kurum\nIssuer",
    "Getiri Türü\nType of I",
    "Yıllık Kupon Sıklığı\nCoupons Frequency",
    "İtfa Tarihi\nMaturity Date",
    "Sonraki Kupon Oranı %\nNext Coupon Rate %",
    "Para Birimi\nCurrency",
]

CURRENCY_COL = "Para Birimi\nCurrency"

HEADER_MARKER = "ISIN Kodu"  # used to locate the header row if it isn't the first row


# --------------------------------------------------------------------------- helpers
def parse_date(date_str: str) -> datetime:
    """Accept DD.MM.YYYY (e.g. 08.10.2026), DD/MM/YYYY or YYYY-MM-DD."""
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date '{date_str}'. Use DD.MM.YYYY, e.g. 08.10.2026")


def normalise(label) -> str:
    """Canonical form for header matching: NFC unicode, collapsed whitespace/newlines."""
    if label is None or (isinstance(label, float) and pd.isna(label)):
        return ""
    text = unicodedata.normalize("NFC", str(label))
    return " ".join(text.split())


def tbliste_path(date: datetime) -> Path:
    iso = date.strftime("%Y-%m-%d")
    return BASE_DIR / iso / f"tbliste_{iso}.xls"


def read_raw_sheet(path: Path) -> pd.DataFrame:
    """
    Read the file with no header, whatever its real format is.
    Exchange '.xls' files are sometimes true BIFF xls, sometimes xlsx, sometimes HTML.
    """
    with open(path, "rb") as fh:
        magic = fh.read(8)

    if magic.startswith(b"\xd0\xcf\x11\xe0"):          # legacy OLE2 .xls
        return pd.read_excel(path, header=None, dtype=str, engine="xlrd")
    if magic.startswith(b"PK"):                         # xlsx disguised as .xls
        return pd.read_excel(path, header=None, dtype=str, engine="openpyxl")

    # Otherwise assume an HTML table saved with an .xls extension
    raw = path.read_bytes()
    for enc in ("utf-8", "cp1254", "iso-8859-9"):
        try:
            html = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    tables = pd.read_html(StringIO(html), header=None)
    # pick the table that actually contains the ISIN header
    for t in tables:
        if t.astype(str).apply(lambda c: c.str.contains(HEADER_MARKER, na=False)).any().any():
            return t.astype(str).where(t.notna(), None)
    raise ValueError(f"No table containing '{HEADER_MARKER}' found in {path}")


def promote_header(raw: pd.DataFrame) -> pd.DataFrame:
    """Find the row holding the column titles and use it as the header."""
    for idx, row in raw.iterrows():
        if any(HEADER_MARKER in normalise(v) for v in row.values):
            df = raw.iloc[idx + 1:].copy()
            df.columns = [normalise(v) for v in row.values]
            return df.reset_index(drop=True)
    raise ValueError(f"Header row containing '{HEADER_MARKER}' not found")


# --------------------------------------------------------------------------- main loader
def load_tbliste(date_str: str) -> pd.DataFrame:
    date = parse_date(date_str)
    path = tbliste_path(date)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    df = promote_header(read_raw_sheet(path))

    # Map normalised header -> requested original name
    wanted_norm = {normalise(c): c for c in WANTED_COLUMNS}
    missing = [orig for norm, orig in wanted_norm.items() if norm not in df.columns]
    if missing:
        available = "\n  ".join(repr(c) for c in df.columns if c)
        raise KeyError(
            "Columns not found in file:\n  "
            + "\n  ".join(repr(m) for m in missing)
            + f"\nAvailable columns:\n  {available}"
        )

    tbliste = df[list(wanted_norm)].rename(columns=wanted_norm)

    # Drop blank / footer rows (no ISIN)
    isin_col = WANTED_COLUMNS[0]
    tbliste = tbliste[tbliste[isin_col].notna() & (tbliste[isin_col].str.strip() != "")]
    tbliste = tbliste.reset_index(drop=True)

    return tbliste


ISSUER_COL = "İhraçcı Kurum\nIssuer"
TREASURY_ISSUER = "Hazine / Turkish Treasury"

COUPON_TYPE_COL = "Getiri Türü\nType of I"
DISCOUNTED = "İskontolu / Discounted"
FIXED = "Sabit / Fixed"
KEEP_COUPON_TYPES = [DISCOUNTED, FIXED]

COUPON_FREQ_COL = "Yıllık Kupon Sıklığı\nCoupons Frequency"


def keep_values(df: pd.DataFrame, column: str, values) -> pd.DataFrame:
    """
    Keep only rows whose `column` equals one of `values`.
    Matching ignores extra whitespace/newlines, unicode form and case.
    """
    if isinstance(values, str):
        values = [values]
    targets = {normalise(v).casefold() for v in values}
    mask = df[column].map(lambda v: normalise(v).casefold() in targets)
    if not mask.any():
        found = sorted({normalise(v) for v in df[column] if normalise(v)})
        raise ValueError(
            f"No rows with {normalise(column)!r} in {list(values)}. Values in file:\n  "
            + "\n  ".join(found[:30])
        )
    return df[mask].reset_index(drop=True)


def keep_treasury(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only Turkish Treasury issues."""
    return keep_values(df, ISSUER_COL, TREASURY_ISSUER)


def keep_discounted_and_fixed(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only discounted and fixed-coupon securities."""
    return keep_values(df, COUPON_TYPE_COL, KEEP_COUPON_TYPES)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import tbliste for a given date.")
    parser.add_argument("date", help="Date as DD.MM.YYYY, e.g. 08.10.2026")
    args = parser.parse_args()

    global tbliste
    try:
        tbliste = load_tbliste(args.date)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Imported {len(tbliste)} rows, {tbliste.shape[1]} columns")

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    try:
        # 0) Keep only TRY-denominated securities, then drop the currency column
        is_try = tbliste[CURRENCY_COL].map(lambda v: "TRY" in normalise(v).upper())
        if not is_try.any():
            found = sorted({normalise(v) for v in tbliste[CURRENCY_COL] if normalise(v)})
            raise ValueError(f"No TRY rows. Currencies in file: {found}")
        tbliste = tbliste[is_try].drop(columns=[CURRENCY_COL]).reset_index(drop=True)
        print(f"TRY securities: {len(tbliste)} rows")

        # 1) Keep only Turkish Treasury issues
        tbliste = keep_treasury(tbliste)
        print(f"Treasury issues: {len(tbliste)} rows")

        # 2) Keep only discounted and fixed-coupon securities
        tbliste = keep_discounted_and_fixed(tbliste)
        print(f"Discounted / fixed: {len(tbliste)} rows")

        # 3) Issuer column is now constant (Treasury) -> drop it
        tbliste = tbliste.drop(columns=[ISSUER_COL])

        # 4) Discounted securities pay no coupons -> frequency 0
        is_discounted = tbliste[COUPON_TYPE_COL].map(
            lambda v: normalise(v).casefold() == normalise(DISCOUNTED).casefold()
        )
        tbliste.loc[is_discounted, COUPON_FREQ_COL] = "0"

        # 5) Fixed-coupon securities with frequency "-" -> frequency 1
        is_fixed = tbliste[COUPON_TYPE_COL].map(
            lambda v: normalise(v).casefold() == normalise(FIXED).casefold()
        )
        freq_is_dash = tbliste[COUPON_FREQ_COL].map(lambda v: normalise(v) == "-")
        tbliste.loc[is_fixed & freq_is_dash, COUPON_FREQ_COL] = "1"

        # 6) Keep only ISINs beginning with "TR"
        isin_col = WANTED_COLUMNS[0]
        tbliste[isin_col] = tbliste[isin_col].map(lambda v: normalise(v).upper())
        tbliste = tbliste[
            tbliste[isin_col].map(lambda v: normalise(v).upper().startswith("TR"))
        ].reset_index(drop=True)
        print(f"TR ISINs: {len(tbliste)} rows")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(tbliste.head())

    # 7) Save the final table in the current working directory
    out_path = Path.cwd() / "definitions.xlsx"
    tbliste.to_excel(out_path, index=False, engine="openpyxl")
    print(f"Saved {len(tbliste)} rows to {out_path}")

    # ------------------------------------------------------------------
    # Further processing of `tbliste` goes here
    # ------------------------------------------------------------------
    return 0


if __name__ == "__main__":
    sys.exit(main())
