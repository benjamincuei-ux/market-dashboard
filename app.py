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
    page_title="台美股市場過熱與總經風險儀表板",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📊 台股過熱與總經通膨危機即時監控儀表板")
st.markdown(
    "整合**固定門檻 ＋ 歷史百分位雙軌風險評分**、**GitHub 每日籌碼存檔走勢**、**歷史策略回測**、**個股診斷**與**庫存截圖健檢**。"
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
        "美元指數": "DX-Y.NYB",
    }
    data = {}
    for name, sym in tickers.items():
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period="5d")
            if len(hist) >= 2:
                last_price = float(hist["Close"].iloc[-1])
                prev_price = float(hist["Close"].iloc[-2])
                change = ((last_price - prev_price) / prev_price) * 100
                data_date = hist.index[-1].strftime("%Y-%m-%d")
                data[name] = {"price": last_price, "change": change, "date": data_date, "ok": True}
            elif not hist.empty:
                last_price = float(hist["Close"].iloc[-1])
                data_date = hist.index[-1].strftime("%Y-%m-%d")
                data[name] = {"price": last_price, "change": 0.0, "date": data_date, "ok": True}
            else:
                data[name] = {"price": 0.0, "change": 0.0, "date": "無資料", "ok": False}
        except Exception:
            data[name] = {"price": 0.0, "change": 0.0, "date": "連線失敗", "ok": False}
    return data


@st.cache_data(ttl=1800)
def compute_dynamic_percentiles():
    """計算主要市場指標近 1 年的歷史百分位（0~100），作為動態過熱參考"""
    results = {}
    try:
        vix_1y = yf.Ticker("^VIX").history(period="1y")["Close"].dropna()
        if len(vix_1y) >= 20:
            # VIX 越低代表市場越鬆懈，故反向百分位越高代表越警戒
            pct_low = float((vix_1y >= vix_1y.iloc[-1]).mean() * 100)
            results["VIX 鬆懈度百分位"] = round(pct_low, 1)

        wti_1y = yf.Ticker("CL=F").history(period="1y")["Close"].dropna()
        if len(wti_1y) >= 20:
            pct_high = float((wti_1y <= wti_1y.iloc[-1]).mean() * 100)
            results["WTI 原油高檔百分位"] = round(pct_high, 1)

        twii_1y = yf.Ticker("^TWII").history(period="1y")["Close"].dropna()
        if len(twii_1y) >= 125:
            ma125 = twii_1y.rolling(125).mean()
            bias_series = ((twii_1y - ma125) / ma125 * 100).dropna()
            if len(bias_series) >= 20:
                pct_mom = float((bias_series <= bias_series.iloc[-1]).mean() * 100)
                results["台股半年線乖離百分位"] = round(pct_mom, 1)

        sox_1y = yf.Ticker("^SOX").history(period="1y")["Close"].dropna()
        if len(sox_1y) >= 125:
            sox_ma125 = sox_1y.rolling(125).mean()
            sox_bias = ((sox_1y - sox_ma125) / sox_ma125 * 100).dropna()
            if len(sox_bias) >= 20:
                results["費半半年線乖離百分位"] = round(float((sox_bias <= sox_bias.iloc[-1]).mean() * 100), 1)
    except Exception:
        pass
    return results


@st.cache_data(ttl=600)
def fetch_indicator_history(symbol: str, timeframe: str):
    """依日／週／月週期抓取市場指標歷史走勢"""
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
    """免 API Key 抓取美國最新 CPI 與核心 CPI 年增率 (%)：優先用 BLS 官方 API v1，備援用 FRED CSV"""
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
    """從臺灣證券交易所抓取最近一個交易日的上市融資餘額（億元）"""
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


def _extract_net_oi_from_record(item: dict):
    """從期交所三大法人單筆紀錄中解析未平倉淨口數（嚴格排除契約金額 Value 欄位）"""
    def _is_value_col(key: str) -> bool:
        kl = key.lower()
        return any(bad in kl for bad in ("value", "amount", "金額", "價值", "contractvalue"))

    for k, v in item.items():
        if _is_value_col(k):
            continue
        kl = k.lower()
        if ("net" in kl and "openinterest" in kl and any(u in kl for u in ("lot", "contract", "volume"))) or (
            "多空淨額" in k and "口數" in k
        ):
            try:
                return int(str(v).replace(",", "").strip())
            except ValueError:
                pass

    long_oi, short_oi = None, None
    for k, v in item.items():
        if _is_value_col(k):
            continue
        kl = k.lower()
        if ("long" in kl and "openinterest" in kl and any(u in kl for u in ("lot", "contract", "volume"))) or (
            "多方" in k and "未平倉" in k and "口數" in k
        ):
            try:
                long_oi = int(str(v).replace(",", "").strip())
            except ValueError:
                pass
        if ("short" in kl and "openinterest" in kl and any(u in kl for u in ("lot", "contract", "volume"))) or (
            "空方" in k and "未平倉" in k and "口數" in k
        ):
            try:
                short_oi = int(str(v).replace(",", "").strip())
            except ValueError:
                pass
    if long_oi is not None and short_oi is not None:
        return long_oi - short_oi
    return None


@st.cache_data(ttl=1800)
def fetch_taifex_futures_data():
    """從臺灣期貨交易所 OpenAPI 抓取外資大台淨空單與微台散戶多空比"""
    headers = {"User-Agent": "Mozilla/5.0"}
    inst_url = "https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
    daily_urls = [
        "https://openapi.taifex.com.tw/v1/DailyMarketReportFut",
        "https://openapi.taifex.com.tw/v1/DailyMarketReport",
    ]

    foreign_tx = {"net_oi": None, "short_abs": None, "date": "抓取失敗", "ok": False, "source": "手動輸入"}
    retail_tmf = {"ratio": None, "date": "抓取失敗", "ok": False, "source": "手動輸入"}

    try:
        resp = requests.get(inst_url, headers=headers, timeout=8)
        if resp.status_code == 200:
            records = resp.json()
            if isinstance(records, list):
                tmf_inst_net = 0
                tmf_found = False
                tmf_date = "最近交易日"

                for item in records:
                    vals = " ".join(str(v) for v in item.values())
                    net_oi = _extract_net_oi_from_record(item)
                    if net_oi is None:
                        continue

                    date_str = str(item.get("Date") or item.get("日期") or item.get("TradeDate") or "最近交易日").strip()
                    if len(date_str) == 8 and date_str.isdigit():
                        date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

                    if ("臺股期貨" in vals or "台股期貨" in vals) and "外資" in vals and not foreign_tx["ok"]:
                        foreign_tx = {
                            "net_oi": net_oi,
                            "short_abs": max(-net_oi, 0),
                            "date": date_str,
                            "ok": True,
                            "source": "臺灣期交所 (TAIFEX)",
                        }

                    if "微型臺指" in vals or "微型台指" in vals:
                        if any(role in vals for role in ("外資", "投信", "自營商")):
                            tmf_inst_net += net_oi
                            tmf_found = True
                            tmf_date = date_str

                if tmf_found:
                    total_tmf_oi = 0
                    for d_url in daily_urls:
                        try:
                            d_resp = requests.get(d_url, headers=headers, timeout=8)
                            if d_resp.status_code != 200:
                                continue
                            d_records = d_resp.json()
                            if not isinstance(d_records, list):
                                continue
                            for d_item in d_records:
                                contract = str(d_item.get("Contract") or d_item.get("契約") or "").strip().upper()
                                session = str(d_item.get("TradingSession") or d_item.get("交易時段") or "")
                                if contract == "TMF" and ("一般" in session or "Regular" in session or session == ""):
                                    oi_val = d_item.get("OpenInterest") or d_item.get("未沖銷契約數") or 0
                                    try:
                                        total_tmf_oi += int(str(oi_val).replace(",", "").strip())
                                    except ValueError:
                                        pass
                            if total_tmf_oi > 0:
                                break
                        except Exception:
                            continue

                    if total_tmf_oi == 0:
                        try:
                            csv_url = "https://www.taifex.com.tw/cht/3/dlFutDataDown"
                            q_date = tmf_date.replace("-", "/") if "-" in tmf_date else datetime.now().strftime("%Y/%m/%d")
                            form = {
                                "down_type": "1",
                                "commodity_id": "TMF",
                                "queryStartDate": q_date,
                                "queryEndDate": q_date,
                            }
                            c_resp = requests.post(csv_url, data=form, headers=headers, timeout=8)
                            if c_resp.status_code == 200 and "未沖銷契約數" in c_resp.text:
                                df_csv = pd.read_csv(io.StringIO(c_resp.text))
                                if "未沖銷契約數" in df_csv.columns:
                                    if "交易時段" in df_csv.columns:
                                        df_csv = df_csv[df_csv["交易時段"].astype(str).str.contains("一般", na=False)]
                                    total_tmf_oi = int(pd.to_numeric(df_csv["未沖銷契約數"], errors="coerce").fillna(0).sum())
                        except Exception:
                            pass

                    if total_tmf_oi > 0:
                        retail_net = -tmf_inst_net
                        ratio_pct = round((retail_net / total_tmf_oi) * 100, 1)
                        retail_tmf = {
                            "ratio": ratio_pct,
                            "date": tmf_date,
                            "ok": True,
                            "source": "臺灣期交所換算 (TMF)",
                        }
    except Exception:
        pass

    return foreign_tx, retail_tmf


@st.cache_data(ttl=300)
def analyze_single_stock(raw_symbol: str):
    """抓取個股近半年日線資料並計算均線、RSI、乖離率與支撐壓力"""
    sym = raw_symbol.strip().upper()
    candidates = [sym]
    if "." not in sym and any(ch.isdigit() for ch in sym):
        candidates = [f"{sym}.TW", f"{sym}.TWO", sym]

    hist = pd.DataFrame()
    used_symbol = sym
    for cand in candidates:
        try:
            df = yf.Ticker(cand).history(period="6mo")
            if not df.empty and len(df) >= 20:
                hist = df.copy()
                used_symbol = cand
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
        "hist": hist,
    }


def evaluate_stock_action(info: dict, market_risk_score: int, cost_price: float = 0.0):
    price = info["price"]
    ma20 = info["ma20"]
    ma60 = info["ma60"]
    rsi = info["rsi14"]
    bias20 = info["bias20"]

    is_above_ma20 = price >= ma20
    is_above_ma60 = (price >= ma60) if ma60 is not None else True
    pnl_pct = ((price - cost_price) / cost_price * 100) if cost_price > 0 else None

    if cost_price > 0:
        if (not is_above_ma20 and not is_above_ma60) or (pnl_pct is not None and pnl_pct <= -10):
            signal = "🔴 減碼／停損觀察"
            summary = "股價同時跌破月線與季線（或虧損達 -10% 防線），技術面轉弱。"
            action = "建議反彈無力時分批減碼降風險，保留現金等待重新站回月線。"
        elif (rsi >= 75 or bias20 >= 10) and market_risk_score > 65:
            signal = "🟡 逢高分批停利"
            summary = f"個股短線過熱（RSI {rsi:.1f}、月線乖離 {bias20:+.1f}%），且大盤處於高風險紅燈區。"
            action = "不建議再加碼，可先獲利了結 1/3～1/2 部位，其餘守住 5 日或 20 日均線續抱。"
        elif is_above_ma20 and is_above_ma60:
            signal = "🟢 續抱觀察（守月線）"
            summary = "股價穩居月線與季線之上，多頭架構尚未破壞。"
            action = f"可續抱，將 {ma20:.2f}（月線）設為移動防守點；若大盤紅燈則暫不追高加碼。"
        else:
            signal = "🟡 區間整理（觀望）"
            summary = "股價在月線附近震盪，方向尚待確認。"
            action = "暫不加碼，觀察能否帶量站穩月線之上。"
    else:
        if market_risk_score > 65 and (rsi >= 65 or bias20 >= 6):
            signal = "🔴 暫緩買進（避免追高）"
            summary = f"大盤系統性風險高（{market_risk_score} 分），且該股已有追價風險（乖離 {bias20:+.1f}%）。"
            action = f"建議忍住不追，耐心等待拉回至月線 {ma20:.2f} 附近或風險降溫再分批切入。"
        elif is_above_ma20 and is_above_ma60 and 45 <= rsi <= 68 and bias20 < 6:
            signal = "🟢 可分批低接／小額試單"
            summary = "均線呈多頭排列、RSI 位於健康區間且乖離未過熱。"
            action = (
                f"若大盤偏熱可先用 1/3 資金試單，防守點看月線 {ma20:.2f}"
                + (f"／季線 {ma60:.2f}" if ma60 else "")
                + "。"
            )
        elif not is_above_ma20 and not is_above_ma60:
            signal = "🔴 趨勢偏空（勿急接刀）"
            summary = "股價位於月線與季線下方，尚處修正格局。"
            action = f"先觀望，等股價止跌並重新站上月線 {ma20:.2f} 再考慮進場。"
        else:
            signal = "🟡 等待拉回觀察"
            summary = "目前位置處於震盪或待確認區，勝率普通。"
            action = f"建議掛在支撐區 {info['low20']:.2f}～{ma20:.2f} 附近分批低接，不追漲。"

    return signal, summary, action, pnl_pct


@st.cache_data(ttl=1800)
def run_strategy_backtest(ticker: str, years: int, red_exposure: float, yellow_exposure: float):
    """使用過去數年歷史資料，模擬「大盤動態風險減碼策略」vs「買進持有」績效（T+1 日開盤/收盤執行，含手續費與交易稅）"""
    period_str = f"{years}y"
    df_asset = yf.Ticker(ticker).history(period=period_str)[["Close"]].rename(columns={"Close": "asset"})
    df_vix = yf.Ticker("^VIX").history(period=period_str)[["Close"]].rename(columns={"Close": "vix"})
    df_wti = yf.Ticker("CL=F").history(period=period_str)[["Close"]].rename(columns={"Close": "wti"})

    if df_asset.empty or df_vix.empty:
        return None

    # 對齊日期索引（移除時區差異）
    for d in (df_asset, df_vix, df_wti):
        d.index = pd.to_datetime(d.index).tz_localize(None).normalize()

    df = df_asset.join(df_vix, how="inner").join(df_wti, how="left").ffill().dropna()
    if len(df) < 130:
        return None

    # 以可回溯的市場指標計算歷史風險分數（0~100）
    df["ma125"] = df["asset"].rolling(125).mean()
    df["bias125"] = (df["asset"] - df["ma125"]) / df["ma125"] * 100
    df["vix_pct"] = df["vix"].rolling(252, min_periods=60).apply(lambda s: (s >= s.iloc[-1]).mean() * 100, raw=False)
    df["wti_pct"] = df["wti"].rolling(252, min_periods=60).apply(lambda s: (s <= s.iloc[-1]).mean() * 100, raw=False)
    df["bias_pct"] = df["bias125"].rolling(252, min_periods=60).apply(lambda s: (s <= s.iloc[-1]).mean() * 100, raw=False)

    df["risk_score"] = df[["vix_pct", "wti_pct", "bias_pct"]].mean(axis=1)
    df = df.dropna(subset=["risk_score"]).copy()
    if len(df) < 60:
        return None

    # 避免前視偏差：用前一日收盤計算出的風險分數，決定今日持股比例（T+1 執行）
    def _target_weight(score):
        if score > 65:
            return red_exposure
        elif score > 40:
            return yellow_exposure
        return 1.0

    df["target_weight"] = df["risk_score"].shift(1).fillna(1.0).apply(_target_weight)
    df["asset_ret"] = df["asset"].pct_change().fillna(0.0)

    # 計算換倉交易成本（台股買進 0.1425%、賣出 0.1425%+0.3% 證交稅）
    df["weight_diff"] = df["target_weight"].diff().fillna(0.0)
    df["tx_cost"] = df["weight_diff"].apply(lambda d: abs(d) * (0.004425 if d < 0 else 0.001425))

    df["strat_ret"] = df["target_weight"] * df["asset_ret"] - df["tx_cost"]
    df["bh_nav"] = (1 + df["asset_ret"]).cumprod()
    df["strat_nav"] = (1 + df["strat_ret"]).cumprod()

    def _metrics(nav_series, ret_series):
        n_years = max((nav_series.index[-1] - nav_series.index[0]).days / 365.25, 0.1)
        total_ret = (nav_series.iloc[-1] - 1) * 100
        cagr = ((nav_series.iloc[-1] ** (1 / n_years)) - 1) * 100
        roll_max = nav_series.cummax()
        mdd = ((nav_series / roll_max) - 1).min() * 100
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
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt, image],
    )
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
dyn_pcts = compute_dynamic_percentiles()
us_cpi = fetch_us_cpi_data()

# 若有 chips_history.csv，優先取其最新一筆有效值，並補上即時 API 備援
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
        f"籌碼歷史庫：{hist_msg} ｜ "
        f"融資：{'✅ ' + twse_margin['date'] if twse_margin['ok'] else '⚠️ 手動'} ｜ "
        f"外資期貨：{'✅ ' + str(taifex_tx['date']) if taifex_tx['ok'] else '⚠️ 手動'} ｜ "
        f"美國CPI：{'✅ ' + str(us_cpi['date']) if us_cpi['ok'] else '⚠️ 手動'}"
    )

# ===========================
# 側邊欄：評分模式切換 + 指標參數
# ===========================
st.sidebar.header("⚙️ 評分模式與指標參數")
score_mode = st.sidebar.radio(
    "風險評分演算法",
    ["混合模式（推薦：固定門檻＋歷史百分位）", "傳統固定門檻（0~12點制）", "純動態歷史百分位（對標 CNN 模型）"],
)

use_auto_tx = st.sidebar.checkbox("自動帶入外資台指期淨空單", value=taifex_tx["ok"], disabled=not taifex_tx["ok"])
if use_auto_tx and taifex_tx["ok"]:
    custom_tx_short = taifex_tx["short_abs"]
    st.sidebar.info(f"📡 {taifex_tx['source']} ({taifex_tx['date']})：空單 {custom_tx_short:,} 口")
else:
    custom_tx_short = st.sidebar.number_input("外資台指期未平倉淨空單（口）", value=53072, step=1000)

use_auto_retail = st.sidebar.checkbox("自動換算微台散戶多空比", value=taifex_retail["ok"], disabled=not taifex_retail["ok"])
if use_auto_retail and taifex_retail["ok"]:
    custom_retail_ratio = taifex_retail["ratio"]
    st.sidebar.info(f"📡 {taifex_retail['source']} ({taifex_retail['date']})：{custom_retail_ratio:+.1f}% (偏多)")
else:
    custom_retail_ratio = st.sidebar.slider("微台散戶多空比（偏多 %）", min_value=-50.0, max_value=50.0, value=22.0, step=0.5)

use_auto_margin = st.sidebar.checkbox("自動帶入證交所上市融資餘額", value=twse_margin["ok"], disabled=not twse_margin["ok"])
if use_auto_margin and twse_margin["ok"]:
    custom_margin_debt = twse_margin["value"]
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
# 2. 雙軌風險綜合評分計算
# ===========================
risk_points = 0
advices = []

if custom_tx_short > 50000:
    risk_points += 2
    advices.append(("🚨", f"外資期貨淨空單 {custom_tx_short:,} 口（> 5 萬口極限高位）", "外資避險與看空力道極強，權值股與大盤短線面臨較大壓抑。", "避免重倉追漲大盤或大型權值股，待空單明顯回補再加碼。"))
elif custom_tx_short > 35000:
    risk_points += 1
    advices.append(("⚠️", f"外資期貨淨空單 {custom_tx_short:,} 口（> 3.5 萬口警戒區）", "法人心態轉趨謹慎，大盤容易出現高檔震盪。", "採逢低分批買進，不追突破紅K。"))

if custom_retail_ratio > 20:
    risk_points += 2
    advices.append(("🚨", f"微台散戶偏多 {custom_retail_ratio:.1f}%（> 20% 強烈反指標）", "散戶高度一致做多，籌碼凌亂，行情急跌時容易出現「多殺多」洗盤。", "切勿跟風開槓桿做多，耐心等待急跌洗盤後再找承接點。"))
elif custom_retail_ratio > 10:
    risk_points += 1
    advices.append(("⚠️", f"微台散戶偏多 {custom_retail_ratio:.1f}%（> 10% 偏熱）", "散戶作多意願升溫，短線波動可能放大。", "縮小單筆投入金額，分批進場為主。"))

if custom_margin_debt > 4800:
    risk_points += 2
    advices.append(("🚨", f"上市融資餘額 {custom_margin_debt:,.1f} 億元（> 4,800 億高槓桿）", "市場借錢買股水位過高，若遇急跌易觸發融資追繳與斷頭連鎖賣壓。", "自己絕不使用融資買股，並避開近期融資暴增的中小型飆股。"))

if custom_etf_premium > 3.0:
    risk_points += 1
    advices.append(("⚠️", f"熱門 ETF 溢價 {custom_etf_premium}%（> 3% 明顯過熱）", "市價已大幅高於淨值，此時買進等於一進場就多付溢價成本。", "暫停買進高溢價 ETF，等溢價收斂至 1% 以內再考慮。"))

if custom_cpi >= 3.8:
    risk_points += 2
    advices.append(("🚨", f"美國 CPI {custom_cpi}%（≥ 3.8% 通膨黏滯）", "通膨降溫受阻，降息空間受限甚至有緊縮預期，高估值股票容易面臨修正。", "降低高本益比科技股比重，增加現金或高股息／防禦型資產比例。"))

wti_price = market_data["WTI 紐約原油"]["price"]
if wti_price >= 100:
    risk_points += 2
    advices.append(("🚨", f"WTI 原油 ${wti_price:.2f}（突破 100 美元大關）", "能源成本飆升將推升二次通膨壓力，壓抑企業獲利與消費動能。", "提高現金防守比例，避開高耗能產業。"))
elif wti_price >= 85:
    risk_points += 1
    advices.append(("⚠️", f"WTI 原油 ${wti_price:.2f}（處於 85~100 美元偏高區）", "油價維持高檔，通膨回落速度可能放緩。", "持續關注下一次美國 CPI 公布值，暫不擴大槓桿。"))

vix_val = market_data["VIX 恐慌指數"]["price"]
if 0 < vix_val < 18:
    risk_points += 1
    advices.append(("⚠️", f"VIX 指數 {vix_val:.2f}（< 18 市場過度樂觀）", "市場避險情緒極低，遇到突發利空時回檔速度會特別快。", "檢查手中持股的停利與停損點，保留緊急備用現金。"))

fixed_score = min(int((risk_points / 12) * 100), 100)

# 若已有 chips_history.csv >= 10 天，加入融資歷史百分位
if not chips_df.empty and chips_df["margin_debt"].dropna().shape[0] >= 10:
    m_series = chips_df["margin_debt"].dropna()
    dyn_pcts["上市融資歷史百分位"] = round(float((m_series <= custom_margin_debt).mean() * 100), 1)

dynamic_score = int(round(sum(dyn_pcts.values()) / len(dyn_pcts))) if dyn_pcts else fixed_score

if score_mode.startswith("混合"):
    total_risk_score = int(round(0.6 * fixed_score + 0.4 * dynamic_score))
elif score_mode.startswith("傳統"):
    total_risk_score = fixed_score
else:
    total_risk_score = dynamic_score

# ===========================
# 分頁架構（4 大分頁）
# ===========================
tab_macro, tab_backtest, tab_stock, tab_portfolio = st.tabs([
    "📊 大盤過熱與籌碼趨勢",
    "📈 歷史回測驗證",
    "🔍 個股買進／賣出診斷",
    "📸 庫存持股截圖健檢",
])

with tab_macro:
    col_score1, col_score2 = st.columns([1, 2])

    with col_score1:
        fig_gauge = go.Figure(
            go.Indicator(
                mode="gauge+number",
                value=total_risk_score,
                domain={"x": [0, 1], "y": [0, 1]},
                title={"text": "市場過熱與脆弱度指數 (0-100)"},
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
        fig_gauge.update_layout(height=250, margin=dict(l=30, r=35, t=45, b=20))
        st.plotly_chart(fig_gauge, use_container_width=True)
        st.caption(f"固定門檻：{fixed_score} 分 ｜ 動態歷史百分位：{dynamic_score} 分")

    with col_score2:
        st.subheader("🧭 目前綜合判斷與操作建議")
        if total_risk_score > 65:
            st.error(
                "**【紅燈｜高風險警戒區（> 65 分）】市場過熱且籌碼／總經風險偏高**\n\n"
                "* **建議水位**：股票部位降至 **30%～50%**，預留 **50%～70% 現金**。\n"
                "* **核心動作**：**停止追高與開槓桿**；漲多且破線的標的逢彈分批減碼。"
            )
        elif total_risk_score > 40:
            st.warning(
                "**【黃燈｜中度警戒區（41～65 分）】部分指標偏熱，高檔震盪機率高**\n\n"
                "* **建議水位**：持股 **50%～70%**、現金 **30%～50%**。\n"
                "* **核心動作**：**買黑不買紅**，避免單筆重押與追高溢價 ETF。"
            )
        else:
            st.success(
                "**【綠燈｜穩健安全區（0～40 分）】籌碼與總經環境相對平穩**\n\n"
                "* **建議水位**：可維持正常偏多配置（持股 **70%～85%**、現金 **15%～30%**）。\n"
                "* **核心動作**：依既定紀律分批布局或定期定額。"
            )

        if dyn_pcts:
            pct_text = " ｜ ".join(f"**{k}**：{v}%" for k, v in dyn_pcts.items())
            st.caption(f"📐 近一年歷史百分位參考（越高代表越處於警戒水位）：{pct_text}")

    if advices:
        with st.expander(f"📋 點此查看目前觸發的 {len(advices)} 項警訊白話解讀與對應做法", expanded=True):
            for icon, title, meaning, action in advices:
                st.markdown(f"**{icon} {title}**\n- **代表意義**：{meaning}\n- **建議做法**：{action}")

    st.divider()

    # 台股籌碼歷史走勢圖（讀取 chips_history.csv）
    st.markdown("### 📉 台股籌碼歷史走勢圖（外資空單 / 融資餘額 / 微台散戶）")
    if chips_df.empty:
        st.info(
            "尚未偵測到 `chips_history.csv` 歷史存檔。請將一併產出的 `fetch_daily_chips.py` 與 "
            "`.github/workflows/daily_chips.yml` 上傳到 GitHub 並在 Actions 執行一次，即可自動回補並每日累積歷史走勢！"
        )
    else:
        chip_col1, chip_col2 = st.columns(2)
        with chip_col1:
            df_m = chips_df.dropna(subset=["margin_debt"])
            if not df_m.empty:
                fig_m = go.Figure()
                fig_m.add_trace(go.Scatter(x=df_m["date"], y=df_m["margin_debt"], name="上市融資餘額(億)", line=dict(color="#d9534f", width=2.2)))
                fig_m.add_hline(y=4800, line_dash="dash", line_color="gray", annotation_text="4,800億警戒線")
                fig_m.update_layout(title="上市融資餘額歷史走勢（億元）", height=320, margin=dict(l=20, r=20, t=40, b=20))
                st.plotly_chart(fig_m, use_container_width=True)
            else:
                st.caption("尚無融資餘額歷史紀錄。")

        with chip_col2:
            df_tx = chips_df.dropna(subset=["tx_foreign_short"])
            if not df_tx.empty:
                fig_tx = go.Figure()
                fig_tx.add_trace(go.Bar(x=df_tx["date"], y=df_tx["tx_foreign_short"], name="外資淨空單(口)", marker_color="#f0ad4e"))
                fig_tx.add_hline(y=50000, line_dash="dash", line_color="red", annotation_text="5萬口極限高位")
                fig_tx.update_layout(title="外資台指期淨空單歷史紀錄（口）", height=320, margin=dict(l=20, r=20, t=40, b=20))
                st.plotly_chart(fig_tx, use_container_width=True)
            else:
                st.info("期交所每日累積中：待 GitHub Actions 每天 16:00 執行後，外資空單長條圖就會逐日累積顯示。")

    st.divider()
    st.subheader("📌 即時盤面與跨市場指標一覽（Yahoo Finance）")

    cards_row1 = [
        ("費城半導體 (^SOX)", "", market_data["費城半導體"]),
        ("那斯達克 (^IXIC)", "", market_data["那斯達克"]),
        ("台積電 ADR (TSM)", "$", market_data["台積電 ADR"]),
    ]
    cards_row2 = [
        ("VIX 恐慌指數", "", market_data["VIX 恐慌指數"]),
        ("WTI 原油 (美元/桶)", "$", market_data["WTI 紐約原油"]),
        ("布蘭特原油 (美元/桶)", "$", market_data["布蘭特原油"]),
    ]

    for row_cards in (cards_row1, cards_row2):
        cols = st.columns(3)
        for col, (label, prefix, item) in zip(cols, row_cards):
            with col:
                st.metric(label, f"{prefix}{item['price']:,.2f}", f"{item['change']:.2f}%")
                st.caption(f"{'✅' if item['ok'] else '❌'} 資料日期：{item['date']}")

    # 關鍵市場指標走勢圖（日／週／月）
    st.markdown("### 📈 關鍵市場指標走勢圖（日／週／月）")
    trend_options = {
        "台灣加權指數 (^TWII)": ("^TWII", None, None),
        "費城半導體指數 (^SOX)": ("^SOX", None, None),
        "那斯達克指數 (^IXIC)": ("^IXIC", None, None),
        "VIX 恐慌指數 (^VIX)": ("^VIX", 18.0, "低於 18 警戒線 (過度樂觀)"),
        "WTI 紐約原油 (CL=F)": ("CL=F", 100.0, "100 美元通膨警戒線"),
        "布蘭特原油 (BZ=F)": ("BZ=F", None, None),
        "台積電 ADR (TSM)": ("TSM", None, None),
        "S&P 500 指數 (^GSPC)": ("^GSPC", None, None),
        "美元指數 (DX-Y.NYB)": ("DX-Y.NYB", None, None),
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


with tab_backtest:
    st.subheader("📈 風險燈號策略歷史回測驗證（T+1 執行，含台股手續費與證交稅）")
    st.caption("驗證過去數年「依歷史百分位風險燈號自動加減碼」vs「單純買進持有 (Buy & Hold)」的報酬與抗跌表現。")

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
        k4.metric("夏普值 (風險調整報酬)", f"{strat['sharpe']:.2f}", f"大盤 {bh['sharpe']:.2f}（調倉 {bt_res['rebalances']} 次）")

        bt_df = bt_res["df"]
        fig_bt = go.Figure()
        fig_bt.add_trace(go.Scatter(x=bt_df.index, y=bt_df["strat_nav"], name="動態風險控管策略", line=dict(color="#d9534f", width=2.3)))
        fig_bt.add_trace(go.Scatter(x=bt_df.index, y=bt_df["bh_nav"], name=f"{bt_ticker} 買進持有", line=dict(color="#1f77b4", width=1.8, dash="dot")))
        fig_bt.update_layout(title=f"{bt_ticker} 淨值曲線比較（初始淨值 = 1.0）", height=380, margin=dict(l=20, r=20, t=45, b=20), hovermode="x unified")
        st.plotly_chart(fig_bt, use_container_width=True)
    else:
        st.warning("無法取得足夠的歷史回測資料，請稍後再試。")


with tab_stock:
    st.subheader("🔍 個股／ETF 買進與賣出風險診斷")
    st.caption("輸入台股代號（如 `2330`、`0050`、`00919`、`00713`）或美股代號（如 `TSM`、`NVDA`），結合當前大盤風險給出評估。")

    sc1, sc2, sc3 = st.columns([2, 2, 1])
    with sc1:
        target_sym = st.text_input("股票／ETF 代號", value="2330")
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

            pnl_msg = f" ｜ 帳面報酬率：**{pnl_pct:+.2f}%**" if pnl_pct is not None else ""
            st.info(f"### {signal}{pnl_msg}\n\n* **技術與大盤判讀**：{summary}\n* **建議做法**：{action}")

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


with tab_portfolio:
    st.subheader("📸 庫存持股截圖自動辨識與健檢")
    st.caption("上傳券商 App 的庫存截圖，系統會擷取持股並自動抓取最新技術面，結合大盤燈號給出每檔持股的操作建議。")

    secret_key = ""
    try:
        secret_key = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        secret_key = ""

    api_key_input = st.text_input("Gemini API Key", value=secret_key, type="password")
    uploaded_img = st.file_uploader("上傳庫存持股相片／截圖（PNG、JPG）", type=["png", "jpg", "jpeg", "webp"])

    if uploaded_img is not None:
        img_col1, img_col2 = st.columns([1, 2])
        pil_img = Image.open(uploaded_img)
        with img_col1:
            st.image(pil_img, caption="已上傳的庫存截圖", use_container_width=True)
        with img_col2:
            if not api_key_input:
                st.warning("尚未設定 `GEMINI_API_KEY`，請於 Streamlit Cloud Secrets 加入或貼於上方密碼欄。")
            else:
                if st.button("🤖 辨識截圖並進行持股健檢", type="primary"):
                    with st.spinner("正在辨識圖片中的持股並抓取最新市場報價..."):
                        try:
                            holdings = parse_holdings_from_image(pil_img, api_key_input)
                            results = []
                            for h in holdings:
                                code = str(h.get("code", "")).strip()
                                name = str(h.get("name", code))
                                shares = int(h.get("shares", 0) or 0)
                                cost = float(h.get("cost", 0) or 0)
                                info = analyze_single_stock(code)
                                if info:
                                    sig, _, act, pnl_pct = evaluate_stock_action(info, total_risk_score, cost)
                                    results.append({
                                        "代號": info["symbol"],
                                        "名稱": name,
                                        "股數": f"{shares:,}",
                                        "成本": f"{cost:.2f}" if cost > 0 else "未顯示",
                                        "最新價": f"{info['price']:.2f}",
                                        "損益%": f"{pnl_pct:+.2f}%" if pnl_pct is not None else "-",
                                        "RSI": f"{info['rsi14']:.1f}",
                                        "判斷燈號": sig,
                                        "建議動作": act,
                                    })
                                else:
                                    results.append({
                                        "代號": code,
                                        "名稱": name,
                                        "股數": f"{shares:,}",
                                        "成本": f"{cost:.2f}" if cost > 0 else "未顯示",
                                        "最新價": "查無報價",
                                        "損益%": "-",
                                        "RSI": "-",
                                        "判斷燈號": "⚠️ 需確認代號",
                                        "建議動作": "請至「個股診斷」分頁手動輸入代號查詢",
                                    })
                            st.success(f"成功辨識 {len(results)} 檔持股！")
                            st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
                        except Exception as exc:
                            st.error(f"辨識或查詢失敗：{exc}")