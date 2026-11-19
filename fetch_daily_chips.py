import io
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

CSV_PATH = "chips_history.csv"
HEADERS = {"User-Agent": "Mozilla/5.0"}


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


def _extract_net_oi(item: dict):
    """從期交所紀錄中解析未平倉淨口數（嚴格排除契約金額 Value 欄位）"""
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


def fetch_latest_taifex():
    """抓取期交所最新一日外資台指期淨口數與微台散戶多空比"""
    inst_url = "https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
    daily_urls = [
        "https://openapi.taifex.com.tw/v1/DailyMarketReportFut",
        "https://openapi.taifex.com.tw/v1/DailyMarketReport",
    ]
    res = {"date": None, "tx_foreign_net": None, "tx_foreign_short": None, "retail_tmf_ratio": None}

    try:
        resp = requests.get(inst_url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return res
        records = resp.json()
        if not isinstance(records, list):
            return res

        tmf_inst_net = 0
        tmf_found = False

        for item in records:
            vals = " ".join(str(v) for v in item.values())
            net_oi = _extract_net_oi(item)
            if net_oi is None:
                continue

            date_str = str(item.get("Date") or item.get("日期") or item.get("TradeDate") or "").strip()
            if len(date_str) == 8 and date_str.isdigit():
                date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

            if ("臺股期貨" in vals or "台股期貨" in vals) and "外資" in vals and res["tx_foreign_net"] is None:
                res["date"] = date_str or res["date"]
                res["tx_foreign_net"] = net_oi
                res["tx_foreign_short"] = max(-net_oi, 0)

            if "微型臺指" in vals or "微型台指" in vals:
                if any(role in vals for role in ("外資", "投信", "自營商")):
                    tmf_inst_net += net_oi
                    tmf_found = True
                    if not res["date"] and date_str:
                        res["date"] = date_str

        if tmf_found:
            total_tmf_oi = 0
            for d_url in daily_urls:
                try:
                    d_resp = requests.get(d_url, headers=HEADERS, timeout=10)
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

            if total_tmf_oi == 0 and res["date"]:
                try:
                    csv_url = "https://www.taifex.com.tw/cht/3/dlFutDataDown"
                    q_date = res["date"].replace("-", "/")
                    form = {"down_type": "1", "commodity_id": "TMF", "queryStartDate": q_date, "queryEndDate": q_date}
                    c_resp = requests.post(csv_url, data=form, headers=HEADERS, timeout=10)
                    if c_resp.status_code == 200 and "未沖銷契約數" in c_resp.text:
                        df_csv = pd.read_csv(io.StringIO(c_resp.text))
                        if "未沖銷契約數" in df_csv.columns:
                            if "交易時段" in df_csv.columns:
                                df_csv = df_csv[df_csv["交易時段"].astype(str).str.contains("一般", na=False)]
                            total_tmf_oi = int(pd.to_numeric(df_csv["未沖銷契約數"], errors="coerce").fillna(0).sum())
                except Exception:
                    pass

            if total_tmf_oi > 0:
                res["retail_tmf_ratio"] = round((-tmf_inst_net / total_tmf_oi) * 100, 1)
    except Exception:
        pass
    return res


def main():
    if os.path.exists(CSV_PATH):
        df = pd.read_csv(CSV_PATH)
    else:
        df = pd.DataFrame(columns=["date", "margin_debt", "tx_foreign_net", "tx_foreign_short", "retail_tmf_ratio"])

    existing_dates = set(df["date"].astype(str)) if not df.empty else set()
    now_tw = datetime.utcnow() + timedelta(hours=8)

    # 若尚無歷史資料，先自動回補過去 45 天內的證交所融資餘額
    lookback_days = 45 if df.empty else 5
    new_rows = {}

    for i in range(lookback_days):
        t_date = now_tw - timedelta(days=i)
        if t_date.weekday() >= 5:
            continue
        d_str = t_date.strftime("%Y-%m-%d")
        if d_str in existing_dates and i > 1:
            continue
        margin_val = fetch_twse_margin_for_date(t_date)
        if margin_val is not None:
            new_rows[d_str] = {
                "date": d_str,
                "margin_debt": margin_val,
                "tx_foreign_net": None,
                "tx_foreign_short": None,
                "retail_tmf_ratio": None,
            }
        time.sleep(0.35)

    # 抓取期交所最新一日資料並合併到對應日期
    taifex_latest = fetch_latest_taifex()
    if taifex_latest["date"]:
        td = taifex_latest["date"]
        if td not in new_rows:
            # 保留既有的融資欄位（若有）
            old_margin = None
            if not df.empty and td in existing_dates:
                old_margin = df.loc[df["date"] == td, "margin_debt"].iloc[0]
            new_rows[td] = {
                "date": td,
                "margin_debt": old_margin,
                "tx_foreign_net": taifex_latest["tx_foreign_net"],
                "tx_foreign_short": taifex_latest["tx_foreign_short"],
                "retail_tmf_ratio": taifex_latest["retail_tmf_ratio"],
            }
        else:
            new_rows[td]["tx_foreign_net"] = taifex_latest["tx_foreign_net"]
            new_rows[td]["tx_foreign_short"] = taifex_latest["tx_foreign_short"]
            new_rows[td]["retail_tmf_ratio"] = taifex_latest["retail_tmf_ratio"]

    if new_rows:
        df_new = pd.DataFrame(list(new_rows.values()))
        df = pd.concat([df, df_new], ignore_index=True)
        df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
        df.to_csv(CSV_PATH, index=False, encoding="utf-8-sig")
        print(f"Updated {CSV_PATH}: total {len(df)} rows.")
    else:
        print("No new data fetched.")


if __name__ == "__main__":
    main()