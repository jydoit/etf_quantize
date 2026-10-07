"""Tushare ETF basic-info interface (etf_basic).

`fetch_etf_basic()` mirrors Tushare's filters and returns every available page.
The default output fields match the ETF basic-info page in Tushare.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd


ETF_BASIC_FIELDS = (
    "ts_code", "csname", "extname", "cname", "index_code", "index_name",
    "setup_date", "list_date", "list_status", "exchange", "mgr_name",
    "custod_name", "mgt_fee", "etf_type",
)
MAX_PAGE_SIZE = 5000


def tushare_pro_from_env() -> Any:
    """Create a Pro client from the environment or Tushare's saved token."""
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    import tushare as ts

    if token:
        return ts.pro_api(token)
    try:
        return ts.pro_api()
    except Exception as exc:
        raise RuntimeError("Set TUSHARE_TOKEN or configure the SDK with tushare.set_token()") from exc


def fetch_etf_basic(
    *,
    ts_code: str | None = None,
    index_code: str | None = None,
    list_date: str | None = None,
    list_status: str | None = None,
    exchange: str | None = None,
    mgr: str | None = None,
    limit: int = MAX_PAGE_SIZE,
    offset: int = 0,
    fields: Sequence[str] | str | None = None,
    fetch_page: Callable[..., pd.DataFrame] | None = None,
    interval: float = 0.25,
) -> pd.DataFrame:
    """Fetch all pages, starting at `offset` for each status.

    Pass `fetch_page=pro.etf_basic` to reuse a configured Tushare client.
    When omitted, TUSHARE_TOKEN is read from the environment. If no status is
    specified, L, D, and P are queried explicitly so no status is omitted.
    """
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if offset < 0 or interval < 0:
        raise ValueError("offset and interval must be non-negative")
    if list_status not in (None, "L", "D", "P"):
        raise ValueError("list_status must be L, D, or P")
    if exchange not in (None, "SH", "SZ"):
        raise ValueError("exchange must be SH or SZ")
    selected_fields = tuple(field.strip() for field in fields.split(",")) if isinstance(fields, str) else tuple(fields or ETF_BASIC_FIELDS)
    if not selected_fields or any(field not in ETF_BASIC_FIELDS for field in selected_fields):
        raise ValueError("fields must be names from ETF_BASIC_FIELDS")
    if "ts_code" not in selected_fields:
        selected_fields = ("ts_code", *selected_fields)

    request = fetch_page or tushare_pro_from_env().etf_basic
    filters = {
        name: item for name, item in {
            "ts_code": ts_code, "index_code": index_code, "list_date": list_date,
            "exchange": exchange, "mgr": mgr,
        }.items() if item is not None
    }
    chunks: list[pd.DataFrame] = []
    for status in ((list_status,) if list_status else ("L", "D", "P")):
        page_offset = offset
        last_codes: tuple[str, ...] | None = None
        while True:
            page = request(**filters, list_status=status, limit=limit, offset=page_offset, fields=",".join(selected_fields))
            if not isinstance(page, pd.DataFrame):
                raise TypeError(f"etf_basic returned {type(page).__name__}, expected pandas.DataFrame")
            if page.empty:
                break
            if "ts_code" not in page:
                raise RuntimeError("etf_basic response is missing ts_code")
            codes = tuple(page["ts_code"].astype(str))
            if codes == last_codes:
                raise RuntimeError("etf_basic returned the same page twice; offset may have been ignored")
            last_codes = codes
            chunks.append(page)
            if len(page) < limit:
                break
            page_offset += len(page)
            if interval:
                time.sleep(interval)

    if not chunks:
        return pd.DataFrame(columns=selected_fields)
    # Rebuild the small basic-info catalog row-wise: pandas 3 warns when
    # concatenating status pages whose optional columns are entirely empty.
    records = [row for page in chunks for row in page.to_dict("records")]
    result = pd.DataFrame.from_records(records, columns=selected_fields)
    return result.drop_duplicates("ts_code", keep="last").reset_index(drop=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download all matching ETF basic records from Tushare")
    parser.add_argument("--ts-code", help="ETF code, e.g. 510300.SH")
    parser.add_argument("--index-code", help="Tracked index code")
    parser.add_argument("--list-date", help="Listing date in YYYYMMDD format")
    parser.add_argument("--list-status", choices=("L", "D", "P"), help="L listed, D delisted, P pending")
    parser.add_argument("--exchange", choices=("SH", "SZ"))
    parser.add_argument("--mgr", help="Fund manager short name")
    parser.add_argument("--limit", type=int, default=MAX_PAGE_SIZE, help="Rows per API request (max 5000)")
    parser.add_argument("--offset", type=int, default=0, help="Starting row offset")
    parser.add_argument("--output", type=Path, default=Path("data/etf_basic.csv"))
    args = parser.parse_args(argv)
    try:
        result = fetch_etf_basic(
            ts_code=args.ts_code, index_code=args.index_code, list_date=args.list_date,
            list_status=args.list_status, exchange=args.exchange, mgr=args.mgr,
            limit=args.limit, offset=args.offset,
        )
    except (RuntimeError, ValueError, TypeError) as exc:
        parser.exit(1, f"ETF basic download failed: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"Saved {len(result)} ETF records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
