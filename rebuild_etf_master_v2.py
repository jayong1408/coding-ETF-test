#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Non-destructive ETF Daily Master V2 rebuild for Google Colab.

Real provider observations only. No synthetic fallback. V1 files are never deleted.
"""
from __future__ import annotations
import hashlib, json, logging, re, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd
import yfinance as yf

# --------------------------- SETTINGS ---------------------------
BASE = Path("/content/drive/MyDrive/ETF_Data")
INPUT = BASE / "input"
OUT = BASE / "rebuild_v2"
CHECKPOINT = OUT / "checkpoints"
LOGS = OUT / "logs"
KR_FILE = INPUT / "KR_ETF_Universe.xlsx"
US_FILE = INPUT / "US_ETF_Universe.xlsx"
FETCH_START = "1990-01-01"   # provider's earliest available history from this date
FETCH_END = None             # exclusive; None = latest available
BATCH_SIZE = 25
MAX_RETRIES = 3
SLEEP_SECONDS = 0.15
EXPORT_EXCEL = True
SAMPLE_MODE = True  # First run only the 7 known test ETFs; set False for full universe after validation.
SAMPLE_TICKERS = {
    ("KR", "360750"), ("KR", "069500"), ("KR", "102110"), ("KR", "252670"),
    ("US", "SPY"), ("US", "QQQ"), ("US", "IVV"),
}
EXCEL_DATA_ROWS = 1_000_000  # below Excel's 1,048,576 row limit
# ---------------------------------------------------------------

for p in (OUT, CHECKPOINT, LOGS):
    p.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=LOGS / "rebuild.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("yfinance").setLevel(logging.ERROR)
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
PRICE_OUT = OUT / "ETF_Daily_Master_v2.parquet"
ACTIONS_OUT = OUT / "ETF_Corporate_Actions_v2.parquet"
EXCEL_OUT = OUT / "ETF_Daily_Master_v2.xlsx"
STATUS_OUT = OUT / "ETF_Collection_Status_v2.csv"
FAIL_OUT = OUT / "collection_failures.csv"
QUALITY_OUT = OUT / "ETF_Data_Quality_Report_v2.csv"
MANIFEST_OUT = OUT / "run_manifest.json"
UNIVERSE_OUT = OUT / "universe_snapshot.csv"


def txt(v: Any) -> str:
    return "" if pd.isna(v) else str(v).strip()


def find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    cols = {re.sub(r"[\s_\-]", "", str(c)).casefold(): c for c in df.columns}
    for x in candidates:
        key = re.sub(r"[\s_\-]", "", x).casefold()
        if key in cols:
            return cols[key]
    return None


def normalize_ticker(v: Any, market: str) -> str:
    s = txt(v)
    if market == "KR" and re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    if market == "KR" and s.isdigit():
        s = s.zfill(6)
    return s


def normalize_provider_dates(values: pd.Series, market: str) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Preserve raw timestamps and normalize through the exchange timezone.

    The previous UTC-first conversion could move a Seoul-midnight session to
    the prior calendar day.  Aware timestamps are now converted to the exchange
    timezone before the timezone is removed.
    """
    exchange_timezone = "Asia/Seoul" if market == "KR" else "America/New_York"
    dates, raw_values, reasons, timezones = [], [], [], []
    for value in values:
        raw_values.append("" if pd.isna(value) else str(value))
        try:
            stamp = pd.Timestamp(value)
        except (TypeError, ValueError, OverflowError):
            stamp = pd.NaT
        if pd.isna(stamp):
            dates.append(pd.NaT); reasons.append("INVALID_OR_MISSING_DATE")
        elif stamp.tzinfo is not None:
            dates.append(stamp.tz_convert(exchange_timezone).tz_localize(None).normalize())
            reasons.append("TZ_AWARE_CONVERTED_TO_EXCHANGE_LOCAL")
        else:
            dates.append(stamp.normalize())
            reasons.append("DATE_ONLY_UNCHANGED" if stamp == stamp.normalize() else "NAIVE_TIMESTAMP_ASSUMED_EXCHANGE_LOCAL")
        timezones.append(exchange_timezone)
    return pd.Series(dates, index=values.index), pd.Series(raw_values, index=values.index), pd.Series(reasons, index=values.index), pd.Series(timezones, index=values.index)


def code_version() -> str:
    try:
        value = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
        return value or "UNKNOWN"
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def load_universe(path: Path, market: str) -> pd.DataFrame:
    if not path.exists():
        logging.warning("Missing universe file: %s", path)
        return pd.DataFrame(columns=["market", "ticker", "name", "currency", "universe_source_file"])
    src = pd.read_excel(path, dtype=object)
    tc = find_col(src, ["ticker", "symbol", "티커", "종목코드", "단축코드", "code", "yf_ticker"])
    nc = find_col(src, ["name", "종목명", "ETF명", "상품명", "fund_name"])
    cc = find_col(src, ["currency", "통화", "거래통화"])
    if tc is None:
        raise ValueError(f"{path.name}: ticker column not found. Columns={list(src.columns)}")
    rows = []
    for _, row in src.iterrows():
        ticker = normalize_ticker(row[tc], market)
        if not ticker or ticker.casefold() in {"nan", "none", "nat"}:
            continue
        rows.append({
            "market": market, "ticker": ticker,
            "name": txt(row[nc]) if nc else "",
            "currency": (txt(row[cc]) if cc else "") or ("KRW" if market == "KR" else "USD"),
            "universe_source_file": path.name,
        })
    return pd.DataFrame(rows).drop_duplicates(["market", "ticker"], keep="first").reset_index(drop=True)


def candidates(ticker: str, market: str) -> list[str]:
    if market != "KR" or ticker.endswith((".KS", ".KQ")):
        return [ticker]
    if re.fullmatch(r"\d{6}", ticker):
        return [ticker + ".KS", ticker + ".KQ"]
    return [ticker]


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df.columns, pd.MultiIndex):
        return df
    fields = {"Open", "High", "Low", "Close", "Adj Close", "Volume",
              "Dividends", "Stock Splits", "Capital Gains"}
    out = df.copy()
    out.columns = [
        next((str(part) for part in col if str(part) in fields),
             str(next(iter(col), "")))
        for col in df.columns
    ]
    return out


def download_one(item: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    last_error = "NO_DATA"
    for symbol in candidates(item["ticker"], item["market"]):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                raw = yf.download(
                    symbol, start=FETCH_START, end=FETCH_END, auto_adjust=False,
                    actions=True, repair=False, keepna=True, progress=False,
                    threads=False, group_by="column", timeout=30,
                )
                if raw is None or raw.empty:
                    last_error = f"EMPTY_RESPONSE:{symbol}"
                    break
                df = flatten_columns(raw).reset_index()
                date_col = "Date" if "Date" in df.columns else ("Datetime" if "Datetime" in df.columns else df.columns[0])
                df = df.rename(columns={
                    date_col: "date", "Open": "open", "High": "high", "Low": "low",
                    "Close": "close", "Adj Close": "adj_close", "Volume": "volume",
                    "Dividends": "dividends", "Stock Splits": "stock_splits",
                    "Capital Gains": "capital_gains",
                })
                if "date" not in df:
                    last_error = f"MISSING_DATE:{symbol}"
                    break
                normalized_dates = normalize_provider_dates(df["date"], item["market"])
                df["date"], df["date_raw"], df["date_normalization_reason"], df["exchange_timezone"] = normalized_dates
                df = df.dropna(subset=["date"]).copy()
                if df.empty:
                    last_error = f"NO_VALID_DATES:{symbol}"
                    break
                numeric = ["open", "high", "low", "close", "adj_close", "volume",
                           "dividends", "stock_splits", "capital_gains"]
                for col in numeric:
                    if col not in df:
                        df[col] = np.nan
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df["turnover_estimated"] = df["close"] * df["volume"]
                df["market"] = item["market"]
                df["ticker"] = item["ticker"]
                df["instrument_id"] = item["market"] + ":" + item["ticker"]
                df["name"] = item.get("name", "")
                df["provider_symbol"] = symbol
                df["currency"] = item.get("currency", "KRW" if item["market"] == "KR" else "USD")
                df["source"] = "Yahoo Finance via yfinance"
                df["retrieved_at_utc"] = datetime.now(timezone.utc).isoformat()
                df["run_id"] = RUN_ID
                df["data_status"] = "OBSERVED_PROVIDER_DATA"
                df = df.sort_values("date").drop_duplicates("date", keep="last")
                price_cols = [
                    "date", "market", "instrument_id", "ticker", "name", "provider_symbol",
                    "currency", "date_raw", "date_normalization_reason", "exchange_timezone",
                    "open", "high", "low", "close", "adj_close", "volume",
                    "turnover_estimated", "source", "retrieved_at_utc", "run_id", "data_status",
                ]
                action_cols = [
                    "date", "market", "instrument_id", "ticker", "provider_symbol",
                    "dividends", "stock_splits", "capital_gains", "source", "retrieved_at_utc", "run_id",
                ]
                prices = df[price_cols].copy()
                actions = df[action_cols].copy()
                actions = actions[actions[["dividends", "stock_splits", "capital_gains"]].fillna(0).ne(0).any(axis=1)].copy()
                return prices, actions, {
                    "market": item["market"], "ticker": item["ticker"], "name": item.get("name", ""),
                    "provider_symbol": symbol, "status": "SUCCESS", "download_status": "SUCCESS",
                    "quality_status": "PENDING", "rows": len(prices),
                    "first_date": prices["date"].min().date().isoformat(),
                    "last_date": prices["date"].max().date().isoformat(), "error": "",
                }
            except Exception as exc:
                last_error = f"{type(exc).__name__}:{str(exc)[:250]}"
                logging.warning("%s attempt %s failed: %s", symbol, attempt, last_error)
                if attempt < MAX_RETRIES:
                    time.sleep(attempt * 2)
        time.sleep(0.2)
    return pd.DataFrame(), pd.DataFrame(), {
        "market": item["market"], "ticker": item["ticker"], "name": item.get("name", ""),
        "provider_symbol": "", "status": "FAILED_NO_DATA", "download_status": "FAILED_NO_DATA",
        "quality_status": "NOT_EVALUATED", "rows": 0,
        "first_date": "", "last_date": "", "error": last_error,
    }


def atomic_parquet(df: pd.DataFrame, path: Path) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(temp, index=False)
    temp.replace(path)


def load_completed() -> set[str]:
    done = set()
    for path in CHECKPOINT.glob("status_*.csv"):
        try:
            df = pd.read_csv(path, dtype=str)
            if {"market", "ticker", "status"}.issubset(df.columns):
                status_col = "download_status" if "download_status" in df.columns else "status"
                ok = df[df[status_col] == "SUCCESS"]
                done.update(ok["market"] + ":" + ok["ticker"])
        except Exception:
            logging.exception("Could not read checkpoint status %s", path)
    return done


def export_excel(prices: pd.DataFrame, actions: pd.DataFrame, status: pd.DataFrame, quality: pd.DataFrame) -> None:
    # One workbook; price rows are split by year/market/part to respect Excel's row ceiling.
    try:
        with pd.ExcelWriter(EXCEL_OUT, engine="openpyxl") as writer:
            p = prices.copy()
            p["date"] = pd.to_datetime(p["date"]).dt.strftime("%Y-%m-%d")
            p["year"] = pd.to_datetime(p["date"]).dt.year.astype(str)
            for year, ydf in p.groupby("year", sort=True):
                ydf = ydf.drop(columns=["year"])
                for market, mdf in ydf.groupby("market", sort=True):
                    mdf = mdf.reset_index(drop=True)
                    n = max(1, int(np.ceil(len(mdf) / EXCEL_DATA_ROWS)))
                    for part in range(n):
                        mdf.iloc[part * EXCEL_DATA_ROWS:(part + 1) * EXCEL_DATA_ROWS].to_excel(
                            writer, sheet_name=f"P_{year}_{market}_{part+1}"[:31], index=False
                        )
            if not actions.empty:
                a = actions.copy()
                a["date"] = pd.to_datetime(a["date"]).dt.strftime("%Y-%m-%d")
                a.to_excel(writer, sheet_name="Corporate_Actions", index=False)
            if not status.empty:
                status.to_excel(writer, sheet_name="Collection_Status", index=False)
            quality.to_excel(writer, sheet_name="Data_Quality", index=False)
            pd.DataFrame([{
                "run_id": RUN_ID, "fetch_start": FETCH_START,
                "fetch_end_exclusive": FETCH_END or "latest available",
                "source": "Yahoo Finance via yfinance",
                "synthetic_data_allowed": False,
                "note": "Universe completeness and delisted ETF history require separate verification.",
            }]).to_excel(writer, sheet_name="README", index=False)
        print("Excel workbook:", EXCEL_OUT)
    except Exception:
        logging.exception("Excel export failed; Parquet remains canonical.")
        print("WARNING: Excel export failed; inspect logs. Parquet remains available.")


def main() -> None:
    parts = [load_universe(KR_FILE, "KR"), load_universe(US_FILE, "US")]
    universe = pd.concat([x for x in parts if not x.empty], ignore_index=True)
    if universe.empty:
        raise FileNotFoundError(f"No universe rows. Expected files in {INPUT}: {KR_FILE.name}, {US_FILE.name}")
    universe = universe.drop_duplicates(["market", "ticker"], keep="first").reset_index(drop=True)
    if SAMPLE_MODE:
        before_count = len(universe)
        universe = universe[
            universe.apply(lambda row: (row["market"], row["ticker"]) in SAMPLE_TICKERS, axis=1)
        ].reset_index(drop=True)
        found = set(zip(universe["market"], universe["ticker"]))
        missing_sample = sorted(SAMPLE_TICKERS - found)
        print(f"SAMPLE_MODE=True: selected {len(universe)} of {before_count} universe rows.")
        if missing_sample:
            print("Sample tickers absent from the supplied universe files:", missing_sample)
        if universe.empty:
            raise RuntimeError("No sample tickers matched the universe. Check ticker codes and input files.")
    else:
        print(f"SAMPLE_MODE=False: full universe selected ({len(universe):,} instruments).")
    universe.to_csv(UNIVERSE_OUT, index=False, encoding="utf-8-sig")

    universe_signature = hashlib.sha256(
        universe[["market", "ticker"]].sort_values(["market", "ticker"]).to_csv(index=False).encode("utf-8")
    ).hexdigest()
    prior_manifest = {}
    if MANIFEST_OUT.exists():
        try:
            prior_manifest = json.loads(MANIFEST_OUT.read_text(encoding="utf-8"))
        except Exception:
            prior_manifest = {}
    checkpoint_exists = any(CHECKPOINT.glob("batch_*.parquet")) or any(CHECKPOINT.glob("status_*.csv"))
    same_run_config = (
        prior_manifest.get("fetch_start") == FETCH_START
        and prior_manifest.get("fetch_end_exclusive") == FETCH_END
        and prior_manifest.get("universe_signature") == universe_signature
    )
    if checkpoint_exists and prior_manifest and not same_run_config:
        raise RuntimeError(
            "Existing checkpoints belong to a different date range or universe. "
            "To avoid mixing runs, move the existing rebuild_v2/checkpoints folder aside "
            "and archive run_manifest.json before starting a new rebuild."
        )

    manifest = {
        "run_id": prior_manifest.get("run_id", RUN_ID) if same_run_config else RUN_ID,
        "started_at_utc": prior_manifest.get("started_at_utc", datetime.now(timezone.utc).isoformat()) if same_run_config else datetime.now(timezone.utc).isoformat(),
        "fetch_start": FETCH_START, "fetch_end_exclusive": FETCH_END,
        "universe_rows": len(universe), "universe_signature": universe_signature,
        "source": "Yahoo Finance via yfinance",
        "synthetic_data_allowed": False, "output_directory": str(OUT),
        "warning": "This rebuild does not itself guarantee universe completeness or eliminate survivorship bias.",
    }
    MANIFEST_OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    completed = load_completed()
    todo = [r.to_dict() for _, r in universe.iterrows()
            if f"{r['market']}:{r['ticker']}" not in completed]
    batch_no = max([int(p.stem.split("_")[1]) for p in CHECKPOINT.glob("batch_*.parquet")] or [0])
    print(f"Universe={len(universe):,}; successful checkpoints={len(completed):,}; remaining={len(todo):,}")

    for offset in range(0, len(todo), BATCH_SIZE):
        batch = todo[offset:offset + BATCH_SIZE]
        pframes, aframes, statuses = [], [], []
        for item in batch:
            prices, actions, status = download_one(item)
            statuses.append(status)
            if not prices.empty:
                pframes.append(prices)
            if not actions.empty:
                aframes.append(actions)
            time.sleep(SLEEP_SECONDS)
        batch_no += 1
        if pframes:
            atomic_parquet(pd.concat(pframes, ignore_index=True), CHECKPOINT / f"batch_{batch_no:05d}.parquet")
        if aframes:
            atomic_parquet(pd.concat(aframes, ignore_index=True), CHECKPOINT / f"actions_{batch_no:05d}.parquet")
        pd.DataFrame(statuses).to_csv(CHECKPOINT / f"status_{batch_no:05d}.csv", index=False, encoding="utf-8-sig")
        print(f"Processed {min(offset + len(batch), len(todo)):,}/{len(todo):,} remaining; success={sum(s['status']=='SUCCESS' for s in statuses)}; failed={sum(s['status']!='SUCCESS' for s in statuses)}")

    price_files = sorted(CHECKPOINT.glob("batch_*.parquet"))
    if not price_files:
        raise RuntimeError("No observed prices collected. Final master was not written.")
    prices = pd.concat([pd.read_parquet(p) for p in price_files], ignore_index=True)
    prices["date"] = pd.to_datetime(prices["date"], errors="coerce")
    prices = prices.dropna(subset=["date", "market", "ticker"])
    duplicate_rows = prices[prices.duplicated(["market", "ticker", "date"], keep=False)].copy()
    prices = prices.sort_values(["market", "ticker", "date"]).drop_duplicates(["market", "ticker", "date"], keep="last").reset_index(drop=True)
    prices["quality_flag"] = ""
    prices.loc[prices["close"].isna() | (prices["close"] <= 0), "quality_flag"] += "INVALID_CLOSE;"
    prices.loc[prices["high"].notna() & prices["low"].notna() & (prices["high"] < prices["low"]), "quality_flag"] += "HIGH_LT_LOW;"
    prices.loc[prices["volume"].notna() & (prices["volume"] < 0), "quality_flag"] += "NEGATIVE_VOLUME;"
    quality = prices.groupby(["market", "ticker"], as_index=False).agg(
        rows=("date", "size"), first_date=("date", "min"), last_date=("date", "max"),
        missing_close=("close", lambda s: int(s.isna().sum())),
        missing_adj_close=("adj_close", lambda s: int(s.isna().sum())),
        missing_open=("open", lambda s: int(s.isna().sum())),
        missing_volume=("volume", lambda s: int(s.isna().sum())),
        flagged_rows=("quality_flag", lambda s: int((s != "").sum())),
    )
    duplicate_counts = (duplicate_rows.groupby(["market", "ticker"]).size().rename("duplicate_rows").reset_index()
                        if not duplicate_rows.empty else pd.DataFrame(columns=["market", "ticker", "duplicate_rows"]))
    quality = quality.merge(duplicate_counts, on=["market", "ticker"], how="left")
    quality["duplicate_rows"] = quality["duplicate_rows"].fillna(0).astype(int)
    blocking = quality[["missing_close", "missing_adj_close", "missing_open", "missing_volume", "flagged_rows", "duplicate_rows"]].sum(axis=1)
    quality["quality_status"] = np.where(blocking.gt(0), "BLOCKED", "CONDITIONAL")
    quality["quality_status_reason"] = np.where(blocking.gt(0), "DATA_QUALITY_ERRORS", "TRADING_CALENDAR_NOT_VERIFIED")
    status_files = sorted(CHECKPOINT.glob("status_*.csv"))
    statuses = pd.concat([pd.read_csv(p, dtype={"market": str, "ticker": str}) for p in status_files], ignore_index=True) if status_files else pd.DataFrame()
    if not statuses.empty:
        statuses = statuses.drop_duplicates(["market", "ticker"], keep="last")
        statuses = statuses.drop(columns=["quality_status"], errors="ignore").merge(
            quality[["market", "ticker", "quality_status"]], on=["market", "ticker"], how="left"
        )
        statuses["quality_status"] = statuses["quality_status"].fillna("NOT_EVALUATED")
        statuses.to_csv(STATUS_OUT, index=False, encoding="utf-8-sig")
        statuses[statuses["status"] != "SUCCESS"].to_csv(FAIL_OUT, index=False, encoding="utf-8-sig")
    quality.to_csv(QUALITY_OUT, index=False, encoding="utf-8-sig")
    (OUT / "ETF_Data_Quality_Report_v2.json").write_text(
        json.dumps(quality.to_dict(orient="records"), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    duplicate_rows.to_csv(OUT / "duplicate_keys_v2.csv", index=False, encoding="utf-8-sig")

    # Publish to new V2 filenames only. Never touch V1 files.
    atomic_parquet(prices, PRICE_OUT)
    action_files = sorted(CHECKPOINT.glob("actions_*.parquet"))
    actions = pd.concat([pd.read_parquet(p) for p in action_files], ignore_index=True) if action_files else pd.DataFrame(
        columns=["date", "market", "instrument_id", "ticker", "provider_symbol", "dividends", "stock_splits", "capital_gains", "source", "retrieved_at_utc", "run_id"]
    )
    if not actions.empty:
        actions = actions.drop_duplicates(["market", "ticker", "date", "provider_symbol"], keep="last")
    atomic_parquet(actions, ACTIONS_OUT)
    if EXPORT_EXCEL:
        export_excel(prices, actions, statuses, quality)

    manifest.update({
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "price_rows": int(len(prices)),
        "instrument_count": int(prices[["market", "ticker"]].drop_duplicates().shape[0]),
        "corporate_action_rows": int(len(actions)),
        "failed_instruments": int((statuses["status"] != "SUCCESS").sum()) if not statuses.empty else None,
        "quality_flagged_rows": int((prices["quality_flag"] != "").sum()),
        "duplicate_rows_detected_before_deduplication": int(len(duplicate_rows)),
        "code_version": code_version(),
        "price_output": str(PRICE_OUT), "actions_output": str(ACTIONS_OUT),
        "excel_output": str(EXCEL_OUT) if EXPORT_EXCEL else None,
    })
    MANIFEST_OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REBUILD COMPLETE")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
