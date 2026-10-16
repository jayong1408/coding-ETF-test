"""Verified-source-only ETF master repair with cell-level provenance."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .schema import KEY_COLUMNS, PRICE_COLUMNS, normalize_master


PROVENANCE_COLUMNS = ["verification_status", "source_name", "source_uri", "source_retrieved_at", "source_hash"]


@dataclass
class RepairResult:
    candidate: pd.DataFrame
    repair_audit: pd.DataFrame
    source_comparison: pd.DataFrame
    unresolved_cases: pd.DataFrame
    summary: dict[str, Any]


def _is_valid_value(field: str, value: Any) -> tuple[bool, str]:
    if pd.isna(value):
        return False, "SOURCE_VALUE_MISSING"
    if field in {"open", "high", "low", "close", "adj_close"} and float(value) <= 0:
        return False, "NON_POSITIVE_SOURCE_PRICE"
    if field == "volume" and float(value) < 0:
        return False, "NEGATIVE_SOURCE_VOLUME"
    return True, "VALUE_RULES_PASSED"


def _row_ohlc_valid(row: pd.Series) -> tuple[bool, str]:
    values = {field: row.get(field) for field in ["open", "high", "low", "close"]}
    if any(pd.isna(value) for value in values.values()):
        return False, "SOURCE_OHLC_INCOMPLETE"
    if values["high"] < max(values.values()) or values["low"] > min(values.values()):
        return False, "SOURCE_OHLC_INCONSISTENT"
    return True, "SOURCE_OHLC_PASSED"


def _provenance_valid(row: pd.Series) -> tuple[bool, str]:
    if str(row.get("verification_status", "")).strip().upper() != "VERIFIED":
        return False, "SOURCE_NOT_VERIFIED"
    missing = [column for column in ["source_name", "source_uri", "source_hash"] if not str(row.get(column, "")).strip()]
    if missing:
        return False, "SOURCE_PROVENANCE_INCOMPLETE:" + ",".join(missing)
    return True, "SOURCE_PROVENANCE_VERIFIED"


def _same_value(left: Any, right: Any) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    if pd.isna(left) or pd.isna(right):
        return False
    try:
        return bool(np.isclose(float(left), float(right), rtol=1e-9, atol=1e-9))
    except (TypeError, ValueError):
        return str(left) == str(right)


def repair_master(
    original: pd.DataFrame,
    external_sources: pd.DataFrame,
    *,
    repair_fields: tuple[str, ...] = tuple(PRICE_COLUMNS),
    allow_new_rows: bool = True,
) -> RepairResult:
    """Fill only missing values from fully identified, explicitly VERIFIED rows.

    Existing non-null cells are never overwritten.  Conflicting verified
    sources remain unresolved.  New date rows require ``calendar_verified`` and
    a complete valid OHLCV row.
    """
    base = normalize_master(original, origin="original_master")
    sources = normalize_master(external_sources, origin="external_source")
    for column in PROVENANCE_COLUMNS:
        if column not in sources.columns:
            sources[column] = ""
    if "calendar_verified" not in sources.columns:
        sources["calendar_verified"] = False
    sources["calendar_verified"] = sources["calendar_verified"].map(
        lambda value: value if isinstance(value, (bool, np.bool_)) else str(value).casefold() in {"1", "true", "yes", "y"}
    )

    candidate = base.drop_duplicates(KEY_COLUMNS, keep="last").copy().set_index(KEY_COLUMNS)
    source_groups = sources.groupby(KEY_COLUMNS, dropna=False, sort=False)
    audit: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for key, group in source_groups:
        market, ticker, date_value = key
        verified_group = group[group.apply(lambda row: _provenance_valid(row)[0], axis=1)]
        base_exists = key in candidate.index
        if not base_exists:
            if not allow_new_rows:
                unresolved.append({"market": market, "ticker": ticker, "date": date_value, "field": "ROW",
                                   "reason": "NEW_ROWS_DISABLED", "source_count": len(group)})
                continue
            if verified_group.empty or not bool(verified_group["calendar_verified"].all()):
                unresolved.append({"market": market, "ticker": ticker, "date": date_value, "field": "ROW",
                                   "reason": "NEW_ROW_REQUIRES_VERIFIED_SOURCE_AND_CALENDAR", "source_count": len(group)})
                continue
            reference = verified_group.iloc[0]
            conflicts = []
            for field in repair_fields:
                values = verified_group[field].dropna().tolist()
                if values and not all(_same_value(values[0], value) for value in values[1:]):
                    conflicts.append(field)
            ohlc_ok, ohlc_reason = _row_ohlc_valid(reference)
            values_ok = all(_is_valid_value(field, reference.get(field))[0] for field in repair_fields)
            if conflicts or not ohlc_ok or not values_ok:
                unresolved.append({"market": market, "ticker": ticker, "date": date_value, "field": "ROW",
                                   "reason": "VERIFIED_SOURCE_CONFLICT:" + ",".join(conflicts) if conflicts else ohlc_reason,
                                   "source_count": len(group)})
                continue
            new_record = {column: reference.get(column) for column in candidate.columns}
            candidate.loc[key, list(new_record)] = list(new_record.values())
            for field in repair_fields:
                audit.append({"market": market, "ticker": ticker, "date": date_value, "field": field,
                              "original_value": None, "replacement_value": reference[field],
                              "source_name": reference["source_name"], "source_uri": reference["source_uri"],
                              "source_retrieved_at": reference.get("source_retrieved_at", ""), "source_hash": reference["source_hash"],
                              "repair_reason": "CONFIRMED_MISSING_TRADING_DATE", "validation_result": "APPLIED_VERIFIED_NEW_ROW",
                              "applied": True})
            continue

        for field in repair_fields:
            original_value = candidate.loc[key, field]
            if isinstance(original_value, pd.Series):
                original_value = original_value.iloc[-1]
            verified_values = verified_group.loc[verified_group[field].notna(), field]
            comparison_status = "NO_VERIFIED_VALUE"
            if not verified_values.empty:
                comparison_status = "MATCH" if _same_value(original_value, verified_values.iloc[0]) else ("ORIGINAL_MISSING" if pd.isna(original_value) else "DIFFERENT")
            for _, source_row in group.iterrows():
                comparisons.append({"market": market, "ticker": ticker, "date": date_value, "field": field,
                                    "original_value": original_value, "source_value": source_row.get(field),
                                    "source_name": source_row.get("source_name", ""), "source_uri": source_row.get("source_uri", ""),
                                    "verification_status": source_row.get("verification_status", ""), "comparison_status": comparison_status})
            if pd.notna(original_value):
                if not verified_values.empty and not all(_same_value(original_value, value) for value in verified_values):
                    unresolved.append({"market": market, "ticker": ticker, "date": date_value, "field": field,
                                       "reason": "EXISTING_VALUE_CONFLICT_NOT_OVERWRITTEN", "source_count": len(group)})
                continue
            if verified_values.empty:
                provenance_reasons = sorted({_provenance_valid(row)[1] for _, row in group.iterrows()})
                unresolved.append({"market": market, "ticker": ticker, "date": date_value, "field": field,
                                   "reason": ";".join(provenance_reasons) or "NO_VERIFIED_SOURCE_VALUE", "source_count": len(group)})
                continue
            first_value = verified_values.iloc[0]
            if not all(_same_value(first_value, value) for value in verified_values):
                unresolved.append({"market": market, "ticker": ticker, "date": date_value, "field": field,
                                   "reason": "VERIFIED_SOURCE_CONFLICT", "source_count": len(verified_values)})
                continue
            valid, validation = _is_valid_value(field, first_value)
            source_row = verified_group[verified_group[field].notna()].iloc[0]
            if field in {"open", "high", "low", "close"}:
                valid, validation = _row_ohlc_valid(source_row)
            if not valid:
                unresolved.append({"market": market, "ticker": ticker, "date": date_value, "field": field,
                                   "reason": validation, "source_count": len(verified_values)})
                continue
            candidate.loc[key, field] = first_value
            audit.append({"market": market, "ticker": ticker, "date": date_value, "field": field,
                          "original_value": None, "replacement_value": first_value,
                          "source_name": source_row["source_name"], "source_uri": source_row["source_uri"],
                          "source_retrieved_at": source_row.get("source_retrieved_at", ""), "source_hash": source_row["source_hash"],
                          "repair_reason": "MISSING_ORIGINAL_CELL", "validation_result": validation, "applied": True})

    repaired = candidate.reset_index().sort_values(KEY_COLUMNS).reset_index(drop=True)
    audit_frame = pd.DataFrame(audit, columns=["market", "ticker", "date", "field", "original_value", "replacement_value",
                                                    "source_name", "source_uri", "source_retrieved_at", "source_hash",
                                                    "repair_reason", "validation_result", "applied"])
    comparison_frame = pd.DataFrame(comparisons, columns=["market", "ticker", "date", "field", "original_value", "source_value",
                                                                  "source_name", "source_uri", "verification_status", "comparison_status"])
    unresolved_frame = pd.DataFrame(unresolved, columns=["market", "ticker", "date", "field", "reason", "source_count"])
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "original_rows": int(len(base)), "candidate_rows": int(len(repaired)),
        "applied_cell_repairs": int(len(audit_frame)), "unresolved_cases": int(len(unresolved_frame)),
        "synthetic_or_interpolated_values_used": False, "original_overwritten": False,
        "repair_policy": "Only missing cells/rows from fully identified VERIFIED sources; existing valid values are preserved.",
    }
    return RepairResult(repaired, audit_frame, comparison_frame, unresolved_frame, summary)


def _frame_hash(frame: pd.DataFrame) -> str:
    payload = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_repair_outputs(result: RepairResult, output_dir: str | Path, *, candidate_path: str | Path | None = None) -> dict[str, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    target = Path(candidate_path) if candidate_path else output / "ETF_Daily_Master_repaired_candidate.csv"
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite an existing repair candidate: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.casefold() in {".parquet", ".pq"}:
        result.candidate.to_parquet(target, index=False)
    else:
        result.candidate.to_csv(target, index=False, encoding="utf-8")
    paths["candidate"] = target
    for name, frame in {
        "repair_audit.csv": result.repair_audit,
        "source_comparison.csv": result.source_comparison,
        "unresolved_cases.csv": result.unresolved_cases,
    }.items():
        path = output / name
        frame.to_csv(path, index=False, encoding="utf-8")
        paths[name] = path
    summary = dict(result.summary)
    summary["candidate_content_sha256"] = _frame_hash(result.candidate)
    summary_path = output / "repair_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    paths[summary_path.name] = summary_path
    return paths
