"""Fetch ETF PCF baskets, then each distinct constituent's basic and daily data.

Default target: 2023-01-01 through 2026-10-01, including ETFs that have since
delisted. Requires data/etf_basic.csv, Tushare permissions, and a configured
TUSHARE_TOKEN (or a token saved with tushare.set_token()).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from etf_constituent_ingest import main as ingest_constituents
from etf_ingest import PERMISSION_ERROR, TushareClient, atomic_csv, parse_day
from etf_market_ingest import save_basket, ymd
from tushare_etf_api import tushare_pro_from_env


def etfs_in_range(catalog_path: Path, last: date, listed_only: bool) -> list[tuple[str, str, date]]:
    if not catalog_path.is_file():
        raise FileNotFoundError(f"ETF catalog not found: {catalog_path}; run tushare_etf_api.py first")
    catalog = pd.read_csv(catalog_path, dtype=str, keep_default_na=False)
    required = {"ts_code", "list_status", "list_date", "exchange"}
    if not required.issubset(catalog.columns):
        raise ValueError(f"ETF catalog missing columns: {sorted(required - set(catalog.columns))}")
    statuses = {"L"} if listed_only else {"L", "D"}
    selected: dict[str, tuple[str, str, date]] = {}
    for row in catalog[catalog["list_status"].isin(statuses)].to_dict("records"):
        exchange = row["exchange"].strip().upper()
        code = row["ts_code"].strip().upper()
        if code.endswith(".OF") and exchange in {"SH", "SZ"}:
            code = f"{code[:6]}.{exchange}"
        if exchange not in {"SH", "SZ"} or not re.fullmatch(r"\d{6}\.(SH|SZ)", code):
            raise ValueError(f"Unsupported ETF code/exchange in catalog: {row['ts_code']}, {exchange}")
        if not code.endswith(f".{exchange}"):
            raise ValueError(f"ETF exchange mismatch in catalog: {row['ts_code']}, {exchange}")
        listed_on = parse_day(row["list_date"]) or parse_day(row.get("setup_date", ""))
        if listed_on is None:
            raise ValueError(f"Missing listing/setup date for ETF {code}")
        if listed_on > last:
            continue
        if code not in selected or listed_on < selected[code][2]:
            selected[code] = (code, exchange, listed_on)
    return [selected[code] for code in sorted(selected)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="20230101", help="First PCF and stock daily date (YYYYMMDD)")
    parser.add_argument("--end-date", default="20261001", help="Last PCF and stock daily date (YYYYMMDD)")
    parser.add_argument("--catalog", type=Path, default=Path("data/etf_basic.csv"))
    parser.add_argument("--pcf-output", type=Path, default=Path("data/etf_market"))
    parser.add_argument("--output", type=Path, default=Path("data/constituents"))
    parser.add_argument("--listed-only", action="store_true", help="Exclude ETFs currently marked delisted")
    parser.add_argument("--markets", nargs="+", choices=("CN", "HK", "US"), default=("CN", "HK", "US"))
    parser.add_argument("--max-stocks", type=int, help="Limit unique stocks for a trial run")
    parser.add_argument("--interval", type=float, default=1.3, help="Seconds between API requests")
    parser.add_argument("--refresh-pcf", action="store_true", help="Re-fetch PCF months already queried")
    parser.add_argument("--refresh-basic", action="store_true")
    parser.add_argument("--refresh-daily", action="store_true")
    parser.add_argument("--plan", action="store_true", help="Show ETF count without writing data or calling Tushare")
    args = parser.parse_args(argv)
    try:
        first = parse_day(args.start_date)
        last = parse_day(args.end_date)
        if first is None or last is None or first > last or last > date.today():
            parser.error("Date range must be nonempty, ordered, and end today or earlier")
        if args.interval < 0 or (args.max_stocks is not None and args.max_stocks < 1):
            parser.error("--interval must be non-negative and --max-stocks must be positive")
        etfs = etfs_in_range(args.catalog, last, args.listed_only)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(f"ETF count: {len(etfs)}; PCF and stock daily range: {ymd(first)}-{ymd(last)}", flush=True)
    if args.plan:
        return 0

    try:
        client = TushareClient(tushare_pro_from_env(), interval=args.interval)
    except RuntimeError as exc:
        parser.error(str(exc))
    pcf_errors: list[dict[str, str]] = []
    for index, (code, exchange, listed_on) in enumerate(etfs, start=1):
        start = max(first, listed_on)
        try:
            count = save_basket(client, code, exchange, start, last, args.pcf_output, args.refresh_pcf)
            print(f"PCF {index}/{len(etfs)} {code}: +{count} rows", flush=True)
        except Exception as exc:
            pcf_errors.append({"ts_code": code, "start_date": ymd(start), "end_date": ymd(last), "error": str(exc)})
            print(f"PCF {code}: {exc}", file=sys.stderr, flush=True)
            if PERMISSION_ERROR.search(str(exc)):
                break
    atomic_csv(pd.DataFrame(pcf_errors, columns=("ts_code", "start_date", "end_date", "error")),
               args.output / "pcf_errors.csv")
    if pcf_errors:
        print(f"PCF incomplete: {len(pcf_errors)} errors; fix and rerun before stock aggregation", file=sys.stderr)
        return 1

    constituent_args = [
        "--pcf-root", str(args.pcf_output / "pcf"), "--output", str(args.output),
        "--basket-start", ymd(first), "--basket-end", ymd(last),
        "--start-date", ymd(first), "--end-date", ymd(last),
        "--markets", *args.markets, "--interval", str(args.interval),
    ]
    if args.max_stocks is not None:
        constituent_args.extend(("--max-stocks", str(args.max_stocks)))
    if args.refresh_basic:
        constituent_args.append("--refresh-basic")
    if args.refresh_daily:
        constituent_args.append("--refresh-daily")
    return ingest_constituents(constituent_args)


if __name__ == "__main__":
    raise SystemExit(main())
