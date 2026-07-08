# ==========================================
# 📰 NEWS SENTIMENT MODULE
# ==========================================
# โมดูลนี้แยกออกมาต่างหาก (เหมือน journal.py) เพื่อไม่กระทบโค้ดเดิมใน app_ai.py
# หน้าที่: ดึงข่าวล่าสุดของหุ้นแต่ละตัว (ฟรีจาก yfinance) แล้วให้ Claude Haiku
# แท็ก sentiment (positive/negative/neutral) พร้อมเช็คว่าเป็นข่าวเฉพาะตัว หรือภาพรวมตลาด/sector
# เก็บผลลง SQLite ไฟล์เดียวกับ family_portfolio.db เพื่อให้ app_ai.py และ scheduled job อ่านร่วมกันได้
#
# วิธีใช้ใน app_ai.py:
#   import news
#   news.init_news_db()                                  # เรียกตอน setup เหมือน journal.init_journal_db()
#   news.refresh_news(ticker, ANTHROPIC_API_KEY)          # ดึง+วิเคราะห์+บันทึกล่าสุด (ควร cache ด้วย st.cache_data ที่ app_ai.py)
#   flags = news.get_latest_flags(ticker)                 # เอามาโชว์ badge บน UI
#   context = news.get_news_context(ticker)               # เอามาแปะเพิ่มใน prompt ของ claude_challenge_and_verdict

import sqlite3
import json
from datetime import datetime, timedelta

import yfinance as yf
import anthropic

DB_FILE = 'family_portfolio.db'  # ใช้ไฟล์ DB เดียวกับ app_ai.py และ journal.py

MARKET_PROXIES = {
    "default": "SPY",
    "semiconductor": "SMH",
    "tech": "QQQ",
}


def init_news_db():
    """สร้างตาราง news_flags หากยังไม่มี (เรียกตอนเริ่มแอปได้เลย ปลอดภัยเพราะใช้ IF NOT EXISTS)"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS news_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            headline TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            reasoning TEXT,
            published_at TEXT,
            fetched_at TEXT NOT NULL,
            market_wide INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()


def _fetch_raw_news(ticker, max_items=5):
    """ดึงหัวข้อข่าวล่าสุดของ ticker จาก yfinance (ฟรี ไม่ต้องขอ API key เพิ่ม)"""
    try:
        news_items = yf.Ticker(ticker).news or []
    except Exception as e:
        print(f"[news] ดึงข่าว {ticker} ไม่สำเร็จ: {e}")
        return []

    results = []
    for item in news_items[:max_items]:
        content = item.get("content", item)
        title = content.get("title") or item.get("title", "")
        pub_date = content.get("pubDate") or item.get("providerPublishTime", "")
        if title:
            results.append({"headline": title, "published_at": str(pub_date)})
    return results


def _check_market_wide_move(proxy_ticker="SPY", threshold_pct=-1.0):
    """เช็คว่า ETF ภาพรวมตลาด/sector ร่วงเกิน threshold ในวันล่าสุดหรือไม่ (สำหรับแยกว่าเป็นข่าวเฉพาะตัวหรือทั้งตลาด)"""
    try:
        hist = yf.Ticker(proxy_ticker).history(period="2d")
        if len(hist) < 2:
            return False
        change_pct = (hist["Close"].iloc[-1] / hist["Close"].iloc[-2] - 1) * 100
        return change_pct <= threshold_pct
    except Exception as e:
        print(f"[news] เช็คตลาดรวม {proxy_ticker} ไม่สำเร็จ: {e}")
        return False


def _analyze_sentiment(ticker, headlines, api_key):
    """ส่งหัวข้อข่าวเข้า Claude Haiku ให้แท็ก sentiment + สรุปเหตุผลสั้นๆ (ถูก เร็ว พอสำหรับงานนี้)"""
    if not headlines or not api_key:
        return []

    client = anthropic.Anthropic(api_key=api_key)
    headlines_text = "\n".join(f"- {h['headline']} (เผยแพร่: {h['published_at']})" for h in headlines)

    prompt = f"""วิเคราะห์ sentiment ของข่าวหุ้น {ticker} ต่อไปนี้ ตอบเป็น JSON array เท่านั้น ไม่มีข้อความอื่น ไม่มี markdown fence:

{headlines_text}

รูปแบบ JSON: [{{"headline": "...", "sentiment": "positive|negative|neutral", "reasoning": "เหตุผลสั้นๆ ภาษาไทยง่ายๆ ไม่เกิน 1 ประโยค"}}]"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text.strip()
        raw_text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(raw_text)
        for p, h in zip(parsed, headlines):
            p["published_at"] = h["published_at"]
        return parsed
    except Exception as e:
        print(f"[news] วิเคราะห์ sentiment {ticker} ไม่สำเร็จ: {e}")
        return []


def _save_flags(ticker, sentiments, market_wide):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    fetched_at = datetime.now().isoformat()
    for s in sentiments:
        cursor.execute(
            '''INSERT INTO news_flags
               (ticker, headline, sentiment, reasoning, published_at, fetched_at, market_wide)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (ticker, s.get("headline", ""), s.get("sentiment", "neutral"), s.get("reasoning", ""),
             s.get("published_at", ""), fetched_at, int(market_wide))
        )
    conn.commit()
    conn.close()


def refresh_news(ticker, api_key, market_proxy="SPY"):
    """
    ฟังก์ชันรวม: ดึงข่าวใหม่ -> วิเคราะห์ sentiment -> เช็ค market-wide -> บันทึกลง DB
    เรียกจาก app_ai.py ควรครอบด้วย @st.cache_data(ttl=...) เพื่อไม่ยิงซ้ำทุกครั้งที่ rerun หน้าเว็บ
    คืนค่า list ของ dict ที่บันทึกไป (เผื่อจะโชว์ทันทีโดยไม่ต้อง query ซ้ำ)
    """
    headlines = _fetch_raw_news(ticker)
    if not headlines:
        return []
    is_market_wide = _check_market_wide_move(market_proxy)
    sentiments = _analyze_sentiment(ticker, headlines, api_key)
    if sentiments:
        _save_flags(ticker, sentiments, is_market_wide)
        for s in sentiments:
            s["market_wide"] = is_market_wide
    return sentiments


def get_latest_flags(ticker, hours=48):
    """ดึง flag ล่าสุดของ ticker จาก DB ในชั่วโมงที่กำหนด ใช้โชว์ badge บน UI"""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        '''SELECT * FROM news_flags WHERE ticker = ? AND fetched_at >= ?
           ORDER BY fetched_at DESC''',
        (ticker, cutoff)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_news_context(ticker, hours=48):
    """
    สรุปข่าว+sentiment เป็นข้อความสั้นๆ ภาษาไทย สำหรับแปะเพิ่มใน prompt ของ claude_challenge_and_verdict
    คืนค่า "" ถ้าไม่มีข่าว (จะไม่กระทบ prompt เดิมเลยถ้าไม่มีข่าว)
    """
    flags = get_latest_flags(ticker, hours=hours)
    if not flags:
        return ""

    lines = []
    for f in flags:
        tag = " [ข่าวภาพรวมตลาด ไม่ใช่ปัจจัยเฉพาะตัวหุ้น]" if f["market_wide"] else ""
        lines.append(f"- ({f['sentiment']}) {f['headline']}{tag} — {f['reasoning']}")
    return "ข่าวล่าสุดที่เกี่ยวข้อง:\n" + "\n".join(lines)