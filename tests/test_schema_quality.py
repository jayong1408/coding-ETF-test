from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

import pandas as pd

from etf_data_pipeline.quality import assess_quality
from etf_data_pipeline.schema import normalize_master, read_table
from etf_data_pipeline.streaming import assess_quality_streaming


def price_row(date, ticker="360750", **updates):
    row = {
        "date": date, "market": "KR", "ticker": ticker,
        "open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0,
        "adj_close": 102.0, "volume": 1000.0,
    }
    row.update(updates)
    return row


class DateAndQualityTests(unittest.TestCase):
    def test_parquet_round_trip_is_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.parquet"
            expected = pd.DataFrame([price_row("2024-01-02")])
            expected.to_parquet(path, index=False)
            actual = read_table(path)
            self.assertEqual(len(actual), 1)
            self.assertEqual(str(actual.loc[0, "ticker"]), "360750")

    def test_streaming_quality_detects_duplicate_across_batches_and_cleans_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "sample.parquet"
            pd.DataFrame([price_row("2024-01-02"), price_row("2024-01-02")]).to_parquet(path, index=False, row_group_size=1)
            calendar = pd.DataFrame([{"date": "2024-01-02", "market": "KR", "is_trading_day": True,
                                      "calendar_verified": True, "calendar_source": "TEST"}])
            result = assess_quality_streaming(path, calendar=calendar, batch_rows=1, staging_dir=root, sample_data=True)
            self.assertEqual(len(result.duplicate_keys), 2)
            self.assertEqual(result.validation_results["execution_mode"], "STREAMING_DISK_STAGED_BY_INSTRUMENT")
            self.assertFalse(list(root.glob("etf_quality_stage_*.sqlite")))

    def test_kr_midnight_is_not_shifted_to_previous_utc_day(self):
        frame = pd.DataFrame([price_row(pd.Timestamp("2022-12-22 00:00:00", tz="Asia/Seoul"))])
        normalized = normalize_master(frame)
        self.assertEqual(normalized.loc[0, "date"], pd.Timestamp("2022-12-22"))
        self.assertEqual(normalized.loc[0, "date_normalization_reason"], "TZ_AWARE_CONVERTED_TO_EXCHANGE_LOCAL")
        self.assertIn("2022-12-22", normalized.loc[0, "date_raw"])

    def test_360750_suspect_window_uses_verified_sessions_only(self):
        trading_dates = ["2022-12-22", "2022-12-23", "2022-12-26", "2022-12-27", "2022-12-28", "2022-12-29"]
        master = pd.DataFrame([price_row(pd.Timestamp(value, tz="Asia/Seoul")) for value in trading_dates])
        calendar = pd.DataFrame([
            {"date": value, "market": "KR", "is_trading_day": True, "calendar_verified": True,
             "calendar_source": "TEST_FIXTURE_NOT_PRODUCTION"} for value in trading_dates
        ] + [
            {"date": "2022-12-24", "market": "KR", "is_trading_day": False, "calendar_verified": True,
             "calendar_source": "TEST_FIXTURE_NOT_PRODUCTION"},
            {"date": "2022-12-25", "market": "KR", "is_trading_day": False, "calendar_verified": True,
             "calendar_source": "TEST_FIXTURE_NOT_PRODUCTION"},
        ])
        result = assess_quality(master, calendar=calendar)
        self.assertTrue(result.missing_dates.empty)
        self.assertEqual(result.quality_summary_by_ticker.loc[0, "quality_status"], "PASS")

    def test_missing_date_not_confirmed_without_verified_calendar(self):
        master = pd.DataFrame([price_row("2022-12-22"), price_row("2022-12-26")])
        result = assess_quality(master)
        self.assertTrue(result.missing_dates.empty)
        self.assertEqual(result.quality_summary_by_ticker.loc[0, "quality_status"], "CONDITIONAL")

    def test_missing_cells_duplicates_and_ohlc_are_blocked(self):
        bad = price_row("2024-01-02", close=None, high=90.0, low=95.0)
        master = pd.DataFrame([bad, bad])
        calendar = pd.DataFrame([{"date": "2024-01-02", "market": "KR", "is_trading_day": True,
                                  "calendar_verified": True, "calendar_source": "TEST"}])
        result = assess_quality(master, calendar=calendar)
        self.assertEqual(len(result.duplicate_keys), 2)
        self.assertEqual(len(result.missing_cells), 2)
        self.assertIn("HIGH_BELOW_LOW", set(result.anomaly_dates["issue"]))
        self.assertEqual(result.quality_summary_by_ticker.loc[0, "quality_status"], "BLOCKED")

    def test_confirmed_gap_and_consecutive_run_are_reported(self):
        master = pd.DataFrame([price_row("2024-01-02"), price_row("2024-01-05")])
        calendar = pd.DataFrame([
            {"date": value, "market": "KR", "is_trading_day": True, "calendar_verified": True, "calendar_source": "TEST"}
            for value in ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        ])
        result = assess_quality(master, calendar=calendar)
        self.assertEqual(len(result.missing_dates), 2)
        run = result.missing_runs[result.missing_runs["category"] == "CONFIRMED_MISSING_TRADING_DATE"].iloc[0]
        self.assertEqual(run["consecutive_sessions"], 2)


if __name__ == "__main__":
    unittest.main()
