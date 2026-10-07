"""Aggregate ETF PCF constituents, then download stock basics and daily prices.

Input: data/etf_market/pcf/{SH,SZ}/{ETF_CODE}/{YYYYMM}.csv, produced by
etf_market_ingest.py. The local PCF scan is incremental. Securities are
deduplicated across ETFs before any Tushare stock requests are made.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from etf_ingest import PERMISSION_ERROR, TushareClient, atomic_csv, parse_day
from etf_market_ingest import combine, ymd
from tushare_etf_api import tushare_pro_from_env


BASIC_API = {"CN": "stock_basic", "HK": "hk_basic", "US": "us_basic"}
DAILY_API = {"CN": "daily", "HK": "hk_daily", "US": "us_daily"}
DAILY_LIMIT = {"CN": 6000, "HK": 5000, "US": 6000}
CN_BASIC_FIELDS = (
    "ts_code,symbol,name,area,industry,fullname,enname,market,exchange,"
    "curr_type,list_status,list_date,delist_date,is_hs"
)
US_BASIC_FIELDS = "ts_code,name,enname,classify,list_date,delist_date"
MEMBERSHIP_COLUMNS = (
    "source_file", "etf_code", "trade_date", "con_code", "con_name", "qty",
    "sub_flag", "exchange", "market", "quote_code", "cpr", "rdr",
    "sca", "sub_cc", "red_cc",
)
CONSTITUENT_COLUMNS = (
    "market", "quote_code", "first_seen", "last_seen", "etf_count", "pcf_rows",
)
DAY = timedelta(days=1)


def classify_pcf_code(raw_code: str, raw_name: str, raw_exchange: str) -> tuple[str, str]:
    """Map PCF identifiers to Tushare stock identifiers; keep uncertain rows."""
    code = raw_code.strip().upper()
    name = raw_name.strip()
    exchange = raw_exchange.strip().upper()
    if "现金" in name or "保证金" in name or code == "159900.SZ":
        return "CASH", ""
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
        return "CN", code
    if re.fullmatch(r"\d{1,5}\.HK", code):
        return "HK", f"{int(code[:-3]):05d}.HK"
    if exchange == "HK" and re.fullmatch(r"\d{1,5}", code):
        return "HK", f"{int(code):05d}.HK"
    if code.endswith(".US"):
        code = code[:-3]
        exchange = "US"
    elif exchange in {"US", "NYSE", "NASDAQ", "AMEX"}:
        code = re.sub(r"\.(NYSE|NASDAQ|AMEX|N|O)$", "", code)
    elif exchange != "OTH":
        return "UNKNOWN", ""
    if exchange in {"US", "NYSE", "NASDAQ", "AMEX", "OTH"} and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", code):
        return "US", code
    return "UNKNOWN", ""


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS pcf_files (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS membership (
            source_file TEXT NOT NULL, etf_code TEXT NOT NULL,
            trade_date TEXT NOT NULL, con_code TEXT NOT NULL,
            con_name TEXT, qty TEXT, sub_flag TEXT, exchange TEXT,
            market TEXT NOT NULL, quote_code TEXT,
            cpr TEXT, rdr TEXT, sca TEXT, sub_cc TEXT, red_cc TEXT,
            PRIMARY KEY (source_file, etf_code, trade_date, con_code)
        );
        CREATE INDEX IF NOT EXISTS membership_stock ON membership(market, quote_code);
        CREATE INDEX IF NOT EXISTS membership_etf_date ON membership(etf_code, trade_date);
    """)


def pcf_paths(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"PCF directory not found: {root}; run etf_market_ingest.py first")
    paths = sorted(path for exchange in ("SH", "SZ")
                   for path in (root / exchange).glob("*/*.csv") if path.is_file())
    if not paths:
        raise FileNotFoundError(f"No ETF PCF CSV files found below {root}")
    return paths


def import_pcf_file(db: sqlite3.Connection, path: Path, root: Path) -> int:
    relative = path.relative_to(root).as_posix()
    stat = path.stat()
    stored = db.execute("SELECT size, mtime_ns FROM pcf_files WHERE path=?", (relative,)).fetchone()
    if stored == (stat.st_size, stat.st_mtime_ns):
        return 0
    expected_etf = path.parent.name
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle, db:
        reader = csv.DictReader(handle)
        required = {"trade_date", "ts_code", "con_code", "con_name", "exchange"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"PCF CSV missing columns {sorted(required)}: {path}")
        db.execute("DELETE FROM membership WHERE source_file=?", (relative,))
        batch = []
        for line_number, row in enumerate(reader, start=2):
            etf_code = (row.get("ts_code") or "").strip().upper()
            trade_date = (row.get("trade_date") or "").strip()
            con_code = (row.get("con_code") or "").strip().upper()
            if etf_code != expected_etf or not re.fullmatch(r"\d{8}", trade_date) or not con_code:
                raise ValueError(f"Invalid ETF/date/constituent at {path}:{line_number}")
            try:
                parse_day(trade_date)
            except ValueError as exc:
                raise ValueError(f"Invalid trade_date at {path}:{line_number}: {trade_date}") from exc
            market, quote_code = classify_pcf_code(
                con_code, row.get("con_name") or "", row.get("exchange") or ""
            )
            values = {
                "source_file": relative, "etf_code": etf_code, "trade_date": trade_date,
                "con_code": con_code, "con_name": row.get("con_name") or "",
                "qty": row.get("qty") or "", "sub_flag": row.get("sub_flag") or "",
                "exchange": row.get("exchange") or "", "market": market,
                "quote_code": quote_code, "cpr": row.get("cpr") or "",
                "rdr": row.get("rdr") or "", "sca": row.get("sca") or "",
                "sub_cc": row.get("sub_cc") or "", "red_cc": row.get("red_cc") or "",
            }
            batch.append(tuple(values[name] for name in MEMBERSHIP_COLUMNS))
            if len(batch) >= 2000:
                db.executemany(
                    f"INSERT OR REPLACE INTO membership VALUES ({','.join('?' for _ in MEMBERSHIP_COLUMNS)})", batch
                )
                count += len(batch)
                batch.clear()
        if batch:
            db.executemany(
                f"INSERT OR REPLACE INTO membership VALUES ({','.join('?' for _ in MEMBERSHIP_COLUMNS)})", batch
            )
            count += len(batch)
        db.execute(
            "INSERT OR REPLACE INTO pcf_files(path,size,mtime_ns) VALUES (?,?,?)",
            (relative, stat.st_size, stat.st_mtime_ns),
        )
    return count


def sync_pcf(db: sqlite3.Connection, root: Path) -> tuple[int, int]:
    paths = pcf_paths(root)
    observed = {path.relative_to(root).as_posix() for path in paths}
    imported = 0
    for index, path in enumerate(paths, start=1):
        imported += import_pcf_file(db, path, root)
        if index % 1000 == 0:
            print(f"PCF files scanned: {index}/{len(paths)}; imported rows: {imported}", flush=True)
    previous = {row[0] for row in db.execute("SELECT path FROM pcf_files")}
    with db:
        for removed in previous - observed:
            db.execute("DELETE FROM membership WHERE source_file=?", (removed,))
            db.execute("DELETE FROM pcf_files WHERE path=?", (removed,))
    return len(paths), imported


def atomic_rows(path: Path, header: Iterable[str], rows: Iterable[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", prefix=".tmp-", suffix=".csv", dir=path.parent,
        encoding="utf-8-sig", newline="", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_universe(db: sqlite3.Connection, output: Path,
                   first: date | None = None, last: date | None = None) -> list[tuple[str, str, str, str, int, int]]:
    bounds = (ymd(first) if first else "00000000", ymd(last) if last else "99999999")
    rows = list(db.execute("""
        SELECT market, quote_code, MIN(trade_date), MAX(trade_date),
               COUNT(DISTINCT etf_code), COUNT(*)
        FROM membership
        WHERE market IN ('CN','HK','US') AND trade_date BETWEEN ? AND ?
        GROUP BY market, quote_code
        ORDER BY market, quote_code
    """, bounds))
    atomic_rows(output / "constituents.csv", CONSTITUENT_COLUMNS, rows)
    atomic_rows(output / "non_stock_pcf_codes.csv",
                ("market", "con_code", "con_name", "pcf_rows"), db.execute("""
                    SELECT market, con_code, MAX(con_name), COUNT(*)
                    FROM membership
                    WHERE market NOT IN ('CN','HK','US') AND trade_date BETWEEN ? AND ?
                    GROUP BY market, con_code ORDER BY market, con_code
                """, bounds))
    return rows


def safe_name(code: str) -> str:
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", code) or ".." in code:
        raise ValueError(f"Unsafe stock code for file name: {code!r}")
    return code


def fetch_basic(client: TushareClient, market: str, code: str) -> pd.DataFrame:
    method = BASIC_API[market]
    if market == "US":
        frame = client.call(method, ts_code=code, fields=US_BASIC_FIELDS)
    else:
        frame = pd.DataFrame()
        for status in ("L", "D", "P"):
            kwargs = {"ts_code": code, "list_status": status}
            if market == "CN":
                kwargs["fields"] = CN_BASIC_FIELDS
            frame = client.call(method, **kwargs)
            if not frame.empty:
                break
    if frame.empty:
        return pd.DataFrame(columns=("ts_code", "source_market"))
    if "ts_code" not in frame:
        raise RuntimeError(f"{method} returned no ts_code for {code}")
    frame = frame[frame["ts_code"].astype(str).str.upper() == code].copy()
    frame["source_market"] = market
    return frame.reset_index(drop=True)


def load_or_fetch_basic(client: TushareClient, market: str, code: str,
                        output: Path, refresh: bool) -> pd.DataFrame:
    path = output / "basic" / market / f"{safe_name(code)}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    frame = fetch_basic(client, market, code)
    atomic_csv(frame, path)
    return frame


def validate_daily(frame: pd.DataFrame, method: str, code: str, first: date, last: date) -> pd.DataFrame:
    if frame.empty:
        return frame
    if not {"ts_code", "trade_date"}.issubset(frame.columns):
        raise RuntimeError(f"{method} returned no ts_code/trade_date for {code}")
    frame = frame.copy()
    frame["ts_code"] = frame["ts_code"].astype(str).str.upper()
    frame["trade_date"] = frame["trade_date"].astype(str)
    if (frame["ts_code"] != code).any() or not frame["trade_date"].between(ymd(first), ymd(last)).all():
        raise RuntimeError(f"{method} returned rows outside {code} {ymd(first)}-{ymd(last)}")
    return frame


def fetch_daily(client: TushareClient, market: str, code: str, first: date, last: date) -> pd.DataFrame:
    method = DAILY_API[market]
    limit = DAILY_LIMIT[market]
    frame = validate_daily(client.call(
        method, ts_code=code, start_date=ymd(first), end_date=ymd(last),
    ), method, code, first, last)
    if len(frame) < limit:
        return frame
    if first == last:
        raise RuntimeError(f"{method} hit its {limit}-row limit on one day for {code}")
    middle = first + (last - first) // 2
    return combine((fetch_daily(client, market, code, first, middle),
                    fetch_daily(client, market, code, middle + DAY, last)))


def save_daily(client: TushareClient, market: str, code: str, first: date, last: date,
               output: Path, refresh: bool) -> int:
    if first > last:
        return 0
    path = output / "daily" / market / f"{safe_name(code)}.csv"
    old = pd.read_csv(path, dtype={"ts_code": str, "trade_date": str}) if path.exists() else pd.DataFrame()
    intervals = [(first, last)]
    if not refresh and not old.empty:
        earliest = parse_day(old["trade_date"].min())
        latest = parse_day(old["trade_date"].max())
        intervals = []
        if first < earliest:
            intervals.append((first, earliest - DAY))
        if last > latest:
            intervals.append((latest + DAY, last))
    new = combine(fetch_daily(client, market, code, a, b) for a, b in intervals)
    if new.empty:
        return 0
    if refresh and not old.empty:
        old = old[~old["trade_date"].between(ymd(first), ymd(last))]
    merged = combine((old, new)).drop_duplicates(("ts_code", "trade_date"), keep="last")
    atomic_csv(merged.sort_values("trade_date").reset_index(drop=True), path)
    return len(new)


def oldest_listing(basic: pd.DataFrame) -> date | None:
    if "list_date" not in basic:
        return None
    dates = [parse_day(raw) for raw in basic["list_date"] if pd.notna(raw) and str(raw).strip()]
    return min(dates) if dates else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pcf-root", type=Path, default=Path("data/etf_market/pcf"))
    parser.add_argument("--output", type=Path, default=Path("data/constituents"))
    parser.add_argument("--aggregate-only", action="store_true", help="Build the stock list without Tushare calls")
    parser.add_argument("--markets", nargs="+", choices=("CN", "HK", "US"), default=("CN", "HK", "US"))
    parser.add_argument("--start-date", help="Override the first daily-price date (YYYYMMDD)")
    parser.add_argument("--basket-start", help="First PCF date included in the stock universe (YYYYMMDD)")
    parser.add_argument("--basket-end", help="Last PCF date included in the stock universe (YYYYMMDD)")
    parser.add_argument("--from-listing", action="store_true", help="Start each stock at its listing date")
    parser.add_argument("--end-date", help="Last daily-price date (YYYYMMDD); default: today")
    parser.add_argument("--refresh-basic", action="store_true")
    parser.add_argument("--refresh-daily", action="store_true")
    parser.add_argument("--max-stocks", type=int, help="Limit the number of unique stocks for a trial run")
    parser.add_argument("--interval", type=float, default=1.3, help="Seconds between API calls")
    args = parser.parse_args(argv)
    if args.start_date and args.from_listing:
        parser.error("--start-date and --from-listing are mutually exclusive")
    if args.interval < 0 or (args.max_stocks is not None and args.max_stocks < 1):
        parser.error("--interval must be non-negative and --max-stocks must be positive")
    try:
        start_override = parse_day(args.start_date) if args.start_date else None
        last = parse_day(args.end_date) if args.end_date else date.today()
        basket_first = parse_day(args.basket_start) if args.basket_start else None
        basket_last = parse_day(args.basket_end) if args.basket_end else None
        if last > date.today() or (start_override and start_override > last):
            parser.error("Date range must end today or earlier and start no later than end")
        if basket_first and basket_last and basket_first > basket_last:
            parser.error("--basket-start must be on or before --basket-end")
        args.output.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(args.output / "pcf_membership.sqlite") as db:
            create_schema(db)
            files, imported = sync_pcf(db, args.pcf_root)
            universe = write_universe(db, args.output, basket_first, basket_last)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    selected = [row for row in universe if row[0] in args.markets]
    if args.max_stocks is not None:
        selected = selected[:args.max_stocks]
    print(f"PCF files: {files}; newly imported rows: {imported}; unique stock candidates: {len(universe)}; selected: {len(selected)}")
    if args.aggregate_only:
        return 0
    try:
        client = TushareClient(tushare_pro_from_env(), interval=args.interval)
    except RuntimeError as exc:
        parser.error(str(exc))
    errors: list[dict[str, str]] = []
    issues: list[dict[str, str]] = []
    basic_frames: list[pd.DataFrame] = []
    blocked: set[str] = set()
    for market, code, first_seen, _, _, _ in selected:
        basic_method = BASIC_API[market]
        daily_method = DAILY_API[market]
        if basic_method in blocked:
            continue
        phase = basic_method
        try:
            basic = load_or_fetch_basic(client, market, code, args.output, args.refresh_basic)
            if basic.empty:
                issues.append({"market": market, "ts_code": code, "reason": "No matching stock basic record; daily skipped"})
                continue
            basic_frames.append(basic)
            if market == "US" and "list_date" in basic and basic["list_date"].nunique() > 1:
                issues.append({"market": market, "ts_code": code, "reason": "US ticker has multiple listing dates; daily skipped"})
                continue
            if daily_method in blocked:
                continue
            first = start_override or parse_day(first_seen)
            if args.from_listing:
                first = min(oldest_listing(basic) or first, first)
            phase = daily_method
            count = save_daily(client, market, code, first, last, args.output, args.refresh_daily)
            print(f"{market} {code}: +{count} daily rows", flush=True)
        except Exception as exc:
            errors.append({"market": market, "ts_code": code, "api": phase, "error": str(exc)})
            print(f"{market} {code}: {exc}", file=sys.stderr, flush=True)
            if PERMISSION_ERROR.search(str(exc)):
                blocked.add(phase)
    atomic_csv(combine(basic_frames, columns=("ts_code", "source_market")), args.output / "basic_catalog.csv")
    atomic_csv(pd.DataFrame(issues, columns=("market", "ts_code", "reason")), args.output / "issues.csv")
    atomic_csv(pd.DataFrame(errors, columns=("market", "ts_code", "api", "error")), args.output / "errors.csv")
    print(f"Finished: {len(selected)} stock candidates, {len(issues)} issues, {len(errors)} errors; output: {args.output}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
