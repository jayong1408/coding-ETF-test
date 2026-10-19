#!/usr/bin/env python
"""Compare original and repaired candidates, then apply the Alpha V1 gate."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from etf_data_pipeline.alpha import DEFAULT_CONTRACT, check_alpha_compatibility, write_alpha_report
from etf_data_pipeline.handoff import sha256_file
from etf_data_pipeline.quality import assess_quality, write_quality_reports
from etf_data_pipeline.schema import read_table
from etf_data_pipeline.validation import compare_quality, verify_repair_audit, write_validation_comparison


def git_version() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "UNKNOWN"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--calendar", required=True, help="Verified trading calendar CSV/Parquet")
    parser.add_argument("--actions", help="Corporate-action CSV/Parquet used to explain adjustment-factor changes")
    parser.add_argument("--repair-audit", required=True, help="Cell-level audit produced with the candidate")
    parser.add_argument("--repair-run-manifest", required=True, help="Repair run manifest containing input/output SHA-256 values")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--alpha-contract", default="config/alpha_v1_contract.json")
    parser.add_argument("--sample-data", action="store_true", help="Label outputs as synthetic and never production-ready")
    args = parser.parse_args()
    output = Path(args.output_dir); output.mkdir(parents=True, exist_ok=True)
    calendar = read_table(args.calendar)
    actions = read_table(args.actions) if args.actions else None
    before_path = Path(args.before).resolve(); candidate_path = Path(args.candidate).resolve()
    audit_path = Path(args.repair_audit).resolve(); repair_manifest_path = Path(args.repair_run_manifest).resolve()
    repair_manifest = json.loads(repair_manifest_path.read_text(encoding="utf-8"))
    if repair_manifest.get("status") != "COMPLETE":
        raise ValueError("Repair run manifest is not COMPLETE")
    if repair_manifest.get("source_manifest_validation", {}).get("status") != "PASS":
        raise ValueError("Repair source manifest validation did not PASS")
    expected_before_hash = repair_manifest.get("inputs", {}).get("master", {}).get("sha256")
    if expected_before_hash != sha256_file(before_path):
        raise ValueError("Original master SHA-256 does not match the repair run manifest")
    candidate_record = repair_manifest.get("outputs", {}).get("candidate", {})
    audit_record = repair_manifest.get("outputs", {}).get("repair_audit.csv", {})
    if not isinstance(candidate_record, dict) or candidate_record.get("sha256") != sha256_file(candidate_path):
        raise ValueError("Candidate SHA-256 does not match the repair run manifest")
    if not isinstance(audit_record, dict) or audit_record.get("sha256") != sha256_file(audit_path):
        raise ValueError("Repair audit SHA-256 does not match the repair run manifest")

    before_frame = read_table(before_path); candidate = read_table(candidate_path)
    repair_audit = read_table(audit_path)
    audit_discrepancies, audit_validation = verify_repair_audit(before_frame, candidate, repair_audit)
    audit_discrepancies.to_csv(output / "repair_audit_discrepancies.csv", index=False, encoding="utf-8")
    (output / "repair_audit_validation.json").write_text(json.dumps(audit_validation, ensure_ascii=False, indent=2), encoding="utf-8")
    if audit_validation["status"] != "PASS":
        raise ValueError("Repair audit does not account for every changed price cell")
    before = assess_quality(before_frame, calendar=calendar, actions=actions, origin=str(before_path), sample_data=args.sample_data)
    after = assess_quality(candidate, calendar=calendar, actions=actions, origin=str(candidate_path), sample_data=args.sample_data)
    write_quality_reports(before, output / "before")
    write_quality_reports(after, output / "after")
    comparison, report = compare_quality(before, after)
    write_validation_comparison(comparison, report, output)
    contract_path = Path(args.alpha_contract)
    contract = json.loads(contract_path.read_text(encoding="utf-8")) if contract_path.exists() else DEFAULT_CONTRACT
    admitted, alpha_report = check_alpha_compatibility(candidate, after.quality_summary_by_ticker, contract=contract)
    alpha_report["sample_data"] = args.sample_data
    admitted.to_csv(output / "alpha_admitted_rows.csv", index=False, encoding="utf-8")
    write_alpha_report(alpha_report, output / "alpha_v1_compatibility.json")
    input_paths = {"before": before_path, "candidate": candidate_path,
                   "calendar": Path(args.calendar).resolve(), "repair_audit": audit_path,
                   "repair_run_manifest": repair_manifest_path}
    if args.actions:
        input_paths["actions"] = Path(args.actions).resolve()
    if contract_path.exists():
        input_paths["alpha_contract"] = contract_path.resolve()
    manifest = {
        "executed_at_utc": datetime.now(timezone.utc).isoformat(), "status": "COMPLETE", "code_version": git_version(),
        "inputs": {name: {"path": str(path), "sha256": sha256_file(path)} for name, path in input_paths.items()},
        "outputs": ["before/", "after/", "repair_summary.csv", "post_repair_validation.json", "repair_audit_discrepancies.csv", "repair_audit_validation.json", "alpha_admitted_rows.csv", "alpha_v1_compatibility.json"],
        "repair_audit_validation": audit_validation,
        "sample_data": args.sample_data,
        "production_ready": bool(not args.sample_data and report["production_ready"] and alpha_report["overall_status"] == "PASS"),
    }
    (output / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"post_repair": report, "alpha": alpha_report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
