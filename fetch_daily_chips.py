import io
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

CSV_PATH = "chips_history.csv"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _decode_taifex_csv(content: bytes) -> str:
    for enc in ("cp950", "big5", "utf-8-sig", "utf-8"):
        try:
            text = content.decode(enc)
            if "商品" in text or "契約" in text or "日期" in text:
                return text
        except Exception:
            continue
    return content.decode("utf-8", errors="ignore")


def fetch_twse_margin_for_date(target_date: datetime):
    """抓取指定日期的上市融資餘額（億元）"""
    date_param = target_date.strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={date_param}&selectType=MS"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code != 200:
            return None
        js = resp.json()
        if js.get("stat") != "OK":
            return None

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
                    return round(float(raw_val) / 100000, 1)
    except Exception:
        pass
    return None


def fetch_taifex_range_csv(start_date: datetime, end_date: datetime):
    """透過期交所官方 CSV 下載指定區間的：1) 外資台指期淨空單 2) 微台散戶多空比"""
    s_str = start_date.strftime("%Y/%m/%d")
    e_str = end_date.strftime("%Y/%m/%d")
    results = {}  # date_str (YYYY-MM-DD) -> dict

    # 1. 三大法人區分各期貨契約 CSV（一次抓整個日期區間）
    inst_url = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
    try:
        resp = requests.post(
            inst_url,
            data={"queryStartDate": s_str, "queryEndDate": e_str},
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            text = _decode_taifex_csv(resp.content)
            df_inst = pd.read_csv(io.StringIO(text))
            df_inst.columns = [c.strip() for c in df_inst.columns]

            # 找外資臺股期貨（TXF）
            if {"日期", "商品名稱", "身份別", "多空未平倉口數淨額"}.issubset(df_inst.columns):
                tx_df = df_inst[
                    df_inst["商品名稱"].astype(str).str.contains("臺股期貨|台股期貨", na=False)
                    & df_inst["身份別"].astype(str).str.contains("外資", na=False)
                ]
                for _, row in tx_df.iterrows():
                    d_norm = str(row["日期"]).strip().replace("/", "-")
                    if len(d_norm) == 10:
                        try:
                            net_oi = int(str(row["多空未平倉口數淨額"]).replace(",", "").strip())
                            results.setdefault(d_norm, {})
                            results[d_norm]["tx_foreign_net"] = net_oi
                            results[d_norm]["tx_foreign_short"] = max(-net_oi, 0)
                        except ValueError:
                            pass

                # 找微型臺指期貨三大法人合計
                tmf_df = df_inst[df_inst["商品名稱"].astype(str).str.contains("微型臺指|微型台指", na=False)]
                tmf_grouped = {}
                for _, row in tmf_df.iterrows():
                    d_norm = str(row["日期"]).strip().replace("/", "-")
                    if len(d_norm) == 10:
                        try:
                            net_oi = int(str(row["多空未平倉口數淨額"]).replace(",", "").strip())
                            tmf_grouped[d_norm] = tmf_grouped.get(d_norm, 0) + net_oi
                        except ValueError:
                            pass
            else:
                tmf_grouped = {}
        else:
            tmf_grouped = {}
    except Exception:
        tmf_grouped = {}

    # 2. 下載微台（TMF）全市場每日未平倉口數 CSV，換算散戶多空比
    if tmf_grouped:
        try:
            daily_url = "https://www.taifex.com.tw/cht/3/dlFutDataDown"
            resp2 = requests.post(
                daily_url,
                data={"down_type": "1", "commodity_id": "TMF", "queryStartDate": s_str, "queryEndDate": e_str},
                headers=HEADERS,
                timeout=15,
            )
            if resp2.status_code == 200:
                text2 = _decode_taifex_csv(resp2.content)
                df_daily = pd.read_csv(io.StringIO(text2))
                df_daily.columns = [c.strip() for c in df_daily.columns]
                if {"交易日期", "未沖銷契約數"}.issubset(df_daily.columns):
                    if "交易時段" in df_daily.columns:
                        df_daily = df_daily[df_daily["交易時段"].astype(str).str.contains("一般", na=False)]
                    df_daily["未沖銷契約數"] = pd.to_numeric(df_daily["未沖銷契約數"], errors="coerce").fillna(0)
                    oi_by_date = df_daily.groupby("交易日期")["未沖銷契約數"].sum()
                    for raw_d, total_oi in oi_by_date.items():
                        d_norm = str(raw_d).strip().replace("/", "-")
                        if d_norm in tmf_grouped and total_oi > 0:
                            results.setdefault(d_norm, {})
                            results[d_norm]["retail_tmf_ratio"] = round((-tmf_grouped[d_norm] / float(total_oi)) * 100, 1)
        except Exception:
            pass

    return results


def main():
    if os.path.exists(CSV_PATH):
        df = pd.read_csv(CSV_PATH)
    else:
        df = pd.DataFrame(columns=["date", "margin_debt", "tx_foreign_net", "tx_foreign_short", "retail_tmf_ratio"])

    existing_dates = set(df["date"].astype(str)) if not df.empty else set()
    need_tx_backfill = df.empty or df["tx_foreign_short"].dropna().shape[0] < 5
    now_tw = datetime.utcnow() + timedelta(hours=8)

    # 1) 證交所融資餘額
    lookback_days = 45 if df.empty else 5
    rows_by_date = {}
    if not df.empty:
        for _, r in df.iterrows():
            rows_by_date[str(r["date"])] = r.to_dict()

    for i in range(lookback_days):
        t_date = now_tw - timedelta(days=i)
        if t_date.weekday() >= 5:
            continue
        d_str = t_date.strftime("%Y-%m-%d")
        if d_str in existing_dates and i > 1 and rows_by_date.get(d_str, {}).get("margin_debt") is not None:
            continue
        margin_val = fetch_twse_margin_for_date(t_date)
        if margin_val is not None:
            rows_by_date.setdefault(d_str, {"date": d_str})
            rows_by_date[d_str]["margin_debt"] = margin_val
        time.sleep(0.35)

    # 2) 期交所外資台指期與微台散戶多空比（若尚無歷史資料則一次回補 45 天）
    tx_lookback = 45 if need_tx_backfill else 5
    taifex_map = fetch_taifex_range_csv(now_tw - timedelta(days=tx_lookback), now_tw)
    for d_str, vals in taifex_map.items():
        rows_by_date.setdefault(d_str, {"date": d_str})
        for k, v in vals.items():
            rows_by_date[d_str][k] = v

    if rows_by_date:
        cols = ["date", "margin_debt", "tx_foreign_net", "tx_foreign_short", "retail_tmf_ratio"]
        df_out = pd.DataFrame(list(rows_by_date.values()))
        for c in cols:
            if c not in df_out.columns:
                df_out[c] = None
        df_out = df_out[cols].sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
        df_out.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
        print(f"Updated {CSV_PATH}: total {len(df_out)} rows.")
    else:
        print("No data fetched.")


if __name__ == "__main__":
    main()