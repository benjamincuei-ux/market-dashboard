import io
import json
import os
import re
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf
from PIL import Image

# 設定網頁標題與寬螢幕版面
st.set_page_config(
    page_title="台美股市場過熱與大師風控儀表板",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📊 台美股市場過熱與大師風控儀表板")
st.markdown(
    "結合**籌碼槓桿、債市信用流動性、市場廣度結構**三大維度判讀大盤，並提供**巴菲特基本面估值個股診斷**、**達利歐庫存配置健檢**與**策略歷史回測**。"
)


# ===========================
# 1. 資料讀取與自動抓取函數
# ===========================
@st.cache_data(ttl=300)
def load_chips_history():
    """讀取 GitHub Actions 每日定時存檔的 chips_history.csv"""
    csv_path = "chips_history.csv"
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path)
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)
                return df
        except Exception:
            pass
    return pd.DataFrame()


@st.cache_data(ttl=600)
def compute_rolling_margin_stats(chips_df: pd.DataFrame, current_margin: float):
    """依 SPC / 布林通道標準差理論計算滾動式融資警戒線，並檢查融資與大盤背離"""
    res = {
        "has_rolling": False,
        "ma": 4800.0,
        "yellow_line": 4800.0,
        "red_line": 5000.0,
        "z_score": 0.0,
        "margin_chg_pct": None,
        "twii_chg_pct": None,
        "divergence": False,
        "series_df": None,
    }
    if chips_df is None or chips_df.empty or "margin_debt" not in chips_df.columns:
        return res

    df_m = chips_df[["date", "margin_debt"]].dropna().copy().sort_values("date").reset_index(drop=True)
    if len(df_m) < 5:
        return res

    win = min(len(df_m), 60)
    df_m["ma"] = df_m["margin_debt"].rolling(win, min_periods=5).mean()
    df_m["std"] = df_m["margin_debt"].rolling(win, min_periods=5).std().fillna(0.0)
    df_m["yellow_line"] = df_m["ma"] + 1.5 * df_m["std"]
    df_m["red_line"] = df_m["ma"] + 2.0 * df_m["std"]

    last_ma = float(df_m["ma"].iloc[-1])
    last_std = max(float(df_m["std"].iloc[-1]), 1.0)
    yellow_line = round(last_ma + 1.5 * last_std, 1)
    red_line = round(last_ma + 2.0 * last_std, 1)
    z_score = round((current_margin - last_ma) / last_std, 2)

    res.update({
        "has_rolling": True,
        "ma": round(last_ma, 1),
        "yellow_line": yellow_line,
        "red_line": red_line,
        "z_score": z_score,
        "series_df": df_m,
    })

    lookback = min(len(df_m) - 1, 20)
    if lookback >= 10:
        prev_margin = float(df_m["margin_debt"].iloc[-1 - lookback])
        if prev_margin > 0:
            m_chg = ((current_margin - prev_margin) / prev_margin) * 100
            res["margin_chg_pct"] = round(m_chg, 2)
            try:
                twii = yf.Ticker("^TWII").history(period="2mo")["Close"].dropna()
                if len(twii) > lookback:
                    t_chg = ((float(twii.iloc[-1]) - float(twii.iloc[-1 - lookback])) / float(twii.iloc[-1 - lookback])) * 100
                    res["twii_chg_pct"] = round(t_chg, 2)
                    if m_chg >= 5.0 and t_chg <= 0.0:
                        res["divergence"] = True
            except Exception:
                pass
    return res


@st.cache_data(ttl=300)
def fetch_global_market_data():
    tickers = {
        "VIX 恐慌指數": "^VIX",
        "WTI 紐約原油": "CL=F",
        "布蘭特原油": "BZ=F",
        "費城半導體": "^SOX",
        "那斯達克": "^IXIC",
        "S&P 500 指數": "^GSPC",
        "台積電 ADR": "TSM",
        "美元/台幣": "TWD=X",
        "日經 225": "^N225",
        "韓國 KOSPI": "^KS11",
        "美元指數": "DX-Y.NYB",
    }
    data = {}
    for name, sym in tickers.items():
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period="2mo")
            if len(hist) >= 2:
                last_price = float(hist["Close"].iloc[-1])
                prev_price = float(hist["Close"].iloc[-2])
                change = ((last_price - prev_price) / prev_price) * 100
                ma20 = float(hist["Close"].tail(20).mean())
                data_date = hist.index[-1].strftime("%Y-%m-%d")
                series_df = hist[["Close"]].tail(30).copy()
                series_df["MA20"] = hist["Close"].rolling(20).mean().tail(30)
                data[name] = {
                    "price": last_price,
                    "change": change,
                    "ma20": ma20,
                    "date": data_date,
                    "series": series_df,
                    "ok": True,
                }
            elif not hist.empty:
                last_price = float(hist["Close"].iloc[-1])
                data_date = hist.index[-1].strftime("%Y-%m-%d")
                series_df = hist[["Close"]].tail(30).copy()
                series_df["MA20"] = last_price
                data[name] = {
                    "price": last_price,
                    "change": 0.0,
                    "ma20": last_price,
                    "date": data_date,
                    "series": series_df,
                    "ok": True,
                }
            else:
                data[name] = {"price": 0.0, "change": 0.0, "ma20": 0.0, "date": "無資料", "series": None, "ok": False}
        except Exception:
            data[name] = {"price": 0.0, "change": 0.0, "ma20": 0.0, "date": "連線失敗", "series": None, "ok": False}
    return data


@st.cache_data(ttl=900)
def fetch_bond_credit_data():
    """霍華·馬克斯 / 達利歐債市與信用利差指標：美國10年債殖利率(^TNX)、高收益債風險壓力(IEF/HYG)與10Y-2Y利差"""
    res = {
        "tnx_ok": False,
        "tnx_yield": 0.0,
        "tnx_20d_bps": 0.0,
        "tnx_date": "無資料",
        "tnx_df": None,
        "credit_ok": False,
        "hyg_ief_ratio": 0.0,
        "credit_stress_z": 0.0,
        "credit_alert": False,
        "credit_date": "無資料",
        "credit_df": None,
        "t10y2y_ok": False,
        "t10y2y_val": None,
        "t10y2y_date": "無資料",
        "t10y2y_df": None,
    }
    try:
        tnx = yf.Ticker("^TNX").history(period="1y")[["Close"]].dropna()
        if len(tnx) >= 21:
            tnx["MA20"] = tnx["Close"].rolling(20).mean()
            last_y = float(tnx["Close"].iloc[-1])
            prev_20 = float(tnx["Close"].iloc[-21])
            res.update({
                "tnx_ok": True,
                "tnx_yield": round(last_y, 2),
                "tnx_20d_bps": round((last_y - prev_20) * 100, 1),
                "tnx_date": tnx.index[-1].strftime("%Y-%m-%d"),
                "tnx_df": tnx,
            })
    except Exception:
        pass

    try:
        hyg = yf.Ticker("HYG").history(period="1y")[["Close"]].rename(columns={"Close": "HYG"})
        ief = yf.Ticker("IEF").history(period="1y")[["Close"]].rename(columns={"Close": "IEF"})
        for d in (hyg, ief):
            d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
        cdf = hyg.join(ief, how="inner").dropna()
        if len(cdf) >= 60:
            # IEF/HYG 上升代表資金逃往美債、拋售高收益債（信用利差擴大）
            cdf["stress_ratio"] = cdf["IEF"] / cdf["HYG"]
            cdf["hyg_ief"] = cdf["HYG"] / cdf["IEF"]
            cdf["stress_ma60"] = cdf["stress_ratio"].rolling(60).mean()
            cdf["stress_std60"] = cdf["stress_ratio"].rolling(60).std().replace(0, pd.NA)
            cdf["stress_z"] = (cdf["stress_ratio"] - cdf["stress_ma60"]) / cdf["stress_std60"]
            last_z = float(cdf["stress_z"].iloc[-1]) if pd.notna(cdf["stress_z"].iloc[-1]) else 0.0
            res.update({
                "credit_ok": True,
                "hyg_ief_ratio": round(float(cdf["hyg_ief"].iloc[-1]), 4),
                "credit_stress_z": round(last_z, 2),
                "credit_alert": last_z >= 2.0,
                "credit_date": cdf.index[-1].strftime("%Y-%m-%d"),
                "credit_df": cdf,
            })
    except Exception:
        pass

    # 嘗試抓取 FRED 10Y-2Y 殖利率利差 (T10Y2Y)
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y2Y", headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if r.status_code == 200 and "T10Y2Y" in r.text:
            df_yc = pd.read_csv(io.StringIO(r.text))
            d_col, v_col = df_yc.columns[0], df_yc.columns[1]
            df_yc[d_col] = pd.to_datetime(df_yc[d_col])
            df_yc[v_col] = pd.to_numeric(df_yc[v_col], errors="coerce")
            df_yc = df_yc.dropna().tail(260).reset_index(drop=True)
            if not df_yc.empty:
                res.update({
                    "t10y2y_ok": True,
                    "t10y2y_val": round(float(df_yc[v_col].iloc[-1]), 2),
                    "t10y2y_date": df_yc[d_col].iloc[-1].strftime("%Y-%m-%d"),
                    "t10y2y_df": df_yc.rename(columns={d_col: "date", v_col: "spread"}),
                })
    except Exception:
        pass

    return res


@st.cache_data(ttl=900)
def fetch_market_breadth_data():
    """德魯肯米勒 / 歐尼爾市場廣度指標：美股 RSP/SPY 強弱比、台股櫃買(^TWOII) vs 加權(^TWII)"""
    res = {
        "us_ok": False,
        "us_divergence": False,
        "us_ratio_now": 0.0,
        "us_ratio_ma20": 0.0,
        "us_date": "無資料",
        "us_df": None,
        "tw_ok": False,
        "tw_divergence": False,
        "tw_otc_above_ma20": True,
        "tw_twii_above_ma20": True,
        "tw_date": "無資料",
        "tw_df": None,
    }
    try:
        rsp = yf.Ticker("RSP").history(period="1y")[["Close"]].rename(columns={"Close": "RSP"})
        spy = yf.Ticker("SPY").history(period="1y")[["Close"]].rename(columns={"Close": "SPY"})
        for d in (rsp, spy):
            d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
        udf = rsp.join(spy, how="inner").dropna()
        if len(udf) >= 25:
            udf["ratio"] = udf["RSP"] / udf["SPY"]
            udf["ratio_ma20"] = udf["ratio"].rolling(20).mean()
            udf["spy_ma20"] = udf["SPY"].rolling(20).mean()
            spy_up = float(udf["SPY"].iloc[-1]) >= float(udf["spy_ma20"].iloc[-1])
            ratio_down = float(udf["ratio"].iloc[-1]) < float(udf["ratio_ma20"].iloc[-1])
            res.update({
                "us_ok": True,
                "us_divergence": bool(spy_up and ratio_down),
                "us_ratio_now": round(float(udf["ratio"].iloc[-1]), 4),
                "us_ratio_ma20": round(float(udf["ratio_ma20"].iloc[-1]), 4),
                "us_date": udf.index[-1].strftime("%Y-%m-%d"),
                "us_df": udf,
            })
    except Exception:
        pass

    try:
        twii = yf.Ticker("^TWII").history(period="1y")[["Close"]].rename(columns={"Close": "TWII"})
        twoii = yf.Ticker("^TWOII").history(period="1y")[["Close"]].rename(columns={"Close": "TWOII"})
        for d in (twii, twoii):
            d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
        tdf = twii.join(twoii, how="inner").dropna()
        if len(tdf) >= 25:
            tdf["twii_ma20"] = tdf["TWII"].rolling(20).mean()
            tdf["twoii_ma20"] = tdf["TWOII"].rolling(20).mean()
            tdf["ratio"] = tdf["TWOII"] / tdf["TWII"]
            tdf["ratio_ma20"] = tdf["ratio"].rolling(20).mean()
            twii_up = float(tdf["TWII"].iloc[-1]) >= float(tdf["twii_ma20"].iloc[-1])
            otc_up = float(tdf["TWOII"].iloc[-1]) >= float(tdf["twoii_ma20"].iloc[-1])
            res.update({
                "tw_ok": True,
                "tw_twii_above_ma20": twii_up,
                "tw_otc_above_ma20": otc_up,
                "tw_divergence": bool(twii_up and (not otc_up)),
                "tw_date": tdf.index[-1].strftime("%Y-%m-%d"),
                "tw_df": tdf,
            })
    except Exception:
        pass

    return res


@st.cache_data(ttl=1800)
def compute_dynamic_percentiles():
    """計算主要市場指標近 1 年的歷史百分位（0~100）"""
    results = {}
    try:
        vix_1y = yf.Ticker("^VIX").history(period="1y")["Close"].dropna()
        if len(vix_1y) >= 20:
            results["VIX 鬆懈度百分位"] = round(float((vix_1y >= vix_1y.iloc[-1]).mean() * 100), 1)

        wti_1y = yf.Ticker("CL=F").history(period="1y")["Close"].dropna()
        if len(wti_1y) >= 20:
            results["WTI 原油高檔百分位"] = round(float((wti_1y <= wti_1y.iloc[-1]).mean() * 100), 1)

        twii_1y = yf.Ticker("^TWII").history(period="1y")["Close"].dropna()
        if len(twii_1y) >= 125:
            ma125 = twii_1y.rolling(125).mean()
            bias_series = ((twii_1y - ma125) / ma125 * 100).dropna()
            if len(bias_series) >= 20:
                results["台股半年線乖離百分位"] = round(float((bias_series <= bias_series.iloc[-1]).mean() * 100), 1)

        sox_1y = yf.Ticker("^SOX").history(period="1y")["Close"].dropna()
        if len(sox_1y) >= 125:
            sox_ma125 = sox_1y.rolling(125).mean()
            sox_bias = ((sox_1y - sox_ma125) / sox_ma125 * 100).dropna()
            if len(sox_bias) >= 20:
                results["費半半年線乖離百分位"] = round(float((sox_bias <= sox_bias.iloc[-1]).mean() * 100), 1)

        twd_1y = yf.Ticker("TWD=X").history(period="1y")["Close"].dropna()
        if len(twd_1y) >= 20:
            results["台幣貶值百分位"] = round(float((twd_1y <= twd_1y.iloc[-1]).mean() * 100), 1)

        tnx_1y = yf.Ticker("^TNX").history(period="1y")["Close"].dropna()
        if len(tnx_1y) >= 20:
            results["10年債殖利率百分位"] = round(float((tnx_1y <= tnx_1y.iloc[-1]).mean() * 100), 1)
    except Exception:
        pass
    return results


@st.cache_data(ttl=600)
def fetch_indicator_history(symbol: str, timeframe: str):
    cfg = {
        "日線": {"period": "6mo", "interval": "1d", "ma_short": 20, "ma_long": 60, "ma_s_name": "20日均線", "ma_l_name": "60日均線"},
        "週線": {"period": "2y", "interval": "1wk", "ma_short": 20, "ma_long": 60, "ma_s_name": "20週均線", "ma_l_name": "60週均線"},
        "月線": {"period": "5y", "interval": "1mo", "ma_short": 12, "ma_long": 24, "ma_s_name": "12月均線(年線)", "ma_l_name": "24月均線"},
    }[timeframe]
    try:
        df = yf.Ticker(symbol).history(period=cfg["period"], interval=cfg["interval"])
        if df.empty:
            return None, cfg
        df = df.copy()
        df["MA_S"] = df["Close"].rolling(cfg["ma_short"]).mean()
        df["MA_L"] = df["Close"].rolling(cfg["ma_long"]).mean()
        return df, cfg
    except Exception:
        return None, cfg


@st.cache_data(ttl=43200)
def fetch_us_cpi_data():
    now_year = datetime.now().year
    try:
        bls_url = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
        payload = {
            "seriesid": ["CUSR0000SA0", "CUSR0000SA0L1E"],
            "startyear": str(now_year - 2),
            "endyear": str(now_year),
        }
        resp = requests.post(bls_url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        if resp.status_code == 200:
            js = resp.json()
            if js.get("status") == "REQUEST_SUCCEEDED":
                series_map = {}
                for s in js.get("Results", {}).get("series", []):
                    sid = s.get("seriesID")
                    rows = []
                    for item in s.get("data", []):
                        period = item.get("period", "")
                        if period.startswith("M") and period != "M13":
                            ym = f"{item['year']}-{period[1:]}"
                            try:
                                rows.append((ym, float(item["value"])))
                            except ValueError:
                                pass
                    rows.sort(key=lambda x: x[0])
                    if len(rows) >= 13:
                        latest_ym, latest_val = rows[-1]
                        prev_val = rows[-13][1]
                        yoy = round(((latest_val / prev_val) - 1) * 100, 2)
                        series_map[sid] = (yoy, latest_ym)
                if "CUSR0000SA0" in series_map and "CUSR0000SA0L1E" in series_map:
                    return {
                        "cpi": series_map["CUSR0000SA0"][0],
                        "core_cpi": series_map["CUSR0000SA0L1E"][0],
                        "date": series_map["CUSR0000SA0"][1],
                        "ok": True,
                        "source": "美國勞工局 (BLS)",
                    }
    except Exception:
        pass

    def _get_fred_yoy(series_id: str):
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        date_col, val_col = df.columns[0], df.columns[1]
        df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
        df = df.dropna(subset=[val_col]).reset_index(drop=True)
        if len(df) >= 13:
            latest = float(df[val_col].iloc[-1])
            prev_yr = float(df[val_col].iloc[-13])
            yoy = round(((latest / prev_yr) - 1) * 100, 2)
            obs_month = str(df[date_col].iloc[-1])[:7]
            return yoy, obs_month
        return None, None

    try:
        cpi_yoy, cpi_date = _get_fred_yoy("CPIAUCSL")
        core_yoy, core_date = _get_fred_yoy("CPILFESL")
        if cpi_yoy is not None and core_yoy is not None:
            return {
                "cpi": cpi_yoy,
                "core_cpi": core_yoy,
                "date": cpi_date or core_date,
                "ok": True,
                "source": "美國聯準會 (FRED)",
            }
    except Exception:
        pass

    return {"cpi": None, "core_cpi": None, "date": "抓取失敗", "ok": False, "source": "手動輸入"}


@st.cache_data(ttl=1800)
def fetch_twse_margin_debt():
    headers = {"User-Agent": "Mozilla/5.0"}
    today = datetime.now()
    for i in range(10):
        target = today - timedelta(days=i)
        date_param = target.strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={date_param}&selectType=MS"
        try:
            resp = requests.get(url, headers=headers, timeout=6)
            if resp.status_code != 200:
                continue
            js = resp.json()
            if js.get("stat") != "OK":
                continue
            rows = js.get("creditList") or []
            if not rows and isinstance(js.get("tables"), list):
                for tbl in js["tables"]:
                    if isinstance(tbl, dict) and tbl.get("data"):
                        rows = tbl["data"]
                        break
            for row in rows:
                if row and "融資金額" in str(row[0]):
                    raw_val = str(row[-1]).replace(",", "").strip()
                    if raw_val.replace(".", "", 1).isdigit():
                        billion_ntd = round(float(raw_val) / 100000, 1)
                        return {
                            "value": billion_ntd,
                            "date": target.strftime("%Y-%m-%d"),
                            "ok": True,
                            "source": "臺灣證交所 (TWSE)",
                        }
        except Exception:
            continue
    return {"value": None, "date": "抓取失敗", "ok": False, "source": "手動輸入"}


def _finmind_get(dataset: str, data_id: str, start_date: str, end_date: str):
    try:
        resp = requests.get(
            "https://api.finmindtrade.com/api/v4/data",
            params={"dataset": dataset, "data_id": data_id, "start_date": start_date, "end_date": end_date},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            js = resp.json()
            if js.get("msg") == "success" and isinstance(js.get("data"), list):
                return pd.DataFrame(js["data"])
    except Exception:
        pass
    return pd.DataFrame()


@st.cache_data(ttl=1800)
def fetch_taifex_futures_data():
    foreign_tx = {"net_oi": None, "short_abs": None, "date": "抓取失敗", "ok": False, "source": "手動輸入"}
    retail_tmf = {"ratio": None, "date": "抓取失敗", "ok": False, "source": "手動輸入"}

    now_tw = datetime.utcnow() + timedelta(hours=8)
    s_str = (now_tw - timedelta(days=10)).strftime("%Y-%m-%d")
    e_str = now_tw.strftime("%Y-%m-%d")

    df_tx = _finmind_get("TaiwanFuturesInstitutionalInvestors", "TX", s_str, e_str)
    if not df_tx.empty and {"date", "institutional_investors", "long_open_interest_balance_volume", "short_open_interest_balance_volume"}.issubset(df_tx.columns):
        df_f = df_tx[df_tx["institutional_investors"].astype(str).str.contains("外資", na=False)].sort_values("date")
        if not df_f.empty:
            last_tx = df_f.iloc[-1]
            net_oi = int(last_tx["long_open_interest_balance_volume"]) - int(last_tx["short_open_interest_balance_volume"])
            foreign_tx = {
                "net_oi": net_oi,
                "short_abs": max(-net_oi, 0),
                "date": str(last_tx["date"]).strip(),
                "ok": True,
                "source": "FinMind / 期交所 (TX)",
            }

    for fid, label in (("TMF", "微台TMF"), ("MTX", "小台MTX")):
        df_inst = _finmind_get("TaiwanFuturesInstitutionalInvestors", fid, s_str, e_str)
        df_daily = _finmind_get("TaiwanFuturesDaily", fid, s_str, e_str)
        if df_inst.empty or df_daily.empty:
            continue
        df_inst["net_oi"] = (
            pd.to_numeric(df_inst["long_open_interest_balance_volume"], errors="coerce").fillna(0)
            - pd.to_numeric(df_inst["short_open_interest_balance_volume"], errors="coerce").fillna(0)
        )
        inst_by_date = df_inst.groupby("date")["net_oi"].sum()
        if "trading_session" in df_daily.columns:
            df_daily = df_daily[df_daily["trading_session"] == "position"]
        df_daily["open_interest"] = pd.to_numeric(df_daily["open_interest"], errors="coerce").fillna(0)
        oi_by_date = df_daily.groupby("date")["open_interest"].sum()
        valid_dates = sorted(set(inst_by_date.index) & set(oi_by_date.index))
        for d_str in reversed(valid_dates):
            total_oi = float(oi_by_date.get(d_str, 0))
            if total_oi > 0:
                retail_tmf = {
                    "ratio": round((-float(inst_by_date[d_str]) / total_oi) * 100, 1),
                    "date": str(d_str),
                    "ok": True,
                    "source": f"FinMind 換算 ({label})",
                }
                break
        if retail_tmf["ok"]:
            break

    return foreign_tx, retail_tmf


@st.cache_data(ttl=300)
def analyze_single_stock(raw_symbol: str):
    """抓取個股近半年技術面 + 巴菲特基本面估值指標 (P/E, P/B, ROE, 殖利率, 產業)"""
    sym = raw_symbol.strip().upper()
    candidates = [sym]
    if "." not in sym and any(ch.isdigit() for ch in sym):
        candidates = [f"{sym}.TW", f"{sym}.TWO", sym]

    hist = pd.DataFrame()
    used_symbol = sym
    ticker_obj = None
    for cand in candidates:
        try:
            t = yf.Ticker(cand)
            df = t.history(period="6mo")
            if not df.empty and len(df) >= 20:
                hist = df.copy()
                used_symbol = cand
                ticker_obj = t
                break
        except Exception:
            continue

    if hist.empty:
        return None

    close = hist["Close"]
    hist["MA5"] = close.rolling(5).mean()
    hist["MA20"] = close.rolling(20).mean()
    hist["MA60"] = close.rolling(60).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    rsi_series = 100 - (100 / (1 + rs))

    last_close = float(close.iloc[-1])
    prev_close = float(close.iloc[-2]) if len(close) >= 2 else last_close
    pct_chg = ((last_close - prev_close) / prev_close) * 100
    ma5 = float(hist["MA5"].iloc[-1])
    ma20 = float(hist["MA20"].iloc[-1])
    ma60 = float(hist["MA60"].iloc[-1]) if pd.notna(hist["MA60"].iloc[-1]) else None
    rsi14 = float(rsi_series.iloc[-1]) if pd.notna(rsi_series.iloc[-1]) else 50.0
    bias20 = ((last_close - ma20) / ma20) * 100

    recent_20_high = float(hist["High"].tail(20).max())
    recent_20_low = float(hist["Low"].tail(20).min())
    vol_now = float(hist["Volume"].iloc[-1])
    vol_ma5 = float(hist["Volume"].tail(5).mean())
    vol_ratio = (vol_now / vol_ma5) if vol_ma5 > 0 else 1.0

    # 讀取基本面資訊（防呆處理）
    info_meta = {}
    try:
        info_meta = ticker_obj.info or {}
    except Exception:
        info_meta = {}

    pe = info_meta.get("trailingPE")
    fwd_pe = info_meta.get("forwardPE")
    pb = info_meta.get("priceToBook")
    roe = info_meta.get("returnOnEquity")
    div_yield = info_meta.get("dividendYield")
    if div_yield is not None and div_yield < 0.35:
        div_yield = div_yield * 100  # 轉成百分比
    sector = info_meta.get("sector") or ("ETF/指數基金" if info_meta.get("quoteType") == "ETF" else "未分類")

    return {
        "symbol": used_symbol,
        "date": hist.index[-1].strftime("%Y-%m-%d"),
        "price": last_close,
        "pct_chg": pct_chg,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "rsi14": rsi14,
        "bias20": bias20,
        "high20": recent_20_high,
        "low20": recent_20_low,
        "vol_ratio": vol_ratio,
        "pe": float(pe) if isinstance(pe, (int, float)) else None,
        "fwd_pe": float(fwd_pe) if isinstance(fwd_pe, (int, float)) else None,
        "pb": float(pb) if isinstance(pb, (int, float)) else None,
        "roe": round(float(roe) * 100, 1) if isinstance(roe, (int, float)) else None,
        "div_yield": round(float(div_yield), 2) if isinstance(div_yield, (int, float)) else None,
        "sector": sector,
        "hist": hist,
    }


def evaluate_stock_action(info: dict, market_risk_score: int, cost_price: float = 0.0):
    """三維判斷：基本面估值（巴菲特）＋ 技術面趨勢 ＋ 大盤風險"""
    price = info["price"]
    ma20 = info["ma20"]
    ma60 = info["ma60"]
    rsi = info["rsi14"]
    bias20 = info["bias20"]
    pe = info.get("pe")
    roe = info.get("roe")

    is_above_ma20 = price >= ma20
    is_above_ma60 = (price >= ma60) if ma60 is not None else True
    pnl_pct = ((price - cost_price) / cost_price * 100) if cost_price > 0 else None

    # 估值標籤
    val_note = ""
    if pe is not None:
        if pe > 35:
            val_note = f"本益比偏高 ({pe:.1f}x)，安全邊際較薄。"
        elif pe <= 16:
            val_note = f"本益比處於相對合理/低估區 ({pe:.1f}x)。"
        else:
            val_note = f"本益比約 {pe:.1f}x（中性區間）。"
    if roe is not None and roe >= 15:
        val_note += f" ROE 達 {roe:.1f}%，獲利體質優良。"

    if cost_price > 0:
        if (not is_above_ma20 and not is_above_ma60) or (pnl_pct is not None and pnl_pct <= -10):
            signal = "🔴 減碼／停損觀察"
            summary = f"股價跌破月季線（或虧損達 -10% 防線）。{val_note}"
            action = "建議反彈無力時分批減碼降風險，保留現金等待重新站回月線。"
        elif (rsi >= 75 or bias20 >= 10) and market_risk_score > 65:
            signal = "🟡 逢高分批停利"
            summary = f"短線過熱（RSI {rsi:.1f}、乖離 {bias20:+.1f}%）且大盤紅燈。{val_note}"
            action = "不建議再加碼，可先獲利了結 1/3～1/2 部位，其餘守月線續抱。"
        elif is_above_ma20 and is_above_ma60:
            signal = "🟢 續抱觀察（守月線）"
            summary = f"股價穩居月線與季線之上。{val_note}"
            action = f"可續抱，以月線 {ma20:.2f} 為移動防守點；若大盤紅燈則暫不追高加碼。"
        else:
            signal = "🟡 區間整理（觀望）"
            summary = f"股價在月線附近震盪，方向待確認。{val_note}"
            action = "暫不加碼，觀察能否帶量站穩月線之上。"
    else:
        if market_risk_score > 65 and (rsi >= 65 or bias20 >= 6 or (pe is not None and pe > 35)):
            signal = "🔴 暫緩買進（安全邊際不足）"
            summary = f"大盤風險偏高（{market_risk_score} 分），且短期乖離或估值偏貴。{val_note}"
            action = f"耐心等待拉回至月線 {ma20:.2f} 附近或估值修正後再分批切入。"
        elif is_above_ma20 and is_above_ma60 and 45 <= rsi <= 68 and bias20 < 6:
            signal = "🟢 可分批低接／小額試單"
            summary = f"均線多頭排列、RSI 健康且乖離未過熱。{val_note}"
            action = (
                f"建議先用 1/3 資金分批試單，防守點看月線 {ma20:.2f}"
                + (f"／季線 {ma60:.2f}" if ma60 else "")
                + "。"
            )
        elif not is_above_ma20 and not is_above_ma60:
            signal = "🔴 趨勢偏空（勿急接刀）"
            summary = f"股價位於月線與季線下方。{val_note}"
            action = f"先觀望，等股價止跌並重新站上月線 {ma20:.2f} 再考慮進場。"
        else:
            signal = "🟡 等待拉回觀察"
            summary = f"目前位置處於震盪整理區。{val_note}"
            action = f"建議掛在支撐區 {info['low20']:.2f}～{ma20:.2f} 附近分批低接。"

    return signal, summary, action, pnl_pct


@st.cache_data(ttl=1800)
def run_strategy_backtest(ticker: str, years: int, red_exposure: float, yellow_exposure: float):
    period_str = f"{years}y"
    df_asset = yf.Ticker(ticker).history(period=period_str)[["Close"]].rename(columns={"Close": "asset"})
    df_vix = yf.Ticker("^VIX").history(period=period_str)[["Close"]].rename(columns={"Close": "vix"})
    df_wti = yf.Ticker("CL=F").history(period=period_str)[["Close"]].rename(columns={"Close": "wti"})
    if df_asset.empty or df_vix.empty:
        return None
    for d in (df_asset, df_vix, df_wti):
        d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
    df = df_asset.join(df_vix, how="inner").join(df_wti, how="left").ffill().dropna()
    if len(df) < 130:
        return None

    df["ma125"] = df["asset"].rolling(125).mean()
    df["bias125"] = (df["asset"] - df["ma125"]) / df["ma125"] * 100
    df["vix_pct"] = df["vix"].rolling(252, min_periods=60).apply(lambda s: (s >= s.iloc[-1]).mean() * 100, raw=False)
    df["wti_pct"] = df["wti"].rolling(252, min_periods=60).apply(lambda s: (s <= s.iloc[-1]).mean() * 100, raw=False)
    df["bias_pct"] = df["bias125"].rolling(252, min_periods=60).apply(lambda s: (s <= s.iloc[-1]).mean() * 100, raw=False)
    df["risk_score"] = df[["vix_pct", "wti_pct", "bias_pct"]].mean(axis=1)
    df = df.dropna(subset=["risk_score"]).copy()
    if len(df) < 60:
        return None

    def _target_weight(score):
        if score > 65:
            return red_exposure
        elif score > 40:
            return yellow_exposure
        return 1.0

    df["target_weight"] = df["risk_score"].shift(1).fillna(1.0).apply(_target_weight)
    df["asset_ret"] = df["asset"].pct_change().fillna(0.0)
    df["weight_diff"] = df["target_weight"].diff().fillna(0.0)
    df["tx_cost"] = df["weight_diff"].apply(lambda d: abs(d) * (0.004425 if d < 0 else 0.001425))
    df["strat_ret"] = df["target_weight"] * df["asset_ret"] - df["tx_cost"]
    df["bh_nav"] = (1 + df["asset_ret"]).cumprod()
    df["strat_nav"] = (1 + df["strat_ret"]).cumprod()

    def _metrics(nav_series, ret_series):
        n_years = max((nav_series.index[-1] - nav_series.index[0]).days / 365.25, 0.1)
        total_ret = (nav_series.iloc[-1] - 1) * 100
        cagr = ((nav_series.iloc[-1] ** (1 / n_years)) - 1) * 100
        mdd = ((nav_series / nav_series.cummax()) - 1).min() * 100
        vol = ret_series.std() * (252 ** 0.5)
        sharpe = (ret_series.mean() * 252) / vol if vol > 0 else 0.0
        return {"total_ret": total_ret, "cagr": cagr, "mdd": mdd, "sharpe": sharpe}

    return {
        "df": df,
        "bh": _metrics(df["bh_nav"], df["asset_ret"]),
        "strat": _metrics(df["strat_nav"], df["strat_ret"]),
        "rebalances": int((df["weight_diff"] != 0).sum()),
    }


def parse_holdings_from_image(image: Image.Image, api_key: str):
    from google import genai

    client = genai.Client(api_key=api_key)
    prompt = (
        "請辨識這張台灣或美國券商 App 的「庫存持股」截圖，擷取每一檔持股資訊。\n"
        "請只回傳純 JSON 陣列（不要任何 Markdown 說明文字），格式如下：\n"
        '[{"code": "2330", "name": "台積電", "shares": 1000, "cost": 950.0}]\n'
        "規則：\n"
        "1. code 請填股票代號（例如 2330、0050、00919、TSM）；若圖上只有中文名稱沒有代號，請依常見台股代號補上。\n"
        "2. shares 請填股數（整數，若圖上顯示「1張」請換算為 1000）。\n"
        "3. cost 請填平均成本價（浮點數，若截圖未顯示請填 0）。"
    )
    resp = client.models.generate_content(model="gemini-2.5-flash", contents=[prompt, image])
    text = resp.text.strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError("無法從圖片中解析出持股清單，請確認截圖是否包含股票名稱或代號。")
    return json.loads(match.group(0))


# ===========================
# 頂部控制列與資料載入
# ===========================
ctrl_col1, ctrl_col2 = st.columns([1, 4])
with ctrl_col1:
    if st.button("🔄 立即重新抓取最新資料", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

chips_df = load_chips_history()
market_data = fetch_global_market_data()
bond_data = fetch_bond_credit_data()
breadth_data = fetch_market_breadth_data()
dyn_pcts = compute_dynamic_percentiles()
us_cpi = fetch_us_cpi_data()
twse_margin = fetch_twse_margin_debt()
taifex_tx, taifex_retail = fetch_taifex_futures_data()

if not chips_df.empty:
    valid_m = chips_df.dropna(subset=["margin_debt"])
    if not valid_m.empty and not twse_margin["ok"]:
        twse_margin = {
            "value": float(valid_m["margin_debt"].iloc[-1]),
            "date": valid_m["date"].iloc[-1].strftime("%Y-%m-%d"),
            "ok": True,
            "source": "GitHub 每日存檔",
        }
    valid_tx = chips_df.dropna(subset=["tx_foreign_short"])
    if not valid_tx.empty and not taifex_tx["ok"]:
        taifex_tx = {
            "net_oi": int(valid_tx["tx_foreign_net"].iloc[-1]),
            "short_abs": int(valid_tx["tx_foreign_short"].iloc[-1]),
            "date": valid_tx["date"].iloc[-1].strftime("%Y-%m-%d"),
            "ok": True,
            "source": "GitHub 每日存檔",
        }
    valid_rt = chips_df.dropna(subset=["retail_tmf_ratio"])
    if not valid_rt.empty and not taifex_retail["ok"]:
        taifex_retail = {
            "ratio": float(valid_rt["retail_tmf_ratio"].iloc[-1]),
            "date": valid_rt["date"].iloc[-1].strftime("%Y-%m-%d"),
            "ok": True,
            "source": "GitHub 每日存檔",
        }

with ctrl_col2:
    hist_msg = f"存檔 {len(chips_df)} 天" if not chips_df.empty else "尚未建檔"
    st.caption(
        f"🕒 刷新：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ｜ "
        f"籌碼庫：{hist_msg} ｜ "
        f"融資：{'✅ ' + twse_margin['date'] if twse_margin['ok'] else '⚠️ 手動'} ｜ "
        f"外資期貨：{'✅ ' + str(taifex_tx['date']) if taifex_tx['ok'] else '⚠️ 手動'} ｜ "
        f"債市信用：{'✅ ' + bond_data['credit_date'] if bond_data['credit_ok'] else '⚠️ 無資料'}"
    )

# ===========================
# 側邊欄：評分模式與參數設定
# ===========================
st.sidebar.header("⚙️ 大師風控參數與來源狀態")
score_mode = st.sidebar.radio(
    "風險評分演算法",
    ["大師三維混合模式（推薦）", "傳統固定門檻", "純動態歷史百分位"],
)

use_auto_tx = st.sidebar.checkbox("自動帶入外資台指期淨空單", value=taifex_tx["ok"], disabled=not taifex_tx["ok"])
if use_auto_tx and taifex_tx["ok"]:
    custom_tx_short = taifex_tx["short_abs"]
    st.sidebar.info(f"📡 {taifex_tx['source']} ({taifex_tx['date']})：空單 {custom_tx_short:,} 口")
else:
    custom_tx_short = st.sidebar.number_input("外資台指期未平倉淨空單（口）", value=53072, step=1000)

use_auto_retail = st.sidebar.checkbox("自動換算微台／小台散戶多空比", value=taifex_retail["ok"], disabled=not taifex_retail["ok"])
if use_auto_retail and taifex_retail["ok"]:
    custom_retail_ratio = taifex_retail["ratio"]
    st.sidebar.info(f"📡 {taifex_retail['source']} ({taifex_retail['date']})：{custom_retail_ratio:+.1f}% (偏多)")
else:
    custom_retail_ratio = st.sidebar.slider("散戶多空比（偏多 %）", min_value=-50.0, max_value=50.0, value=22.0, step=0.5)

use_auto_margin = st.sidebar.checkbox("自動帶入證交所上市融資餘額", value=twse_margin["ok"], disabled=not twse_margin["ok"])
if use_auto_margin and twse_margin["ok"]:
    custom_margin_debt = twse_margin["value"]
    _ms_preview = compute_rolling_margin_stats(chips_df, custom_margin_debt)
    if _ms_preview["has_rolling"]:
        st.sidebar.info(
            f"📡 {twse_margin['source']} ({twse_margin['date']})：{custom_margin_debt:,.1f} 億元\n\n"
            f"📐 滾動線：黃 {_ms_preview['yellow_line']:,.1f} / 紅 {_ms_preview['red_line']:,.1f} 億 (Z={_ms_preview['z_score']:+.2f})"
        )
    else:
        st.sidebar.info(f"📡 {twse_margin['source']} ({twse_margin['date']})：{custom_margin_debt:,.1f} 億元")
else:
    custom_margin_debt = st.sidebar.number_input("台股上市融資餘額（億元）", value=4911.0, step=50.0)

use_auto_cpi = st.sidebar.checkbox("自動帶入美國 CPI / 核心 CPI", value=us_cpi["ok"], disabled=not us_cpi["ok"])
if use_auto_cpi and us_cpi["ok"]:
    custom_cpi = us_cpi["cpi"]
    custom_core_cpi = us_cpi["core_cpi"]
    st.sidebar.info(f"📡 {us_cpi['source']} ({us_cpi['date']})：CPI {custom_cpi}% / 核心 {custom_core_cpi}%")
else:
    custom_cpi = st.sidebar.number_input("美國最新公佈 CPI（%）", value=3.8, step=0.1)
    custom_core_cpi = st.sidebar.number_input("美國最新核心 CPI（%）", value=2.8, step=0.1)

st.sidebar.divider()
custom_etf_premium = st.sidebar.slider("熱門 ETF 溢價率（%）", min_value=-2.0, max_value=10.0, value=1.5, step=0.1)

# ===========================
# 2. 三維大師風控計分引擎 + 流動性強制警戒
# ===========================
advices = []
margin_stats = compute_rolling_margin_stats(chips_df, custom_margin_debt)

# --- 維度一：籌碼與槓桿（滿分 9 點）---
chip_pts = 0
if custom_tx_short > 50000:
    chip_pts += 2
    advices.append(("🚨", f"外資期貨淨空單 {custom_tx_short:,} 口（> 5 萬口極限高位）", "外資避險與看空力道極強，權值股上檔承壓。", "避免重倉追漲大盤或權值股，待空單回補再加碼。"))
elif custom_tx_short > 35000:
    chip_pts += 1
    advices.append(("⚠️", f"外資期貨淨空單 {custom_tx_short:,} 口（> 3.5 萬口警戒區）", "法人心態偏謹慎，大盤容易高檔震盪。", "採逢低分批買進，不追紅K。"))

if custom_retail_ratio > 20:
    chip_pts += 2
    advices.append(("🚨", f"散戶多空比偏多 {custom_retail_ratio:.1f}%（> 20% 強烈反指標）", "散戶高度一致做多，急跌易引發多殺多洗盤。", "切勿開槓桿追多，等待急跌洗盤後再承接。"))
elif custom_retail_ratio > 10:
    chip_pts += 1

if margin_stats["has_rolling"]:
    if custom_margin_debt >= margin_stats["red_line"]:
        chip_pts += 2
        advices.append(("🚨", f"上市融資 {custom_margin_debt:,.1f} 億（突破滾動紅燈線 {margin_stats['red_line']:,.1f} 億）", "融資已衝破均值 + 2 倍標準差，槓桿處於統計極端擁擠區。", "絕不使用融資買股，避開近期融資暴增個股。"))
    elif custom_margin_debt >= margin_stats["yellow_line"]:
        chip_pts += 1
    if margin_stats["divergence"]:
        chip_pts += 1
        advices.append(("🚨", f"融資與大盤背離（融資 {margin_stats['margin_chg_pct']:+.1f}% vs 大盤 {margin_stats['twii_chg_pct']:+.1f}%）", "大盤走弱但融資逆勢增加，散戶接刀攤平使籌碼轉差。", "留意二次下殺風險，待融資降溫再低接。"))
elif custom_margin_debt > 4800:
    chip_pts += 2

if custom_etf_premium > 3.0:
    chip_pts += 1
    advices.append(("⚠️", f"熱門 ETF 溢價 {custom_etf_premium}%（> 3% 追價過熱）", "市價大幅高於淨值，進場即承擔溢價虧損風險。", "暫停買進高溢價 ETF，等收斂至 1% 內再考慮。"))

twd_info = market_data["美元/台幣"]
twd_pct = dyn_pcts.get("台幣貶值百分位", 0.0)
if twd_info["ok"] and twd_info["price"] > twd_info["ma20"] and twd_pct >= 75 and custom_tx_short > 35000:
    chip_pts += 1
    advices.append(("🚨", f"外資空單 ＋ 台幣走貶共振（{twd_info['price']:.2f}，貶值百分位 {twd_pct}%）", "台幣轉弱搭配外資高空單，反映實質資金外流壓力。", "拉高現金防守比例，反彈不重倉追權值股。"))

dim_chip_score = min(int(round((chip_pts / 9) * 100)), 100)

# --- 維度二：總經與債市流動性（滿分 8 點，霍華·馬克斯 / 達利歐）---
macro_pts = 0
if custom_cpi >= 3.8:
    macro_pts += 2
    advices.append(("🚨", f"美國 CPI {custom_cpi}%（≥ 3.8% 通膨黏滯）", "降息受阻甚至有緊縮預期，高估值股票面臨修正壓力。", "降低高本益比成長股比重，增加現金或防禦資產。"))
elif custom_cpi >= 3.0:
    macro_pts += 1

wti_price = market_data["WTI 紐約原油"]["price"]
if wti_price >= 100:
    macro_pts += 2
    advices.append(("🚨", f"WTI 原油 ${wti_price:.2f}（突破 100 美元大關）", "能源成本飆升將推升二次通膨風險。", "提高現金比例，避開高耗能產業。"))
elif wti_price >= 85:
    macro_pts += 1

if bond_data["tnx_ok"]:
    if bond_data["tnx_yield"] >= 4.5 or bond_data["tnx_20d_bps"] >= 35:
        macro_pts += 2
        advices.append(("🚨", f"美國 10 年債殖利率 {bond_data['tnx_yield']:.2f}%（近 20 日變動 {bond_data['tnx_20d_bps']:+.0f} bps）", "無風險利率處於高檔或短期急飆，直接壓縮科技股與股市合理估值。", "嚴格檢視個股本益比與安全邊際，避免追高高估值股。"))
    elif bond_data["tnx_yield"] >= 4.2:
        macro_pts += 1

if bond_data["credit_ok"]:
    if bond_data["credit_stress_z"] >= 2.0:
        macro_pts += 2
        advices.append(("🚨", f"【債市流動性警訊】高收益債信用壓力急升（IEF/HYG Z={bond_data['credit_stress_z']:+.2f}）", "霍華·馬克斯核心指標：債市聰明錢正加速撤出風險債券，系統性回檔風險顯著升高。", "強烈建議提高現金比重至 50% 以上，暫停加碼高波動資產。"))
    elif bond_data["credit_stress_z"] >= 1.2:
        macro_pts += 1

dim_macro_score = min(int(round((macro_pts / 8) * 100)), 100)

# --- 維度三：市場廣度與結構健康度（滿分 5 點，德魯肯米勒 / 歐尼爾）---
breadth_pts = 0
vix_val = market_data["VIX 恐慌指數"]["price"]
if 0 < vix_val < 18:
    breadth_pts += 1
    advices.append(("⚠️", f"VIX 恐慌指數 {vix_val:.2f}（< 18 市場過度樂觀）", "避險防備偏低，遇突發利空易引發快速回檔。", "檢視停利與停損線，保留備用現金。"))

if breadth_data["us_ok"] and breadth_data["us_divergence"]:
    breadth_pts += 2
    advices.append(("⚠️", "【美股廣度背離】S&P 500 站上月線，但等權重 RSP/SPY 走弱", "漲勢集中在少數大型權值股，多數個股未跟漲，行情結構轉脆。", "切勿盲目追進落後補漲股，操作聚焦核心龍頭或減碼觀望。"))

if breadth_data["tw_ok"] and breadth_data["tw_divergence"]:
    breadth_pts += 1
    advices.append(("⚠️", "【台股拉積盤背離】加權指數在月線上，但櫃買指數 (^TWOII) 跌破月線", "大盤由權值股撐盤，中小型內資股已開始轉弱。", "減碼弱勢中小型股，避免接刀。"))

n225 = market_data["日經 225"]
ks11 = market_data["韓國 KOSPI"]
if n225["ok"] and ks11["ok"] and n225["price"] < n225["ma20"] and ks11["price"] < ks11["ma20"]:
    breadth_pts += 1
    advices.append(("⚠️", "日經 225 與韓國 KOSPI 同步跌破 20 日月線", "亞太主要股市與半導體同業轉弱，須留意台股補跌壓力。", "確認日韓指數重新站回月線前，不急著放大部位。"))

dim_breadth_score = min(int(round((breadth_pts / 5) * 100)), 100)

# --- 綜合評分與霍華·馬克斯流動性強制警戒 ---
structured_score = int(round(0.40 * dim_chip_score + 0.35 * dim_macro_score + 0.25 * dim_breadth_score))
if not chips_df.empty and chips_df["margin_debt"].dropna().shape[0] >= 10:
    m_series = chips_df["margin_debt"].dropna()
    dyn_pcts["上市融資歷史百分位"] = round(float((m_series <= custom_margin_debt).mean() * 100), 1)
dynamic_score = int(round(sum(dyn_pcts.values()) / len(dyn_pcts))) if dyn_pcts else structured_score

if score_mode.startswith("大師三維"):
    total_risk_score = int(round(0.65 * structured_score + 0.35 * dynamic_score))
elif score_mode.startswith("傳統"):
    total_risk_score = structured_score
else:
    total_risk_score = dynamic_score

liquidity_override = False
if bond_data["credit_alert"] and total_risk_score < 68:
    total_risk_score = 68
    liquidity_override = True

# 判斷市場體制（短線過熱 vs 結構轉空 vs 穩健）
if liquidity_override or (dim_macro_score >= 60 and dim_breadth_score >= 50):
    market_regime = "🛑 結構性轉弱／流動性收緊（優先保本與高現金）"
elif dim_chip_score >= 60 and dim_macro_score < 50:
    market_regime = "⚡ 短線籌碼過熱（多頭急漲洗盤風險，不追高、等拉回低接）"
elif total_risk_score > 65:
    market_regime = "🔴 全面過熱高風險警戒"
elif total_risk_score > 40:
    market_regime = "🟡 局部偏熱高檔震盪"
else:
    market_regime = "🟢 三維結構相對穩健（依紀律偏多配置）"

# ===========================
# 5 大分頁架構（清爽不擁擠）
# ===========================
tab_macro, tab_bond, tab_stock, tab_portfolio, tab_backtest = st.tabs([
    "📊 大盤總覽與大師燈號",
    "🏦 債市流動性與市場廣度",
    "🔍 個股估值與買賣診斷",
    "📸 庫存截圖與資產配置",
    "📈 歷史回測驗證",
])

# ---------------------------
# 分頁 1：大盤總覽與大師燈號
# ---------------------------
with tab_macro:
    col_score1, col_score2 = st.columns([1, 2])

    with col_score1:
        fig_gauge = go.Figure(
            go.Indicator(
                mode="gauge+number",
                value=total_risk_score,
                domain={"x": [0, 1], "y": [0, 1]},
                title={"text": "大師綜合風險指數 (0-100)"},
                gauge={
                    "axis": {"range": [0, 100]},
                    "bar": {
                        "color": "#D9534F" if total_risk_score > 65 else "#F0AD4E" if total_risk_score > 40 else "#5CB85C"
                    },
                    "steps": [
                        {"range": [0, 40], "color": "#e8f5e9"},
                        {"range": [40, 65], "color": "#fff9c4"},
                        {"range": [65, 100], "color": "#ffebee"},
                    ],
                    "threshold": {"line": {"color": "red", "width": 4}, "thickness": 0.75, "value": 75},
                },
            )
        )
        fig_gauge.update_layout(height=240, margin=dict(l=30, r=35, t=45, b=15))
        st.plotly_chart(fig_gauge, use_container_width=True)
        st.caption(f"三維結構：{structured_score} 分 ｜ 歷史百分位：{dynamic_score} 分")

    with col_score2:
        st.subheader(f"🧭 市場體制判定：{market_regime}")
        if liquidity_override:
            st.error("🚨 **已觸發霍華·馬克斯債市流動性強制警戒**：高收益債信用壓力急升，分數已強制拉升至紅燈防禦區！")

        if total_risk_score > 65:
            st.error(
                "**【紅燈｜高風險警戒區（> 65 分）】**\n\n"
                "* **建議水位**：股票部位降至 **30%～50%**，預留 **50%～70% 現金**（或短債／定存）。\n"
                "* **核心動作**：**停止追高與開槓桿**；漲多破線標的逢彈分批減碼，耐心等風險降溫。"
            )
        elif total_risk_score > 40:
            st.warning(
                "**【黃燈｜中度警戒區（41～65 分）】**\n\n"
                "* **建議水位**：持股 **50%～70%**、現金 **30%～50%**。\n"
                "* **核心動作**：**買黑不買紅**，避開高溢價 ETF 與弱勢中小型股，採分批低接。"
            )
        else:
            st.success(
                "**【綠燈｜穩健安全區（0～40 分）】**\n\n"
                "* **建議水位**：維持正常偏多配置（持股 **70%～85%**、彈性現金 **15%～30%**）。\n"
                "* **核心動作**：依紀律定期定額或挑選估值合理之標的布局。"
            )

        # 三大維度燈號拆解條
        d1, d2, d3 = st.columns(3)
        d1.metric("1️⃣ 籌碼與槓桿風險", f"{dim_chip_score} 分", "短線洗盤力道", delta_color="off")
        d2.metric("2️⃣ 總經與債市流動性", f"{dim_macro_score} 分", "中長線系統風險", delta_color="off")
        d3.metric("3️⃣ 市場廣度與結構", f"{dim_breadth_score} 分", "多頭健康程度", delta_color="off")

    if advices:
        with st.expander(f"📋 點此查看目前觸發的 {len(advices)} 項警訊白話解讀與對應做法", expanded=True):
            for icon, title, meaning, action in advices:
                st.markdown(f"**{icon} {title}**\n- **代表意義**：{meaning}\n- **建議做法**：{action}")

    st.divider()

    # 台股籌碼歷史走勢圖
    st.markdown("### 📉 台股籌碼歷史走勢圖（滾動警戒帶 / 外資空單）")
    if chips_df.empty:
        st.info("尚未偵測到 `chips_history.csv`，請確認 GitHub Actions 已成功執行。")
    else:
        chip_col1, chip_col2 = st.columns(2)
        with chip_col1:
            if margin_stats["has_rolling"] and margin_stats["series_df"] is not None:
                df_m = margin_stats["series_df"]
                fig_m = go.Figure()
                fig_m.add_trace(go.Scatter(x=df_m["date"], y=df_m["margin_debt"], name="上市融資餘額", line=dict(color="#1f77b4", width=2.4)))
                fig_m.add_trace(go.Scatter(x=df_m["date"], y=df_m["ma"], name="滾動均值 (μ)", line=dict(color="#f0ad4e", width=1.4, dash="dot")))
                fig_m.add_trace(go.Scatter(x=df_m["date"], y=df_m["yellow_line"], name="黃燈線 (μ+1.5σ)", line=dict(color="#ff7f0e", width=1.2, dash="dash")))
                fig_m.add_trace(go.Scatter(x=df_m["date"], y=df_m["red_line"], name="紅燈線 (μ+2.0σ)", line=dict(color="#d9534f", width=1.4, dash="dash")))
                fig_m.update_layout(title=f"上市融資餘額與滾動警戒帶（Z={margin_stats['z_score']:+.2f}）", height=320, margin=dict(l=20, r=20, t=40, b=20), hovermode="x unified")
                st.plotly_chart(fig_m, use_container_width=True)
            else:
                st.caption("尚無足夠融資歷史紀錄。")

        with chip_col2:
            df_tx = chips_df.dropna(subset=["tx_foreign_short"])
            if not df_tx.empty:
                fig_tx = go.Figure()
                fig_tx.add_trace(go.Bar(x=df_tx["date"], y=df_tx["tx_foreign_short"], name="外資淨空單(口)", marker_color="#f0ad4e"))
                fig_tx.add_hline(y=50000, line_dash="dash", line_color="red", annotation_text="5萬口極限高位")
                fig_tx.update_layout(title="外資台指期淨空單歷史紀錄（口）", height=320, margin=dict(l=20, r=20, t=40, b=20))
                st.plotly_chart(fig_tx, use_container_width=True)
            else:
                st.info("待 GitHub Actions 跑完 FinMind 回補後，外資空單歷史長條圖就會顯示於此。")

    st.divider()
    st.subheader("📌 即時盤面與跨市場指標一覽（近 30 日迷你走勢 ＋ 20MA）")

    cards_row1 = [
        ("費城半導體 (^SOX)", "", market_data["費城半導體"]),
        ("那斯達克 (^IXIC)", "", market_data["那斯達克"]),
        ("台積電 ADR (TSM)", "$", market_data["台積電 ADR"]),
    ]
    cards_row2 = [
        ("美元/台幣 (TWD=X)", "NT$", market_data["美元/台幣"]),
        ("日經 225 (^N225)", "", market_data["日經 225"]),
        ("韓國 KOSPI (^KS11)", "", market_data["韓國 KOSPI"]),
    ]
    cards_row3 = [
        ("VIX 恐慌指數", "", market_data["VIX 恐慌指數"]),
        ("WTI 原油 (美元/桶)", "$", market_data["WTI 紐約原油"]),
        ("布蘭特原油 (美元/桶)", "$", market_data["布蘭特原油"]),
    ]

    for row_idx, row_cards in enumerate((cards_row1, cards_row2, cards_row3)):
        cols = st.columns(3)
        for col_idx, (col, (label, prefix, item)) in enumerate(zip(cols, row_cards)):
            with col:
                st.metric(label, f"{prefix}{item['price']:,.2f}", f"{item['change']:.2f}%")
                st.caption(f"{'✅' if item['ok'] else '❌'} 資料日期：{item['date']}")
                s_df = item.get("series")
                if s_df is not None and len(s_df) >= 2:
                    line_color = "#d9534f" if item["change"] >= 0 else "#2ca02c"
                    fig_mini = go.Figure()
                    fig_mini.add_trace(
                        go.Scatter(
                            x=s_df.index,
                            y=s_df["Close"],
                            mode="lines",
                            name="收盤",
                            line=dict(color=line_color, width=2),
                            fill="tozeroy",
                            fillcolor="rgba(217,83,79,0.08)" if item["change"] >= 0 else "rgba(44,160,44,0.08)",
                        )
                    )
                    if "MA20" in s_df.columns and s_df["MA20"].notna().any():
                        fig_mini.add_trace(go.Scatter(x=s_df.index, y=s_df["MA20"], mode="lines", name="20MA", line=dict(color="#888888", width=1, dash="dot")))
                    fig_mini.update_layout(
                        height=130,
                        margin=dict(l=0, r=0, t=5, b=0),
                        showlegend=False,
                        xaxis=dict(visible=False),
                        yaxis=dict(visible=True, tickfont=dict(size=9), nticks=3),
                        hovermode="x unified",
                    )
                    st.plotly_chart(fig_mini, use_container_width=True, key=f"mini_{row_idx}_{col_idx}")

    st.markdown("### 📈 關鍵市場指標大型走勢圖（日／週／月）")
    trend_options = {
        "台灣加權指數 (^TWII)": ("^TWII", None, None),
        "美元/台幣匯率 (TWD=X)": ("TWD=X", None, None),
        "韓國 KOSPI 指數 (^KS11)": ("^KS11", None, None),
        "日經 225 指數 (^N225)": ("^N225", None, None),
        "費城半導體指數 (^SOX)": ("^SOX", None, None),
        "那斯達克指數 (^IXIC)": ("^IXIC", None, None),
        "美國 10 年期公債殖利率 (^TNX)": ("^TNX", 4.5, "4.5% 估值高壓線"),
        "VIX 恐慌指數 (^VIX)": ("^VIX", 18.0, "低於 18 警戒線"),
        "WTI 紐約原油 (CL=F)": ("CL=F", 100.0, "100 美元通膨警戒線"),
    }
    tc1, tc2 = st.columns([2, 2])
    with tc1:
        selected_trend = st.selectbox("選擇要檢視走勢的指標", list(trend_options.keys()))
    with tc2:
        selected_tf = st.radio("切換時間週期", ["日線", "週線", "月線"], horizontal=True)

    sym_t, warn_val, warn_label = trend_options[selected_trend]
    trend_df, tf_cfg = fetch_indicator_history(sym_t, selected_tf)
    if trend_df is not None and not trend_df.empty:
        fig_trend = go.Figure()
        fig_trend.add_trace(go.Scatter(x=trend_df.index, y=trend_df["Close"], name="收盤價", line=dict(color="#1f77b4", width=2.2)))
        if trend_df["MA_S"].notna().any():
            fig_trend.add_trace(go.Scatter(x=trend_df.index, y=trend_df["MA_S"], name=tf_cfg["ma_s_name"], line=dict(color="#ff7f0e", width=1.4)))
        if trend_df["MA_L"].notna().any():
            fig_trend.add_trace(go.Scatter(x=trend_df.index, y=trend_df["MA_L"], name=tf_cfg["ma_l_name"], line=dict(color="#2ca02c", width=1.4)))
        if warn_val is not None:
            fig_trend.add_hline(y=warn_val, line_dash="dash", line_color="red", annotation_text=warn_label)
        fig_trend.update_layout(title=f"{selected_trend} — {selected_tf}走勢（{tf_cfg['period']}）", height=350, margin=dict(l=20, r=20, t=45, b=20), hovermode="x unified")
        st.plotly_chart(fig_trend, use_container_width=True)


# ---------------------------
# 分頁 2：債市流動性與市場廣度（新）
# ---------------------------
with tab_bond:
    st.subheader("🏦 債市信用流動性（霍華·馬克斯 / 達利歐）")
    st.caption("債券與信用市場往往比股市更早嗅到流動性緊縮與衰退風險。")

    b1, b2, b3 = st.columns(3)
    with b1:
        if bond_data["tnx_ok"]:
            st.metric("美國 10 年期公債殖利率 (^TNX)", f"{bond_data['tnx_yield']:.2f}%", f"近20日 {bond_data['tnx_20d_bps']:+.0f} bps")
            st.caption(f"資料日期：{bond_data['tnx_date']} ｜ >4.5% 壓抑科技股估值")
        else:
            st.metric("美國 10 年期公債殖利率", "暫無資料")
    with b2:
        if bond_data["credit_ok"]:
            status_txt = "🔴 信用壓力急升" if bond_data["credit_alert"] else "🟢 信用環境穩定"
            st.metric("高收益債信用壓力 (IEF/HYG Z-Score)", f"{bond_data['credit_stress_z']:+.2f} σ", status_txt, delta_color="off")
            st.caption(f"資料日期：{bond_data['credit_date']} ｜ ≥ +2.0σ 觸發強制紅燈警戒")
        else:
            st.metric("高收益債信用壓力", "暫無資料")
    with b3:
        if bond_data["t10y2y_ok"]:
            yc_txt = "⚠️ 倒掛區間" if bond_data["t10y2y_val"] < 0 else "🟢 正斜率"
            st.metric("美債 10Y - 2Y 殖利率利差", f"{bond_data['t10y2y_val']:+.2f}%", yc_txt, delta_color="off")
            st.caption(f"FRED 資料日期：{bond_data['t10y2y_date']}")
        else:
            st.metric("美債 10Y - 2Y 利差", "FRED 暫無回應")

    bc1, bc2 = st.columns(2)
    with bc1:
        if bond_data["tnx_ok"] and bond_data["tnx_df"] is not None:
            df_t = bond_data["tnx_df"]
            fig_tnx = go.Figure()
            fig_tnx.add_trace(go.Scatter(x=df_t.index, y=df_t["Close"], name="10年債殖利率(%)", line=dict(color="#d9534f", width=2)))
            fig_tnx.add_trace(go.Scatter(x=df_t.index, y=df_t["MA20"], name="20日均線", line=dict(color="#888", width=1.2, dash="dot")))
            fig_tnx.add_hline(y=4.5, line_dash="dash", line_color="red", annotation_text="4.5% 警戒線")
            fig_tnx.update_layout(title="美國 10 年期公債殖利率近一年走勢", height=310, margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_tnx, use_container_width=True)
    with bc2:
        if bond_data["credit_ok"] and bond_data["credit_df"] is not None:
            df_c = bond_data["credit_df"]
            fig_cr = go.Figure()
            fig_cr.add_trace(go.Scatter(x=df_c.index, y=df_c["stress_z"], name="信用壓力 Z-Score", line=dict(color="#8e44ad", width=2)))
            fig_cr.add_hline(y=2.0, line_dash="dash", line_color="red", annotation_text="+2.0σ 流動性危機警戒")
            fig_cr.add_hline(y=0.0, line_dash="dot", line_color="gray")
            fig_cr.update_layout(title="高收益債信用壓力 Z-Score（越高越危險）", height=310, margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_cr, use_container_width=True)

    st.divider()
    st.subheader("🌐 市場廣度與結構健康度（德魯肯米勒 / 歐尼爾）")
    st.caption("檢查行情是「百花齊放的健康多頭」，還是「只靠少數權值股硬撐的虛胖行情（拉積盤／廣度背離）」。")

    br1, br2 = st.columns(2)
    with br1:
        if breadth_data["us_ok"] and breadth_data["us_df"] is not None:
            us_status = "⚠️ 出現廣度背離（少數巨頭撐盤）" if breadth_data["us_divergence"] else "🟢 廣度結構正常"
            st.markdown(f"**美股等權重 vs 市值權重 (RSP / SPY)** — {us_status}")
            udf = breadth_data["us_df"]
            fig_usb = go.Figure()
            fig_usb.add_trace(go.Scatter(x=udf.index, y=udf["ratio"], name="RSP / SPY 比值", line=dict(color="#1f77b4", width=2)))
            fig_usb.add_trace(go.Scatter(x=udf.index, y=udf["ratio_ma20"], name="20日均線", line=dict(color="#ff7f0e", width=1.4, dash="dot")))
            fig_usb.update_layout(title="美股 RSP/SPY 強弱比（往下代表中小型股掉隊）", height=310, margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_usb, use_container_width=True)
    with br2:
        if breadth_data["tw_ok"] and breadth_data["tw_df"] is not None:
            tw_status = "⚠️ 拉積盤背離（加權強、櫃買破月線）" if breadth_data["tw_divergence"] else "🟢 台股大中小型結構同步"
            st.markdown(f"**台股櫃買 (^TWOII) vs 加權 (^TWII)** — {tw_status}")
            tdf = breadth_data["tw_df"]
            fig_twb = go.Figure()
            fig_twb.add_trace(go.Scatter(x=tdf.index, y=tdf["ratio"], name="櫃買 / 加權比值", line=dict(color="#2ca02c", width=2)))
            fig_twb.add_trace(go.Scatter(x=tdf.index, y=tdf["ratio_ma20"], name="20日均線", line=dict(color="#ff7f0e", width=1.4, dash="dot")))
            fig_twb.update_layout(title="台股 櫃買/加權 相對強弱（往下代表資金只集中權值股）", height=310, margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_twb, use_container_width=True)


# ---------------------------
# 分頁 3：個股估值與買賣診斷（升級巴菲特基本面）
# ---------------------------
with tab_stock:
    st.subheader("🔍 個股／ETF 基本面估值 ＋ 技術面三維診斷")
    st.caption("結合巴菲特估值安全邊際（P/E、P/B、ROE、殖利率）與技術均線／RSI，同步考量當前大盤風險。")

    sc1, sc2, sc3 = st.columns([2, 2, 1])
    with sc1:
        target_sym = st.text_input("股票／ETF 代號（如 2330、0050、00919、TSM）", value="2330")
    with sc2:
        cost_input = st.number_input("你的持有成本價（0 代表空手想買）", min_value=0.0, value=0.0, step=1.0)
    with sc3:
        st.write("")
        st.write("")
        run_stock = st.button("🔎 開始診斷", use_container_width=True)

    if target_sym:
        stock_info = analyze_single_stock(target_sym)
        if not stock_info:
            st.error(f"找不到代號 `{target_sym}` 的報價資料。")
        else:
            signal, summary, action, pnl_pct = evaluate_stock_action(stock_info, total_risk_score, cost_input)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric(f"{stock_info['symbol']} 最新價", f"{stock_info['price']:.2f}", f"{stock_info['pct_chg']:.2f}%")
            m2.metric("20 日月線 (MA20)", f"{stock_info['ma20']:.2f}", f"乖離 {stock_info['bias20']:+.2f}%")
            m3.metric("60 日季線 (MA60)", f"{stock_info['ma60']:.2f}" if stock_info["ma60"] else "資料不足")
            m4.metric("14 日 RSI 強弱", f"{stock_info['rsi14']:.1f}", f"量比 {stock_info['vol_ratio']:.2f}x")

            st.markdown("##### 🏛️ 巴菲特基本面與安全邊際指標")
            f1, f2, f3, f4, f5 = st.columns(5)
            f1.metric("本益比 (P/E)", f"{stock_info['pe']:.1f}x" if stock_info["pe"] else "N/A")
            f2.metric("預估本益比 (Fwd P/E)", f"{stock_info['fwd_pe']:.1f}x" if stock_info["fwd_pe"] else "N/A")
            f3.metric("股價淨值比 (P/B)", f"{stock_info['pb']:.2f}x" if stock_info["pb"] else "N/A")
            f4.metric("股東權益報酬率 (ROE)", f"{stock_info['roe']:.1f}%" if stock_info["roe"] is not None else "N/A")
            f5.metric("殖利率 / 屬性", f"{stock_info['div_yield']:.2f}%" if stock_info["div_yield"] else "N/A", stock_info["sector"], delta_color="off")

            pnl_msg = f" ｜ 帳面報酬率：**{pnl_pct:+.2f}%**" if pnl_pct is not None else ""
            st.info(f"### {signal}{pnl_msg}\n\n* **三維綜合判讀**：{summary}\n* **建議做法**：{action}")

            hist_df = stock_info["hist"]
            fig_stock = go.Figure()
            fig_stock.add_trace(go.Scatter(x=hist_df.index, y=hist_df["Close"], name="收盤價", line=dict(color="#1f77b4", width=2)))
            fig_stock.add_trace(go.Scatter(x=hist_df.index, y=hist_df["MA20"], name="月線 (20MA)", line=dict(color="#ff7f0e", width=1.5)))
            if hist_df["MA60"].notna().any():
                fig_stock.add_trace(go.Scatter(x=hist_df.index, y=hist_df["MA60"], name="季線 (60MA)", line=dict(color="#2ca02c", width=1.5)))
            if cost_input > 0:
                fig_stock.add_hline(y=cost_input, line_dash="dash", line_color="red", annotation_text=f"持有成本 {cost_input}")
            fig_stock.update_layout(title=f"{stock_info['symbol']} 近半年走勢與均線參考", height=360, margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_stock, use_container_width=True)


# ---------------------------
# 分頁 4：庫存截圖與達利歐資產配置健檢（升級）
# ---------------------------
with tab_portfolio:
    st.subheader("📸 庫存持股截圖辨識 ＋ 達利歐資產配置與集中度健檢")
    st.caption("上傳券商庫存截圖，除了逐檔買賣建議，系統會自動計算**持股權重圓餅圖、單一持股與產業集中度警訊**，並依大盤燈號試算建議加減碼金額。")

    secret_key = ""
    app_pin = ""
    try:
        secret_key = str(st.secrets.get("GEMINI_API_KEY", "")).strip()
        app_pin = str(st.secrets.get("APP_PIN", "")).strip()
    except Exception:
        secret_key, app_pin = "", ""

    p_col1, p_col2 = st.columns(2)
    active_api_key = ""
    with p_col1:
        if secret_key and app_pin:
            pin_input = st.text_input("🔐 個人解鎖碼（輸入正確即可啟用後台安全金鑰）", value="", type="password")
            if pin_input == app_pin:
                active_api_key = secret_key
                st.success("✅ 解鎖成功，已於伺服器端安全載入個人金鑰。")
            else:
                visitor_key = st.text_input("或：訪客自行貼上臨時 Gemini API Key（不會儲存）", value="", type="password")
                if visitor_key.strip():
                    active_api_key = visitor_key.strip()
        elif secret_key:
            active_api_key = secret_key
            st.info("🔒 已於伺服器端安全載入後台金鑰（未傳送至瀏覽器）。分享給他人前，建議於 Secrets 補上 `APP_PIN` 避免額度被消耗。")
        else:
            visitor_key = st.text_input("Gemini API Key（僅供本次辨識，不會儲存）", value="", type="password")
            if visitor_key.strip():
                active_api_key = visitor_key.strip()
    with p_col2:
        cash_reserve = st.number_input("目前帳戶預備現金（萬元，供計算股債／現金水位）", min_value=0.0, value=0.0, step=10.0)

    uploaded_img = st.file_uploader("上傳庫存持股相片／截圖（PNG、JPG）", type=["png", "jpg", "jpeg", "webp"])

    if uploaded_img is not None:
        img_col1, img_col2 = st.columns([1, 2])
        pil_img = Image.open(uploaded_img)
        with img_col1:
            st.image(pil_img, caption="已上傳的庫存截圖", use_container_width=True)
        with img_col2:
            if not active_api_key:
                st.warning("請輸入正確的個人解鎖碼，或貼上自己的 Gemini API Key 以進行截圖辨識。")
            else:
                if st.button("🤖 辨識截圖並進行達利歐投資組合健檢", type="primary"):
                    with st.spinner("正在辨識截圖、抓取估值與技術面並計算投資組合集中度..."):
                        try:
                            holdings = parse_holdings_from_image(pil_img, active_api_key)
                            results = []
                            val_rows = []
                            for h in holdings:
                                code = str(h.get("code", "")).strip()
                                name = str(h.get("name", code))
                                shares = int(h.get("shares", 0) or 0)
                                cost = float(h.get("cost", 0) or 0)
                                info = analyze_single_stock(code)
                                if info:
                                    sig, _, act, pnl_pct = evaluate_stock_action(info, total_risk_score, cost)
                                    mkt_val = shares * info["price"]
                                    val_rows.append({"name": f"{name} ({info['symbol']})", "sector": info["sector"], "value": mkt_val})
                                    results.append({
                                        "代號": info["symbol"],
                                        "名稱": name,
                                        "屬性/產業": info["sector"],
                                        "股數": f"{shares:,}",
                                        "現值(元)": f"{mkt_val:,.0f}",
                                        "損益%": f"{pnl_pct:+.2f}%" if pnl_pct is not None else "-",
                                        "P/E": f"{info['pe']:.1f}x" if info["pe"] else "-",
                                        "判斷燈號": sig,
                                        "建議動作": act,
                                    })
                                else:
                                    results.append({
                                        "代號": code,
                                        "名稱": name,
                                        "屬性/產業": "-",
                                        "股數": f"{shares:,}",
                                        "現值(元)": "-",
                                        "損益%": "-",
                                        "P/E": "-",
                                        "判斷燈號": "⚠️ 需確認代號",
                                        "建議動作": "請至「個股診斷」分頁手動查詢",
                                    })

                            st.success(f"成功辨識 {len(results)} 檔持股！")
                            st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)

                            # 達利歐資產配置與集中度分析
                            df_val = pd.DataFrame(val_rows)
                            total_stock_val = float(df_val["value"].sum()) if not df_val.empty else 0.0
                            if total_stock_val > 0:
                                st.divider()
                                st.markdown("#### 🥧 投資組合權重與達利歐集中度診斷")
                                df_val["weight"] = df_val["value"] / total_stock_val * 100
                                pc1, pc2 = st.columns([1.2, 1.3])
                                with pc1:
                                    fig_pie = go.Figure(data=[go.Pie(labels=df_val["name"], values=df_val["value"], hole=0.45)])
                                    fig_pie.update_layout(title="各持股現值權重分布", height=300, margin=dict(l=10, r=10, t=40, b=10))
                                    st.plotly_chart(fig_pie, use_container_width=True)
                                with pc2:
                                    max_row = df_val.loc[df_val["weight"].idxmax()]
                                    sector_weights = df_val.groupby("sector")["weight"].sum()
                                    top_sector = sector_weights.idxmax()
                                    top_sector_w = float(sector_weights.max())

                                    st.metric("持股總市值估算", f"NT$ {total_stock_val/10000:,.1f} 萬元")
                                    if max_row["weight"] > 30:
                                        st.warning(f"⚠️ **單一持股集中度過高**：`{max_row['name']}` 佔比達 **{max_row['weight']:.1f}%**（> 30%），建議適度分散單一公司風險。")
                                    else:
                                        st.success(f"✅ 最大單一持股 `{max_row['name']}` 佔比 **{max_row['weight']:.1f}%**，分散度合理。")

                                    if top_sector_w > 60 and top_sector != "ETF/指數基金":
                                        st.warning(f"⚠️ **產業高度相關風險**：`{top_sector}` 佔比達 **{top_sector_w:.1f}%**（> 60%），景氣反轉時容易齊跌。")

                                    # 依大盤燈號試算建議持股水位
                                    target_eq_ratio = 0.40 if total_risk_score > 65 else (0.60 if total_risk_score > 40 else 0.80)
                                    total_assets = total_stock_val + (cash_reserve * 10000)
                                    if cash_reserve > 0:
                                        cur_eq_ratio = total_stock_val / total_assets
                                        diff_amt = (total_stock_val - total_assets * target_eq_ratio) / 10000
                                        if diff_amt > 1:
                                            st.info(f"🧭 **水位調整試算**：目前持股比 **{cur_eq_ratio*100:.1f}%**；對應當前風險燈號（建議 {target_eq_ratio*100:.0f}%），可評估逢高減碼約 **{diff_amt:,.1f} 萬元** 轉入現金防守。")
                                        else:
                                            st.info(f"🧭 **水位調整試算**：目前持股比 **{cur_eq_ratio*100:.1f}%**，已低於或符合當前風險燈號建議上限（{target_eq_ratio*100:.0f}%），防守水位充足。")
                        except Exception as exc:
                            st.error(f"辨識或查詢失敗：{exc}")


# ---------------------------
# 分頁 5：歷史回測驗證
# ---------------------------
with tab_backtest:
    st.subheader("📈 風險燈號策略歷史回測驗證（T+1 執行，含台股手續費與證交稅）")
    st.caption("驗證過去數年「依歷史百分位風險燈號自動加減碼」vs「買進持有」的報酬與最大回撤表現。")

    bc1, bc2, bc3, bc4 = st.columns(4)
    with bc1:
        bt_ticker = st.selectbox("回測標的", ["0050.TW", "^TWII", "2330.TW", "^SOX", "^IXIC", "^GSPC"])
    with bc2:
        bt_years = st.selectbox("回測年數", [3, 5], index=1)
    with bc3:
        bt_red = st.slider("紅燈時保留持股比例", 0.0, 0.8, 0.4, 0.1)
    with bc4:
        bt_yellow = st.slider("黃燈時保留持股比例", 0.3, 1.0, 0.7, 0.1)

    bt_res = run_strategy_backtest(bt_ticker, bt_years, bt_red, bt_yellow)
    if bt_res:
        bh, strat = bt_res["bh"], bt_res["strat"]
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("策略累積報酬 vs 大盤", f"{strat['total_ret']:.1f}%", f"大盤 {bh['total_ret']:.1f}%")
        k2.metric("年化報酬率 (CAGR)", f"{strat['cagr']:.1f}%", f"大盤 {bh['cagr']:.1f}%")
        k3.metric("最大回撤 (Max Drawdown)", f"{strat['mdd']:.1f}%", f"大盤 {bh['mdd']:.1f}%")
        k4.metric("夏普值", f"{strat['sharpe']:.2f}", f"大盤 {bh['sharpe']:.2f}（調倉 {bt_res['rebalances']} 次）")

        bt_df = bt_res["df"]
        fig_bt = go.Figure()
        fig_bt.add_trace(go.Scatter(x=bt_df.index, y=bt_df["strat_nav"], name="動態風險控管策略", line=dict(color="#d9534f", width=2.3)))
        fig_bt.add_trace(go.Scatter(x=bt_df.index, y=bt_df["bh_nav"], name=f"{bt_ticker} 買進持有", line=dict(color="#1f77b4", width=1.8, dash="dot")))
        fig_bt.update_layout(title=f"{bt_ticker} 淨值曲線比較（初始淨值 = 1.0）", height=380, margin=dict(l=20, r=20, t=45, b=20), hovermode="x unified")
        st.plotly_chart(fig_bt, use_container_width=True)
    else:
        st.warning("無法取得足夠的歷史回測資料，請稍後再試。")