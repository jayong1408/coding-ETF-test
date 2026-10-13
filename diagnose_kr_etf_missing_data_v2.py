#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Multi-path diagnostic for Korean ETF historical gaps.
Does not modify the master, invent prices, or fill missing values.
"""
from pathlib import Path
from datetime import datetime, timezone
import json, time
import pandas as pd
import numpy as np
import yfinance as yf
import requests

BASE = Path("/content/drive/MyDrive/ETF_Data")
OUT = BASE / "diagnostics_v3"
OUT.mkdir(parents=True, exist_ok=True)
TARGETS = [
    {"ticker":"069500","symbol":"069500.KS","start":"2023-07-01","end":"2023-07-18","dates":["2023-07-03","2023-07-04","2023-07-05","2023-07-11","2023-07-12","2023-07-13"]},
    {"ticker":"102110","symbol":"102110.KS","start":"2023-07-01","end":"2023-07-18","dates":["2023-07-03","2023-07-04","2023-07-05","2023-07-11","2023-07-12","2023-07-13"]},
    {"ticker":"252670","symbol":"252670.KS","start":"2023-07-01","end":"2023-07-18","dates":["2023-07-03","2023-07-04","2023-07-05","2023-07-11","2023-07-12","2023-07-13"]},
    {"ticker":"360750","symbol":"360750.KS","start":"2022-12-20","end":"2023-01-03","dates":["2022-12-22","2022-12-23","2022-12-26","2022-12-27","2022-12-28","2022-12-29"]},
]
FIELDS = ["Open","High","Low","Close","Adj Close","Volume","Dividends","Stock Splits","Capital Gains"]
PRICE_FIELDS = ["Open","High","Low","Close","Adj Close","Volume"]

def normalize_frame(df):
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        # yfinance versions differ in MultiIndex level ordering; select actual price field explicitly.
        cols=[]
        for col in df.columns:
            parts=[str(x) for x in col]
            cols.append(next((x for x in parts if x in FIELDS), parts[-1]))
        df.columns=cols
    df=df.reset_index()
    date_col=next((c for c in ["Date","Datetime","date","index"] if c in df.columns), df.columns[0])
    df=df.rename(columns={date_col:"date"})
    # Preserve the exchange-local calendar date. Converting a Seoul-midnight index to UTC
    # before dropping the timezone shifts the date to the previous day.
    parsed = pd.to_datetime(df["date"], errors="coerce")
    if getattr(parsed.dt, "tz", None) is not None:
        parsed = parsed.dt.tz_localize(None)
    df["date"] = parsed.dt.normalize()
    for f in FIELDS:
        if f not in df.columns: df[f]=np.nan
        df[f]=pd.to_numeric(df[f],errors="coerce")
    return df

def save_frame(df, path):
    df.to_csv(path,index=False,encoding="utf-8-sig")

def main():
    run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summaries=[]; detail=[]; raw_paths=[]
    session=requests.Session()
    for item in TARGETS:
        symbol=item["symbol"]
        # Three independent access paths: yfinance download default, Ticker.history with repair,
        # and Yahoo's chart endpoint directly. Preserve each response independently.
        methods=[]
        try:
            d=normalize_frame(yf.download(symbol,start=item["start"],end=item["end"],auto_adjust=False,
                actions=True,repair=False,keepna=True,progress=False,threads=False,timeout=30))
            methods.append(("download_repair_false",d,""))
        except Exception as e: methods.append(("download_repair_false",pd.DataFrame(),repr(e)))
        try:
            h=normalize_frame(yf.Ticker(symbol).history(start=item["start"],end=item["end"],
                auto_adjust=False,actions=True,repair=True,keepna=True,timeout=30))
            methods.append(("history_repair_true",h,""))
        except Exception as e: methods.append(("history_repair_true",pd.DataFrame(),repr(e)))
        try:
            p1=int(pd.Timestamp(item["start"],tz="UTC").timestamp())
            p2=int(pd.Timestamp(item["end"],tz="UTC").timestamp())
            url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            resp=session.get(url,params={"period1":p1,"period2":p2,"interval":"1d","events":"div,splits,capitalGains"},timeout=30)
            api_error=""
            try: payload=resp.json()
            except Exception: payload={}
            chart=(payload.get("chart") or {})
            result=(chart.get("result") or [])
            if not result:
                api_error=json.dumps(chart.get("error") or {"http_status":resp.status_code,"body":resp.text[:300]},ensure_ascii=False)
                direct=pd.DataFrame()
            else:
                r=result[0]; ts=r.get("timestamp") or []
                q=r.get("indicators",{}).get("quote",[{}])[0]
                adj=(r.get("indicators",{}).get("adjclose") or [{}])[0].get("adjclose",[])
                # Yahoo chart timestamps represent exchange-local session timestamps. Use the
                # exchange timezone supplied by Yahoo metadata, rather than forcing UTC.
                exchange_tz = ((r.get("meta") or {}).get("exchangeTimezoneName") or "Asia/Seoul")
                direct_dates = pd.to_datetime(ts, unit="s", utc=True).tz_convert(exchange_tz).tz_localize(None).normalize()
                direct=pd.DataFrame({"date":direct_dates})
                for key,src in [("Open","open"),("High","high"),("Low","low"),("Close","close"),("Volume","volume")]:
                    vals=q.get(src,[])
                    direct[key]=vals if len(vals)==len(direct) else np.nan
                direct["Adj Close"]=adj if len(adj)==len(direct) else np.nan
                ev=r.get("events",{})
                for field,event_key in [("Dividends","dividends"),("Stock Splits","splits"),("Capital Gains","capitalGains")]:
                    direct[field]=0.0
                    for tskey,evrow in (ev.get(event_key) or {}).items():
                        dte=pd.to_datetime(int(tskey),unit="s",utc=True).tz_convert(exchange_tz).tz_localize(None).normalize()
                        mask=direct["date"].eq(dte)
                        if field=="Dividends": direct.loc[mask,field]=evrow.get("amount",0)
                        elif field=="Stock Splits":
                            direct.loc[mask,field]=evrow.get("numerator",0)/max(evrow.get("denominator",1),1)
                            if evrow.get("numerator",0)==0: direct.loc[mask,field]=0
                        else: direct.loc[mask,field]=evrow.get("amount",0)
                api_error=f"HTTP {resp.status_code}; chart_error={chart.get('error')}"
            methods.append(("direct_chart_api",normalize_frame(direct) if not direct.empty else direct,api_error))
        except Exception as e: methods.append(("direct_chart_api",pd.DataFrame(),repr(e)))
        for method,df,err in methods:
            raw_path=OUT/f"raw_{item['ticker']}_{method}.csv"
            if not df.empty:
                df["ticker"]=item["ticker"]; df["provider_symbol"]=symbol
                df["method"]=method; df["diagnostic_run_id"]=run_id
                save_frame(df,raw_path); raw_paths.append(str(raw_path))
            date_map={}
            if not df.empty and "date" in df.columns:
                date_map={d.strftime("%Y-%m-%d"):r for d,(_,r) in zip(df["date"],df.iterrows()) if pd.notna(d)}
            for target_date in item["dates"]:
                rows=df[df["date"].dt.strftime("%Y-%m-%d").eq(target_date)] if not df.empty and "date" in df.columns else pd.DataFrame()
                if rows.empty:
                    detail.append({"ticker":item["ticker"],"symbol":symbol,"date":target_date,"method":method,
                        "result":"NO_ROW","missing_fields":"NO_ROW_RETURNED","open":None,"high":None,"low":None,
                        "close":None,"adj_close":None,"volume":None,"error":err})
                else:
                    r=rows.iloc[-1]; missing=[f for f in PRICE_FIELDS if pd.isna(r.get(f))]
                    detail.append({"ticker":item["ticker"],"symbol":symbol,"date":target_date,"method":method,
                        "result":"MISSING_FIELDS" if missing else "DATA_PRESENT","missing_fields":";".join(missing),
                        "open":r.get("Open"),"high":r.get("High"),"low":r.get("Low"),"close":r.get("Close"),
                        "adj_close":r.get("Adj Close"),"volume":r.get("Volume"),"error":err})
            summaries.append({"ticker":item["ticker"],"symbol":symbol,"method":method,"rows":len(df),
                "start_returned":str(df["date"].min().date()) if not df.empty else "",
                "end_returned":str(df["date"].max().date()) if not df.empty else "",
                "missing_price_cells":int(df[PRICE_FIELDS].isna().sum().sum()) if not df.empty else None,
                "http_or_exception":err})
            time.sleep(0.3)
    pd.DataFrame(detail).to_csv(OUT/"ETF_Missing_Data_Detail_v3.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(summaries).to_csv(OUT/"ETF_Missing_Data_Method_Comparison_v3.csv",index=False,encoding="utf-8-sig")
    manifest={"run_id":run_id,"source":"Yahoo Finance; yfinance plus direct chart endpoint",
        "targets":TARGETS,"raw_files":raw_paths,"detail_report":str(OUT/"ETF_Missing_Data_Detail_v3.csv"),
        "comparison_report":str(OUT/"ETF_Missing_Data_Method_Comparison_v3.csv"),
        "synthetic_data_allowed":False,"master_modified":False,
        "note":"No prices are inferred or filled. Compare methods to distinguish provider-side missing data from client-library behavior."}
    (OUT/"diagnostic_manifest_v3.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(manifest,ensure_ascii=False,indent=2))
    print("\nMETHOD COMPARISON\n",pd.DataFrame(summaries).to_string(index=False))
    print("\nTARGET DATES\n",pd.DataFrame(detail).to_string(index=False))

if __name__=="__main__": main()
