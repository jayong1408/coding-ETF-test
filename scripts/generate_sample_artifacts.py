#!/usr/bin/env python
"""Generate clearly labelled synthetic artifacts for end-to-end verification."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from etf_data_pipeline.alpha import check_alpha_compatibility, write_alpha_report
from etf_data_pipeline.handoff import build_handoff_package
from etf_data_pipeline.quality import assess_quality, write_quality_reports
from etf_data_pipeline.repair import repair_master, validate_source_manifest, write_repair_outputs
from etf_data_pipeline.validation import compare_quality, verify_repair_audit, write_validation_comparison


def price(date: str, ticker: str, base: float, **updates):
    row = {"date": date, "market": "KR", "ticker": ticker, "name": f"SYNTHETIC_{ticker}",
           "open": base, "high": base + 2, "low": base - 2, "close": base + 1,
           "adj_close": base + 1, "volume": 1000, "data_status": "SYNTHETIC_TEST_DATA"}
    row.update(updates)
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="artifacts/sample_validation")
    args = parser.parse_args()
    root = Path(args.output_dir)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Use a new or empty sample output directory: {root}")
    inputs = root / "inputs"; before_dir = root / "quality_before"; after_dir = root / "quality_after"; repair_dir = root / "repair"
    for path in [inputs, before_dir, after_dir, repair_dir]:
        path.mkdir(parents=True, exist_ok=True)

    dates_360750 = ["2022-12-22", "2022-12-23", "2022-12-26", "2022-12-27", "2022-12-28", "2022-12-29"]
    master = pd.DataFrame(
        [price(value, "360750", 100 + index) for index, value in enumerate(dates_360750)]
        + [price("2024-01-02", "069500", 200), price("2024-01-03", "069500", 201, adj_close=None), price("2024-01-04", "069500", 202)]
    )
    calendar_dates = dates_360750 + ["2024-01-02", "2024-01-03", "2024-01-04"]
    calendar = pd.DataFrame([{"date": value, "market": "KR", "is_trading_day": True, "calendar_verified": True,
                              "calendar_source": "SYNTHETIC_TEST_CALENDAR_NOT_PRODUCTION"} for value in calendar_dates])
    source = pd.DataFrame([{
        **price("2024-01-03", "069500", 201), "verification_status": "VERIFIED",
        "source_name": "SYNTHETIC_TEST_SOURCE", "source_uri": "synthetic://sample/069500/2024-01-03",
        "source_retrieved_at": "2026-10-16T00:00:00Z", "source_hash": "SYNTHETIC_HASH_NOT_EVIDENCE",
        "calendar_verified": True,
    }])
    master_path = inputs / "SYNTHETIC_master.parquet"; calendar_path = inputs / "SYNTHETIC_calendar.csv"; source_path = inputs / "SYNTHETIC_verified_source.csv"
    master.to_parquet(master_path, index=False)
    master.to_csv(inputs / "SYNTHETIC_master.csv", index=False, encoding="utf-8")
    calendar.to_csv(calendar_path, index=False, encoding="utf-8")
    source.to_csv(source_path, index=False, encoding="utf-8")
    source_file_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_manifest = {
        "verification_status": "VERIFIED", "verified_at_utc": "2026-10-16T00:00:00Z",
        "verification_basis": "SYNTHETIC_TEST_FIXTURE_ONLY",
        "verified_source_file": {"sha256": source_file_hash, "rows": len(source)},
        "source_evidence": [{
            "source_name": "SYNTHETIC_TEST_SOURCE",
            "source_uri": "synthetic://sample/069500/2024-01-03",
            "source_hash": "SYNTHETIC_HASH_NOT_EVIDENCE",
        }],
    }
    source_manifest_path = inputs / "SYNTHETIC_source_manifest.json"
    source_manifest_path.write_text(json.dumps(source_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    source_manifest_validation = validate_source_manifest(source_path, source_manifest_path, source)
    (inputs / "README.md").write_text("# Synthetic fixtures\n\nThese files are generated test data, not market observations and not repair evidence.\n", encoding="utf-8")

    before = assess_quality(master, calendar=calendar, origin="SYNTHETIC_TEST_DATA", sample_data=True)
    write_quality_reports(before, before_dir)
    repair = repair_master(master, source)
    candidate_path = repair_dir / "SYNTHETIC_repaired_candidate.parquet"
    write_repair_outputs(repair, repair_dir, candidate_path=candidate_path)
    audit_discrepancies, audit_validation = verify_repair_audit(master, repair.candidate, repair.repair_audit)
    audit_discrepancies.to_csv(repair_dir / "repair_audit_discrepancies.csv", index=False, encoding="utf-8")
    (repair_dir / "repair_audit_validation.json").write_text(json.dumps(audit_validation, ensure_ascii=False, indent=2), encoding="utf-8")
    after = assess_quality(repair.candidate, calendar=calendar, origin="SYNTHETIC_TEST_DATA", sample_data=True)
    write_quality_reports(after, after_dir)
    comparison, validation = compare_quality(before, after)
    validation["sample_data"] = True
    write_validation_comparison(comparison, validation, repair_dir)
    admitted, alpha_report = check_alpha_compatibility(repair.candidate, after.quality_summary_by_ticker)
    alpha_report["sample_data"] = True
    write_alpha_report(alpha_report, root / "alpha_v1_compatibility.json")
    admitted.to_csv(root / "SYNTHETIC_alpha_admitted_rows.csv", index=False, encoding="utf-8")
    handoff = build_handoff_package(master_path=candidate_path, quality_dir=after_dir, repair_dir=repair_dir,
                                    output_dir=root / "handoff", max_file_bytes=1_000_000, sample_data=True)
    run = {"sample_data": True, "production_repair_completed": False, "source_master_rows": len(master),
           "candidate_rows": len(repair.candidate), "repairs_applied": len(repair.repair_audit),
           "source_manifest_validation": source_manifest_validation,
           "repair_audit_validation": audit_validation,
           "before_statuses": before.quality_summary_by_ticker["quality_status"].value_counts().to_dict(),
           "after_statuses": after.quality_summary_by_ticker["quality_status"].value_counts().to_dict(),
           "alpha_contract_status": alpha_report["contract_status"], "alpha_admitted_rows": len(admitted),
           "handoff_validation": handoff["validation"]}
    (root / "SAMPLE_RUN_RESULT.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "README.md").write_text(
        "# Synthetic sample validation\n\nThis directory proves the code path and package structure only. It contains no real ETF prices, no validated external repair source, and no completed Alpha V1 integration.\n",
        encoding="utf-8",
    )
    print(json.dumps(run, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
