"""Before/after quality comparison for repaired ETF candidates."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .quality import QualityResult
from .schema import KEY_COLUMNS, PRICE_COLUMNS, normalize_master


def _same_value(left: Any, right: Any) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    if pd.isna(left) or pd.isna(right):
        return False
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return str(left) == str(right)


def verify_repair_audit(
    original: pd.DataFrame,
    candidate: pd.DataFrame,
    repair_audit: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Prove every price-cell delta has exactly one matching applied audit row."""
    before = normalize_master(original, origin="repair_audit_original").drop_duplicates(KEY_COLUMNS, keep="last").set_index(KEY_COLUMNS)
    after = normalize_master(candidate, origin="repair_audit_candidate").drop_duplicates(KEY_COLUMNS, keep="last").set_index(KEY_COLUMNS)
    audit_columns = {"market", "ticker", "date", "field", "original_value", "replacement_value", "applied"}
    missing = sorted(audit_columns - set(repair_audit.columns))
    if missing:
        raise ValueError(f"Repair audit is missing required columns: {missing}")
    audit = repair_audit.copy()
    audit["market"] = audit["market"].astype(str).str.upper().str.strip()
    audit["ticker"] = audit["ticker"].astype("string").str.replace(r"\.0$", "", regex=True)
    audit.loc[audit["market"].eq("KR"), "ticker"] = audit.loc[audit["market"].eq("KR"), "ticker"].str.zfill(6)
    audit["date"] = pd.to_datetime(audit["date"], errors="coerce").dt.normalize()
    audit["applied"] = audit["applied"].map(
        lambda value: bool(value) if isinstance(value, bool) else str(value).casefold() in {"1", "true", "yes", "y"}
    )
    applied = audit[audit["applied"]].copy()
    signature = KEY_COLUMNS + ["field"]
    discrepancies: list[dict[str, Any]] = []
    duplicate_audits = applied[applied.duplicated(signature, keep=False)]
    for _, row in duplicate_audits.iterrows():
        discrepancies.append({**{column: row[column] for column in signature}, "issue": "DUPLICATE_APPLIED_AUDIT"})

    all_keys = before.index.union(after.index)
    changed: dict[tuple[Any, ...], tuple[Any, Any]] = {}
    for key in all_keys:
        for field in PRICE_COLUMNS:
            old = before.at[key, field] if key in before.index else None
            new = after.at[key, field] if key in after.index else None
            if not _same_value(old, new):
                changed[(*key, field)] = (old, new)

    applied_by_signature = {tuple(row[column] for column in signature): row for _, row in applied.iterrows()}
    for key, (old, new) in changed.items():
        row = applied_by_signature.get(key)
        if row is None:
            discrepancies.append({"market": key[0], "ticker": key[1], "date": key[2], "field": key[3],
                                  "issue": "CHANGED_CELL_WITHOUT_APPLIED_AUDIT"})
            continue
        if not _same_value(old, row["original_value"]):
            discrepancies.append({"market": key[0], "ticker": key[1], "date": key[2], "field": key[3],
                                  "issue": "AUDIT_ORIGINAL_VALUE_MISMATCH"})
        if not _same_value(new, row["replacement_value"]):
            discrepancies.append({"market": key[0], "ticker": key[1], "date": key[2], "field": key[3],
                                  "issue": "AUDIT_REPLACEMENT_VALUE_MISMATCH"})
    for key in applied_by_signature:
        if key not in changed:
            discrepancies.append({"market": key[0], "ticker": key[1], "date": key[2], "field": key[3],
                                  "issue": "APPLIED_AUDIT_WITHOUT_CELL_CHANGE"})

    discrepancy_frame = pd.DataFrame(discrepancies, columns=signature + ["issue"])
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if discrepancy_frame.empty else "BLOCKED",
        "changed_price_cells": int(len(changed)), "applied_audit_rows": int(len(applied)),
        "discrepancies": int(len(discrepancy_frame)),
        "rule": "Every changed price cell must have exactly one applied audit whose original and replacement values match.",
    }
    return discrepancy_frame, report


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
    sample_data = bool(before.validation_results.get("sample_data") or after.validation_results.get("sample_data"))
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "before": before.validation_results,
        "after": after.validation_results,
        "ticker_status_counts": {str(key): int(value) for key, value in counts.items()},
        "sample_data": sample_data,
        "pass_rule": "PASS has no confirmed missing cells/dates, duplicate keys, blocking anomalies, conditional anomalies, and uses a verified trading calendar.",
        "production_ready": bool(not sample_data and not comparison.empty and comparison["final_status"].eq("PASS").all()),
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
