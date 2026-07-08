# ==========================================
# 📓 TRADE JOURNAL & WIN-RATE MODULE
# ==========================================
# โมดูลนี้แยกออกมาจาก app_ai.py โดยตั้งใจ เพื่อไม่ให้กระทบโค้ดเดิมที่ใช้งานได้อยู่แล้ว
# หน้าที่: บันทึกทุก verdict ที่ AI ให้ไว้ลง SQLite แล้วเช็คย้อนหลังกับราคาจริงว่าแม่นแค่ไหน
#
# วิธีตัดสินแพ้/ชนะ: ไล่ดูราคารายวันตั้งแต่วันที่บันทึก verdict จนถึงวันนี้ (หรือจนหมดอายุ)
# - ถ้าสัญญาณเป็น BUY: ชนะถ้าราคาขึ้นไปแตะ Take Profit ก่อนลงไปแตะ Stop Loss
# - ถ้าสัญญาณเป็น SELL: ชนะถ้าราคาลงไปแตะ Take Profit ก่อนขึ้นไปแตะ Stop Loss
# - ถ้าแตะทั้งคู่ในวันเดียวกัน (ไม่รู้ว่าอันไหนเกิดก่อน) ตัดสินให้เป็น "แพ้" แบบระมัดระวังไว้ก่อน (conservative)
# - ถ้าผ่านไปนานเกิน max_age_days แล้วไม่แตะอันไหนเลย ปิดสถานะเป็น "expired" ไม่นับเป็นแพ้หรือชนะ
# - สัญญาณ HOLD ไม่มีเป้ากำไร/จุดตัดขาดทุนที่ตัดสินผลได้ตรงไปตรงมา จึงไม่นำมาคำนวณ win-rate

import sqlite3
import re
import pandas as pd
import yfinance as yf
from datetime import datetime

DB_FILE = 'family_portfolio.db'  # ใช้ไฟล์ DB เดียวกับ app_ai.py (อยู่ใน working directory เดียวกัน)


def init_journal_db():
    """สร้างตาราง trade_journal หากยังไม่มี (เรียกตอนเริ่มแอปได้เลย ปลอดภัยเพราะใช้ IF NOT EXISTS)"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            created_at TEXT NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            final_signal TEXT,
            risk_level TEXT,
            source TEXT,
            status TEXT DEFAULT 'open',
            outcome TEXT DEFAULT 'pending',
            outcome_price REAL,
            outcome_at TEXT
        )
    ''')
    conn.commit()
    conn.close()


def _parse_price(text):
    """
    ดึงตัวเลขแรกที่หาเจอจากข้อความ เพราะ entry_price/stop_loss/take_profit จาก AI เป็น string
    เช่น "150.20" หรือ "148-152" (ช่วงราคา) หรือมีหน่วยติดมา — เอาตัวเลขแรกที่เจอเป็นค่าประมาณ
    """
    if text is None:
        return None
    cleaned = str(text).replace(",", "")
    match = re.search(r"-?\d+(\.\d+)?", cleaned)
    if match:
        try:
            return float(match.group())
        except ValueError:
            return None
    return None


def log_verdict(ticker, claude_verdict, source="ai_debate"):
    """
    บันทึก verdict ของ Claude (จาก run_ai_debate) ลง Trade Journal หนึ่งแถว
    เรียกใช้ทุกครั้งที่ AI Debate ให้คำตัดสินใหม่ เพื่อสะสมข้อมูลไว้คำนวณ win-rate ย้อนหลัง
    """
    try:
        entry_price = _parse_price(claude_verdict.get("entry_price"))
        stop_loss = _parse_price(claude_verdict.get("stop_loss"))
        take_profit = _parse_price(claude_verdict.get("take_profit"))
        final_signal = str(claude_verdict.get("final_signal", "HOLD")).upper()
        risk_level = claude_verdict.get("risk_level", "")

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO trade_journal
            (ticker, created_at, entry_price, stop_loss, take_profit, final_signal, risk_level, source, status, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', 'pending')
        ''', (ticker, datetime.now().isoformat(), entry_price, stop_loss, take_profit,
              final_signal, risk_level, source))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Journal log error: {e}")
        return False


def _normalize_price_history(raw_df, ticker):
    """รองรับทั้งกรณี yfinance คืนคอลัมน์เดี่ยวและ MultiIndex (เปลี่ยนไปเปลี่ยนมาได้ตามเวอร์ชันไลบรารี)"""
    if isinstance(raw_df.columns, pd.MultiIndex):
        if ticker in raw_df.columns.get_level_values(0):
            return raw_df[ticker].dropna()
        elif ticker in raw_df.columns.get_level_values(1):
            return raw_df.xs(ticker, axis=1, level=1).dropna()
        else:
            return raw_df.xs(raw_df.columns.get_level_values(0)[0], axis=1, level=0).dropna()
    return raw_df.dropna()


def settle_journal_entries(max_age_days=30):
    """
    ไล่เช็ครายการที่ยังเปิดอยู่ (status='open') ทั้งหมดทีละตัว ว่าราคาหลังจากบันทึกไปแล้วแตะ
    Take Profit หรือ Stop Loss ก่อนกัน แล้วอัปเดตผลแพ้/ชนะ/หมดอายุกลับเข้า DB
    หมายเหตุ: ฟังก์ชันนี้ยิง yfinance ต่อ ticker ที่ยังเปิดอยู่ทุกตัว ควรเรียกผ่านปุ่มกดเอง
    ไม่ควรเรียกอัตโนมัติทุกครั้งที่โหลดหน้าเว็บ เพราะจะช้าและยิง request เกินจำเป็น
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, ticker, created_at, entry_price, stop_loss, take_profit, final_signal
        FROM trade_journal WHERE status = 'open'
    ''')
    rows = cursor.fetchall()

    updated, errors = 0, 0
    for entry_id, ticker, created_at, entry_price, stop_loss, take_profit, final_signal in rows:
        try:
            # HOLD หรือไม่มี SL/TP ให้ตัดสินผลตรงไปตรงมาไม่ได้ ปิดเป็น not_applicable แทน
            if final_signal not in ("BUY", "SELL") or stop_loss is None or take_profit is None:
                cursor.execute(
                    "UPDATE trade_journal SET status='closed', outcome='not_applicable' WHERE id=?",
                    (entry_id,)
                )
                updated += 1
                continue

            entry_dt = datetime.fromisoformat(created_at)
            age_days = (datetime.now() - entry_dt).days

            raw_hist = yf.download(ticker, start=entry_dt.date().isoformat(), progress=False)
            if raw_hist is None or raw_hist.empty:
                continue
            hist = _normalize_price_history(raw_hist, ticker)
            if hist.empty or "High" not in hist.columns or "Low" not in hist.columns:
                continue

            outcome, outcome_price, outcome_at = None, None, None
            for idx, bar in hist.iterrows():
                high, low = float(bar["High"]), float(bar["Low"])
                if final_signal == "BUY":
                    hit_tp, hit_sl = high >= take_profit, low <= stop_loss
                else:  # SELL
                    hit_tp, hit_sl = low <= take_profit, high >= stop_loss

                if hit_tp and hit_sl:
                    outcome, outcome_price = "loss", stop_loss  # แตะทั้งคู่วันเดียวกัน ตัดสินแบบระมัดระวัง
                elif hit_tp:
                    outcome, outcome_price = "win", take_profit
                elif hit_sl:
                    outcome, outcome_price = "loss", stop_loss

                if outcome:
                    outcome_at = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
                    break

            if outcome:
                cursor.execute(
                    "UPDATE trade_journal SET status='closed', outcome=?, outcome_price=?, outcome_at=? WHERE id=?",
                    (outcome, outcome_price, outcome_at, entry_id)
                )
                updated += 1
            elif age_days > max_age_days:
                cursor.execute(
                    "UPDATE trade_journal SET status='closed', outcome='expired' WHERE id=?",
                    (entry_id,)
                )
                updated += 1
        except Exception as e:
            print(f"Settle error for journal id {entry_id} ({ticker}): {e}")
            errors += 1
            continue

    conn.commit()
    conn.close()
    return updated, errors


def get_win_rate_stats():
    """สรุปสถิติ win-rate โดยรวม และแยกตามประเภทสัญญาณ (BUY/SELL)"""
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM trade_journal", conn)
    conn.close()

    if df.empty:
        return {
            "total": 0, "open": 0, "win": 0, "loss": 0, "expired": 0,
            "win_rate_pct": None, "by_signal": pd.DataFrame(columns=["final_signal", "win_rate_pct", "n"])
        }

    judged = df[df["outcome"].isin(["win", "loss"])]
    win_count = int((judged["outcome"] == "win").sum())
    loss_count = int((judged["outcome"] == "loss").sum())
    total_judged = win_count + loss_count
    win_rate = round(win_count / total_judged * 100, 1) if total_judged > 0 else None

    if not judged.empty:
        by_signal = (
            judged.groupby("final_signal")["outcome"]
            .agg(win_rate_pct=lambda s: round((s == "win").mean() * 100, 1), n="count")
            .reset_index()
        )
    else:
        by_signal = pd.DataFrame(columns=["final_signal", "win_rate_pct", "n"])

    return {
        "total": len(df),
        "open": int((df["status"] == "open").sum()),
        "win": win_count,
        "loss": loss_count,
        "expired": int((df["outcome"] == "expired").sum()),
        "win_rate_pct": win_rate,
        "by_signal": by_signal
    }


def get_recent_entries(limit=50):
    """ดึงรายการล่าสุดมาแสดงในตาราง UI"""
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query(
        "SELECT * FROM trade_journal ORDER BY created_at DESC LIMIT ?", conn, params=(limit,)
    )
    conn.close()
    return df


def delete_entry(entry_id):
    """ลบรายการทิ้ง (เผื่อกรณีบันทึกผิด/ทดสอบ)"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM trade_journal WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
