# ETF data collector V2

This branch adds a safer collector without overwriting the existing script or changing the default branch.

## Files
- `collector_v2.py`: replacement collection workflow for Google Colab.
- Existing files are intentionally left unchanged.

## Before running
1. Open Google Colab and connect the intended Google account.
2. Upload/run `collector_v2.py` in a Python cell or run it from a Colab cell with `%run /content/collector_v2.py`.
3. Put `미국_전체_ETF_유니버스.xlsx` in `MyDrive/ETF_Data/input/`. The Korean universe is rebuilt from pykrx where possible; if that fails, the existing `한국_전체_ETF_유니버스.xlsx` is used.
4. Confirm the output folder is `MyDrive/ETF_Data/`. The script does not clear or delete anything under `/content/drive`.
5. Run on a copy or ensure there is sufficient Drive storage. The script backs up the current Parquet master before replacing it.

## Data handling
- Date range defaults to 2021-01-01 through tomorrow (end date is exclusive).
- Keys: `날짜 + 시장 + 티커/종목코드`.
- Preserves `종가` and `수정종가` separately and writes aliases `adj_close` and `trading_value` for compatibility.
- Korea: attempts to retrieve the current ETF listing and official daily OHLCV/traded value through pykrx. If official turnover retrieval fails, it falls back to a close-times-volume proxy and marks the source.
- US: yfinance provides Close, Adj Close and Volume. `거래대금` is only estimated as Close × Volume and marked `close_x_volume_proxy`; it is not official exchange turnover.
- Saves a raw batch, a failed-ticker CSV, and a backup of the prior master.
- Does not fill missing adjusted prices with closing prices.
- Upserts non-null incoming values by the three-part key and checks for duplicate keys before replacing the master.

## Important limitations
- The collector has not been executed against the user's Google Drive or current ETF universe in this repository branch. Verify row counts, coverage, missing adjusted prices and turnover-source counts after running.
- The current ETF list is survivorship-biased: ETFs already delisted and absent from today's universe are not automatically recovered. Add a historical/delisted universe if backtests require it.
- A successful download is not the same as a validated Alpha V1 dataset. Run the project's data-quality checks and Forward Test after collection.
- Do not treat US close-times-volume proxies as official trading value or feed them into Alpha's liquidity factor without an explicit, validated policy.

## Security notice
The legacy script contains KRX credential-like values in explanatory text. If these correspond to a real account, change the password and remove secrets from the repository/history. Use Colab Secrets for credentials; this V2 collector does not require KRX login credentials.
