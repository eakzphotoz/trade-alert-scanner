"""
rating_engine.py
=================
โมดูลกลางสำหรับคำนวณ "เรตติ้งเทคนิค" (Strong Buy → Strong Sell) และ "Market Regime"
(ภาวะตลาดรวมขาขึ้น/ขาลง/ไซด์เวย์) แบบ rule-based ล้วนๆ ไม่พึ่ง AI/LLM เลย

ตั้งใจแยกออกมาจาก app_ai.py เพื่อให้:
1) ทั้งหน้าเว็บ Streamlit (app_ai.py) และสคริปต์แจ้งเตือนพื้นหลัง (alert_scanner.py ที่รันบน
   GitHub Actions) ใช้สูตรคำนวณชุดเดียวกันเป๊ะๆ ไม่มีวันเพี้ยนกันไปคนละทาง
2) โมดูลนี้ไม่ import streamlit เลย จึงรันได้ทั้งในแอปและรันเดี่ยวๆ ในสภาพแวดล้อมไม่มี streamlit
   (เช่น GitHub Actions runner) โดยไม่ error

⚠️ นี่คือการวิเคราะห์เชิงเทคนิคจากสูตรคำนวณเท่านั้น ไม่ใช่คำแนะนำการลงทุน และไม่การันตีผลกำไร
"""

import pandas as pd
import yfinance as yf

# ==========================================
# ⭐ TECHNICAL RATING (Rule-based Strong Buy → Strong Sell)
# ==========================================
# เรตติ้งนี้เลียนแบบหลักการ "Technical Rating" ของเว็บอย่าง TradingView/Yahoo คือให้อินดิเคเตอร์
# หลายตัวโหวต +1 (ฝั่งซื้อ) / -1 (ฝั่งขาย) / 0 (เป็นกลาง) แล้วเฉลี่ยเป็นคะแนน -1.0 ถึง +1.0
# จากนั้นเทียบช่วงเป็น 5 ระดับ
RATING_LEVELS = [
    (0.5, "STRONG_BUY", "ซื้อเด่นชัด", "🟢🟢"),
    (0.15, "BUY", "ซื้อ", "🟢"),
    (-0.15, "NEUTRAL", "ถือ/เป็นกลาง", "⚪"),
    (-0.5, "SELL", "ขาย", "🔴"),
    (-1.01, "STRONG_SELL", "ขายเด่นชัด", "🔴🔴"),
]

# แผนที่ดัชนีอ้างอิงสำหรับเช็คภาวะตลาดรวมของแต่ละตลาด (ใช้กับ get_market_regime)
MARKET_REGIME_INDEX = {
    "US": "^GSPC",       # S&P 500
    "TH": "^SET.BK",      # SET Index
    "Crypto": "BTC-USD",  # ใช้ BTC เป็นตัวแทนภาวะตลาดคริปโตโดยรวม
}


def compute_technical_rating(c, rsi14, macd_now, macd_prev, ema20_now, ema50_now,
                             bb_upper, bb_lower, vol_ratio, regime=None):
    """
    คำนวณเรตติ้งเทคนิครวมจากอินดิเคเตอร์หลายตัว คืน dict ที่มี code/label/icon/score/votes

    - votes: รายละเอียดว่าอินดิเคเตอร์แต่ละตัวโหวตอะไร (เอาไว้โชว์ให้ผู้ใช้เห็นว่าทำไมได้เรตนี้)
    - regime: "BULLISH" / "BEARISH" / "NEUTRAL" / None — ถ้าใส่มา จะนับเป็นอีกหนึ่งเสียงโหวต
      ให้เรตติ้งเอนเอียงตามภาวะตลาดรวมด้วย (เช่น สัญญาณซื้อรายตัว แต่ตลาดรวมเป็นขาลง คะแนนจะถูกหักลง)
      ถ้าไม่ใส่ (None) จะไม่นับโหวตนี้เลย เพื่อให้เรียกใช้แบบเดิมได้โดยไม่พัง (backward compatible)
    """
    votes = []

    # 1) RSI14: <30 oversold = ซื้อ, >70 overbought = ขาย, ช่วงกลางเป็นกลาง
    if rsi14 < 30:
        votes.append(("RSI", 1, f"Oversold ({rsi14:.0f})"))
    elif rsi14 > 70:
        votes.append(("RSI", -1, f"Overbought ({rsi14:.0f})"))
    else:
        votes.append(("RSI", 0, f"กลาง ({rsi14:.0f})"))

    # 2) MACD Histogram: บวก+กำลังยกขึ้น = ซื้อ, ลบ+กำลังกดลง = ขาย
    if macd_now > 0 and macd_now >= macd_prev:
        votes.append(("MACD", 1, "บวก & ยกตัวขึ้น"))
    elif macd_now < 0 and macd_now <= macd_prev:
        votes.append(("MACD", -1, "ลบ & กดตัวลง"))
    else:
        votes.append(("MACD", 0, "ก้ำกึ่ง"))

    # 3) เทรนด์ EMA20 vs EMA50: ราคาเหนือ EMA20 และ EMA20 เหนือ EMA50 = ขาขึ้น
    if c > ema20_now and ema20_now > ema50_now:
        votes.append(("Trend", 1, "ขาขึ้น (เหนือ EMA)"))
    elif c < ema20_now and ema20_now < ema50_now:
        votes.append(("Trend", -1, "ขาลง (ใต้ EMA)"))
    else:
        votes.append(("Trend", 0, "ไซด์เวย์"))

    # 4) ตำแหน่งเทียบ Bollinger Bands: ใกล้ขอบล่าง = ซื้อ, ใกล้ขอบบน = ขาย
    if bb_upper > bb_lower:
        bb_pos = (c - bb_lower) / (bb_upper - bb_lower)  # 0=ขอบล่าง, 1=ขอบบน
        if bb_pos < 0.25:
            votes.append(("BB", 1, "ใกล้ขอบล่าง"))
        elif bb_pos > 0.75:
            votes.append(("BB", -1, "ใกล้ขอบบน"))
        else:
            votes.append(("BB", 0, "กลางแบนด์"))
    else:
        votes.append(("BB", 0, "-"))

    # 5) Volume: วอลุ่มพุ่ง (>=1.5 เท่า) เสริมน้ำหนักไปทางทิศทางของราคาวันนี้
    if vol_ratio >= 1.5:
        if c > ema20_now:
            votes.append(("Volume", 1, f"พุ่ง x{vol_ratio} หนุนขึ้น"))
        else:
            votes.append(("Volume", -1, f"พุ่ง x{vol_ratio} กดลง"))
    else:
        votes.append(("Volume", 0, f"ปกติ x{vol_ratio}"))

    # 6) Market Regime (ถ้ามีข้อมูล): ตลาดรวมขาขึ้น/ขาลง ช่วยยืนยัน/หักล้างสัญญาณรายตัว
    if regime == "BULLISH":
        votes.append(("Regime", 1, "ตลาดรวมเป็นขาขึ้น"))
    elif regime == "BEARISH":
        votes.append(("Regime", -1, "ตลาดรวมเป็นขาลง"))
    elif regime == "NEUTRAL":
        votes.append(("Regime", 0, "ตลาดรวมไซด์เวย์"))
    # ถ้า regime เป็น None หรือ "UNKNOWN" จะไม่นับโหวตนี้เลย

    score = round(sum(v[1] for v in votes) / len(votes), 3)

    for threshold, code, label, icon in RATING_LEVELS:
        if score >= threshold:
            return {"code": code, "label": label, "icon": icon, "score": score, "votes": votes}
    last = RATING_LEVELS[-1]
    return {"code": last[1], "label": last[2], "icon": last[3], "score": score, "votes": votes}


def calculate_indicators_from_history(df):
    """
    รับ DataFrame ราคาหุ้นตัวเดียว (ต้องมีคอลัมน์ Open/High/Low/Close/Volume, เรียงวันจากเก่า→ใหม่)
    คืน dict ค่าของอินดิเคเตอร์ล่าสุด พร้อมสำหรับส่งเข้า compute_technical_rating() ต่อได้เลย
    ใช้เวลาต้องการค่าล่าสุดของหุ้นตัวเดียว (เช่น ในสคริปต์แจ้งเตือนพื้นหลัง) โดยไม่ต้องพึ่งพา
    Streamlit หรือโค้ดสแกนแบบ batch ของ app_ai.py
    """
    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).copy()
    if len(df) < 25:  # ข้อมูลน้อยเกินไปคำนวณอินดิเคเตอร์ (ต้องการอย่างน้อย ~20 วันสำหรับ MA20/BB)
        return None

    close = df["Close"]
    volume = df["Volume"]

    ma20 = close.rolling(window=20).mean()
    std20 = close.rolling(window=20).std()
    bb_upper = (ma20 + std20 * 2).iloc[-1]
    bb_lower = (ma20 - std20 * 2).iloc[-1]

    delta = close.diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = -delta.where(delta < 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
    rsi14 = (100 - (100 / (1 + (gain / (loss + 1e-10))))).iloc[-1]

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - signal_line

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    vol_ma = volume.rolling(window=20).mean().iloc[-1]
    vol_now = volume.iloc[-1]
    vol_ratio = round(float(vol_now) / float(vol_ma), 2) if vol_ma and vol_ma > 0 else 1.0

    prev_close = close.iloc[-2] if len(close) > 1 else close.iloc[-1]
    change_pct = round((close.iloc[-1] - prev_close) / prev_close * 100, 2) if prev_close else 0.0

    return {
        "price": float(close.iloc[-1]),
        "change_pct": change_pct,
        "rsi14": float(rsi14),
        "macd_now": float(macd_hist.iloc[-1]),
        "macd_prev": float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else float(macd_hist.iloc[-1]),
        "ema20_now": float(ema20.iloc[-1]),
        "ema50_now": float(ema50.iloc[-1]),
        "bb_upper": float(bb_upper),
        "bb_lower": float(bb_lower),
        "vol_ratio": vol_ratio,
    }


def get_market_regime(index_ticker):
    """
    เช็คภาวะตลาดรวม (BULLISH/BEARISH/NEUTRAL) โดยเทียบราคาปัจจุบันของดัชนีอ้างอิง
    (เช่น ^GSPC, ^SET.BK, BTC-USD) กับเส้นค่าเฉลี่ย 200 วัน (หรือ 50 วันถ้าข้อมูลไม่พอ)
    หลักการ: ราคาเหนือเส้นค่าเฉลี่ยระยะยาว = ภาวะตลาดเป็นขาขึ้นโดยรวม, ใต้เส้น = ขาลง
    คืน dict ไม่มี exception หลุดออกไป (คืน regime="UNKNOWN" แทนถ้าดึงข้อมูลไม่สำเร็จ)
    """
    try:
        hist = yf.Ticker(index_ticker).history(period="1y", interval="1d", auto_adjust=False)
        hist = hist.dropna(subset=["Close"])
        if hist.empty:
            return {"regime": "UNKNOWN", "price": None, "ma": None, "diff_pct": None, "window": None}

        close = hist["Close"]
        window = 200 if len(close) >= 200 else max(min(50, len(close)), 1)
        ma = close.rolling(window=window).mean().iloc[-1]
        price = float(close.iloc[-1])

        if pd.isna(ma):
            return {"regime": "UNKNOWN", "price": price, "ma": None, "diff_pct": None, "window": window}

        diff_pct = round((price - float(ma)) / float(ma) * 100, 2)
        if diff_pct > 1.0:
            regime = "BULLISH"
        elif diff_pct < -1.0:
            regime = "BEARISH"
        else:
            regime = "NEUTRAL"

        return {"regime": regime, "price": price, "ma": float(ma), "diff_pct": diff_pct, "window": window}
    except Exception as e:
        print(f"[rating_engine] Market regime fetch error for {index_ticker}: {e}")
        return {"regime": "UNKNOWN", "price": None, "ma": None, "diff_pct": None, "window": None}
