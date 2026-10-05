import unittest

import pandas as pd

from tushare_etf_api import ETF_BASIC_FIELDS, fetch_etf_basic


class TushareETFAPITests(unittest.TestCase):
    def test_default_fetches_all_listing_statuses(self):
        calls = []

        def fake_page(**kwargs):
            calls.append((kwargs["list_status"], kwargs["offset"]))
            return pd.DataFrame([{"ts_code": f"{kwargs['list_status']}.SH"}])

        result = fetch_etf_basic(limit=2, interval=0, fetch_page=fake_page)
        self.assertEqual(calls, [("L", 0), ("D", 0), ("P", 0)])
        self.assertEqual(result["ts_code"].tolist(), ["L.SH", "D.SH", "P.SH"])

    def test_filters_fields_and_all_pages(self):
        calls = []

        def fake_page(**kwargs):
            calls.append(kwargs)
            rows = {
                0: [{"ts_code": "510300.SH"}, {"ts_code": "518880.SH"}],
                2: [{"ts_code": "510050.SH"}],
            }
            return pd.DataFrame(rows.get(kwargs["offset"], []))

        result = fetch_etf_basic(
            list_status="L", exchange="SH", limit=2, interval=0,
            fetch_page=fake_page,
        )
        self.assertEqual(result["ts_code"].tolist(), ["510300.SH", "518880.SH", "510050.SH"])
        self.assertEqual([call["offset"] for call in calls], [0, 2])
        self.assertEqual(calls[0]["fields"], ",".join(ETF_BASIC_FIELDS))
        self.assertEqual(calls[0]["list_status"], "L")
        self.assertEqual(calls[0]["exchange"], "SH")

    def test_repeated_page_is_reported(self):
        def fake_page(**kwargs):
            return pd.DataFrame([{"ts_code": "510300.SH"}])

        with self.assertRaisesRegex(RuntimeError, "same page"):
            fetch_etf_basic(limit=1, interval=0, fetch_page=fake_page)

    def test_custom_fields_keep_ts_code_for_downstream_joins(self):
        calls = []

        def fake_page(**kwargs):
            calls.append(kwargs)
            return pd.DataFrame([{"ts_code": "510300.SH", "csname": "沪深300ETF"}])

        result = fetch_etf_basic(fields="csname", fetch_page=fake_page, interval=0)
        self.assertEqual(calls[0]["fields"], "ts_code,csname")
        self.assertEqual(result.iloc[0]["ts_code"], "510300.SH")


if __name__ == "__main__":
    unittest.main()
