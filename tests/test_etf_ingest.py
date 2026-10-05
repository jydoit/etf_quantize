import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from etf_ingest import TushareClient, classify_etf, fetch_window, save_daily


class DummyPro:
    def __init__(self):
        self.calls = []

    def fund_daily(self, **kwargs):
        self.calls.append(kwargs)
        day = kwargs["end_date"]
        return pd.DataFrame([{"ts_code": kwargs["ts_code"], "trade_date": day, "close": 1.0}])


class EtfIngestTests(unittest.TestCase):
    def test_gold_stock_is_stock_but_physical_gold_is_commodity(self):
        gold_stock = pd.Series({"fund_type": "股票型", "csname": "黄金股ETF"})
        gold_spot = pd.Series({"fund_type": "商品型", "csname": "黄金ETF"})
        unknown = pd.Series({"csname": "某主题ETF"})
        self.assertEqual(classify_etf(gold_stock)[0], "stock")
        self.assertEqual(classify_etf(gold_spot)[0], "commodity")
        self.assertEqual(classify_etf(unknown)[0], "unknown")

    def test_request_at_row_limit_is_split_to_avoid_truncation(self):
        class LimitedPro(DummyPro):
            def fund_daily(self, **kwargs):
                self.calls.append(kwargs)
                start = date.fromisoformat(
                    f"{kwargs['start_date'][:4]}-{kwargs['start_date'][4:6]}-{kwargs['start_date'][6:]}"
                )
                end = date.fromisoformat(
                    f"{kwargs['end_date'][:4]}-{kwargs['end_date'][4:6]}-{kwargs['end_date'][6:]}"
                )
                if (end - start).days > 1:
                    return pd.DataFrame({"placeholder": range(5000)})
                return pd.DataFrame(
                    {"ts_code": [kwargs["ts_code"]], "trade_date": [kwargs["start_date"]]}
                )

        pro = LimitedPro()
        rows = fetch_window(TushareClient(pro, interval=0), "510300.SH", date(2026, 1, 1), date(2026, 1, 4))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(pro.calls), 3)

    def test_full_history_is_saved_and_second_run_is_incremental(self):
        pro = DummyPro()
        client = TushareClient(pro, interval=0)
        today = date.today()
        first = today - timedelta(days=WINDOW_TEST_DAYS)
        row = pd.Series({
            "ts_code": "510300.SH", "list_date": first.strftime("%Y%m%d"),
            "etf_category": "stock",
        })
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            saved = save_daily(client, row, output, refresh=False)
            self.assertGreaterEqual(saved, 2)
            self.assertEqual(pro.calls[0]["start_date"], first.strftime("%Y%m%d"))
            self.assertEqual(pro.calls[-1]["end_date"], today.strftime("%Y%m%d"))
            call_count = len(pro.calls)
            self.assertEqual(save_daily(client, row, output, refresh=False), 0)
            self.assertEqual(len(pro.calls), call_count)
            self.assertTrue((output / "daily" / "510300.SH.csv").exists())


WINDOW_TEST_DAYS = 4000


if __name__ == "__main__":
    unittest.main()
