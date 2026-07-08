"""
alert_scanner.py
=================
สคริปต์แจ้งเตือนพื้นหลัง ออกแบบให้รันแบบ scheduled (เช่นบน GitHub Actions cron) โดย:

1. สแกนหุ้นด้วยสูตร rule-based เดียวกับในแอป (ใช้ rating_engine.py ร่วมกัน) — ขั้นตอนนี้
   "ไม่เรียก AI เลยแม้แต่ครั้งเดียว" ใช้แค่ yfinance ซึ่งฟรี จึงรันถี่แค่ไหนก็ไม่เสีย token
2. เทียบเรตติ้งล่าสุดกับสถานะที่บันทึกไว้ครั้งก่อน (alert_state.json) — แจ้งเตือนเฉพาะตอนที่
   เรตติ้ง "เปลี่ยนระดับเข้าสู่ STRONG_BUY หรือ STRONG_SELL" เท่านั้น (ปรับได้ผ่าน ALERT_LEVELS)
   ไม่แจ้งซ้ำทุกรอบที่สแกนแล้วเจอเรตติ้งเดิม กันสแปม
3. ส่งข้อความแจ้งเตือนผ่าน Telegram Bot (ฟรี ไม่จำกัดจำนวนข้อความสำหรับใช้ส่วนตัว)

🔍 ขอบเขตหุ้นที่สแกน (ไม่ต้องพิมพ์ ticker เองทั้งหมด):
    - ตลาด US: ดึงรายชื่อ NASDAQ-100 มาสแกนอัตโนมัติทุกรอบ (จาก Wikipedia) รวมกับหุ้นที่พี่
      เพิ่มเองใน alert_watchlist.json["US"] (เช่น MU ที่ไม่ได้อยู่ใน NASDAQ-100) — ไม่ทับกัน รวมกัน
    - ตลาด TH / Crypto: ยังใช้รายชื่อจาก alert_watchlist.json ตามที่พี่กำหนดไว้ (ยังไม่ได้ทำ
      ดึงอัตโนมัติ เพราะ SET ไม่มีดัชนี "100 ตัวใหญ่สุด" แบบเป็นทางการที่ดึงฟรีได้ง่ายเท่า NASDAQ)
    - ปิดโหมดดึง NASDAQ-100 อัตโนมัติได้ด้วย env var US_UNIVERSE=watchlist_only

การตั้งค่า (ผ่าน environment variables หรือ GitHub Actions secrets):
    TELEGRAM_BOT_TOKEN   - token ของบอทที่สร้างผ่าน @BotFather
    TELEGRAM_CHAT_ID     - chat id ของพี่ (หาได้จากคุยกับ @userinfobot ใน Telegram)
    ALERT_LEVELS         - (optional) รายชื่อเรตติ้งที่จะแจ้งเตือน คั่นด้วยจุลภาค
                            ค่าเริ่มต้น: "STRONG_BUY,STRONG_SELL"
    US_UNIVERSE          - (optional) "nasdaq100" (ค่าเริ่มต้น) หรือ "watchlist_only"

รันด้วยมือ (ทดสอบ):
    python alert_scanner.py

⚠️ นี่เป็นการแจ้งเตือนเชิงเทคนิคจากสูตรคำนวณเท่านั้น ไม่ใช่คำแนะนำการลงทุน โปรดตัดสินใจด้วยวิจารณญาณของตัวเอง
"""

import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf

from rating_engine import (
    MARKET_REGIME_INDEX,
    calculate_indicators_from_history,
    compute_technical_rating,
    get_market_regime,
)

WATCHLIST_FILE = "alert_watchlist.json"
STATE_FILE = "alert_state.json"

US_UNIVERSE_MODE = os.environ.get("US_UNIVERSE", "nasdaq100").strip().lower()

ALERT_LEVELS = set(
    lvl.strip().upper()
    for lvl in os.environ.get("ALERT_LEVELS", "STRONG_BUY,STRONG_SELL").split(",")
    if lvl.strip()
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def fetch_data_with_header(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read()


def fetch_nasdaq100_tickers():
    """ดึงรายชื่อหุ้น NASDAQ-100 สดจาก Wikipedia (100 ตัวใหญ่สุดในตลาด NASDAQ ตอนนี้)
    คืน list ของ ticker หรือ None ถ้าดึงไม่สำเร็จ (เช่นเน็ตมีปัญหา/Wikipedia เปลี่ยนโครงสร้างหน้า)"""
    try:
        html_bytes = fetch_data_with_header('https://en.wikipedia.org/wiki/Nasdaq-100')
        tables = pd.read_html(io.StringIO(html_bytes.decode('utf-8')))
        for df in tables:
            col = 'Ticker' if 'Ticker' in df.columns else ('Symbol' if 'Symbol' in df.columns else None)
            if col:
                tickers = [str(t).strip().upper() for t in df[col].tolist() if str(t).strip()]
                if len(tickers) >= 50:  # เช็คคร่าวๆ ว่าได้ตารางที่ถูกต้องจริง (ควรมีประมาณ 100 ตัว)
                    return tickers
        print("⚠️ ดึง NASDAQ-100 จาก Wikipedia ได้ แต่หาคอลัมน์ ticker ที่ถูกต้องไม่เจอ")
        return None
    except Exception as e:
        print(f"⚠️ ดึงรายชื่อ NASDAQ-100 ไม่สำเร็จ: {e}")
        return None


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ อ่านไฟล์ {path} ไม่สำเร็จ ใช้ค่าเริ่มต้นแทน: {e}")
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ ไม่ได้ตั้งค่า TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — ข้ามการส่งข้อความ (แค่ print ให้ดู)")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"⚠️ ส่ง Telegram ไม่สำเร็จ ({resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"⚠️ ส่ง Telegram error: {e}")


def scan_watchlist_market(tickers, market_type):
    """สแกนหุ้นทั้งหมดในตลาดเดียว คืน list ของ dict ผลลัพธ์ (rule-based ล้วนๆ ไม่เรียก AI)"""
    if not tickers:
        return []

    regime_info = get_market_regime(MARKET_REGIME_INDEX.get(market_type))
    regime = regime_info.get("regime")

    results = []
    try:
        raw_df = yf.download(
            " ".join(tickers), period="6mo", interval="1d",
            group_by="ticker", auto_adjust=False, progress=False, threads=True,
        )
    except Exception as e:
        print(f"⚠️ ดึงข้อมูล batch ตลาด {market_type} ไม่สำเร็จ: {e}")
        return []

    for t in tickers:
        try:
            if isinstance(raw_df.columns, pd.MultiIndex):
                if t not in raw_df.columns.get_level_values(0):
                    continue
                df = raw_df[t].dropna()
            else:
                df = raw_df.dropna()

            ind = calculate_indicators_from_history(df)
            if ind is None:
                continue

            rating = compute_technical_rating(
                c=ind["price"], rsi14=ind["rsi14"], macd_now=ind["macd_now"], macd_prev=ind["macd_prev"],
                ema20_now=ind["ema20_now"], ema50_now=ind["ema50_now"],
                bb_upper=ind["bb_upper"], bb_lower=ind["bb_lower"], vol_ratio=ind["vol_ratio"],
                regime=regime,
            )
            results.append({
                "ticker": t,
                "market": market_type,
                "price": round(ind["price"], 2),
                "change_pct": ind["change_pct"],
                "rating_code": rating["code"],
                "rating_label": rating["label"],
                "rating_icon": rating["icon"],
                "rating_score": rating["score"],
                "regime": regime,
            })
        except Exception as e:
            print(f"⚠️ สแกน {t} error: {e}")
            continue

    return results


def format_alert_message(item, prev_code):
    arrow = f"{prev_code or '-'} → {item['rating_code']}"
    change_icon = "🟢" if item["change_pct"] >= 0 else "🔴"
    return (
        f"⭐ <b>{item['rating_icon']} {item['rating_label']}</b> — {item['ticker']} ({item['market']})\n"
        f"ราคา: {item['price']} | {change_icon} {item['change_pct']:+.2f}%\n"
        f"คะแนนเทคนิค: {item['rating_score']:+.2f}\n"
        f"เรตติ้งเปลี่ยน: {arrow}\n"
        f"เวลา: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"\n⚠️ วิเคราะห์เชิงเทคนิคจากสูตรคำนวณเท่านั้น ไม่ใช่คำแนะนำการลงทุน"
    )


def main():
    watchlist = load_json(WATCHLIST_FILE, {})
    if not watchlist:
        print(f"❌ ไม่พบไฟล์ {WATCHLIST_FILE} หรือไฟล์ว่างเปล่า ยกเลิกการสแกน")
        sys.exit(1)

    # 🔍 ตลาด US: รวมรายชื่อ NASDAQ-100 (ดึงสดอัตโนมัติ) เข้ากับหุ้นที่พี่เพิ่มเองใน watchlist
    # (ปิดได้ด้วย US_UNIVERSE=watchlist_only ถ้าอยากคุมรายชื่อเองล้วนๆ แบบเดิม)
    if US_UNIVERSE_MODE != "watchlist_only":
        manual_us = watchlist.get("US", [])
        nasdaq100 = fetch_nasdaq100_tickers()
        if nasdaq100:
            combined = sorted(set(nasdaq100) | set(t.upper() for t in manual_us))
            print(f"🌐 รวม NASDAQ-100 ({len(nasdaq100)} ตัว) + watchlist เอง ({len(manual_us)} ตัว) "
                  f"= {len(combined)} ตัว (ตัดตัวซ้ำแล้ว)")
            watchlist["US"] = combined
        else:
            # แม้ดึง NASDAQ-100 ไม่สำเร็จ ก็ยัง dedupe watchlist เองด้วย กันสแกนซ้ำ (เช่น MU ที่ใส่ซ้ำ)
            watchlist["US"] = sorted(set(t.upper() for t in watchlist.get("US", [])))
            print("⚠️ ดึง NASDAQ-100 ไม่สำเร็จรอบนี้ ใช้แค่ watchlist ที่พี่กำหนดเองแทน")

    state = load_json(STATE_FILE, {})
    new_state = dict(state)
    alerts_sent = 0

    for market_type, tickers in watchlist.items():
        print(f"🔍 สแกนตลาด {market_type} ({len(tickers)} ตัว)...")
        results = scan_watchlist_market(tickers, market_type)

        for item in results:
            key = f"{market_type}:{item['ticker']}"
            prev_code = state.get(key)
            new_state[key] = item["rating_code"]

            print(f"   {item['ticker']:8s} {prev_code or '-':12s} -> {item['rating_code']:12s} (score={item['rating_score']:+.2f})")

            # แจ้งเตือนเฉพาะตอนเรตติ้ง "เปลี่ยน" และเรตติ้งใหม่อยู่ในระดับที่สนใจ (กัน spam ซ้ำเรตเดิม)
            if item["rating_code"] != prev_code and item["rating_code"] in ALERT_LEVELS:
                send_telegram_message(format_alert_message(item, prev_code))
                alerts_sent += 1

    save_json(STATE_FILE, new_state)
    print(f"\n✅ สแกนเสร็จ ส่งแจ้งเตือนทั้งหมด {alerts_sent} รายการ")


if __name__ == "__main__":
    main()
