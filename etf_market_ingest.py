"""Download listed ETF quotes, adjustment factors, and exchange PCF baskets.

The input universe is data/etf_basic.csv. Only list_status=L is selected.
Quote/factor history starts at listing by default. PCF defaults to the last
30 calendar days; --full-basket-history explicitly requests older baskets.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from etf_ingest import CODE_PATTERN, PERMISSION_ERROR, TushareClient, atomic_csv, parse_day
from tushare_etf_api import fetch_etf_basic, tushare_pro_from_env


LIMITS = {"fund_daily": 5000, "fund_adj": 2000, "etf_sh_cons": 3000, "etf_sz_cons": 3000}
PCF_FIELDS = {
    "SH": ("trade_date", "ts_code", "con_code", "con_name", "qty", "sub_flag", "cpr", "rdr", "sca", "exchange"),
    "SZ": ("trade_date", "ts_code", "con_code", "con_name", "qty", "sub_flag", "cpr", "rdr", "sub_cc", "red_cc", "exchange"),
}
DAY = timedelta(days=1)


def ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def combine(frames: Iterable[pd.DataFrame], columns: Iterable[str] = ()) -> pd.DataFrame:
    """Combine API pages without pandas' all-NA-column concat warning."""
    pages = list(frames)
    names = list(dict.fromkeys([*columns, *(name for page in pages for name in page.columns)]))
    records = [record for page in pages for record in page.to_dict("records")]
    return pd.DataFrame.from_records(records, columns=names)


def load_listed(path: Path, codes: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"ETF catalog not found: {path}; run tushare_etf_api.py first")
    catalog = pd.read_csv(path, dtype=str, keep_default_na=False)
    required = {"ts_code", "list_status", "list_date", "exchange"}
    if not required.issubset(catalog.columns):
        raise ValueError(f"ETF catalog is missing columns: {sorted(required - set(catalog.columns))}")
    listed = catalog[catalog["list_status"] == "L"].copy()
    listed["source_ts_code"] = listed["ts_code"]
    # Tushare occasionally returns an off-exchange suffix for a listed ETF.
    # Its exchange column identifies the trading suffix required by market APIs.
    off_exchange = listed["ts_code"].str.fullmatch(r"\d{6}\.OF")
    listed.loc[off_exchange, "ts_code"] = (
        listed.loc[off_exchange, "ts_code"].str[:6] + "." + listed.loc[off_exchange, "exchange"]
    )
    if codes:
        requested = {code.strip().upper() for code in codes}
        known = set(listed["ts_code"]) | set(listed["source_ts_code"])
        unknown = requested - known
        if unknown:
            raise ValueError(f"Codes are not listed in the ETF catalog: {', '.join(sorted(unknown))}")
        listed = listed[listed["ts_code"].isin(requested) | listed["source_ts_code"].isin(requested)]
    # Prefer the proper exchange code if both .OF and .SZ/.SH describe one ETF.
    listed["_off_exchange"] = listed["source_ts_code"].str.endswith(".OF")
    listed = listed.sort_values("_off_exchange").drop_duplicates("ts_code", keep="first")
    listed = listed.drop(columns="_off_exchange")
    for row in listed.itertuples(index=False):
        if not CODE_PATTERN.fullmatch(row.ts_code) or row.exchange not in ("SH", "SZ"):
            raise ValueError(f"Unsupported ETF code or exchange: {row.ts_code}, {row.exchange}")
        if not (parse_day(row.list_date) or parse_day(getattr(row, "setup_date", ""))):
            raise ValueError(f"Missing list_date and setup_date for {row.ts_code}")
    return listed.sort_values("ts_code").reset_index(drop=True)


def validate_result(frame: pd.DataFrame, method: str, code: str, first: date, last: date) -> pd.DataFrame:
    if frame.empty:
        return frame
    required = {"ts_code", "trade_date"}
    if method.startswith("etf_"):
        required.add("con_code")
    if not required.issubset(frame.columns):
        raise RuntimeError(f"{method} response is missing {sorted(required - set(frame.columns))}")
    frame = frame.copy()
    frame["ts_code"] = frame["ts_code"].astype(str)
    frame["trade_date"] = frame["trade_date"].astype(str)
    if (frame["ts_code"] != code).any() or not frame["trade_date"].between(ymd(first), ymd(last)).all():
        raise RuntimeError(f"{method} returned data outside {code} {ymd(first)}-{ymd(last)}")
    return frame


def fetch_capped(client: TushareClient, method: str, code: str, first: date, last: date) -> pd.DataFrame:
    """Split saturated date ranges; page a single PCF day if necessary."""
    if first > last:
        return pd.DataFrame()
    limit = LIMITS[method]
    kwargs: dict[str, Any] = {
        "ts_code": code, "start_date": ymd(first), "end_date": ymd(last), "limit": limit,
    }
    page = validate_result(client.call(method, **kwargs), method, code, first, last)
    if len(page) < limit:
        return page
    if first < last:
        middle = first + (last - first) // 2
        return combine((
            fetch_capped(client, method, code, first, middle),
            fetch_capped(client, method, code, middle + DAY, last),
        ))
    if not method.startswith("etf_"):
        raise RuntimeError(f"{method} hit its {limit}-row limit for one day: {code} {ymd(first)}")

    pages = [page]
    seen = set(page["con_code"].astype(str))
    offset = limit
    while True:
        next_page = validate_result(client.call(method, **kwargs, offset=offset), method, code, first, last)
        if next_page.empty:
            break
        next_codes = set(next_page["con_code"].astype(str))
        if seen & next_codes:
            raise RuntimeError(f"{method} pagination repeated constituents for {code} {ymd(first)}")
        pages.append(next_page)
        seen.update(next_codes)
        if len(next_page) < limit:
            break
        offset += len(next_page)
    return combine(pages)


def save_series(client: TushareClient, method: str, code: str, first: date, last: date,
                output: Path, refresh: bool = False) -> int:
    destination = output / ("daily" if method == "fund_daily" else "adj_factor") / f"{code}.csv"
    if first > last:
        return 0
    old = (pd.read_csv(destination, dtype={"ts_code": str, "trade_date": str})
           if destination.exists() else pd.DataFrame())
    intervals = [(first, last)]
    if not refresh and not old.empty:
        earliest = parse_day(old["trade_date"].min())
        latest = parse_day(old["trade_date"].max())
        intervals = []
        if first < earliest:
            intervals.append((first, min(last, earliest - DAY)))
        if last > latest:
            intervals.append((max(first, latest + DAY), last))
    if not intervals:
        return 0
    new = combine(fetch_capped(client, method, code, start, end) for start, end in intervals)
    if new.empty:
        return 0
    if refresh and not old.empty:
        old = old[~old["trade_date"].between(ymd(first), ymd(last))]
    combined = combine((old, new)).drop_duplicates(["ts_code", "trade_date"], keep="last")
    atomic_csv(combined.sort_values("trade_date").reset_index(drop=True), destination)
    return len(new)


def month_ranges(first: date, last: date) -> Iterable[tuple[date, date]]:
    cursor = first
    while cursor <= last:
        next_month = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        end = min(last, next_month - DAY)
        yield cursor, end
        cursor = end + DAY


def merge_intervals(intervals: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
    merged: list[tuple[date, date]] = []
    for first, last in sorted(intervals):
        if merged and first <= merged[-1][1] + DAY:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def missing_intervals(first: date, last: date,
                      covered: list[tuple[date, date]]) -> list[tuple[date, date]]:
    cursor = first
    missing: list[tuple[date, date]] = []
    for start, end in merge_intervals(covered):
        if end < cursor or start > last:
            continue
        if start > cursor:
            missing.append((cursor, min(last, start - DAY)))
        cursor = max(cursor, end + DAY)
        if cursor > last:
            break
    if cursor <= last:
        missing.append((cursor, last))
    return missing


def read_coverage(path: Path) -> list[tuple[date, date]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("expected a list of date intervals")
        intervals = [(parse_day(start), parse_day(end)) for start, end in raw]
        if any(start is None or end is None or start > end for start, end in intervals):
            raise ValueError("invalid date interval")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid PCF coverage file {path}: {exc}") from exc
    return merge_intervals(intervals)


def write_coverage(path: Path, intervals: list[tuple[date, date]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps([[ymd(a), ymd(b)] for a, b in intervals]), encoding="utf-8")
    temporary.replace(path)


def save_basket(client: TushareClient, code: str, exchange: str, first: date, last: date,
                output: Path, refresh: bool = False) -> int:
    method = "etf_sh_cons" if exchange == "SH" else "etf_sz_cons"
    directory = output / "pcf" / exchange / code
    total = 0
    for month_first, month_last in month_ranges(first, last):
        stem = ymd(month_first)[:6]
        destination = directory / f"{stem}.csv"
        checkpoint = directory / f"{stem}.coverage.json"
        covered = read_coverage(checkpoint) if destination.exists() else []
        segments = [(month_first, month_last)] if refresh else missing_intervals(month_first, month_last, covered)
        # Current PCFs may be published or revised after an earlier run.
        if month_last >= date.today() - timedelta(days=2):
            segments.append((max(month_first, month_last - timedelta(days=2)), month_last))
        segments = merge_intervals(segments)
        if not segments:
            continue
        new = combine((fetch_capped(client, method, code, a, b) for a, b in segments), PCF_FIELDS[exchange])
        old = pd.DataFrame()
        if destination.exists():
            old = pd.read_csv(destination, dtype={"ts_code": str, "trade_date": str, "con_code": str})
        if refresh and not new.empty and not old.empty:
            old = old[~old["trade_date"].between(ymd(month_first), ymd(month_last))]
        combined = combine((old, new), PCF_FIELDS[exchange])
        if not combined.empty:
            combined = combined.drop_duplicates(["trade_date", "ts_code", "con_code"], keep="last")
            combined = combined.sort_values(["trade_date", "con_code"]).reset_index(drop=True)
        atomic_csv(combined, destination)
        write_coverage(checkpoint, merge_intervals([*covered, *segments]))
        total += len(new)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="Every currently listed ETF in the basic catalog")
    target.add_argument("--codes", nargs="+", metavar="ETF", help="Listed ETF codes, e.g. 510300.SH 159915.SZ")
    parser.add_argument("--catalog", type=Path, default=Path("data/etf_basic.csv"))
    parser.add_argument("--refresh-catalog", action="store_true", help="Re-download ETF basic information first")
    parser.add_argument("--datasets", nargs="+", choices=("daily", "adj", "basket"),
                        default=("daily", "adj", "basket"))
    parser.add_argument("--start-date", help="Earliest quote/factor/PCF date (YYYYMMDD); default: listing date for quotes/factors, last 30 days for PCF")
    parser.add_argument("--end-date", help="Last requested date (YYYYMMDD); default: today")
    basket_scope = parser.add_mutually_exclusive_group()
    basket_scope.add_argument("--basket-start", help="Override the PCF start date (YYYYMMDD); otherwise use --start-date or last 30 calendar days")
    basket_scope.add_argument("--full-basket-history", action="store_true", help="Request PCFs from each ETF's listing date")
    parser.add_argument("--output", type=Path, default=Path("data/etf_market"))
    parser.add_argument("--refresh", action="store_true", help="Re-download selected intervals and replace local files")
    parser.add_argument("--interval", type=float, default=0.25, help="Pause between API requests in seconds")
    parser.add_argument("--plan", action="store_true", help="Show selection without calling Tushare")
    args = parser.parse_args(argv)
    if args.interval < 0:
        parser.error("--interval must be non-negative")
    try:
        last = parse_day(args.end_date) if args.end_date else date.today()
        start_override = parse_day(args.start_date) if args.start_date else None
        basket_override = parse_day(args.basket_start) if args.basket_start else None
        if args.refresh_catalog:
            if args.plan:
                parser.error("--plan cannot be combined with --refresh-catalog")
            refreshed = fetch_etf_basic()
            atomic_csv(refreshed, args.catalog)
        listed = load_listed(args.catalog, args.codes)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    if last > date.today():
        parser.error("--end-date cannot be in the future")
    if not listed.empty and start_override and start_override > last:
        parser.error("--start-date must be on or before --end-date")
    if basket_override and basket_override > last:
        parser.error("--basket-start must be on or before --end-date")
    basket_start = "listing date" if args.full_basket_history else ymd(basket_override or start_override or last - timedelta(days=30))
    print(f"Listed ETFs: {len(listed)}; datasets: {', '.join(args.datasets)}; end: {ymd(last)}; basket from: {basket_start}")
    if args.plan:
        return 0

    try:
        client = TushareClient(tushare_pro_from_env(), interval=args.interval)
    except RuntimeError as exc:
        parser.error(str(exc))
    errors: list[dict[str, str]] = []
    stop = False
    for row in listed.itertuples(index=False):
        code = row.ts_code
        listed_on = parse_day(row.list_date) or parse_day(getattr(row, "setup_date", ""))
        series_start = max(listed_on, start_override) if start_override else listed_on
        pcf_start = max(listed_on, basket_override or start_override or last - timedelta(days=30))
        if args.full_basket_history:
            pcf_start = listed_on
        for dataset in args.datasets:
            try:
                if dataset == "daily":
                    count = save_series(client, "fund_daily", code, series_start, last, args.output, args.refresh)
                elif dataset == "adj":
                    count = save_series(client, "fund_adj", code, series_start, last, args.output, args.refresh)
                else:
                    count = save_basket(client, code, row.exchange, pcf_start, last, args.output, args.refresh)
                print(f"{code} {dataset}: +{count} rows", flush=True)
            except Exception as exc:
                errors.append({"ts_code": code, "dataset": dataset, "error": str(exc)})
                print(f"{code} {dataset}: {exc}", file=sys.stderr, flush=True)
                if PERMISSION_ERROR.search(str(exc)):
                    stop = True
                    break
        if stop:
            break
    atomic_csv(pd.DataFrame(errors, columns=("ts_code", "dataset", "error")), args.output / "errors.csv")
    print(f"Finished: {len(listed)} selected ETFs, {len(errors)} errors; output: {args.output}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
