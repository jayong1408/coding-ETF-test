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
from etf_data_pipeline.validation import compare_quality, write_validation_comparison


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
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--alpha-contract", default="config/alpha_v1_contract.json")
    args = parser.parse_args()
    output = Path(args.output_dir); output.mkdir(parents=True, exist_ok=True)
    calendar = read_table(args.calendar)
    actions = read_table(args.actions) if args.actions else None
    before_frame = read_table(args.before); candidate = read_table(args.candidate)
    before = assess_quality(before_frame, calendar=calendar, actions=actions, origin=str(Path(args.before).resolve()))
    after = assess_quality(candidate, calendar=calendar, actions=actions, origin=str(Path(args.candidate).resolve()))
    write_quality_reports(before, output / "before")
    write_quality_reports(after, output / "after")
    comparison, report = compare_quality(before, after)
    write_validation_comparison(comparison, report, output)
    contract_path = Path(args.alpha_contract)
    contract = json.loads(contract_path.read_text(encoding="utf-8")) if contract_path.exists() else DEFAULT_CONTRACT
    admitted, alpha_report = check_alpha_compatibility(candidate, after.quality_summary_by_ticker, contract=contract)
    admitted.to_csv(output / "alpha_admitted_rows.csv", index=False, encoding="utf-8")
    write_alpha_report(alpha_report, output / "alpha_v1_compatibility.json")
    input_paths = {"before": Path(args.before).resolve(), "candidate": Path(args.candidate).resolve(),
                   "calendar": Path(args.calendar).resolve()}
    if args.actions:
        input_paths["actions"] = Path(args.actions).resolve()
    if contract_path.exists():
        input_paths["alpha_contract"] = contract_path.resolve()
    manifest = {
        "executed_at_utc": datetime.now(timezone.utc).isoformat(), "status": "COMPLETE", "code_version": git_version(),
        "inputs": {name: {"path": str(path), "sha256": sha256_file(path)} for name, path in input_paths.items()},
        "outputs": ["before/", "after/", "repair_summary.csv", "post_repair_validation.json", "alpha_admitted_rows.csv", "alpha_v1_compatibility.json"],
        "production_ready": bool(report["production_ready"] and alpha_report["overall_status"] == "PASS"),
    }
    (output / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"post_repair": report, "alpha": alpha_report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
