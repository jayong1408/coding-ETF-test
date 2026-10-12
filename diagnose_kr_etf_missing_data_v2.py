#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Targeted Yahoo Finance diagnostic for suspected Korean ETF date/field gaps.

This script does not alter ETF_Daily_Master_v2 or fill missing values.
It saves provider responses and a compact diagnostic report to Drive.
"""
from pathlib import Path
from datetime import datetime, timezone
import json
import pandas as pd
import numpy as np
import yfinance as yf

BASE = Path("/content/drive/MyDrive/ETF_Data")
OUT = BASE / "diagnostics_v2"
OUT.mkdir(parents=True, exist_ok=True)

# Period end is exclusive. Windows include the dates reported by the user.
TARGETS = [
    {"ticker": "069500", "symbol": "069500.KS", "market": "KR", "start": "2023-07-01", "end": "2023-07-18", "focus": "common full-row gaps"},
    {"ticker": "102110", "symbol": "102110.KS", "market": "KR", "start": "2023-07-01", "end": "2023-07-18", "focus": "common full-row gaps"},
    {"ticker": "252670", "symbol": "252670.KS", "market": "KR", "start": "2023-07-01", "end": "2023-07-18", "focus": "common full-row gaps"},
    {"ticker": "360750", "symbol": "360750.KS", "market": "KR", "start": "2022-12-20", "end": "2023-01-03", "focus": "open-only gaps"},
]
FIELDS = ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits", "Capital Gains"]
# User-reported dates to explicitly check for missing rows in the provider response.
EXPECTED_DATES = {
    "069500": ["2023-07-03", "2023-07-04", "2023-07-05", "2023-07-11", "2023-07-12", "2023-07-13"],
    "102110": ["2023-07-03", "2023-07-04", "2023-07-05", "2023-07-11", "2023-07-12", "2023-07-13"],
    "252670": ["2023-07-03", "2023-07-04", "2023-07-05", "2023-07-11", "2023-07-12", "2023-07-13"],
    "360750": ["2022-12-22", "2022-12-23", "2022-12-26", "2022-12-27", "2022-12-28", "2022-12-29"],
}

def flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        wanted = set(FIELDS)
        df = df.copy()
        df.columns = [next((str(p) for p in col if str(p) in wanted), str(col[0])) for col in df.columns]
    return df

def main():
    reports, raw_files = [], []
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for item in TARGETS:
        row = dict(item)
        try:
            raw = yf.download(
                item["symbol"], start=item["start"], end=item["end"],
                auto_adjust=False, actions=True, repair=False, keepna=True,
                progress=False, threads=False, group_by="column", timeout=30,
            )
            if raw is None or raw.empty:
                row.update({"status": "EMPTY_RESPONSE", "rows": 0, "error": "Provider returned no rows"})
                reports.append(row)
                continue
            raw = flatten(raw).reset_index()
            date_col = "Date" if "Date" in raw.columns else ("Datetime" if "Datetime" in raw.columns else raw.columns[0])
            raw = raw.rename(columns={date_col: "date"})
            raw["date"] = pd.to_datetime(raw["date"], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
            for f in FIELDS:
                if f not in raw.columns:
                    raw[f] = np.nan
            # Explicitly detect dates that are absent as rows, not just rows with null fields.
            returned_dates = set(raw["date"].dropna().dt.strftime("%Y-%m-%d"))
            for expected_date in EXPECTED_DATES.get(item["ticker"], []):
                if expected_date not in returned_dates:
                    reports.append({
                        "market": "KR", "ticker": item["ticker"], "provider_symbol": item["symbol"],
                        "date": expected_date, "status": "DATE_ROW_ABSENT_FROM_PROVIDER_RESPONSE",
                        "missing_fields": "NO_ROW_RETURNED", "open": np.nan, "high": np.nan,
                        "low": np.nan, "close": np.nan, "adj_close": np.nan, "volume": np.nan,
                        "window_start": item["start"], "window_end_exclusive": item["end"],
                        "raw_file": "", "error": "Expected date was not present in returned rows; verify exchange calendar and provider coverage.",
                    })
            raw["market"] = item["market"]
            raw["ticker"] = item["ticker"]
            raw["provider_symbol"] = item["symbol"]
            raw["diagnostic_run_id"] = run_id
            raw["retrieved_at_utc"] = datetime.now(timezone.utc).isoformat()
            raw_path = OUT / f"raw_{item['ticker']}_{item['start']}_{item['end']}.csv"
            raw.to_csv(raw_path, index=False, encoding="utf-8-sig")
            raw_files.append(str(raw_path))
            for _, r in raw.iterrows():
                vals = {f: (pd.to_numeric(pd.Series([r[f]]), errors="coerce").iloc[0]) for f in FIELDS}
                missing = [f for f in ["Open", "High", "Low", "Close", "Adj Close", "Volume"] if pd.isna(vals[f])]
                if missing:
                    reports.append({
                        "market": "KR", "ticker": item["ticker"], "provider_symbol": item["symbol"],
                        "date": r["date"].date().isoformat() if pd.notna(r["date"]) else "",
                        "status": "ROW_WITH_MISSING_FIELDS", "missing_fields": ";".join(missing),
                        "open": vals["Open"], "high": vals["High"], "low": vals["Low"],
                        "close": vals["Close"], "adj_close": vals["Adj Close"], "volume": vals["Volume"],
                        "window_start": item["start"], "window_end_exclusive": item["end"],
                        "raw_file": str(raw_path), "error": "",
                    })
            row.update({"status": "SUCCESS", "rows": len(raw), "raw_file": str(raw_path), "error": ""})
            reports.append(row)
        except Exception as e:
            row.update({"status": "ERROR", "rows": 0, "error": f"{type(e).__name__}: {str(e)[:300]}"})
            reports.append(row)
    pd.DataFrame(reports).to_csv(OUT / "ETF_Missing_Data_Diagnostic_v2.csv", index=False, encoding="utf-8-sig")
    manifest = {
        "run_id": run_id, "source": "Yahoo Finance via yfinance",
        "synthetic_data_allowed": False, "raw_files": raw_files,
        "report": str(OUT / "ETF_Missing_Data_Diagnostic_v2.csv"),
        "note": "A successful response only describes the provider response at this retrieval time; it does not prove the provider was correct historically. No missing prices are filled.",
    }
    (OUT / "diagnostic_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(pd.DataFrame(reports).to_string(index=False))

if __name__ == "__main__":
    main()
