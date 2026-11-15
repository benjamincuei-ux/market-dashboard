import json
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
    "監控**盤面技術、籌碼背離、槓桿資金、市場情緒與總經通膨**，並支援**個股買賣點診斷**與**庫存持股截圖健檢**。"
)


# ===========================
# 1. 自動抓取外部數據函數
# ===========================
@st.cache_data(ttl=300)
def fetch_global_market_data():
    tickers = {
        "VIX 恐慌指數": "^VIX",
        "WTI 紐約原油": "CL=F",
        "布蘭特原油": "BZ=F",
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


@st.cache_data(ttl=1800)
def fetch_taifex_foreign_tx():
    """從臺灣期貨交易所 OpenAPI 抓取最新外資臺股期貨（大台）未平倉淨口數"""
    url = "https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code == 200:
            records = resp.json()
            if isinstance(records, list):
                for item in records:
                    vals = " ".join(str(v) for v in item.values())
                    if ("臺股期貨" in vals or "台股期貨" in vals) and "外資" in vals:
                        net_oi = None
                        for k, v in item.items():
                            kl = k.lower()
                            if ("net" in kl and "openinterest" in kl and ("contract" in kl or "lot" in kl or "volume" in kl)) or (
                                "多空淨額" in k and "未平倉" in k and "口數" in k
                            ):
                                try:
                                    net_oi = int(str(v).replace(",", "").strip())
                                    break
                                except ValueError:
                                    pass

                        if net_oi is None:
                            long_oi, short_oi = None, None
                            for k, v in item.items():
                                kl = k.lower()
                                if ("long" in kl and "openinterest" in kl and "contract" in kl) or (
                                    "多方" in k and "未平倉" in k and "口數" in k
                                ):
                                    try:
                                        long_oi = int(str(v).replace(",", "").strip())
                                    except ValueError:
                                        pass
                                if ("short" in kl and "openinterest" in kl and "contract" in kl) or (
                                    "空方" in k and "未平倉" in k and "口數" in k
                                ):
                                    try:
                                        short_oi = int(str(v).replace(",", "").strip())
                                    except ValueError:
                                        pass
                            if long_oi is not None and short_oi is not None:
                                net_oi = long_oi - short_oi

                        if net_oi is not None:
                            date_str = (
                                item.get("Date")
                                or item.get("日期")
                                or item.get("TradeDate")
                                or "最近交易日"
                            )
                            ds = str(date_str).strip()
                            if len(ds) == 8 and ds.isdigit():
                                ds = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
                            return {
                                "net_oi": net_oi,
                                "short_abs": max(-net_oi, 0),
                                "date": ds,
                                "ok": True,
                                "source": "臺灣期交所 (TAIFEX)",
                            }
    except Exception:
        pass
    return {"net_oi": None, "short_abs": None, "date": "抓取失敗", "ok": False, "source": "手動輸入"}


@st.cache_data(ttl=300)
def analyze_single_stock(raw_symbol: str):
    """抓取個股近半年日線資料並計算均線、RSI、乖離率與支撐壓力"""
    sym = raw_symbol.strip().upper()
    candidates = [sym]
    # 若為純數字或台股常見格式（如 2330、0050、00919、006208），自動嘗試 .TW 與 .TWO
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

    # 計算 14 日 RSI
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
    """結合個股技術面與大盤風險分數，給出明確買進／賣出／續抱參考"""
    price = info["price"]
    ma20 = info["ma20"]
    ma60 = info["ma60"]
    rsi = info["rsi14"]
    bias20 = info["bias20"]

    is_above_ma20 = price >= ma20
    is_above_ma60 = (price >= ma60) if ma60 is not None else True

    pnl_pct = ((price - cost_price) / cost_price * 100) if cost_price > 0 else None

    if cost_price > 0:
        # 已有持股情境
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
        # 空手想買進情境
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


def parse_holdings_from_image(image: Image.Image, api_key: str):
    """使用 Gemini Vision 辨識券商庫存截圖中的股票清單"""
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
# 頂部控制列：手動重整 & 抓取時間
# ===========================
ctrl_col1, ctrl_col2 = st.columns([1, 4])
with ctrl_col1:
    if st.button("🔄 立即重新抓取最新資料", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

market_data = fetch_global_market_data()
twse_margin = fetch_twse_margin_debt()
taifex_tx = fetch_taifex_foreign_tx()

with ctrl_col2:
    st.caption(
        f"🕒 畫面刷新時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ｜ "
        f"台股融資：{'✅ ' + twse_margin['date'] if twse_margin['ok'] else '⚠️ 未連線(手動)'} ｜ "
        f"外資台指期：{'✅ ' + str(taifex_tx['date']) if taifex_tx['ok'] else '⚠️ 未連線(手動)'}"
    )

# ===========================
# 側邊欄：自動帶入 + 手動微調
# ===========================
st.sidebar.header("⚙️ 指標參數設定與來源狀態")
st.sidebar.caption("已自動串接證交所與期交所；無公開 API 之項目請手動調整：")

use_auto_tx = st.sidebar.checkbox("自動帶入期交所外資台指期淨空單", value=taifex_tx["ok"], disabled=not taifex_tx["ok"])
if use_auto_tx and taifex_tx["ok"]:
    custom_tx_short = taifex_tx["short_abs"]
    st.sidebar.info(f"📡 期交所 ({taifex_tx['date']})：淨口數 {taifex_tx['net_oi']:,} 口（空單 {custom_tx_short:,} 口）")
else:
    custom_tx_short = st.sidebar.number_input("外資台指期未平倉淨空單（口）", value=53072, step=1000)

use_auto_margin = st.sidebar.checkbox("自動帶入證交所上市融資餘額", value=twse_margin["ok"], disabled=not twse_margin["ok"])
if use_auto_margin and twse_margin["ok"]:
    custom_margin_debt = twse_margin["value"]
    st.sidebar.info(f"📡 證交所 ({twse_margin['date']})：{custom_margin_debt:,.1f} 億元")
else:
    custom_margin_debt = st.sidebar.number_input("台股上市融資餘額（億元）", value=4911.0, step=50.0)

st.sidebar.divider()
st.sidebar.caption("以下 4 項無免費即時公開 API，請依最新公告手動調整：")
custom_retail_ratio = st.sidebar.slider("微台散戶多空比（偏多 %）", min_value=-50, max_value=50, value=22, step=1)
custom_etf_premium = st.sidebar.slider("熱門 ETF 溢價率（%）", min_value=-2.0, max_value=10.0, value=5.57, step=0.1)
custom_cpi = st.sidebar.number_input("美國最新公佈 CPI（%）", value=3.8, step=0.1)
custom_core_cpi = st.sidebar.number_input("美國最新核心 CPI（%）", value=2.8, step=0.1)

# ===========================
# 2. 風險綜合評分與判斷建議引擎
# ===========================
risk_points = 0
advices = []

if custom_tx_short > 50000:
    risk_points += 2
    advices.append((
        "🚨",
        f"外資期貨淨空單 {custom_tx_short:,} 口（> 5 萬口極限高位）",
        "外資避險與看空力道極強，權值股與大盤短線面臨較大壓抑。",
        "避免重倉追漲大盤或大型權值股，待空單明顯回補再加碼。"
    ))
elif custom_tx_short > 35000:
    risk_points += 1
    advices.append((
        "⚠️",
        f"外資期貨淨空單 {custom_tx_short:,} 口（> 3.5 萬口警戒區）",
        "法人心態轉趨謹慎，大盤容易出現高檔震盪。",
        "採逢低分批買進，不追突破紅K。"
    ))

if custom_retail_ratio > 20:
    risk_points += 2
    advices.append((
        "🚨",
        f"微台散戶偏多 {custom_retail_ratio}%（> 20% 強烈反指標）",
        "散戶高度一致做多，籌碼凌亂，行情急跌時容易出現「多殺多」洗盤。",
        "切勿跟風開槓桿做多，耐心等待急跌洗盤後再找承接點。"
    ))
elif custom_retail_ratio > 10:
    risk_points += 1
    advices.append((
        "⚠️",
        f"微台散戶偏多 {custom_retail_ratio}%（> 10% 偏熱）",
        "散戶作多意願升溫，短線波動可能放大。",
        "縮小單筆投入金額，分批進場為主。"
    ))

if custom_margin_debt > 4800:
    risk_points += 2
    advices.append((
        "🚨",
        f"上市融資餘額 {custom_margin_debt:,.1f} 億元（> 4,800 億高槓桿）",
        "市場借錢買股水位過高，若遇急跌易觸發融資追繳與斷頭連鎖賣壓。",
        "自己絕不使用融資買股，並避開近期融資暴增的中小型飆股。"
    ))

if custom_etf_premium > 3.0:
    risk_points += 1
    advices.append((
        "⚠️",
        f"熱門 ETF 溢價 {custom_etf_premium}%（> 3% 明顯過熱）",
        "市價已大幅高於淨值，此時買進等於一進場就多付溢價成本。",
        "暫停買進高溢價 ETF，等溢價收斂至 1% 以內再考慮；持有者可評估適度減碼。"
    ))

if custom_cpi >= 3.8:
    risk_points += 2
    advices.append((
        "🚨",
        f"美國 CPI {custom_cpi}%（≥ 3.8% 通膨黏滯）",
        "通膨降溫受阻，降息空間受限甚至有緊縮預期，高估值股票容易面臨修正。",
        "降低高本益比科技股比重，增加現金或高股息／防禦型資產比例。"
    ))

wti_price = market_data["WTI 紐約原油"]["price"]
if wti_price >= 100:
    risk_points += 2
    advices.append((
        "🚨",
        f"WTI 原油 ${wti_price:.2f}（突破 100 美元大關）",
        "能源成本飆升將推升二次通膨壓力，壓抑企業獲利與消費動能。",
        "提高現金防守比例，避開高耗能、高運輸成本受衝擊產業。"
    ))
elif wti_price >= 85:
    risk_points += 1
    advices.append((
        "⚠️",
        f"WTI 原油 ${wti_price:.2f}（處於 85~100 美元偏高區）",
        "油價維持高檔，通膨回落速度可能放緩。",
        "持續關注下一次美國 CPI 公布值，暫不擴大槓桿。"
    ))

vix_val = market_data["VIX 恐慌指數"]["price"]
if 0 < vix_val < 18:
    risk_points += 1
    advices.append((
        "⚠️",
        f"VIX 指數 {vix_val:.2f}（< 18 市場過度樂觀）",
        "市場避險情緒極低，代表多數人毫無防備，遇到突發利空時回檔速度會特別快。",
        "檢查手中持股的停利與停損點，保留緊急備用現金。"
    ))

total_risk_score = min(int((risk_points / 12) * 100), 100)

# ===========================
# 分頁架構：大盤風險 / 個股買賣診斷 / 庫存截圖健檢
# ===========================
tab_macro, tab_stock, tab_portfolio = st.tabs([
    "📊 大盤過熱與總經監控",
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
                        "color": "#D9534F"
                        if total_risk_score > 65
                        else "#F0AD4E"
                        if total_risk_score > 40
                        else "#5CB85C"
                    },
                    "steps": [
                        {"range": [0, 40], "color": "#e8f5e9"},
                        {"range": [40, 65], "color": "#fff9c4"},
                        {"range": [65, 100], "color": "#ffebee"},
                    ],
                    "threshold": {
                        "line": {"color": "red", "width": 4},
                        "thickness": 0.75,
                        "value": 75,
                    },
                },
            )
        )
        fig_gauge.update_layout(height=250, margin=dict(l=30, r=35, t=45, b=20))
        st.plotly_chart(fig_gauge, use_container_width=True)

    with col_score2:
        st.subheader("🧭 目前綜合判斷與操作建議")
        if total_risk_score > 65:
            st.error(
                "**【紅燈｜高風險警戒區（> 65 分）】市場過熱且籌碼／總經風險偏高**\n\n"
                "* **建議水位**：股票部位降至 **30%～50%**，預留 **50%～70% 現金**（或高流動性定存／短債）。\n"
                "* **核心動作**：**停止追高與開槓桿**；漲多且破線的標的逢彈分批減碼，耐心等風險降溫或急跌洗盤後再進場。"
            )
        elif total_risk_score > 40:
            st.warning(
                "**【黃燈｜中度警戒區（41～65 分）】部分指標偏熱，高檔震盪機率高**\n\n"
                "* **建議水位**：持股 **50%～70%**、現金 **30%～50%**，保持進可攻退可守。\n"
                "* **核心動作**：**買黑不買紅**，避免單筆重押與追高溢價 ETF，以定期定額或分批低接取代追價。"
            )
        else:
            st.success(
                "**【綠燈｜穩健安全區（0～40 分）】籌碼與總經環境相對平穩**\n\n"
                "* **建議水位**：可維持正常偏多配置（持股 **70%～85%**、彈性現金 **15%～30%**）。\n"
                "* **核心動作**：依既定投資紀律分批布局或定期定額，持續每週檢視指標變化即可。"
            )
        st.caption("※ 備註：以上建議依本儀表板量化門檻自動生成，僅供個人風險控管與部位配置參考。")

    if advices:
        with st.expander(f"📋 點此查看目前觸發的 {len(advices)} 項警訊白話解讀與對應做法", expanded=True):
            for icon, title, meaning, action in advices:
                st.markdown(f"**{icon} {title}**")
                st.markdown(f"- **代表意義**：{meaning}\n- **建議做法**：{action}")

    st.divider()
    st.subheader("📌 即時盤面與跨市場指標一覽（Yahoo Finance）")

    cards = [
        ("VIX 恐慌指數", "", market_data["VIX 恐慌指數"]),
        ("WTI 原油 (美元/桶)", "$", market_data["WTI 紐約原油"]),
        ("布蘭特原油 (美元/桶)", "$", market_data["布蘭特原油"]),
        ("台積電 ADR (TSM)", "$", market_data["台積電 ADR"]),
    ]

    cols = st.columns(4)
    for col, (label, prefix, item) in zip(cols, cards):
        with col:
            st.metric(label, f"{prefix}{item['price']:.2f}", f"{item['change']:.2f}%")
            status_icon = "✅" if item["ok"] else "❌"
            st.caption(f"{status_icon} 資料日期：{item['date']}")

    st.markdown("### 🔍 核心指標對照與資料來源驗證")
    col_a, col_b = st.columns(2)

    with col_a:
        st.markdown("#### 1. 籌碼與資金結構")
        df_chip = pd.DataFrame(
            {
                "指標名稱": ["外資期貨淨空單", "微台散戶多空比", "上市融資餘額", "熱門 ETF 溢價率"],
                "當前數值": [
                    f"{custom_tx_short:,} 口",
                    f"{custom_retail_ratio}% (偏多)",
                    f"{custom_margin_debt:,.1f} 億元",
                    f"{custom_etf_premium}%",
                ],
                "資料來源 / 日期": [
                    f"{taifex_tx['source']} ({taifex_tx['date']})" if (use_auto_tx and taifex_tx["ok"]) else "手動輸入",
                    "手動輸入",
                    f"{twse_margin['source']} ({twse_margin['date']})" if (use_auto_margin and twse_margin["ok"]) else "手動輸入",
                    "手動輸入",
                ],
                "當前警示燈號": [
                    "🔴 極度危險" if custom_tx_short > 50000 else "🟡 警戒" if custom_tx_short > 35000 else "🟢 正常",
                    "🔴 極度反指標" if custom_retail_ratio > 20 else "🟢 正常",
                    "🔴 嚴重過熱" if custom_margin_debt > 4800 else "🟢 正常",
                    "🔴 嚴重溢價" if custom_etf_premium > 3.0 else "🟢 合理",
                ],
            }
        )
        st.table(df_chip)

    with col_b:
        st.markdown("#### 2. 通膨與總體經濟")
        df_macro = pd.DataFrame(
            {
                "指標名稱": ["美國最新 CPI", "美國核心 CPI", "WTI 原油現價", "VIX 避險意識"],
                "當前數值": [
                    f"{custom_cpi}%",
                    f"{custom_core_cpi}%",
                    f"${market_data['WTI 紐約原油']['price']:.2f}",
                    f"{market_data['VIX 恐慌指數']['price']:.2f}",
                ],
                "資料來源 / 日期": [
                    "手動輸入",
                    "手動輸入",
                    f"Yahoo Finance ({market_data['WTI 紐約原油']['date']})",
                    f"Yahoo Finance ({market_data['VIX 恐慌指數']['date']})",
                ],
                "當前警示燈號": [
                    "🔴 重啟升息風險" if custom_cpi >= 3.8 else "🟡 偏高",
                    "🔴 核心黏滯" if custom_core_cpi >= 2.8 else "🟢 溫和",
                    "🔴 油價破百" if market_data["WTI 紐約原油"]["price"] >= 100 else "🟢 正常",
                    "🟡 忽視風險" if 0 < market_data["VIX 恐慌指數"]["price"] < 18 else "🟢 警覺",
                ],
            }
        )
        st.table(df_macro)


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
            st.error(f"找不到代號 `{target_sym}` 的報價資料，請確認代號是否正確（台股可只輸入數字如 `2330`）。")
        else:
            signal, summary, action, pnl_pct = evaluate_stock_action(stock_info, total_risk_score, cost_input)
            m1, m2, m3, m4 = st.columns(4)
            with m1:
                st.metric(f"{stock_info['symbol']} 最新價", f"{stock_info['price']:.2f}", f"{stock_info['pct_chg']:.2f}%")
                st.caption(f"資料日期：{stock_info['date']}")
            with m2:
                st.metric("20 日月線 (MA20)", f"{stock_info['ma20']:.2f}", f"乖離 {stock_info['bias20']:+.2f}%")
            with m3:
                ma60_txt = f"{stock_info['ma60']:.2f}" if stock_info["ma60"] else "資料不足"
                st.metric("60 日季線 (MA60)", ma60_txt)
            with m4:
                st.metric("14 日 RSI 強弱", f"{stock_info['rsi14']:.1f}", f"量比 {stock_info['vol_ratio']:.2f}x")

            pnl_msg = f" ｜ 帳面報酬率：**{pnl_pct:+.2f}%**" if pnl_pct is not None else ""
            st.info(f"### {signal}{pnl_msg}\n\n* **技術與大盤判讀**：{summary}\n* **建議做法**：{action}")

            # 繪製近半年收盤價與均線圖
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

    # 讀取 Streamlit Secrets 或臨時輸入的 Gemini API Key
    secret_key = ""
    try:
        secret_key = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        secret_key = ""

    api_key_input = st.text_input(
        "Gemini API Key（建議於 Streamlit Cloud Secrets 設定 `GEMINI_API_KEY`，就不用每次輸入）",
        value=secret_key,
        type="password",
    )

    uploaded_img = st.file_uploader("上傳庫存持股相片／截圖（PNG、JPG）", type=["png", "jpg", "jpeg", "webp"])

    if uploaded_img is not None:
        img_col1, img_col2 = st.columns([1, 2])
        pil_img = Image.open(uploaded_img)
        with img_col1:
            st.image(pil_img, caption="已上傳的庫存截圖", use_container_width=True)

        with img_col2:
            if not api_key_input:
                st.warning(
                    "尚未設定 `GEMINI_API_KEY`。\n\n"
                    "請至 Google AI Studio 取得免費 API Key，並在 Streamlit Cloud 專案的 **Settings → Secrets** 加入：\n"
                    '`GEMINI_API_KEY = "你的金鑰"`（或先貼在上方密碼欄測試）。'
                )
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