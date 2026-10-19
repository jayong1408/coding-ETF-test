"""Fail-closed ETF price quality assessment and report generation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .schema import KEY_COLUMNS, PRICE_COLUMNS, inspect_master_contract, normalize_calendar, normalize_master


@dataclass(frozen=True)
class QualityConfig:
    required_price_fields: tuple[str, ...] = tuple(PRICE_COLUMNS)
    maximum_price: float | None = None
    return_outlier_abs: float = 0.50
    adjustment_factor_jump_abs: float = 0.20


@dataclass
class QualityResult:
    normalized: pd.DataFrame
    quality_summary_by_ticker: pd.DataFrame
    missing_cells: pd.DataFrame
    missing_dates: pd.DataFrame
    missing_runs: pd.DataFrame
    anomaly_dates: pd.DataFrame
    duplicate_keys: pd.DataFrame
    missing_summary_by_date: pd.DataFrame
    missing_summary_by_column: pd.DataFrame
    missing_summary_by_year: pd.DataFrame
    common_missing_dates: pd.DataFrame
    schema_issues: pd.DataFrame
    validation_results: dict[str, Any]


def _empty(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def _missing_runs(records: pd.DataFrame, sessions: dict[tuple[str, str], list[pd.Timestamp]]) -> pd.DataFrame:
    columns = ["market", "ticker", "field", "category", "start_date", "end_date", "consecutive_sessions"]
    if records.empty:
        return _empty(columns)
    output: list[dict[str, Any]] = []
    for (market, ticker, field, category), group in records.groupby(["market", "ticker", "field", "category"], dropna=False):
        dates = sorted(pd.to_datetime(group["date"], errors="coerce").dropna().unique())
        if not dates:
            continue
        ordered = sessions.get((str(market), str(ticker))) or list(dates)
        positions = {pd.Timestamp(value): index for index, value in enumerate(ordered)}
        current = [pd.Timestamp(dates[0])]
        for value in dates[1:]:
            value = pd.Timestamp(value)
            previous = current[-1]
            if positions.get(value, -99) == positions.get(previous, -101) + 1:
                current.append(value)
            else:
                output.append({"market": market, "ticker": ticker, "field": field, "category": category,
                               "start_date": current[0], "end_date": current[-1], "consecutive_sessions": len(current)})
                current = [value]
        output.append({"market": market, "ticker": ticker, "field": field, "category": category,
                       "start_date": current[0], "end_date": current[-1], "consecutive_sessions": len(current)})
    return pd.DataFrame(output, columns=columns)


def _add_anomaly(rows: list[dict[str, Any]], row: pd.Series, issue: str, severity: str, field: str, value: Any, detail: str) -> None:
    rows.append({
        "market": row.get("market"), "ticker": row.get("ticker"), "date": row.get("date"),
        "field": field, "issue": issue, "severity": severity, "value": value, "detail": detail,
    })


def summarize_common_missing_dates(
    missing_cells: pd.DataFrame,
    missing_dates: pd.DataFrame,
    quality_summary: pd.DataFrame,
) -> pd.DataFrame:
    """Aggregate dates where two or more instruments have missing evidence."""
    columns = [
        "market", "date", "category", "status", "affected_ticker_count",
        "market_ticker_count", "affected_ratio", "tickers", "fields", "record_count",
    ]
    records = pd.concat([
        missing_cells.reindex(columns=["market", "ticker", "date", "field", "category", "status"]),
        missing_dates.reindex(columns=["market", "ticker", "date", "field", "category", "status"]),
    ], ignore_index=True)
    if records.empty:
        return _empty(columns)
    market_counts = (quality_summary.groupby("market")["ticker"].nunique().to_dict()
                     if not quality_summary.empty else {})
    output: list[dict[str, Any]] = []
    for (market, date_value, category, status), group in records.groupby(
        ["market", "date", "category", "status"], dropna=False
    ):
        tickers = sorted({str(value) for value in group["ticker"].dropna()})
        if len(tickers) < 2:
            continue
        total = int(market_counts.get(market, len(tickers)))
        fields = sorted({str(value) for value in group["field"].dropna()})
        output.append({
            "market": market, "date": date_value, "category": category, "status": status,
            "affected_ticker_count": len(tickers), "market_ticker_count": total,
            "affected_ratio": len(tickers) / total if total else np.nan,
            "tickers": "|".join(tickers), "fields": "|".join(fields),
            "record_count": int(len(group)),
        })
    return pd.DataFrame(output, columns=columns)


def assess_quality(
    frame: pd.DataFrame,
    *,
    calendar: pd.DataFrame | None = None,
    actions: pd.DataFrame | None = None,
    config: QualityConfig | None = None,
    origin: str = "master",
    sample_data: bool = False,
) -> QualityResult:
    config = config or QualityConfig()
    schema_issues = inspect_master_contract(frame, origin=origin)
    data = normalize_master(frame, origin=origin)
    valid_key_mask = data[KEY_COLUMNS].notna().all(axis=1) & data["ticker"].ne("") & data["market"].isin(["KR", "US"])
    invalid_key_count = int((~valid_key_mask).sum())
    duplicates = data[data.duplicated(KEY_COLUMNS, keep=False)].copy().sort_values(KEY_COLUMNS)

    missing_rows: list[dict[str, Any]] = []
    for field in config.required_price_fields:
        for _, row in data[data[field].isna()].iterrows():
            missing_rows.append({"market": row["market"], "ticker": row["ticker"], "date": row["date"],
                                 "field": field, "category": "MISSING_CELL", "status": "CONFIRMED"})
    missing_cells = pd.DataFrame(missing_rows, columns=["market", "ticker", "date", "field", "category", "status"])

    anomaly_rows: list[dict[str, Any]] = []
    for _, row in data.iterrows():
        for field in ["open", "high", "low", "close", "adj_close"]:
            value = row[field]
            if pd.notna(value) and value <= 0:
                _add_anomaly(anomaly_rows, row, "NON_POSITIVE_PRICE", "BLOCKED", field, value, "price must be positive")
            elif pd.notna(value) and config.maximum_price is not None and value > config.maximum_price:
                _add_anomaly(anomaly_rows, row, "PRICE_ABOVE_CONFIGURED_MAX", "BLOCKED", field, value,
                             f"price exceeds configured maximum {config.maximum_price}")
        if pd.notna(row["volume"]) and row["volume"] < 0:
            _add_anomaly(anomaly_rows, row, "NEGATIVE_VOLUME", "BLOCKED", "volume", row["volume"], "volume must be non-negative")
        if pd.notna(row["open"]) and all(pd.isna(row[field]) for field in ["high", "low", "close"]):
            _add_anomaly(anomaly_rows, row, "OPEN_ONLY_PRICE_PRESENT", "BLOCKED", "open", row["open"],
                         "open is present while high, low, and close are all missing")
        if pd.isna(row["open"]) and any(pd.notna(row[field]) for field in ["high", "low", "close"]):
            _add_anomaly(anomaly_rows, row, "OPEN_MISSING_WITH_OTHER_OHLC_PRESENT", "BLOCKED", "open", row["open"],
                         "open is missing while another OHLC value is present")
        if pd.notna(row["high"]) and pd.notna(row["low"]) and row["high"] < row["low"]:
            _add_anomaly(anomaly_rows, row, "HIGH_BELOW_LOW", "BLOCKED", "high", row["high"], "high < low")
        comparable = [row[x] for x in ["open", "low", "close"] if pd.notna(row[x])]
        if pd.notna(row["high"]) and comparable and row["high"] < max(comparable):
            _add_anomaly(anomaly_rows, row, "HIGH_BELOW_OHLC_MEMBER", "BLOCKED", "high", row["high"], "high below open/low/close")
        comparable = [row[x] for x in ["open", "high", "close"] if pd.notna(row[x])]
        if pd.notna(row["low"]) and comparable and row["low"] > min(comparable):
            _add_anomaly(anomaly_rows, row, "LOW_ABOVE_OHLC_MEMBER", "BLOCKED", "low", row["low"], "low above open/high/close")

    action_keys: set[tuple[str, str, pd.Timestamp]] = set()
    if actions is not None and not actions.empty:
        action_data = normalize_master(actions, origin="corporate_actions")
        action_mask = action_data[["dividends", "stock_splits", "capital_gains"]].fillna(0).ne(0).any(axis=1)
        action_keys = set(map(tuple, action_data.loc[action_mask, KEY_COLUMNS].itertuples(index=False, name=None)))
    ordered = data.sort_values(KEY_COLUMNS).copy()
    ordered["adjustment_factor"] = ordered["adj_close"] / ordered["close"]
    ordered["adjustment_factor_change"] = ordered.groupby(["market", "ticker"])["adjustment_factor"].pct_change(fill_method=None)
    ordered["adjusted_return"] = ordered.groupby(["market", "ticker"])["adj_close"].pct_change(fill_method=None)
    for _, row in ordered.iterrows():
        key = (row["market"], row["ticker"], row["date"])
        change = row["adjustment_factor_change"]
        if pd.notna(change) and abs(change) > config.adjustment_factor_jump_abs and key not in action_keys:
            _add_anomaly(anomaly_rows, row, "ADJ_FACTOR_JUMP_UNVERIFIED", "CONDITIONAL", "adj_close", change,
                         "adjustment factor changed without a supplied corporate-action record")
        adjusted_return = row["adjusted_return"]
        if pd.notna(adjusted_return) and abs(adjusted_return) > config.return_outlier_abs:
            _add_anomaly(anomaly_rows, row, "ADJUSTED_RETURN_OUTLIER", "CONDITIONAL", "adj_close", adjusted_return,
                         f"absolute adjusted return exceeds {config.return_outlier_abs:.0%}")
    anomalies = pd.DataFrame(anomaly_rows, columns=["market", "ticker", "date", "field", "issue", "severity", "value", "detail"])

    calendar_status_by_market: dict[str, str] = {}
    missing_date_rows: list[dict[str, Any]] = []
    sessions: dict[tuple[str, str], list[pd.Timestamp]] = {}
    normalized_calendar = None
    if calendar is not None and not calendar.empty:
        normalized_calendar = normalize_calendar(calendar)
    for (market, ticker), group in data[valid_key_mask].groupby(["market", "ticker"]):
        observed_dates = sorted(pd.to_datetime(group["date"]).dropna().unique())
        if not observed_dates:
            continue
        key = (str(market), str(ticker))
        if normalized_calendar is None:
            calendar_status_by_market[str(market)] = "NOT_PROVIDED"
            sessions[key] = [pd.Timestamp(value) for value in observed_dates]
            continue
        market_calendar = normalized_calendar[(normalized_calendar["market"] == market) & normalized_calendar["is_trading_day"]]
        verified = market_calendar[market_calendar["calendar_verified"]]
        if verified.empty:
            calendar_status_by_market[str(market)] = "NOT_VERIFIED"
            sessions[key] = [pd.Timestamp(value) for value in observed_dates]
            continue
        expected = verified[(verified["date"] >= observed_dates[0]) & (verified["date"] <= observed_dates[-1])]
        calendar_status_by_market[str(market)] = "VERIFIED"
        sessions[key] = list(expected["date"].sort_values())
        observed_set = {pd.Timestamp(value) for value in observed_dates}
        for _, cal_row in expected.iterrows():
            date_value = pd.Timestamp(cal_row["date"])
            if date_value not in observed_set:
                missing_date_rows.append({
                    "market": market, "ticker": ticker, "date": date_value, "field": "ROW",
                    "category": "CONFIRMED_MISSING_TRADING_DATE", "status": "CONFIRMED",
                    "calendar_source": cal_row["calendar_source"], "calendar_verified": True,
                })
    missing_dates = pd.DataFrame(missing_date_rows, columns=["market", "ticker", "date", "field", "category", "status", "calendar_source", "calendar_verified"])

    run_records = pd.concat([
        missing_cells[["market", "ticker", "date", "field", "category"]],
        missing_dates[["market", "ticker", "date", "field", "category"]],
    ], ignore_index=True)
    runs = _missing_runs(run_records, sessions)

    summaries: list[dict[str, Any]] = []
    duplicate_counts = duplicates.groupby(["market", "ticker"]).size() if not duplicates.empty else pd.Series(dtype=int)
    missing_cell_counts = missing_cells.groupby(["market", "ticker"]).size() if not missing_cells.empty else pd.Series(dtype=int)
    missing_date_counts = missing_dates.groupby(["market", "ticker"]).size() if not missing_dates.empty else pd.Series(dtype=int)
    blocked_counts = anomalies[anomalies["severity"] == "BLOCKED"].groupby(["market", "ticker"]).size() if not anomalies.empty else pd.Series(dtype=int)
    conditional_counts = anomalies[anomalies["severity"] == "CONDITIONAL"].groupby(["market", "ticker"]).size() if not anomalies.empty else pd.Series(dtype=int)
    schema_blocked_by_ticker = (schema_issues[(schema_issues["severity"] == "BLOCKED") & schema_issues["market"].notna() & schema_issues["ticker"].notna()]
                                .groupby(["market", "ticker"]).size() if not schema_issues.empty else pd.Series(dtype=int))
    global_schema_blocked = int(((schema_issues["severity"] == "BLOCKED") & (schema_issues["scope"] == "DATASET")).sum()) if not schema_issues.empty else 0
    for (market, ticker), group in data[valid_key_mask].groupby(["market", "ticker"]):
        key = (market, ticker)
        schema_blocking = int(schema_blocked_by_ticker.get(key, 0)) + global_schema_blocked
        blocking = int(duplicate_counts.get(key, 0) + missing_cell_counts.get(key, 0) + missing_date_counts.get(key, 0) + blocked_counts.get(key, 0) + schema_blocking)
        conditional = int(conditional_counts.get(key, 0))
        calendar_status = calendar_status_by_market.get(str(market), "NOT_PROVIDED")
        quality_status = "BLOCKED" if blocking else ("CONDITIONAL" if conditional or calendar_status != "VERIFIED" else "PASS")
        summaries.append({
            "market": market, "ticker": ticker, "first_date": group["date"].min(), "last_date": group["date"].max(),
            "rows": int(len(group)), "missing_cells": int(missing_cell_counts.get(key, 0)),
            "confirmed_missing_dates": int(missing_date_counts.get(key, 0)), "duplicate_rows": int(duplicate_counts.get(key, 0)),
            "blocked_anomalies": int(blocked_counts.get(key, 0)), "conditional_anomalies": conditional,
            "schema_violations": schema_blocking,
            "calendar_status": calendar_status, "quality_status": quality_status,
        })
    quality_summary = pd.DataFrame(summaries)

    combined_missing = pd.concat([
        missing_cells.assign(year=pd.to_datetime(missing_cells["date"]).dt.year),
        missing_dates.assign(year=pd.to_datetime(missing_dates["date"]).dt.year),
    ], ignore_index=True)
    if combined_missing.empty:
        by_date = _empty(["market", "ticker", "date", "missing_count"])
        by_column = _empty(["market", "ticker", "field", "missing_count"])
        by_year = _empty(["market", "ticker", "year", "missing_cell_count", "missing_date_count"])
    else:
        by_date = combined_missing.groupby(["market", "ticker", "date"], as_index=False).size().rename(columns={"size": "missing_count"})
        by_column = combined_missing.groupby(["market", "ticker", "field"], as_index=False).size().rename(columns={"size": "missing_count"})
        cells_year = missing_cells.assign(year=pd.to_datetime(missing_cells["date"]).dt.year).groupby(["market", "ticker", "year"], as_index=False).size().rename(columns={"size": "missing_cell_count"})
        dates_year = missing_dates.assign(year=pd.to_datetime(missing_dates["date"]).dt.year).groupby(["market", "ticker", "year"], as_index=False).size().rename(columns={"size": "missing_date_count"})
        by_year = cells_year.merge(dates_year, on=["market", "ticker", "year"], how="outer").fillna(0)
        for column in ["missing_cell_count", "missing_date_count"]:
            by_year[column] = by_year[column].astype(int)

    common_missing = summarize_common_missing_dates(missing_cells, missing_dates, quality_summary)

    status_counts = quality_summary["quality_status"].value_counts().to_dict() if not quality_summary.empty else {}
    validation_results = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "actual_data_run": not sample_data,
        "sample_data": sample_data,
        "input_rows": int(len(frame)),
        "normalized_rows": int(len(data)),
        "invalid_key_rows": invalid_key_count,
        "duplicate_rows": int(len(duplicates)),
        "missing_cells": int(len(missing_cells)),
        "confirmed_missing_dates": int(len(missing_dates)),
        "anomaly_rows": int(len(anomalies)),
        "schema_issue_rows": int(len(schema_issues)),
        "common_missing_dates": int(len(common_missing)),
        "quality_status_counts": {str(key): int(value) for key, value in status_counts.items()},
        "quality_rule_config": {
            "maximum_price": config.maximum_price,
            "return_outlier_abs": config.return_outlier_abs,
            "adjustment_factor_jump_abs": config.adjustment_factor_jump_abs,
            "threshold_policy": "Return and adjustment thresholds create review findings; no default absolute maximum price is assumed.",
        },
        "calendar_rule": "Only dates from a supplied calendar row with calendar_verified=true are classified as missing trading dates.",
        "checks": {
            "key_completeness": {"passed": invalid_key_count == 0, "count": invalid_key_count},
            "required_schema_and_types": {"passed": schema_issues.empty, "count": int(len(schema_issues))},
            "duplicate_keys": {"passed": len(duplicates) == 0, "count": int(len(duplicates))},
            "required_price_fields": {"passed": len(missing_cells) == 0, "count": int(len(missing_cells))},
            "confirmed_trading_dates": {"passed": len(missing_dates) == 0, "count": int(len(missing_dates)), "requires_verified_calendar": True},
            "price_and_ohlc_rules": {"passed": not bool((anomalies["severity"] == "BLOCKED").any()) if not anomalies.empty else True,
                                     "count": int((anomalies["severity"] == "BLOCKED").sum()) if not anomalies.empty else 0},
            "adjustment_and_return_review": {"passed": not bool((anomalies["severity"] == "CONDITIONAL").any()) if not anomalies.empty else True,
                                             "count": int((anomalies["severity"] == "CONDITIONAL").sum()) if not anomalies.empty else 0},
        },
    }
    return QualityResult(
        normalized=data, quality_summary_by_ticker=quality_summary,
        missing_cells=missing_cells, missing_dates=missing_dates, missing_runs=runs,
        anomaly_dates=anomalies, duplicate_keys=duplicates,
        missing_summary_by_date=by_date, missing_summary_by_column=by_column,
        missing_summary_by_year=by_year, common_missing_dates=common_missing,
        schema_issues=schema_issues, validation_results=validation_results,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if pd.isna(value):
        return None
    raise TypeError(type(value).__name__)


def write_quality_reports(result: QualityResult, output_dir: str | Path) -> dict[str, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    frames = {
        "quality_summary_by_ticker.csv": result.quality_summary_by_ticker,
        "missing_cells.csv": result.missing_cells,
        "missing_dates.csv": result.missing_dates,
        "missing_runs.csv": result.missing_runs,
        "anomaly_dates.csv": result.anomaly_dates,
        "duplicate_keys.csv": result.duplicate_keys,
        "missing_summary_by_date.csv": result.missing_summary_by_date,
        "missing_summary_by_column.csv": result.missing_summary_by_column,
        "missing_summary_by_year.csv": result.missing_summary_by_year,
        "common_missing_dates.csv": result.common_missing_dates,
        "schema_issues.csv": result.schema_issues,
    }
    paths: dict[str, Path] = {}
    for name, frame in frames.items():
        path = output / name
        frame.to_csv(path, index=False, encoding="utf-8")
        paths[name] = path
    json_path = output / "validation_results.json"
    json_path.write_text(json.dumps(result.validation_results, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    paths[json_path.name] = json_path
    ticker_json = output / "quality_summary_by_ticker.json"
    ticker_json.write_text(result.quality_summary_by_ticker.to_json(orient="records", date_format="iso", force_ascii=False, indent=2), encoding="utf-8")
    paths[ticker_json.name] = ticker_json
    return paths
