#!/usr/bin/env python
"""Create a separate verified repair candidate and complete change history."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from etf_data_pipeline.handoff import sha256_file
from etf_data_pipeline.repair import repair_master, write_repair_outputs
from etf_data_pipeline.schema import read_table


def git_version() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "UNKNOWN"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", required=True)
    parser.add_argument("--verified-source", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--candidate", help="New candidate path; must not already exist")
    args = parser.parse_args()
    source_path = Path(args.master).resolve()
    candidate = Path(args.candidate).resolve() if args.candidate else Path(args.output_dir).resolve() / "ETF_Daily_Master_repaired_candidate.csv"
    if candidate == source_path:
        raise ValueError("The repair candidate path must differ from the original master path.")
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    try:
        verified_source = Path(args.verified_source).resolve()
        result = repair_master(read_table(source_path), read_table(verified_source))
        paths = write_repair_outputs(result, output, candidate_path=candidate)
        manifest = {
            "executed_at_utc": datetime.now(timezone.utc).isoformat(), "status": "COMPLETE",
            "code_version": git_version(),
            "inputs": {
                "master": {"path": str(source_path), "sha256": sha256_file(source_path)},
                "verified_source": {"path": str(verified_source), "sha256": sha256_file(verified_source)},
            },
            "settings": {"allow_new_rows": True, "synthetic_or_interpolated_values_allowed": False},
            "outputs": {name: str(path.resolve()) for name, path in paths.items()},
            "summary": result.summary,
        }
        (output / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Repair candidate written; original master was not modified.")
        for name, path in paths.items():
            print(f"{name}: {path}")
    except Exception as exc:
        failure = {
            "executed_at_utc": datetime.now(timezone.utc).isoformat(), "status": "FAILED",
            "error_type": type(exc).__name__, "error": str(exc),
            "rerun": f"python run_repair_pipeline.py --master {args.master!r} --verified-source {args.verified_source!r} --output-dir {args.output_dir!r} --candidate {str(candidate)!r}",
        }
        (output / "run_failure.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        raise


if __name__ == "__main__":
    main()
