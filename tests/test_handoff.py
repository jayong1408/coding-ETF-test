from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from etf_data_pipeline.handoff import SUMMARY_FILES, _write_detail_parts, build_handoff_package


class HandoffTests(unittest.TestCase):
    def test_large_detail_is_split_without_row_loss(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([
                {"market": "KR", "ticker": "069500", "date": f"2024-01-{(index % 28) + 1:02d}",
                 "field": f"field_{index}", "category": "MISSING_CELL", "status": "CONFIRMED"}
                for index in range(100)
            ])
            paths, validation = _write_detail_parts(frame, "missing_dates.csv", Path(temp), 500)
            self.assertGreater(len(paths), 1)
            self.assertEqual(validation["source_rows"], validation["part_rows"])
            self.assertTrue(validation["row_count_match"])

    def test_required_package_files_and_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            master = root / "master.csv"
            quality = root / "quality"
            repair = root / "repair"
            quality.mkdir(); repair.mkdir()
            pd.DataFrame([{
                "date": "2024-01-02", "market": "KR", "ticker": "069500", "open": 100,
                "high": 105, "low": 95, "close": 102, "adj_close": 102, "volume": 1000,
            }]).to_csv(master, index=False)
            pd.DataFrame([{"market": "KR", "ticker": "069500", "rows": 1, "first_date": "2024-01-02",
                           "last_date": "2024-01-02", "quality_status": "PASS"}]).to_csv(quality / "quality_summary_by_ticker.csv", index=False)
            pd.DataFrame(columns=["market", "ticker", "year", "missing_cell_count", "missing_date_count"]).to_csv(quality / "missing_summary_by_year.csv", index=False)
            pd.DataFrame(columns=["market", "ticker", "date", "field", "category", "status"]).to_csv(quality / "missing_dates.csv", index=False)
            pd.DataFrame([{"market": "KR", "ticker": "069500", "date": "2024-01-02", "field": "adj_close",
                           "category": "MISSING_CELL", "status": "CONFIRMED"}]).to_csv(quality / "missing_cells.csv", index=False)
            pd.DataFrame(columns=["market", "ticker", "date", "field", "issue", "severity", "value", "detail"]).to_csv(quality / "anomaly_dates.csv", index=False)
            (quality / "validation_results.json").write_text(json.dumps({"checks": {"sample": {"passed": True}}}), encoding="utf-8")
            for name, columns in {
                "repair_audit.csv": ["market", "ticker", "date", "field"],
                "source_comparison.csv": ["market", "ticker", "date", "field"],
                "unresolved_cases.csv": ["market", "ticker", "date", "field", "reason"],
            }.items():
                pd.DataFrame(columns=columns).to_csv(repair / name, index=False)
            (repair / "repair_summary.json").write_text(json.dumps({"applied_cell_repairs": 0}), encoding="utf-8")
            output = root / "handoff"
            manifest = build_handoff_package(master_path=master, quality_dir=quality, repair_dir=repair,
                                             output_dir=output, max_file_bytes=1000, sample_data=True)
            for name in SUMMARY_FILES:
                self.assertTrue((output / "handoff_summary" / name).exists(), name)
            self.assertTrue((output / "handoff_manifest.json").exists())
            self.assertTrue((output / "HANDOFF_README_KR.md").exists())
            self.assertTrue(manifest["validation"]["split_row_counts_match"])
            self.assertTrue(manifest["validation"]["detail_file_size_limit_passed"])
            handoff_missing = pd.read_csv(output / "handoff_details" / "missing_dates.csv", dtype={"ticker": "string"})
            self.assertEqual(len(handoff_missing), 1)
            for record in manifest["files"]:
                payload = (output / record["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(payload).hexdigest(), record["sha256"])


if __name__ == "__main__":
    unittest.main()
