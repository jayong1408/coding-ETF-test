#!/usr/bin/env python
"""Generate CSV/JSON ETF quality diagnostics without modifying the master."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from etf_data_pipeline.quality import assess_quality, write_quality_reports
from etf_data_pipeline.handoff import sha256_file
from etf_data_pipeline.schema import read_table
from etf_data_pipeline.streaming import assess_quality_streaming


def git_version() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "UNKNOWN"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", required=True, help="Input master in Parquet or CSV format")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--calendar", help="Verified calendar CSV/Parquet with source and verification columns")
    parser.add_argument("--actions", help="Corporate-action CSV/Parquet")
    parser.add_argument("--sample-data", action="store_true", help="Label outputs as synthetic test artifacts")
    parser.add_argument("--batch-rows", type=int, default=100_000, help="Parquet/CSV staging batch size")
    parser.add_argument("--in-memory", action="store_true", help="Use only for small fixtures")
    args = parser.parse_args()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    try:
        calendar = read_table(args.calendar) if args.calendar else None
        actions = read_table(args.actions) if args.actions else None
        if args.in_memory:
            master = read_table(args.master)
            result = assess_quality(master, calendar=calendar, actions=actions, origin=str(Path(args.master).resolve()), sample_data=args.sample_data)
        else:
            result = assess_quality_streaming(args.master, calendar=calendar, actions=actions, sample_data=args.sample_data,
                                              batch_rows=args.batch_rows, staging_dir=output)
        paths = write_quality_reports(result, output)
        master_path = Path(args.master).resolve()
        calendar_path = Path(args.calendar).resolve() if args.calendar else None
        actions_path = Path(args.actions).resolve() if args.actions else None
        manifest = {
            "executed_at_utc": datetime.now(timezone.utc).isoformat(),
            "inputs": {
                "master": {"path": str(master_path), "sha256": sha256_file(master_path)},
                "calendar": {"path": str(calendar_path), "sha256": sha256_file(calendar_path)} if calendar_path else None,
                "actions": {"path": str(actions_path), "sha256": sha256_file(actions_path)} if actions_path else None,
            },
            "settings": {
                "sample_data": args.sample_data, "batch_rows": args.batch_rows, "in_memory": args.in_memory,
                "quality_rules": result.validation_results.get("quality_rule_config", {}),
            },
            "code_version": git_version(),
            "outputs": {key: str(value.resolve()) for key, value in paths.items()},
            "status": "COMPLETE",
        }
        (output / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    except Exception as exc:
        failure = {
            "executed_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "FAILED", "error_type": type(exc).__name__, "error": str(exc),
            "rerun": f"python run_quality_pipeline.py --master {args.master!r} --output-dir {args.output_dir!r}",
        }
        (output / "run_failure.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        raise


if __name__ == "__main__":
    main()
