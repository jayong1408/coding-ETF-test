from __future__ import annotations

import unittest
import hashlib
import json
from pathlib import Path
import tempfile

import pandas as pd

from etf_data_pipeline.alpha import check_alpha_compatibility
from etf_data_pipeline.quality import assess_quality
from etf_data_pipeline.repair import repair_master, validate_source_manifest
from etf_data_pipeline.validation import compare_quality, verify_repair_audit


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

    def test_repair_rerun_is_idempotent_and_does_not_duplicate_audit(self):
        first = repair_master(self.original, pd.DataFrame([self.source(close=102.0)]))
        second = repair_master(first.candidate, pd.DataFrame([self.source(close=102.0)]))
        pd.testing.assert_frame_equal(first.candidate, second.candidate)
        self.assertTrue(second.repair_audit.empty)
        self.assertTrue(second.unresolved_cases.empty)

    def test_source_manifest_must_match_file_hash_and_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_path = root / "source.csv"
            source_frame = pd.DataFrame([self.source(close=102.0)])
            source_frame.to_csv(source_path, index=False)
            source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            manifest = {
                "verification_status": "VERIFIED",
                "verified_at_utc": "2026-10-16T00:00:00Z",
                "verification_basis": "test fixture evidence reviewed",
                "verified_source_file": {"sha256": source_hash, "rows": 1},
                "source_evidence": [{
                    "source_name": "Example official export",
                    "source_uri": "https://example.invalid/evidence",
                    "source_hash": "abc123",
                }],
            }
            manifest_path = root / "source_manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            validation = validate_source_manifest(source_path, manifest_path, source_frame)
            self.assertEqual(validation["status"], "PASS")
            manifest["verified_source_file"]["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                validate_source_manifest(source_path, manifest_path, source_frame)

    def test_repair_audit_accounts_for_every_changed_cell(self):
        result = repair_master(self.original, pd.DataFrame([self.source(close=102.0)]))
        discrepancies, report = verify_repair_audit(self.original, result.candidate, result.repair_audit)
        self.assertTrue(discrepancies.empty)
        self.assertEqual(report["status"], "PASS")
        unaudited = result.candidate.copy()
        unaudited.loc[0, "close"] = 777.0
        discrepancies, report = verify_repair_audit(self.original, unaudited, result.repair_audit)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("CHANGED_CELL_WITHOUT_APPLIED_AUDIT", set(discrepancies["issue"]))

    def test_synthetic_quality_cannot_be_production_ready(self):
        master = self.original.assign(adj_close=102.0)
        calendar = pd.DataFrame([{
            "date": "2024-01-02", "market": "KR", "is_trading_day": True,
            "calendar_verified": True, "calendar_source": "SYNTHETIC_TEST_CALENDAR",
        }])
        result = assess_quality(master, calendar=calendar, sample_data=True)
        _, report = compare_quality(result, result)
        self.assertEqual(result.quality_summary_by_ticker.loc[0, "quality_status"], "PASS")
        self.assertTrue(report["sample_data"])
        self.assertFalse(report["production_ready"])

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
