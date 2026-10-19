"""Disk-staged, per-instrument quality execution for large masters."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

import pandas as pd

from .quality import QualityConfig, QualityResult, assess_quality, summarize_common_missing_dates
from .schema import KEY_COLUMNS, inspect_master_contract, iter_table_chunks, normalize_master


def _concat(results: list[QualityResult], attribute: str) -> pd.DataFrame:
    frames = [getattr(result, attribute) for result in results if not getattr(result, attribute).empty]
    if frames:
        return pd.concat(frames, ignore_index=True)
    if results:
        return getattr(results[0], attribute).iloc[0:0].copy()
    return pd.DataFrame()


def assess_quality_streaming(
    path: str | Path,
    *,
    calendar: pd.DataFrame | None = None,
    actions: pd.DataFrame | None = None,
    config: QualityConfig | None = None,
    sample_data: bool = False,
    batch_rows: int = 100_000,
    staging_dir: str | Path | None = None,
) -> QualityResult:
    """Stage bounded input chunks on disk, then assess one instrument at a time."""
    config = config or QualityConfig()
    stage_parent = Path(staging_dir) if staging_dir else None
    if stage_parent:
        stage_parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix="etf_quality_stage_", suffix=".sqlite", dir=stage_parent, delete=False)
    stage_path = Path(handle.name)
    handle.close()
    input_rows = 0
    invalid_key_rows = 0
    results: list[QualityResult] = []
    schema_issue_parts: list[pd.DataFrame] = []
    connection = sqlite3.connect(stage_path)
    try:
        with connection:
            first = True
            for number, chunk in enumerate(iter_table_chunks(path, batch_rows=batch_rows), start=1):
                chunk_origin = f"{Path(path).resolve()}#chunk={number}"
                issues = inspect_master_contract(chunk, origin=chunk_origin)
                if not issues.empty:
                    issues = issues.copy()
                    issues["input_chunk"] = number
                    schema_issue_parts.append(issues)
                normalized = normalize_master(chunk, origin=chunk_origin)
                input_rows += len(chunk)
                valid = normalized[KEY_COLUMNS].notna().all(axis=1) & normalized["ticker"].ne("") & normalized["market"].isin(["KR", "US"])
                invalid_key_rows += int((~valid).sum())
                normalized.to_sql("master", connection, if_exists="replace" if first else "append", index=False, chunksize=10_000)
                first = False
            if first:
                raise ValueError("Input master contains no rows")
            instruments = pd.read_sql_query(
                "SELECT DISTINCT market, ticker FROM master WHERE market IN ('KR','US') AND ticker IS NOT NULL AND ticker <> '' ORDER BY market, ticker",
                connection,
            )
            for _, instrument in instruments.iterrows():
                market, ticker = str(instrument["market"]), str(instrument["ticker"])
                frame = pd.read_sql_query(
                    "SELECT * FROM master WHERE market = ? AND ticker = ? ORDER BY date", connection, params=(market, ticker)
                )
                action_subset = None
                if actions is not None and not actions.empty:
                    action_columns = set(actions.columns)
                    market_col = "market" if "market" in action_columns else ("시장" if "시장" in action_columns else None)
                    ticker_col = "ticker" if "ticker" in action_columns else ("티커/종목코드" if "티커/종목코드" in action_columns else None)
                    if market_col and ticker_col:
                        action_subset = actions[(actions[market_col].astype(str).str.upper() == market) &
                                                (actions[ticker_col].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6 if market == "KR" else 1) == ticker)]
                    else:
                        action_subset = actions
                results.append(assess_quality(frame, calendar=calendar, actions=action_subset, config=config,
                                              origin=str(Path(path).resolve()), sample_data=sample_data))
    finally:
        connection.close()
        stage_path.unlink(missing_ok=True)

    quality_summary = _concat(results, "quality_summary_by_ticker")
    missing_cells = _concat(results, "missing_cells")
    missing_dates = _concat(results, "missing_dates")
    missing_runs = _concat(results, "missing_runs")
    anomalies = _concat(results, "anomaly_dates")
    duplicates = _concat(results, "duplicate_keys")
    by_date = _concat(results, "missing_summary_by_date")
    by_column = _concat(results, "missing_summary_by_column")
    by_year = _concat(results, "missing_summary_by_year")
    schema_issues = pd.concat(schema_issue_parts, ignore_index=True) if schema_issue_parts else (
        results[0].schema_issues.iloc[0:0].copy() if results else pd.DataFrame()
    )
    if not schema_issues.empty:
        dataset_mask = schema_issues["scope"].eq("DATASET")
        dataset_issues = schema_issues[dataset_mask].drop_duplicates(["field", "issue", "detail"])
        schema_issues = pd.concat([dataset_issues, schema_issues[~dataset_mask]], ignore_index=True)
        global_count = int(schema_issues["scope"].eq("DATASET").sum())
        row_counts = schema_issues[~schema_issues["scope"].eq("DATASET")].groupby(["market", "ticker"]).size()
        if "schema_violations" not in quality_summary:
            quality_summary["schema_violations"] = 0
        for index, row in quality_summary.iterrows():
            count = global_count + int(row_counts.get((row["market"], row["ticker"]), 0))
            quality_summary.at[index, "schema_violations"] = int(quality_summary.at[index, "schema_violations"]) + count
            if count:
                quality_summary.at[index, "quality_status"] = "BLOCKED"
    common_missing = summarize_common_missing_dates(missing_cells, missing_dates, quality_summary)
    blocking_anomalies = int((anomalies["severity"] == "BLOCKED").sum()) if not anomalies.empty else 0
    conditional_anomalies = int((anomalies["severity"] == "CONDITIONAL").sum()) if not anomalies.empty else 0
    status_counts = quality_summary["quality_status"].value_counts().to_dict() if not quality_summary.empty else {}
    validation: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "STREAMING_DISK_STAGED_BY_INSTRUMENT",
        "actual_data_run": not sample_data, "sample_data": sample_data,
        "input_rows": int(input_rows), "normalized_rows": int(input_rows), "invalid_key_rows": int(invalid_key_rows),
        "duplicate_rows": int(len(duplicates)), "missing_cells": int(len(missing_cells)),
        "confirmed_missing_dates": int(len(missing_dates)), "anomaly_rows": int(len(anomalies)),
        "schema_issue_rows": int(len(schema_issues)), "common_missing_dates": int(len(common_missing)),
        "quality_status_counts": {str(key): int(value) for key, value in status_counts.items()},
        "quality_rule_config": {
            "maximum_price": config.maximum_price,
            "return_outlier_abs": config.return_outlier_abs,
            "adjustment_factor_jump_abs": config.adjustment_factor_jump_abs,
            "threshold_policy": "Return and adjustment thresholds create review findings; no default absolute maximum price is assumed.",
        },
        "calendar_rule": "Only dates from a supplied calendar row with calendar_verified=true are classified as missing trading dates.",
        "checks": {
            "key_completeness": {"passed": invalid_key_rows == 0, "count": int(invalid_key_rows)},
            "required_schema_and_types": {"passed": schema_issues.empty, "count": int(len(schema_issues))},
            "duplicate_keys": {"passed": len(duplicates) == 0, "count": int(len(duplicates))},
            "required_price_fields": {"passed": len(missing_cells) == 0, "count": int(len(missing_cells))},
            "confirmed_trading_dates": {"passed": len(missing_dates) == 0, "count": int(len(missing_dates)), "requires_verified_calendar": True},
            "price_and_ohlc_rules": {"passed": blocking_anomalies == 0, "count": blocking_anomalies},
            "adjustment_and_return_review": {"passed": conditional_anomalies == 0, "count": conditional_anomalies},
        },
    }
    return QualityResult(
        normalized=pd.DataFrame(), quality_summary_by_ticker=quality_summary,
        missing_cells=missing_cells, missing_dates=missing_dates, missing_runs=missing_runs,
        anomaly_dates=anomalies, duplicate_keys=duplicates,
        missing_summary_by_date=by_date, missing_summary_by_column=by_column,
        missing_summary_by_year=by_year, common_missing_dates=common_missing,
        schema_issues=schema_issues, validation_results=validation,
    )
