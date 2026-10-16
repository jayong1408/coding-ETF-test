#!/usr/bin/env python
"""Create and verify a compact ETF data handoff package."""
from __future__ import annotations

import argparse
import json

from etf_data_pipeline.handoff import build_handoff_package


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", required=True, help="Original/candidate master in Parquet or CSV")
    parser.add_argument("--quality-dir", required=True)
    parser.add_argument("--repair-dir")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-file-mb", type=float, default=5.0)
    parser.add_argument("--sample-data", action="store_true", help="Mark every brief/manifest as synthetic test data")
    args = parser.parse_args()
    manifest = build_handoff_package(
        master_path=args.master, quality_dir=args.quality_dir, repair_dir=args.repair_dir,
        output_dir=args.output_dir, max_file_bytes=int(args.max_file_mb * 1_000_000), sample_data=args.sample_data,
    )
    print(json.dumps(manifest["validation"], ensure_ascii=False, indent=2))
    checks = manifest["validation"]
    if not all([
        checks["required_summary_files_present"], checks["detail_file_size_limit_passed"],
        checks["split_row_counts_match"], checks["missing_list_has_no_duplicates"],
        checks["unresolved_problems_explicit"],
    ]):
        raise SystemExit("Handoff package validation failed; inspect handoff_manifest.json")


if __name__ == "__main__":
    main()
