import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
import requests
from datetime import datetime, timedelta

# 設定網頁標題與寬螢幕版面
st.set_page_config(
    page_title="台美股市場過熱與總經風險儀表板",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📊 台股過熱與總經通膨危機即時監控儀表板")
st.markdown(
    "監控**盤面技術、籌碼背離、槓桿資金、市場情緒與總經通膨**；每項數據均標註來源與資料日期，並提供對應之風險判讀與操作建議。"
)


# ===========================
# 1. 自動抓取外部數據函數
# ===========================
@st.cache_data(ttl=300)  # 快取 5 分鐘
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


@st.cache_data(ttl=1800)  # 台股盤後資料快取 30 分鐘
def fetch_twse_margin_debt():
    """從臺灣證券交易所抓取最近一個交易日的上市融資餘額（億元）"""
    headers = {"User-Agent": "Mozilla/5.0"}
    today = datetime.now()
    for i in range(10):  # 最多往前找 10 天（跳過週末與連假）
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
                        billion_ntd = round(float(raw_val) / 100000, 1)  # 仟元轉億元
                        return {
                            "value": billion_ntd,
                            "date": target.strftime("%Y-%m-%d"),
                            "ok": True,
                            "source": "臺灣證交所 (TWSE)",
                        }
        except Exception:
            continue
    return {"value": None, "date": "抓取失敗", "ok": False, "source": "手動輸入"}


@st.cache_data(ttl=1800)  # 期交所資料快取 30 分鐘
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
advices = []  # 每筆格式: (燈號, 指標現況, 市場意義, 建議做法)

# (1) 期貨空單
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

# (2) 微台散戶多空比
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

# (3) 融資餘額
if custom_margin_debt > 4800:
    risk_points += 2
    advices.append((
        "🚨",
        f"上市融資餘額 {custom_margin_debt:,.1f} 億元（> 4,800 億高槓桿）",
        "市場借錢買股水位過高，若遇急跌易觸發融資追繳與斷頭連鎖賣壓。",
        "自己絕不使用融資買股，並避開近期融資暴增的中小型飆股。"
    ))

# (4) ETF 溢價
if custom_etf_premium > 3.0:
    risk_points += 1
    advices.append((
        "⚠️",
        f"熱門 ETF 溢價 {custom_etf_premium}%（> 3% 明顯過熱）",
        "市價已大幅高於淨值，此時買進等於一進場就多付溢價成本。",
        "暫停買進高溢價 ETF，等溢價收斂至 1% 以內再考慮；持有者可評估適度減碼。"
    ))

# (5) 通膨 CPI
if custom_cpi >= 3.8:
    risk_points += 2
    advices.append((
        "🚨",
        f"美國 CPI {custom_cpi}%（≥ 3.8% 通膨黏滯）",
        "通膨降溫受阻，降息空間受限甚至有緊縮預期，高估值股票容易面臨修正。",
        "降低高本益比科技股比重，增加現金或高股息／防禦型資產比例。"
    ))

# (6) 原油價格
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

# (7) VIX 恐慌指數
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
# 3. 儀表板頂部：綜合風險評級與操作建議
# ===========================
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
    st.caption("※ 備註：以上建議依本儀表板量化門檻自動生成，僅供個人風險控管與部位配置參考，投資決策仍請搭配自身風險承受度。")

# 分項白話解讀區
if advices:
    with st.expander(f"📋 點此查看目前觸發的 {len(advices)} 項警訊白話解讀與對應做法", expanded=True):
        for icon, title, meaning, action in advices:
            st.markdown(f"**{icon} {title}**")
            st.markdown(f"- **代表意義**：{meaning}\n- **建議做法**：{action}")

st.divider()

# ===========================
# 4. 指標詳細卡片展示區（含資料日期與狀態）
# ===========================
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