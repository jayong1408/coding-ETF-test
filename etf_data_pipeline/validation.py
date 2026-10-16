"""Before/after quality comparison for repaired ETF candidates."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .quality import QualityResult


def compare_quality(before: QualityResult, after: QualityResult) -> tuple[pd.DataFrame, dict[str, Any]]:
    dimensions = ["market", "ticker"]
    left = before.quality_summary_by_ticker.copy()
    right = after.quality_summary_by_ticker.copy()
    selected = ["rows", "missing_cells", "confirmed_missing_dates", "duplicate_rows", "blocked_anomalies", "conditional_anomalies", "quality_status"]
    left = left[dimensions + selected].rename(columns={column: f"before_{column}" for column in selected})
    right = right[dimensions + selected].rename(columns={column: f"after_{column}" for column in selected})
    comparison = left.merge(right, on=dimensions, how="outer")
    numeric = ["missing_cells", "confirmed_missing_dates", "duplicate_rows", "blocked_anomalies", "conditional_anomalies"]
    for metric in numeric:
        before_col, after_col = f"before_{metric}", f"after_{metric}"
        comparison[before_col] = comparison[before_col].fillna(0).astype(int)
        comparison[after_col] = comparison[after_col].fillna(0).astype(int)
        comparison[f"change_{metric}"] = comparison[after_col] - comparison[before_col]
    comparison["final_status"] = comparison["after_quality_status"].fillna("BLOCKED")
    counts = comparison["final_status"].value_counts().to_dict()
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "before": before.validation_results,
        "after": after.validation_results,
        "ticker_status_counts": {str(key): int(value) for key, value in counts.items()},
        "pass_rule": "PASS has no confirmed missing cells/dates, duplicate keys, blocking anomalies, conditional anomalies, and uses a verified trading calendar.",
        "production_ready": bool(not comparison.empty and comparison["final_status"].eq("PASS").all()),
    }
    return comparison, report


def write_validation_comparison(
    comparison: pd.DataFrame,
    report: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / "repair_summary.csv"
    json_path = output / "post_repair_validation.json"
    comparison.to_csv(csv_path, index=False, encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {csv_path.name: csv_path, json_path.name: json_path}
