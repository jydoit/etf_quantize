"""Download the longest available daily history for Chinese ETFs from Tushare."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from tushare_etf_api import fetch_etf_basic, tushare_pro_from_env


ROW_LIMIT = 5000  # Tushare fund_daily's documented per-request limit.
WINDOW_DAYS = 3650  # A single ETF has far fewer than 5000 trading days in this span.
CODE_PATTERN = re.compile(r"^\d{6}\.(SH|SZ)$")
COMMODITY_NAME = re.compile(
    r"黄金\s*ETF|上海金|黄金现货|白银\s*ETF|白银现货|豆粕\s*ETF|"
    r"原油\s*ETF|商品期货|有色金属期货|能源化工期货",
    re.IGNORECASE,
)
EQUITY_NAME = re.compile(r"股票\s*ETF|股票指数|黄金股|油气股")
PERMISSION_ERROR = re.compile(r"权限|积分|token|授权|permission|unauthorized", re.IGNORECASE)


def value(raw: Any) -> str:
    if raw is None or pd.isna(raw):
        return ""
    return str(raw).strip()


def parse_day(raw: Any) -> date | None:
    text = value(raw)
    if not text:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Invalid date returned by Tushare: {text}")


def classify_etf(row: pd.Series) -> tuple[str, str]:
    """Use the fund investment type first; fall back only to unambiguous names."""
    fund_type = value(row.get("fund_type"))
    detail_type = value(row.get("type"))
    declared_type = " ".join((fund_type, detail_type))
    if re.search(r"商品|贵金属|黄金|期货", declared_type):
        return "commodity", "fund_basic.fund_type/type"
    if re.search(r"股票|权益", declared_type):
        return "stock", "fund_basic.fund_type/type"
    if re.search(r"债券|货币|混合|REIT", declared_type, re.IGNORECASE):
        return "other", "fund_basic.fund_type/type"

    names = " ".join(
        value(row.get(field))
        for field in ("csname", "extname", "cname", "name", "index_name", "benchmark")
    )
    if EQUITY_NAME.search(names):
        return "stock", "ETF name/benchmark"
    if COMMODITY_NAME.search(names):
        return "commodity", "ETF name/benchmark"
    return "unknown", "needs manual review"


class TushareClient:
    def __init__(self, pro: Any, interval: float = 0.25) -> None:
        self.pro = pro
        self.interval = interval

    def call(self, method: str, **kwargs: Any) -> pd.DataFrame:
        for attempt in range(3):
            try:
                result = getattr(self.pro, method)(**kwargs)
                if not isinstance(result, pd.DataFrame):
                    raise TypeError(f"{method} returned {type(result).__name__}, expected DataFrame")
                if self.interval:
                    time.sleep(self.interval)
                return result
            except Exception as exc:
                if PERMISSION_ERROR.search(str(exc)) or attempt == 2:
                    raise RuntimeError(f"Tushare {method} failed: {exc}") from exc
                time.sleep(2**attempt)
        raise AssertionError("unreachable")


def load_catalog(client: TushareClient) -> pd.DataFrame:
    etfs = pd.concat(
        [
            fetch_etf_basic(
                list_status=status,
                fetch_page=lambda **params: client.call("etf_basic", **params),
                interval=0,
            )
            for status in ("L", "D")
        ],
        ignore_index=True,
    )
    if etfs.empty:
        raise RuntimeError("Tushare etf_basic returned no listed or delisted ETFs")
    if "ts_code" not in etfs:
        raise RuntimeError("Tushare etf_basic response has no ts_code column")
    etfs = etfs.drop_duplicates("ts_code", keep="first")

    funds = pd.concat(
        [client.call("fund_basic", market="E", status=status) for status in ("L", "D")],
        ignore_index=True,
    )
    fund_columns = [
        "ts_code", "name", "fund_type", "type", "invest_type", "benchmark",
        "found_date", "delist_date",
    ]
    if funds.empty:
        funds = pd.DataFrame(columns=fund_columns)
    else:
        funds = funds[[column for column in fund_columns if column in funds]]
        funds = funds.drop_duplicates("ts_code", keep="first")

    catalog = etfs.merge(funds, on="ts_code", how="left")
    labels = catalog.apply(classify_etf, axis=1)
    catalog["etf_category"] = labels.map(lambda item: item[0])
    catalog["classification_basis"] = labels.map(lambda item: item[1])
    return catalog.sort_values("ts_code").reset_index(drop=True)


def resolve_codes(catalog: pd.DataFrame, requested: list[str] | None) -> pd.DataFrame:
    if not requested:
        return catalog
    codes = set()
    for raw in requested:
        code = raw.strip().upper()
        matches = catalog[catalog["ts_code"].astype(str).str.upper() == code]
        if matches.empty and re.fullmatch(r"\d{6}", code):
            matches = catalog[catalog["ts_code"].astype(str).str.startswith(f"{code}.")]
        if len(matches) != 1:
            raise ValueError(f"ETF code {raw!r} was not found uniquely in etf_basic")
        codes.add(matches.iloc[0]["ts_code"])
    return catalog[catalog["ts_code"].isin(codes)].copy()


def windows(first: date, last: date):
    current = first
    while current <= last:
        end = min(current + timedelta(days=WINDOW_DAYS - 1), last)
        yield current, end
        current = end + timedelta(days=1)


def fetch_window(client: TushareClient, code: str, first: date, last: date) -> pd.DataFrame:
    data = client.call(
        "fund_daily", ts_code=code,
        start_date=first.strftime("%Y%m%d"), end_date=last.strftime("%Y%m%d"),
    )
    if len(data) < ROW_LIMIT:
        return data
    if first == last:
        raise RuntimeError(f"fund_daily reached {ROW_LIMIT} rows on one day for {code}")
    midpoint = first + (last - first) // 2
    return pd.concat(
        [fetch_window(client, code, first, midpoint),
         fetch_window(client, code, midpoint + timedelta(days=1), last)],
        ignore_index=True,
    )


def fetch_history(client: TushareClient, code: str, first: date, last: date) -> pd.DataFrame:
    parts = [fetch_window(client, code, start, end) for start, end in windows(first, last)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def atomic_csv(frame: pd.DataFrame, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", prefix=".tmp-", dir=destination.parent,
        encoding="utf-8-sig", newline="", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        frame.to_csv(handle, index=False)
    try:
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def save_daily(client: TushareClient, row: pd.Series, output: Path, refresh: bool) -> int:
    code = value(row["ts_code"])
    if not CODE_PATTERN.fullmatch(code):
        raise ValueError(f"Unexpected ETF code: {code!r}")
    destination = output / "daily" / f"{code}.csv"
    existing = pd.DataFrame()
    if destination.exists() and not refresh:
        existing = pd.read_csv(destination, dtype={"ts_code": str, "trade_date": str})

    first = parse_day(row.get("list_date")) or parse_day(row.get("found_date")) or date(1990, 1, 1)
    last = min(parse_day(row.get("delist_date")) or date.today(), date.today())
    if not existing.empty:
        latest = parse_day(existing["trade_date"].max())
        if latest:
            first = max(first, latest + timedelta(days=1))
    if first > last:
        return 0

    new = fetch_history(client, code, first, last)
    if new.empty:
        return 0
    if "trade_date" not in new or "ts_code" not in new:
        raise RuntimeError(f"fund_daily returned incomplete columns for {code}")
    new["trade_date"] = new["trade_date"].astype(str)
    new = new[(new["ts_code"] == code) &
              (new["trade_date"] >= first.strftime("%Y%m%d")) &
              (new["trade_date"] <= last.strftime("%Y%m%d"))]
    new["etf_category"] = row["etf_category"]
    combined = pd.concat([existing, new], ignore_index=True)
    combined["etf_category"] = row["etf_category"]
    combined = combined.drop_duplicates(["ts_code", "trade_date"], keep="last")
    combined = combined.sort_values("trade_date").reset_index(drop=True)
    atomic_csv(combined, destination)
    return len(new)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--codes", nargs="+", metavar="ETF", help="ETF codes, e.g. 510300.SH 518880.SH")
    target.add_argument("--all", action="store_true", help="All listed and delisted ETFs")
    parser.add_argument("--category", choices=("both", "stock", "commodity", "all"), default="both")
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--refresh", action="store_true", help="Re-download the full available history")
    parser.add_argument("--interval", type=float, default=0.25, help="Seconds between API requests")
    args = parser.parse_args(argv)
    if args.interval < 0:
        parser.error("--interval must be non-negative")

    try:
        pro = tushare_pro_from_env()
    except RuntimeError as exc:
        parser.error(str(exc))
    client = TushareClient(pro, interval=args.interval)
    catalog = resolve_codes(load_catalog(client), args.codes)
    if args.category != "all":
        allowed = {"stock", "commodity"} if args.category == "both" else {args.category}
        selected = catalog[catalog["etf_category"].isin(allowed)].copy()
    else:
        selected = catalog
    atomic_csv(catalog, args.output / "etf_metadata.csv")
    print(f"Catalog: {len(catalog)} ETFs; selected: {len(selected)}; metadata: {args.output / 'etf_metadata.csv'}")

    errors: list[dict[str, str]] = []
    new_rows = 0
    for row in selected.itertuples(index=False):
        item = pd.Series(row._asdict())
        code = value(item["ts_code"])
        try:
            count = save_daily(client, item, args.output, args.refresh)
            new_rows += count
            print(f"{code} {item['etf_category']}: +{count} daily rows")
        except Exception as exc:
            errors.append({"ts_code": code, "error": str(exc)})
            print(f"{code}: {exc}", file=sys.stderr)
            if PERMISSION_ERROR.search(str(exc)):
                break
    if errors:
        atomic_csv(pd.DataFrame(errors), args.output / "errors.csv")
    print(f"Finished: {new_rows} new rows, {len(errors)} errors; daily files: {args.output / 'daily'}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
