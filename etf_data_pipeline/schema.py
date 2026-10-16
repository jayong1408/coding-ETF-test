"""Canonical schema and exchange-local date normalization.

The canonical key is ``market + ticker + date``.  Raw date text and the
normalization reason are retained so a shifted date can be audited later.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


KEY_COLUMNS = ["market", "ticker", "date"]
PRICE_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]
EXCHANGE_TIMEZONES = {"KR": "Asia/Seoul", "US": "America/New_York"}

ALIASES = {
    "날짜": "date",
    "date": "date",
    "datetime": "date",
    "시장": "market",
    "market": "market",
    "티커/종목코드": "ticker",
    "티커": "ticker",
    "종목코드": "ticker",
    "ticker": "ticker",
    "symbol": "ticker",
    "종목명": "name",
    "name": "name",
    "시가": "open",
    "open": "open",
    "고가": "high",
    "high": "high",
    "저가": "low",
    "low": "low",
    "종가": "close",
    "close": "close",
    "수정종가": "adj_close",
    "adj close": "adj_close",
    "adj_close": "adj_close",
    "거래량": "volume",
    "volume": "volume",
    "거래대금": "trading_value",
    "trading value": "trading_value",
    "trading_value": "trading_value",
    "배당": "dividends",
    "dividends": "dividends",
    "stock splits": "stock_splits",
    "stock_splits": "stock_splits",
    "capital gains": "capital_gains",
    "capital_gains": "capital_gains",
}


def _alias_key(value: Any) -> str:
    return str(value).strip().casefold().replace("_", " ")


def normalize_ticker(value: Any, market: str) -> str:
    if pd.isna(value):
        return ""
    ticker = str(value).strip()
    if ticker.casefold() in {"", "nan", "none", "nat"}:
        return ""
    if market == "KR":
        if ticker.endswith(".0") and ticker[:-2].isdigit():
            ticker = ticker[:-2]
        ticker = ticker.removesuffix(".KS").removesuffix(".KQ")
        if ticker.isdigit():
            ticker = ticker.zfill(6)
    return ticker.upper()


def normalize_date_value(value: Any, market: str) -> tuple[pd.Timestamp, str, str, str]:
    """Return normalized date, raw text, reason, and exchange timezone.

    A timezone-aware timestamp is converted to the exchange timezone before its
    timezone is removed.  This avoids the historical KR bug where Seoul
    midnight was converted to UTC and became the previous calendar date.
    """
    raw = "" if pd.isna(value) else str(value)
    timezone_name = EXCHANGE_TIMEZONES.get(market, "UTC")
    if pd.isna(value):
        return pd.NaT, raw, "INVALID_OR_MISSING_DATE", timezone_name
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return pd.NaT, raw, "INVALID_OR_MISSING_DATE", timezone_name
    if pd.isna(stamp):
        return pd.NaT, raw, "INVALID_OR_MISSING_DATE", timezone_name
    if stamp.tzinfo is not None:
        local = stamp.tz_convert(timezone_name).tz_localize(None)
        reason = "TZ_AWARE_CONVERTED_TO_EXCHANGE_LOCAL"
    else:
        local = stamp
        has_time = bool(local.hour or local.minute or local.second or local.microsecond)
        reason = "NAIVE_TIMESTAMP_ASSUMED_EXCHANGE_LOCAL" if has_time else "DATE_ONLY_UNCHANGED"
    return local.normalize(), raw, reason, timezone_name


def normalize_master(frame: pd.DataFrame, *, origin: str = "input") -> pd.DataFrame:
    """Normalize common V1/V2 Korean and English schemas without mutating input."""
    if frame is None:
        raise ValueError("frame is required")
    out = frame.copy()
    rename: dict[Any, str] = {}
    occupied = {str(c) for c in out.columns}
    for column in out.columns:
        canonical = ALIASES.get(_alias_key(column))
        if canonical and canonical not in occupied:
            rename[column] = canonical
            occupied.add(canonical)
        elif canonical and str(column) != canonical and canonical not in out.columns:
            rename[column] = canonical
    out = out.rename(columns=rename)
    missing_keys = [column for column in ["date", "market", "ticker"] if column not in out.columns]
    if missing_keys:
        raise ValueError(f"{origin}: missing key columns: {missing_keys}; columns={list(out.columns)}")

    out["market"] = out["market"].astype("string").str.strip().str.upper()
    out["ticker"] = [normalize_ticker(value, str(market)) for value, market in zip(out["ticker"], out["market"])]
    normalized = [normalize_date_value(value, str(market)) for value, market in zip(out["date"], out["market"])]
    generated_raw = pd.Series([item[1] for item in normalized], index=out.index)
    generated_reason = pd.Series([item[2] for item in normalized], index=out.index)
    generated_timezone = pd.Series([item[3] for item in normalized], index=out.index)
    out["date_raw"] = out["date_raw"].where(out["date_raw"].notna(), generated_raw) if "date_raw" in out else generated_raw
    out["date"] = pd.to_datetime([item[0] for item in normalized], errors="coerce")
    out["date_normalization_reason"] = (out["date_normalization_reason"].where(out["date_normalization_reason"].notna(), generated_reason)
                                         if "date_normalization_reason" in out else generated_reason)
    out["exchange_timezone"] = (out["exchange_timezone"].where(out["exchange_timezone"].notna(), generated_timezone)
                                if "exchange_timezone" in out else generated_timezone)
    out["data_origin"] = out.get("data_origin", origin)

    for column in PRICE_COLUMNS + ["trading_value", "dividends", "stock_splits", "capital_gains"]:
        if column not in out.columns:
            out[column] = np.nan
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if "name" not in out.columns:
        out["name"] = ""
    out["instrument_id"] = out["market"].astype(str) + ":" + out["ticker"].astype(str)
    return out


def read_table(path: str | Path, *, columns: list[str] | None = None) -> pd.DataFrame:
    """Read exchange data from CSV/Parquet/JSON; Excel is intentionally excluded.

    Large production masters should stay in Parquet.  CSV is provided for small
    handoff packages and test fixtures.  Avoiding Excel here prevents accidental
    all-sheet materialization in memory.
    """
    source = Path(path)
    suffix = source.suffix.casefold()
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(source, columns=columns)
    if suffix == ".csv":
        return pd.read_csv(source, usecols=columns, dtype={"ticker": "string", "티커/종목코드": "string"})
    if suffix in {".json", ".jsonl", ".ndjson"}:
        return pd.read_json(source, lines=suffix != ".json")
    if suffix in {".xlsx", ".xls"}:
        raise ValueError("Excel masters are not accepted by the pipeline; convert once to Parquet or CSV.")
    raise ValueError(f"Unsupported input format: {source}")


def iter_table_chunks(path: str | Path, *, batch_rows: int = 100_000):
    """Yield bounded DataFrames from Parquet or CSV without loading the master at once."""
    source = Path(path)
    suffix = source.suffix.casefold()
    if suffix in {".parquet", ".pq"}:
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError("pyarrow is required for streaming Parquet input") from exc
        parquet = pq.ParquetFile(source)
        for batch in parquet.iter_batches(batch_size=batch_rows):
            yield batch.to_pandas()
        return
    if suffix == ".csv":
        yield from pd.read_csv(source, chunksize=batch_rows, dtype={"ticker": "string", "티커/종목코드": "string"})
        return
    if suffix in {".xlsx", ".xls"}:
        raise ValueError("Excel masters are not accepted; convert once to Parquet or CSV.")
    yield read_table(source)


def normalize_calendar(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "market", "is_trading_day", "calendar_verified", "calendar_source"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Calendar is missing columns: {sorted(missing)}")
    out = frame.copy()
    out["market"] = out["market"].astype(str).str.upper().str.strip()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    for column in ["is_trading_day", "calendar_verified"]:
        out[column] = out[column].map(lambda value: str(value).casefold() in {"1", "true", "yes", "y"} if not isinstance(value, (bool, np.bool_)) else bool(value))
    return out.dropna(subset=["date", "market"]).drop_duplicates(["market", "date"], keep="last")
