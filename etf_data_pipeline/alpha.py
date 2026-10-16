"""Alpha V1 contract checks and fail-closed data admission."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .schema import normalize_master


DEFAULT_CONTRACT = {
    "contract_status": "INFERRED_NOT_CONFIRMED",
    "required_columns": ["date", "market", "ticker", "adj_close", "volume"],
    "unique_key": ["date", "market", "ticker"],
    "allowed_quality_statuses": ["PASS"],
    "note": "Inferred from legacy ETF Daily Master aliases. Replace with the actual Alpha V1 contract before production use.",
}


def check_alpha_compatibility(
    master: pd.DataFrame,
    quality_summary: pd.DataFrame,
    *,
    contract: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    contract = contract or DEFAULT_CONTRACT
    data = normalize_master(master, origin="alpha_candidate")
    required = list(contract.get("required_columns", []))
    missing_columns = [column for column in required if column not in data.columns]
    null_required = {column: int(data[column].isna().sum()) for column in required if column in data.columns}
    key = list(contract.get("unique_key", ["date", "market", "ticker"]))
    duplicate_keys = int(data.duplicated(key).sum()) if all(column in data.columns for column in key) else None
    allowed = set(contract.get("allowed_quality_statuses", ["PASS"]))
    contract_confirmed = contract.get("contract_status") == "CONFIRMED"
    status = quality_summary[["market", "ticker", "quality_status"]].drop_duplicates(["market", "ticker"], keep="last")
    gated = data.merge(status, on=["market", "ticker"], how="left")
    gated["alpha_admission"] = gated["quality_status"].map(
        lambda value: "ALLOW" if value in allowed and contract_confirmed else "BLOCK"
    )
    gated["alpha_block_reason"] = gated.apply(
        lambda row: "" if row["alpha_admission"] == "ALLOW" else (
            "ALPHA_CONTRACT_NOT_CONFIRMED" if not contract_confirmed else f"QUALITY_STATUS_{row['quality_status']}"
        ), axis=1,
    )
    admitted = gated[gated["alpha_admission"] == "ALLOW"].copy()
    blocked_rows = int((gated["alpha_admission"] == "BLOCK").sum())
    schema_passed = not missing_columns and all(value == 0 for value in null_required.values()) and duplicate_keys == 0
    overall = "PASS" if schema_passed and blocked_rows == 0 and contract_confirmed else (
        "BLOCKED" if not schema_passed or blocked_rows else "CONDITIONAL"
    )
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "overall_status": overall,
        "contract_status": contract.get("contract_status", "UNKNOWN"),
        "missing_required_columns": missing_columns,
        "null_required_counts": null_required,
        "duplicate_keys": duplicate_keys,
        "input_rows": int(len(data)), "admitted_rows": int(len(admitted)), "blocked_rows": blocked_rows,
        "feature_engine_schema_check": "PASSED" if schema_passed else "FAILED",
        "backtest_input_gate": "CONFIRMED_CONTRACT_AND_PASS_QUALITY_ONLY",
        "integration_execution_status": "NOT_RUN_WITH_REAL_ALPHA_V1" if not contract_confirmed else "CONTRACT_CONFIRMED",
    }
    return admitted, report


def write_alpha_report(report: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
