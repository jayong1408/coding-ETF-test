# -*- coding: utf-8 -*-
"""Repair historical gaps in ETF_Daily_Master.parquet without touching the source file.

Run in Google Colab. The script:
- backs up/reads the current master and optionally imports historical rows from yearly Excel sheets;
- re-downloads current-universe ETF histories to repair absent rows and null cells;
- never fabricates prices or silently substitutes Close for Adj Close;
- labels US close*volume as an estimate and Korean fallback values as proxies;
- writes a separate repaired candidate plus audit files; source master is never overwritten.
"""
from pathlib import Path
from datetime import date, timedelta, datetime
import logging
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.ERROR)

# Install only if missing, so the notebook can be rerun without reinstalling every package.
try:
    import yfinance as yf
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "yfinance", "pyarrow", "openpyxl", "pykrx"])
    import yfinance as yf

try:
    from google.colab import drive
    drive.mount("/content/drive", force_remount=False)
except ImportError:
    pass  # Allows local testing with paths changed below.

BASE = Path("/content/drive/MyDrive/ETF_Data")
INPUT = BASE / "input"
MASTER_DIR = BASE / "master"
REPORTS = BASE / "reports"
SOURCE_PARQUET = MASTER_DIR / "ETF_Daily_Master.parquet"
YEARLY_EXCEL = MASTER_DIR / "ETF_Daily_Master_By_Year.xlsx"
US_UNIVERSE_FILE = INPUT / "미국_전체_ETF_유니버스.xlsx"
KR_UNIVERSE_FILE = INPUT / "한국_전체_ETF_유니버스.xlsx"

# Output is deliberately separate from the source of truth.
OUTPUT_PARQUET = MASTER_DIR / "ETF_Daily_Master_repaired.parquet"
OUTPUT_EXCEL = MASTER_DIR / "ETF_Daily_Master_repaired.xlsx"
AUDIT_CSV = REPORTS / "master_repair_audit.csv"
FAILURES_CSV = REPORTS / "master_repair_failures.csv"
QUALITY_CSV = REPORTS / "master_repair_quality.csv"

KEYS = ["날짜", "시장", "티커/종목코드"]
CANONICAL_COLUMNS = ["날짜", "시장", "티커/종목코드", "종목명", "종가", "수정종가", "거래량", "거래대금"]
SLEEP_SECONDS = 0.12
CHECKPOINT_EVERY = 20

def find_col(df, candidates):
    norm = {str(c).strip().lower().replace(" ", ""): c for c in df.columns}
    for candidate in candidates:
        k = candidate.strip().lower().replace(" ", "")
        if k in norm:
            return norm[k]
    return None

def normalize_ticker(value, market):
    s = str(value).strip()
    if s.lower() in {"", "nan", "none", "nat"}:
        return ""
    if market == "KR":
        s = s.removesuffix(".0")
        return s.zfill(6)
    return s.upper()

def normalize_frame(df, origin):
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    aliases = {
        "date": "날짜", "market": "시장", "ticker": "티커/종목코드",
        "symbol": "티커/종목코드", "name": "종목명",
        "close": "종가", "adj close": "수정종가", "adj_close": "수정종가",
        "volume": "거래량", "trading_value": "거래대금", "traded_value": "거래대금",
    }
    rename = {}
    for c in df.columns:
        key = str(c).strip().lower().replace("_", " ")
        if key in aliases:
            rename[c] = aliases[key]
    df = df.rename(columns=rename)
    if not all(k in df.columns for k in KEYS):
        raise ValueError(f"{origin}: 필수 키 열이 없습니다. columns={list(df.columns)}")
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["시장"] = df["시장"].astype(str).str.strip().str.upper()
    df["티커/종목코드"] = [
        normalize_ticker(t, m) for t, m in zip(df["티커/종목코드"], df["시장"])
    ]
    df = df[df["날짜"].notna() & df["시장"].isin(["KR", "US"]) & df["티커/종목코드"].ne("")]
    for c in CANONICAL_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan if c not in KEYS + ["종목명"] else ""
    for c in ["종가", "수정종가", "거래량", "거래대금"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "trading_value_source" not in df.columns:
        df["trading_value_source"] = np.where(df["거래대금"].notna(), "legacy_unknown", "")
    df["data_origin"] = origin
    return df

def read_yearly_excel(path):
    if not path.exists():
        return pd.DataFrame()
    frames = []
    book = pd.ExcelFile(path)
    for sheet in book.sheet_names:
        try:
            raw = pd.read_excel(path, sheet_name=sheet)
            if all(find_col(raw, [c]) is not None for c in ["날짜", "시장", "티커/종목코드"]):
                frames.append(normalize_frame(raw, f"yearly_excel:{sheet}"))
        except Exception as exc:
            print(f"[WARN] Excel sheet skipped: {sheet}: {exc}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def load_universe(path, market):
    if not path.exists():
        print(f"[WARN] Universe missing: {path}")
        return pd.DataFrame(columns=["ticker", "name", "market"])
    raw = pd.read_excel(path)
    sc = find_col(raw, ["Symbol", "Ticker", "티커", "종목코드", "Code", "티커/종목코드"])
    nc = find_col(raw, ["Name", "종목명", "ETF명", "상품명"])
    if sc is None:
        raise ValueError(f"{path.name}: ticker/symbol column not found")
    out = pd.DataFrame({
        "ticker": [normalize_ticker(x, market) for x in raw[sc]],
        "name": raw[nc].fillna("").astype(str) if nc else "",
        "market": market,
    })
    return out[out["ticker"].ne("")].drop_duplicates(["market", "ticker"]).reset_index(drop=True)

def yf_symbol(ticker, market):
    if market == "US":
        return ticker
    if ticker.endswith((".KS", ".KQ")):
        return ticker
    return ticker + ".KS"

def download_history(ticker, market, start, end):
    symbol = yf_symbol(ticker, market)
    raw = yf.download(symbol, start=start, end=end, auto_adjust=False,
                      actions=False, progress=False, threads=False, repair=True)
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        # Handle both (field, ticker) and (ticker, field) yfinance layouts.
        if symbol in raw.columns.get_level_values(-1):
            raw.columns = raw.columns.get_level_values(0)
        elif symbol in raw.columns.get_level_values(0):
            raw.columns = raw.columns.get_level_values(1)
        else:
            raw.columns = raw.columns.get_level_values(0)
    raw = raw.reset_index()
    date_col = next((c for c in ["Date", "Datetime", "날짜"] if c in raw.columns), None)
    if date_col is None:
        return pd.DataFrame()
    for c in ["Close", "Adj Close", "Volume"]:
        if c not in raw.columns:
            raw[c] = np.nan
    out = pd.DataFrame({
        "날짜": pd.to_datetime(raw[date_col], errors="coerce").dt.strftime("%Y-%m-%d"),
        "시장": market,
        "티커/종목코드": ticker,
        "종가": pd.to_numeric(raw["Close"], errors="coerce"),
        "수정종가": pd.to_numeric(raw["Adj Close"], errors="coerce"),
        "거래량": pd.to_numeric(raw["Volume"], errors="coerce"),
    })
    out["거래대금"] = out["종가"] * out["거래량"]
    out["trading_value_source"] = "close_x_volume_estimate"
    out["data_origin"] = "yfinance_repair"
    return out.dropna(subset=["날짜"])

def download_kr_official(ticker, start, end):
    try:
        from pykrx import stock
        raw = stock.get_etf_ohlcv_by_date(start.replace("-", ""), end.replace("-", ""), ticker)
        if raw is None or raw.empty:
            return pd.DataFrame()
        raw = raw.reset_index()
        date_col = raw.columns[0]
        close_col = find_col(raw, ["종가"])
        volume_col = find_col(raw, ["거래량"])
        value_col = find_col(raw, ["거래대금"])
        if close_col is None:
            return pd.DataFrame()
        out = pd.DataFrame({
            "날짜": pd.to_datetime(raw[date_col], errors="coerce").dt.strftime("%Y-%m-%d"),
            "시장": "KR", "티커/종목코드": ticker,
            "종가_official": pd.to_numeric(raw[close_col], errors="coerce"),
            "거래량_official": pd.to_numeric(raw[volume_col], errors="coerce") if volume_col else np.nan,
            "거래대금_official": pd.to_numeric(raw[value_col], errors="coerce") if value_col else np.nan,
        })
        return out
    except Exception:
        return pd.DataFrame()

def merge_non_null(existing, incoming):
    """Incoming valid values repair blanks; never replace an existing valid value."""
    if existing.empty:
        return incoming.drop_duplicates(KEYS, keep="last")
    cols = list(dict.fromkeys(list(existing.columns) + list(incoming.columns)))
    a = existing.reindex(columns=cols).drop_duplicates(KEYS, keep="last").set_index(KEYS)
    b = incoming.reindex(columns=cols).drop_duplicates(KEYS, keep="last").set_index(KEYS)
    idx = a.index.union(b.index)
    a, b = a.reindex(idx), b.reindex(idx)
    out = a.copy()
    for c in cols:
        if c in KEYS:
            continue
        if c not in out:
            out[c] = np.nan
        if c not in b:
            continue
        # Keep the old valid cell; use new data only for a missing old cell.
        out[c] = out[c].combine_first(b[c])
    return out.reset_index()

def quality_report(df, label):
    records = []
    for market, group in df.groupby("시장", dropna=False):
        for col in ["종가", "수정종가", "거래량", "거래대금"]:
            records.append({"dataset": label, "market": market, "metric": col,
                            "rows": len(group), "nonnull": int(group[col].notna().sum()),
                            "missing": int(group[col].isna().sum()),
                            "missing_pct": round(float(group[col].isna().mean() * 100), 4)})
    return records

def main():
    for p in [INPUT, MASTER_DIR, REPORTS]:
        p.mkdir(parents=True, exist_ok=True)
    if not SOURCE_PARQUET.exists():
        raise FileNotFoundError(f"Source master not found: {SOURCE_PARQUET}")

    source = normalize_frame(pd.read_parquet(SOURCE_PARQUET), "source_parquet")
    source_rows = len(source)
    source_dupes = int(source.duplicated(KEYS).sum())
    source_start = source["날짜"].dropna().min()
    source_end = source["날짜"].dropna().max()
    start = source_start or "2021-01-01"
    end = (date.today() + timedelta(days=1)).isoformat()

    excel_rows = read_yearly_excel(YEARLY_EXCEL)
    # Combine sources with source parquet taking priority over legacy workbook values.
    combined = merge_non_null(source, excel_rows) if not excel_rows.empty else source.copy()

    us = load_universe(US_UNIVERSE_FILE, "US")
    kr = load_universe(KR_UNIVERSE_FILE, "KR")
    universe = pd.concat([kr, us], ignore_index=True).drop_duplicates(["market", "ticker"])
    if universe.empty:
        raise RuntimeError("No ETF universe available. Refusing to produce a repaired master.")

    # Include currently observed symbols even if the universe workbook is stale.
    observed = combined[["시장", "티커/종목코드", "종목명"]].drop_duplicates(["시장", "티커/종목코드"])
    extra = observed.rename(columns={"시장": "market", "티커/종목코드": "ticker", "종목명": "name"})
    universe = pd.concat([universe, extra[["ticker", "name", "market"]]], ignore_index=True)
    universe = universe.drop_duplicates(["market", "ticker"])

    audit, failures, incoming_frames = [], [], []
    for n, row in universe.iterrows():
        ticker, market = str(row["ticker"]), str(row["market"])
        try:
            hist = download_history(ticker, market, start, end)
            if hist.empty:
                failures.append({"market": market, "ticker": ticker, "stage": "yfinance",
                                 "reason": "empty response"})
                continue
            hist["종목명"] = str(row.get("name", "") or "")
            if market == "KR":
                official = download_kr_official(ticker, start, end)
                if not official.empty:
                    hist = hist.merge(official, on=KEYS, how="left")
                    hist["종가"] = hist["종가_official"].combine_first(hist["종가"])
                    hist["거래량"] = hist["거래량_official"].combine_first(hist["거래량"])
                    official_value = hist["거래대금_official"].notna()
                    hist["거래대금"] = hist["거래대금_official"].combine_first(hist["거래대금"])
                    hist["trading_value_source"] = np.where(
                        official_value, "pykrx_official", "close_x_volume_estimate")
                    hist = hist.drop(columns=["종가_official", "거래량_official", "거래대금_official"])
                else:
                    hist["trading_value_source"] = "close_x_volume_estimate_kr_fallback"
            existing_ticker = combined[
                (combined["시장"] == market) &
                (combined["티커/종목코드"] == ticker)
            ]
            # Count absent date-key rows and null cells before merge for audit.
            existing_keys = set(zip(existing_ticker["날짜"], existing_ticker["시장"], existing_ticker["티커/종목코드"]))
            missing_rows = sum((d, m, t) not in existing_keys for d, m, t in zip(hist["날짜"], hist["시장"], hist["티커/종목코드"]))
            audit.append({"market": market, "ticker": ticker, "download_rows": len(hist),
                          "existing_rows": len(existing_ticker), "new_date_rows_available": int(missing_rows),
                          "download_close_missing": int(hist["종가"].isna().sum()),
                          "download_adj_close_missing": int(hist["수정종가"].isna().sum()),
                          "download_volume_missing": int(hist["거래량"].isna().sum()),
                          "download_trading_value_missing": int(hist["거래대금"].isna().sum())})
            incoming_frames.append(hist)
        except Exception as exc:
            failures.append({"market": market, "ticker": ticker, "stage": "repair",
                             "reason": f"{type(exc).__name__}: {exc}"})
        if (n + 1) % CHECKPOINT_EVERY == 0:
            print(f"Progress: {n + 1}/{len(universe)} tickers; repaired candidates buffered={len(incoming_frames)}")
        time.sleep(SLEEP_SECONDS)

    incoming = pd.concat(incoming_frames, ignore_index=True) if incoming_frames else pd.DataFrame()
    repaired = merge_non_null(combined, incoming) if not incoming.empty else combined.copy()
    repaired = repaired.drop_duplicates(KEYS, keep="last").sort_values(KEYS).reset_index(drop=True)

    # Compatibility aliases used by older analysis scripts.
    repaired["adj_close"] = repaired["수정종가"]
    repaired["trading_value"] = repaired["거래대금"]

    if repaired[KEYS].isna().any().any():
        raise RuntimeError("Validation failed: null primary keys; source master remains untouched.")
    if repaired.duplicated(KEYS).any():
        raise RuntimeError("Validation failed: duplicate primary keys; source master remains untouched.")
    if len(repaired) < source_rows:
        raise RuntimeError(f"Validation failed: output rows ({len(repaired)}) < source rows ({source_rows}).")
    # Verify the written parquet by reading it back before declaring it a candidate.
    temp_path = OUTPUT_PARQUET.with_suffix(".parquet.tmp")
    repaired.to_parquet(temp_path, index=False)
    check = pd.read_parquet(temp_path)
    if len(check) != len(repaired) or check.duplicated(KEYS).any():
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("Read-back validation failed; source master remains untouched.")
    temp_path.replace(OUTPUT_PARQUET)

    # Excel output is a convenience export; Parquet remains the full-fidelity candidate.
    with pd.ExcelWriter(OUTPUT_EXCEL, engine="openpyxl") as writer:
        repaired["year"] = pd.to_datetime(repaired["날짜"]).dt.year
        for year, frame in repaired.groupby("year"):
            frame.drop(columns=["year"]).to_excel(writer, sheet_name=str(year), index=False)

    REPORTS.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(audit).to_csv(AUDIT_CSV, index=False, encoding="utf-8-sig")
    pd.DataFrame(failures, columns=["market", "ticker", "stage", "reason"]).to_csv(FAILURES_CSV, index=False, encoding="utf-8-sig")
    quality = quality_report(source, "source") + quality_report(repaired, "repaired_candidate")
    pd.DataFrame(quality).to_csv(QUALITY_CSV, index=False, encoding="utf-8-sig")

    print("\n=== REPAIR SUMMARY ===")
    print(f"Source rows: {source_rows:,}; source duplicate keys: {source_dupes:,}")
    print(f"Candidate rows: {len(repaired):,}; candidate duplicate keys: {int(repaired.duplicated(KEYS).sum()):,}")
    print(f"Date range: {repaired['날짜'].min()} .. {repaired['날짜'].max()}")
    print(f"Universe tickers attempted: {len(universe):,}; successful downloads: {len(incoming_frames):,}; failures: {len(failures):,}")
    print(f"Candidate Parquet: {OUTPUT_PARQUET}")
    print(f"Candidate Excel: {OUTPUT_EXCEL}")
    print(f"Audit: {AUDIT_CSV}")
    print(f"Failures: {FAILURES_CSV}")
    print(f"Quality report: {QUALITY_CSV}")
    print("NOTE: This is a validated candidate, not proof that every historical/delisted ETF/date has data.")
    print("NOTE: Source master was not overwritten. Promote the candidate only after reviewing all reports.")

if __name__ == "__main__":
    main()
