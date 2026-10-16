from __future__ import annotations

import unittest

import pandas as pd

from etf_data_pipeline.alpha import check_alpha_compatibility
from etf_data_pipeline.repair import repair_master


class RepairAndAlphaTests(unittest.TestCase):
    def setUp(self):
        self.original = pd.DataFrame([{
            "date": "2024-01-02", "market": "KR", "ticker": "069500",
            "open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0,
            "adj_close": None, "volume": 1000.0,
        }])

    def source(self, **updates):
        row = {
            "date": "2024-01-02", "market": "KR", "ticker": "069500",
            "open": 100.0, "high": 105.0, "low": 95.0, "close": 999.0,
            "adj_close": 101.5, "volume": 1000.0, "verification_status": "VERIFIED",
            "source_name": "Example official export", "source_uri": "https://example.invalid/evidence",
            "source_retrieved_at": "2024-02-01T00:00:00Z", "source_hash": "abc123",
            "calendar_verified": True,
        }
        row.update(updates)
        return row

    def test_verified_value_fills_missing_cell_and_preserves_existing(self):
        result = repair_master(self.original, pd.DataFrame([self.source(close=102.0)]))
        self.assertEqual(result.candidate.loc[0, "close"], 102.0)
        self.assertEqual(result.candidate.loc[0, "adj_close"], 101.5)
        self.assertEqual(len(result.repair_audit), 1)
        self.assertEqual(result.repair_audit.loc[0, "field"], "adj_close")

    def test_unverified_value_remains_unresolved(self):
        result = repair_master(self.original, pd.DataFrame([self.source(verification_status="UNVERIFIED", close=102.0)]))
        self.assertTrue(pd.isna(result.candidate.loc[0, "adj_close"]))
        self.assertIn("SOURCE_NOT_VERIFIED", ";".join(result.unresolved_cases["reason"].astype(str)))

    def test_existing_conflict_is_not_overwritten(self):
        result = repair_master(self.original, pd.DataFrame([self.source()]))
        self.assertEqual(result.candidate.loc[0, "close"], 102.0)
        conflicts = result.unresolved_cases[result.unresolved_cases["field"] == "close"]
        self.assertEqual(conflicts.iloc[0]["reason"], "EXISTING_VALUE_CONFLICT_NOT_OVERWRITTEN")

    def test_verified_complete_row_can_be_added_for_verified_session(self):
        source = self.source(date="2024-01-03", close=102.0)
        result = repair_master(self.original, pd.DataFrame([source]))
        self.assertEqual(len(result.candidate), 2)
        added = result.candidate[result.candidate["date"] == pd.Timestamp("2024-01-03")]
        self.assertEqual(added.iloc[0]["adj_close"], 101.5)
        self.assertEqual(set(result.repair_audit["repair_reason"]), {"CONFIRMED_MISSING_TRADING_DATE"})

    def test_alpha_gate_admits_pass_only_and_marks_contract_unconfirmed(self):
        master = pd.concat([self.original.assign(adj_close=102.0), self.original.assign(ticker="102110", adj_close=102.0)], ignore_index=True)
        quality = pd.DataFrame([
            {"market": "KR", "ticker": "069500", "quality_status": "PASS"},
            {"market": "KR", "ticker": "102110", "quality_status": "CONDITIONAL"},
        ])
        confirmed_contract = {
            "contract_status": "CONFIRMED", "required_columns": ["date", "market", "ticker", "adj_close", "volume"],
            "unique_key": ["date", "market", "ticker"], "allowed_quality_statuses": ["PASS"],
        }
        admitted, report = check_alpha_compatibility(master, quality, contract=confirmed_contract)
        self.assertEqual(set(admitted["ticker"]), {"069500"})
        self.assertEqual(report["blocked_rows"], 1)
        self.assertEqual(report["overall_status"], "BLOCKED")
        self.assertEqual(report["integration_execution_status"], "CONTRACT_CONFIRMED")

    def test_unconfirmed_alpha_contract_blocks_every_row(self):
        master = self.original.assign(adj_close=102.0)
        quality = pd.DataFrame([{"market": "KR", "ticker": "069500", "quality_status": "PASS"}])
        admitted, report = check_alpha_compatibility(master, quality)
        self.assertTrue(admitted.empty)
        self.assertEqual(report["blocked_rows"], 1)
        self.assertEqual(report["integration_execution_status"], "NOT_RUN_WITH_REAL_ALPHA_V1")


if __name__ == "__main__":
    unittest.main()
