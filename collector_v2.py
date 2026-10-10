# -*- coding: utf-8 -*-
"""ETF Daily Master collector V2 for Google Colab.

Safety/data rules:
- Never delete or clear /content/drive before mounting Google Drive.
- Preserve raw Close and Adj Close separately.
- Korean ETF traded value comes from pykrx when available.
- US traded value is explicitly a close*volume proxy, NOT official exchange turnover.
- Merge on (날짜, 시장, 티커/종목코드); new non-null values update old values.
- Existing master is backed up before writing.
"""

from pathlib import Path
from datetime import date, datetime, timedelta
import time
import warnings
import logging
import re
import os
import shutil
import subprocess
import sys

subprocess.check_call([
    sys.executable, "-m", "pip", "install", "-q",
    "yfinance", "pyarrow", "openpyxl", "pandas", "numpy", "tqdm", "pykrx"
])

import numpy as np
import pandas as pd
import yfinance as yf
from google.colab import drive

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.ERROR)

# =========================
# Configuration
# =========================
print("Google Drive mounting (no pre-mount deletion is performed).")
drive.mount("/content/drive", force_remount=False)

BASE = Path("/content/drive/MyDrive/ETF_Data")
INPUT = BASE / "input"
MASTER = BASE / "master"
RAW = BASE / "raw"
LOGS = BASE / "logs"
for folder in (INPUT, MASTER, RAW, LOGS):
    folder.mkdir(parents=True, exist_ok=True)

US_UNIVERSE_FILE = INPUT / "미국_전체_ETF_유니버스.xlsx"
DAILY_FILE = MASTER / "ETF_Daily_Master.parquet"
KR_UNIVERSE_FILE = INPUT / "한국_전체_ETF_유니버스.xlsx"

# Match the historical database horizon; change only if intentionally rebuilding
FETCH_START = "2021-01-01"
FETCH_END = (date.today() + timedelta(days=1)).isoformat()  # yfinance end is exclusive
CHECKPOINT_EVERY = 25
SLEEP_SECONDS = 0.15

# =========================
# Helpers
# =========================
def find_col(df, candidates):
    cols = {str(c).strip().lower().replace(" ", ""): c for c in df.columns}
    for candidate in candidates:
        key = candidate.lower().replace(" ", "")
        if key in cols:
            return cols[key]
    return None

def load_us_universe(path):
    if not path.exists():
        raise FileNotFoundError(
            f"미국 ETF 유니버스 파일이 없습니다: {path}. "
            "ETF_Data/input 폴더에 미국_전체_ETF_유니버스.xlsx를 넣으세요."
        )
    src = pd.read_excel(path)
    sym_col = find_col(src, ["Symbol", "Ticker", "티커", "종목코드", "Code"])
    name_col = find_col(src, ["Name", "종목명", "ETF명", "상품명"])
    if sym_col is None:
        raise ValueError(f"미국 유니버스에서 티커 열을 찾지 못했습니다. 열={list(src.columns)}")
    out = pd.DataFrame()
    out["ticker"] = src[sym_col].astype(str).str.strip().str.upper()
    out["name"] = src[name_col].astype(str).str.strip() if name_col else ""
    out["market"] = "US"
    out = out[out.ticker.ne("") & out.ticker.str.lower().ne("nan")]
    return out.drop_duplicates("ticker").reset_index(drop=True)

def load_or_build_kr_universe():
    # Prefer the live KRX ETF list from pykrx; retry recent calendar dates for holidays.
    try:
        from pykrx import stock
        tickers = []
        listing_date = None
        for offset in range(0, 11):
            d = (date.today() - timedelta(days=offset)).strftime("%Y%m%d")
            try:
                candidate = stock.get_etf_ticker_list(date=d)
                if candidate:
                    tickers, listing_date = list(candidate), d
                    break
            except Exception:
                pass
        if tickers:
            rows = [{"ticker": str(t).zfill(6),
                     "name": stock.get_etf_ticker_name(str(t).zfill(6)),
                     "market": "KR"} for t in tickers]
            df = pd.DataFrame(rows).drop_duplicates("ticker")
            df.to_excel(KR_UNIVERSE_FILE, index=False)
            print(f"한국 ETF 유니버스 갱신: {len(df)}개 (기준일 {listing_date})")
            return df.reset_index(drop=True)
        print("경고: pykrx에서 한국 ETF 상장목록을 얻지 못했습니다.")
    except Exception as e:
        print(f"경고: pykrx ETF 유니버스 수집 실패: {e}")

    if KR_UNIVERSE_FILE.exists():
        old = pd.read_excel(KR_UNIVERSE_FILE)
        sym_col = find_col(old, ["Symbol", "Ticker", "티커", "종목코드", "Code"])
        name_col = find_col(old, ["Name", "종목명", "ETF명", "상품명"])
        if sym_col is None:
            raise ValueError("기존 한국 ETF 유니버스에 티커 열이 없습니다.")
        df = pd.DataFrame({
            "ticker": old[sym_col].astype(str).str.replace(r"\\.0$", "", regex=True).str.zfill(6),
            "name": old[name_col].astype(str) if name_col else ""
        })
        df["market"] = "KR"
        return df.drop_duplicates("ticker").reset_index(drop=True)
    raise RuntimeError("한국 ETF 유니버스를 생성하지 못했습니다. pykrx 연결을 확인하세요.")

def yf_symbol(ticker, market):
    ticker = str(ticker).strip()
    if market == "US":
        return ticker
    if ticker.endswith((".KS", ".KQ")):
        return ticker
    return ticker + ".KS"

def download_us_or_adjusted(ticker, market):
    symbol = yf_symbol(ticker, market)
    try:
        df = yf.download(
            symbol, start=FETCH_START, end=FETCH_END, auto_adjust=False,
            actions=False, progress=False, threads=False, repair=True
        )
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            # yfinance versions may return either (field,ticker) or (ticker,field)
            if symbol in df.columns.get_level_values(-1):
                df.columns = df.columns.get_level_values(0)
            else:
                df.columns = df.columns.get_level_values(-1)
        df = df.reset_index()
        date_col = "Date" if "Date" in df.columns else "Datetime"
        if date_col not in df.columns:
            return None
        for c in ["Close", "Adj Close", "Volume"]:
            if c not in df.columns:
                df[c] = np.nan
        out = pd.DataFrame({
            "날짜": pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d"),
            "시장": market,
            "티커/종목코드": str(ticker).zfill(6) if market == "KR" else str(ticker),
            "종목명": "",
            "종가": pd.to_numeric(df["Close"], errors="coerce"),
            "수정종가": pd.to_numeric(df["Adj Close"], errors="coerce"),
            "거래량": pd.to_numeric(df["Volume"], errors="coerce"),
        })
        # Only a proxy for US securities. KR official turnover is added from pykrx below.
        out["거래대금"] = out["종가"] * out["거래량"]
        out["trading_value_source"] = "close_x_volume_proxy"
        return out
    except Exception:
        return None

def download_korean_official(ticker):
    try:
        from pykrx import stock
        raw = stock.get_etf_ohlcv_by_date(
            FETCH_START.replace("-", ""), FETCH_END.replace("-", ""),
            str(ticker).zfill(6)
        )
        if raw is None or raw.empty:
            return None
        raw = raw.reset_index()
        date_col = raw.columns[0]
        close_col = find_col(raw, ["종가"])
        volume_col = find_col(raw, ["거래량"])
        value_col = find_col(raw, ["거래대금"])
        if close_col is None:
            return None
        out = pd.DataFrame({
            "날짜": pd.to_datetime(raw[date_col]).dt.strftime("%Y-%m-%d"),
            "시장": "KR",
            "티커/종목코드": str(ticker).zfill(6),
            "종가_krx": pd.to_numeric(raw[close_col], errors="coerce"),
            "거래량_krx": pd.to_numeric(raw[volume_col], errors="coerce") if volume_col else np.nan,
            "거래대금_krx": pd.to_numeric(raw[value_col], errors="coerce") if value_col else np.nan,
        })
        return out
    except Exception:
        return None

def load_existing():
    if not DAILY_FILE.exists():
        return pd.DataFrame()
    old = pd.read_parquet(DAILY_FILE)
    # Normalize common historical schema aliases without deleting source columns.
    aliases = {
        "date": "날짜", "market": "시장", "ticker": "티커/종목코드",
        "close": "종가", "adj_close": "수정종가", "adj close": "수정종가",
        "volume": "거래량", "trading_value": "거래대금"
    }
    for src, dst in aliases.items():
        if src in old.columns and dst not in old.columns:
            old[dst] = old[src]
    required = ["날짜", "시장", "티커/종목코드"]
    missing = [c for c in required if c not in old.columns]
    if missing:
        raise ValueError(f"기존 마스터에 키 열이 없습니다: {missing}")
    old["날짜"] = pd.to_datetime(old["날짜"], errors="coerce").dt.strftime("%Y-%m-%d")
    old["시장"] = old["시장"].astype(str).str.upper()
    old["티커/종목코드"] = old["티커/종목코드"].astype(str).str.strip()
    return old

def upsert(old, new):
    if new.empty:
        return old
    key = ["날짜", "시장", "티커/종목코드"]
    for df in (old, new):
        for k in key:
            df[k] = df[k].astype(str)
    all_cols = list(dict.fromkeys(list(old.columns) + list(new.columns)))
    old = old.reindex(columns=all_cols)
    new = new.reindex(columns=all_cols)
    # Last duplicate within incoming batch wins; preserve non-null old values when new is missing.
    new = new.drop_duplicates(key, keep="last")
    if old.empty:
        return new.sort_values(key).reset_index(drop=True)
    old = old.drop_duplicates(key, keep="last").set_index(key)
    new = new.set_index(key)
    old, new = old.align(new, join="outer", axis=1)
    # Merge keys are the index; incoming non-null cells replace old cells.
    merged = old.combine_first(new)
    # combine_first keeps old first, so overwrite with non-null incoming values explicitly.
    merged.update(new)
    return merged.reset_index().sort_values(key).reset_index(drop=True)

# =========================
# Run
# =========================
us = load_us_universe(US_UNIVERSE_FILE)
kr = load_or_build_kr_universe()
universe = pd.concat([kr, us], ignore_index=True).drop_duplicates(["market", "ticker"])
print(f"수집 대상: KR {len(kr):,}개 / US {len(us):,}개 / 합계 {len(universe):,}개")
print(f"수집 기간: {FETCH_START} ~ {FETCH_END} (종료일 미포함)")

old_master = load_existing()
if DAILY_FILE.exists():
    backup = MASTER / f"ETF_Daily_Master_backup_{datetime.now():%Y%m%d_%H%M%S}.parquet"
    shutil.copy2(DAILY_FILE, backup)
    print(f"기존 마스터 백업: {backup}")

checkpoint_path = RAW / "collector_v2_checkpoint.parquet"
failed_path = LOGS / "collector_v2_failed.csv"
if checkpoint_path.exists():
    collected = pd.read_parquet(checkpoint_path)
    done = set(collected["시장"].astype(str) + ":" + collected["티커/종목코드"].astype(str))
    print(f"체크포인트 재개: {len(done)}개 종목")
else:
    collected = pd.DataFrame()
    done = set()

failed = []
for i, row in universe.iterrows():
    ticker, market = str(row["ticker"]), str(row["market"])
    key = f"{market}:{ticker}"
    if key in done:
        continue

    prices = download_us_or_adjusted(ticker, market)
    if prices is None or prices.empty:
        failed.append({"시장": market, "티커/종목코드": ticker, "종목명": row.get("name", ""), "사유": "yfinance 데이터 없음/다운로드 실패"})
        done.add(key)
        continue

    prices["종목명"] = row.get("name", "")
    if market == "KR":
        official = download_korean_official(ticker)
        if official is not None and not official.empty:
            prices = prices.merge(official, on=["날짜", "시장", "티커/종목코드"], how="left")
            prices["종가"] = prices["종가_krx"].combine_first(prices["종가"])
            prices["거래량"] = prices["거래량_krx"].combine_first(prices["거래량"])
            prices["거래대금"] = prices["거래대금_krx"].combine_first(prices["거래대금"])
            prices["trading_value_source"] = np.where(
                prices["거래대금_krx"].notna(), "pykrx_official", prices["trading_value_source"]
            )
            prices = prices.drop(columns=["종가_krx", "거래량_krx", "거래대금_krx"])
        else:
            prices["trading_value_source"] = "close_x_volume_proxy_fallback"
    prices["adj_close"] = prices["수정종가"]
    prices["trading_value"] = prices["거래대금"]
    collected = pd.concat([collected, prices], ignore_index=True)
    done.add(key)

    if len(done) % CHECKPOINT_EVERY == 0:
        collected.to_parquet(checkpoint_path, index=False)
        print(f"진행: {len(done):,}/{len(universe):,} 종목, 신규 행 {len(collected):,}")

    time.sleep(SLEEP_SECONDS)

if failed:
    pd.DataFrame(failed).to_csv(failed_path, index=False, encoding="utf-8-sig")
else:
    pd.DataFrame(columns=["시장", "티커/종목코드", "종목명", "사유"]).to_csv(failed_path, index=False, encoding="utf-8-sig")

if not collected.empty:
    # Save raw batch separately for audit; then merge with master.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    collected.to_parquet(RAW / f"ETF_Daily_collected_{stamp}.parquet", index=False)
    merged = upsert(old_master, collected)
    # Validate keys before replacing master.
    if merged.duplicated(["날짜", "시장", "티커/종목코드"]).any():
        raise RuntimeError("검증 실패: 마스터에 날짜/시장/티커 중복이 있습니다. 기존 파일을 유지합니다.")
    tmp = DAILY_FILE.with_suffix(".parquet.tmp")
    merged.to_parquet(tmp, index=False)
    pd.read_parquet(tmp)  # read-back verification
    tmp.replace(DAILY_FILE)
    print(f"마스터 저장/재읽기 검증 완료: {len(merged):,}행")
else:
    print("신규 데이터가 없습니다. 기존 마스터는 변경하지 않았습니다.")

if checkpoint_path.exists():
    checkpoint_path.unlink()

print("수집 작업 종료.")
print(f"실패/미수집 종목 목록: {failed_path}")
print("주의: 미국 trading_value는 종가×거래량 추정치입니다. Alpha의 유동성 점수에 공식 거래대금처럼 사용하지 마세요.")
