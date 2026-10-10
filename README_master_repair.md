# ETF master historical gap repair

This work branch is based on `fix/etf-data-collector-v2`. It does not change `main`, the Alpha V1 algorithm, or the existing master file.

## What it does

Run `repair_etf_master.py` in Google Colab to create a repaired *candidate* from the existing `ETF_Daily_Master.parquet`:

- reads and normalizes the current Parquet master;
- optionally imports rows from `master/ETF_Daily_Master_By_Year.xlsx` where the workbook has the required date/market/ticker key columns;
- re-downloads history for tickers in the Korean and US universe workbooks and symbols already present in the master;
- uses actual non-null data to fill absent rows and missing cells, without interpolation or random/mock values;
- preserves `종가` and `수정종가` separately;
- marks US close × volume as an estimate, not official exchange turnover; Korean official turnover is used when available and fallback is labeled;
- validates key columns, duplicate keys, row-count non-regression and Parquet read-back;
- writes a separate candidate and audit reports. **It never overwrites the source master.**

## Required Drive layout

```
MyDrive/ETF_Data/
  input/
    미국_전체_ETF_유니버스.xlsx
    한국_전체_ETF_유니버스.xlsx
  master/
    ETF_Daily_Master.parquet
    ETF_Daily_Master_By_Year.xlsx   # optional historical backup
  reports/
```

## Outputs

- `master/ETF_Daily_Master_repaired.parquet` — primary candidate
- `master/ETF_Daily_Master_repaired.xlsx` — year-sheet convenience export
- `reports/master_repair_audit.csv` — per-ticker downloaded rows and available new date rows
- `reports/master_repair_failures.csv` — tickers/errors not repaired
- `reports/master_repair_quality.csv` — missingness before/after by market and key fields

## Important limits

- A current-universe workbook cannot recover all delisted ETFs that are absent from both the workbook and historical master. Survivorship bias remains unless a historical/delisted universe is supplied.
- A successful HTTP/data-provider response does not prove all values are correct. Inspect failures, missingness and source labels before promoting the candidate.
- This repairs historical gaps by re-fetching source data; it does not invent values, fill prices forward/backward, or treat missing adjusted closes as closes.
- Alpha V1 is not modified. Do not use estimated US turnover as official turnover in a liquidity factor without an explicit validated policy.
- The script has not yet been executed against the user's live Drive. Repository-side code presence is not the same as successful production validation.
