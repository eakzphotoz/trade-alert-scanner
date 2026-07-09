import streamlit as st
import streamlit.components.v1 as components
from streamlit_lightweight_charts import renderLightweightCharts
import yfinance as yf
import pandas as pd
import numpy as np
import urllib.request
import io
import json
import re
import random
import sqlite3
import os
import time
from datetime import datetime
from pydantic import BaseModel

from google import genai 
from google.genai import types
try:
    import anthropic
except ImportError:
    pass  # รองรับเผื่อบางสภาพแวดล้อมไม่มีไลบรารี anthropic

import journal  # 📓 โมดูลใหม่: Trade Journal + Win-Rate (แยกไฟล์ ไม่กระทบโค้ดเดิม)
import news  # 📰 โมดูลใหม่: News Sentiment (แยกไฟล์ ใช้ DB เดียวกัน ไม่กระทบโค้ดเดิม)

# --- ⚙️ การตั้งค่าหน้าเว็บ (Premium Dark Theme) ---
st.set_page_config(
    page_title="PropFirmX - AI Debate & Shared Portfolio Terminal", 
    layout="wide", 
    page_icon="◆",
    initial_sidebar_state="expanded"
)

# ดึง API Key จาก Secrets / Env
def get_secret(key: str) -> str:
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key, "")

GEMINI_API_KEY = get_secret("GEMINI_API_KEY")
ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY")

# ==========================================
# 🗄️ DATABASE SETUP (Persistent Storage)
# ==========================================
DB_FILE = 'family_portfolio.db'

# รายชื่อตารางที่อนุญาตให้ใช้งานเท่านั้น (ป้องกัน SQL Injection ผ่านชื่อตาราง
# แม้ปัจจุบัน table_name จะมาจากค่าคงที่ในโค้ดเท่านั้น แต่กันไว้เผื่ออนาคตมีการรับชื่อตารางจาก UI/input)
ALLOWED_PORTFOLIO_TABLES = {'port_us', 'port_th', 'port_crypto'}

def _validate_table_name(table_name):
    if table_name not in ALLOWED_PORTFOLIO_TABLES:
        raise ValueError(f"ชื่อตารางไม่ได้รับอนุญาต: {table_name}")

def init_db():
    """สร้างตารางฐานข้อมูล SQLite สำหรับเก็บพอร์ตร่วมกัน หากยังไม่มี"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    tables = {
        'port_us': [("AAPL", 15.0, 172.50), ("NVDA", 25.0, 110.00)],
        'port_th': [("PTT.BK", 1000.0, 32.50), ("CPALL.BK", 500.0, 57.00)],
        'port_crypto': [("BTC-USD", 0.05, 61500.00), ("ETH-USD", 0.50, 3100.00)]
    }
    
    for table_name, default_data in tables.items():
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS {table_name} (
                Ticker TEXT PRIMARY KEY,
                Shares REAL,
                AvgCost REAL
            )
        ''')
        cursor.execute(f'SELECT COUNT(*) FROM {table_name}')
        if cursor.fetchone()[0] == 0:
            cursor.executemany(f'INSERT INTO {table_name} (Ticker, Shares, AvgCost) VALUES (?, ?, ?)', default_data)
            
    conn.commit()
    conn.close()

def load_portfolio(table_name):
    """
    อ่านข้อมูลพอร์ตจาก SQLite เป็น DataFrame
    Normalize Ticker เป็นตัวพิมพ์ใหญ่เสมอ เพราะ yfinance/Yahoo คืนคอลัมน์ราคาเป็นตัวพิมพ์ใหญ่
    ถ้าไม่ normalize ตรงนี้ การจับคู่หาราคาตลาดจะพังเงียบๆ (ตกไปใช้ AvgCost แทนโดยไม่มี error)
    """
    try:
        _validate_table_name(table_name)
        conn = sqlite3.connect(DB_FILE)
        df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
        conn.close()
        if not df.empty and "Ticker" in df.columns:
            df["Ticker"] = df["Ticker"].astype(str).str.strip().str.upper()
        return df
    except Exception as e:
        st.error(f"Database Load Error ({table_name}): {e}")
        return pd.DataFrame(columns=["Ticker", "Shares", "AvgCost"])

def save_portfolio(table_name, df):
    """
    บันทึก DataFrame ลงตาราง SQLite ด้วยวิธี Upsert (อัปเดต/เพิ่ม/ลบเฉพาะแถวที่เปลี่ยนแปลงจริง)
    แทนการ DROP/REPLACE ตารางทั้งก้อนแบบเดิม เพื่อลดความเสี่ยงข้อมูลหายเวลามีคนแก้พอร์ต
    พร้อมกันจากหลายอุปกรณ์ (เช่น คู่รักเปิดพร้อมกันคนละมือถือ)

    หมายเหตุข้อจำกัด: วิธีนี้ลดความเสี่ยงจากการล้างตารางทั้งก้อน แต่ถ้าสองคนแก้ "แถวเดียวกัน"
    พร้อมกันเป๊ะๆ ระบบยังเป็นแบบ Last-Write-Wins อยู่ดี — ถ้าต้องการแก้ปัญหานี้ทั้งหมดต้องเพิ่มระบบ
    version/timestamp column + optimistic locking ซึ่งซับซ้อนกว่านี้
    """
    try:
        _validate_table_name(table_name)
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        clean_df = df.dropna(subset=["Ticker"]).copy()
        clean_df["Ticker"] = clean_df["Ticker"].astype(str).str.strip().str.upper()
        clean_df = clean_df[clean_df["Ticker"] != ""]

        current_tickers = set(clean_df["Ticker"].tolist())

        cursor.execute(f'SELECT Ticker FROM {table_name}')
        existing_tickers = {row[0] for row in cursor.fetchall()}

        tickers_to_delete = existing_tickers - current_tickers
        if tickers_to_delete:
            cursor.executemany(
                f'DELETE FROM {table_name} WHERE Ticker = ?',
                [(t,) for t in tickers_to_delete]
            )

        upsert_rows = list(
            clean_df[["Ticker", "Shares", "AvgCost"]].itertuples(index=False, name=None)
        )
        if upsert_rows:
            cursor.executemany(
                f'INSERT OR REPLACE INTO {table_name} (Ticker, Shares, AvgCost) VALUES (?, ?, ?)',
                upsert_rows
            )

        conn.commit()
        conn.close()
    except Exception as e:
        st.error(f"Database Save Error ({table_name}): {e}")

# เริ่มต้นสร้าง Database (รันครั้งแรก)
if not os.path.exists(DB_FILE):
    init_db()
else:
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.close()
    except sqlite3.Error as e:
        print(f"DB file corrupted or unreadable, recreating: {e}")
        init_db()

journal.init_journal_db()  # 📓 สร้างตาราง trade_journal ถ้ายังไม่มี (ปลอดภัย ใช้ IF NOT EXISTS)
news.init_news_db()  # 📰 สร้างตาราง news_flags ถ้ายังไม่มี (ปลอดภัย ใช้ IF NOT EXISTS)

# ==========================================
# 📊 SCHEMAS & MODELS FOR DEBATE
# ==========================================
class GeminiOpinion(BaseModel):
    market_sentiment: str       # มุมมองตลาดโดยรวม
    initial_signal: str         # BUY / SELL / HOLD (มุมมองแรก)
    key_observation: str        # สิ่งที่สังเกตเห็นจากข้อมูล
    confidence: str             # สูง / กลาง / ต่ำ

class ClaudeVerdict(BaseModel):
    agrees_with_gemini: bool
    final_signal: str           # BUY / SELL / HOLD
    risk_level: str             # ต่ำ / กลาง / สูง
    support_zone: str
    resistance_zone: str
    challenge_notes: str        # สิ่งที่ Claude ท้าทาย/แก้ไขจาก Gemini
    final_reasoning: str        # เหตุผลสรุปสุดท้าย
    action_summary: str         # สรุปสั้น 1-2 บรรทัดเข้าใจง่าย
    entry_price: str            # ราคา/ช่วงราคาที่ควรเข้าซื้อ
    stop_loss: str              # จุดตัดขาดทุน
    take_profit: str            # เป้ากำไร
    position_sizing_note: str   # คำแนะนำเรื่องการบริหารความเสี่ยง

class PennyStockQualityItem(BaseModel):
    ticker: str
    quality_flag: str   # "ปกติ" / "ระมัดระวังสูง" / "ไม่แน่ใจ"
    reason: str          # เหตุผลสั้นๆ 1 ประโยค

class PennyStockQualityBatch(BaseModel):
    assessments: list[PennyStockQualityItem]

# --- 🔄 ระบบจำข้อมูลและสถานะเว็บ ---
if 'active_ticker' not in st.session_state:
    st.session_state.active_ticker = "AAPL"

# 🖱️ รองรับการคลิกการ์ดหุ้น: การ์ดแต่ละใบเป็นลิงก์ ?view=TICKER พอคลิกจะมาตั้ง active_ticker
# ให้กราฟใหญ่ด้านบนอัปเดตทันที แล้วล้าง query param ทิ้งเพื่อไม่ให้ค้างเวลารีเฟรช
_view_ticker = st.query_params.get("view")
if _view_ticker:
    st.session_state.active_ticker = str(_view_ticker).strip().upper()
    st.session_state.ai_debate_result = None
    st.session_state.chat_history = []
    st.query_params.clear()

if 'ai_debate_result' not in st.session_state:
    st.session_state.ai_debate_result = None
if 'timeframe' not in st.session_state:
    st.session_state.timeframe = "6M (รายวัน)"
if 'chat_history' not in st.session_state:
    st.session_state.chat_history = []


# รายชื่อแถบวิ่งด้านบนสุด
TAPE_SYMBOLS = ["NASDAQ:AAPL", "NASDAQ:MSFT", "NASDAQ:NVDA", "NASDAQ:AMZN", "FX:EURUSD", "BITSTAMP:BTCUSD", "CMCMARKETS:GOLD"]

tf_mapping = {
    "1D (1 นาที)": {"period": "1d", "interval": "1m", "tv": "1"},
    "1W (15 นาที)": {"period": "7d", "interval": "15m", "tv": "15"},
    "1M (รายวัน)": {"period": "1mo", "interval": "1d", "tv": "D"},
    "6M (รายวัน)": {"period": "6mo", "interval": "1d", "tv": "D"},
    "1Y (รายสัปดาห์)": {"period": "1y", "interval": "1wk", "tv": "W"}
}
current_tf = tf_mapping[st.session_state.timeframe]

# --- 🎨 การฉีด CSS เพื่อคุมธีมสี Dark ระดับพรีเมียม (Hybrid AI Style) ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
    
    :root {
        --bg: #0a0c10;
        --panel: #111622;
        --panel-2: #181c25;
        --border: #1e293b;
        --text: #f1f5f9;
        --gemini: #4fb3a9;
        --claude: #d97757;
        --verdict: #c9a86a;
        --green: #10b981;
        --red: #ef4444;
    }
    
    html, body, [class*="css"] { font-family: 'JetBrains Mono', monospace; }
    .stApp { background-color: var(--bg); color: var(--text); }
    h1, h2, h3, h4 { font-family: 'Space Grotesk', sans-serif !important; letter-spacing: -0.02em; }
    
    .prop-card { background-color: var(--panel); border: 1px solid var(--border); padding: 16px; border-radius: 12px; margin-bottom: 15px; }
    .chat-bubble-user { background-color: #1e293b; border-radius: 10px; padding: 10px; margin-bottom: 8px; border-left: 4px solid #38bdf8; text-align: left; }
    .chat-bubble-ai { background-color: #161b26; border-radius: 10px; padding: 10px; margin-bottom: 8px; border-left: 4px solid var(--verdict); text-align: left; }
    
    /* VS Banner */
    .vs-banner {
        display: flex; align-items: center; gap: 0; border-radius: 12px; overflow: hidden;
        border: 1px solid var(--border); margin-bottom: 20px;
    }
    .vs-side { flex: 1; padding: 16px 20px; position: relative; }
    .vs-gemini { background: linear-gradient(135deg, rgba(79,179,169,0.12), rgba(79,179,169,0.03)); border-right: 1px solid var(--border); }
    .vs-claude { background: linear-gradient(135deg, rgba(217,119,87,0.03), rgba(217,119,87,0.12)); }
    .vs-label { font-family: 'Space Grotesk', sans-serif; font-weight: 600; font-size: 0.95rem; }
    .vs-gemini .vs-label { color: var(--gemini); }
    .vs-claude .vs-label { color: var(--claude); }
    .vs-sub { color: #7b8494; font-size: 0.72rem; margin-top: 2px; }
    .vs-divider { font-family: 'Space Grotesk', sans-serif; font-weight: 700; color: var(--verdict); padding: 0 10px; font-size: 1.1rem; }

    /* Verdict Box & Action Plan */
    .verdict-box {
        background: linear-gradient(135deg, rgba(201,168,106,0.10), rgba(201,168,106,0.02));
        border: 1px solid rgba(201,168,106,0.35); border-radius: 12px; padding: 20px; margin-top: 15px;
    }
    .verdict-signal { font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 1.8rem; letter-spacing: 0.02em; }
    .signal-buy { color: var(--green); }
    .signal-sell { color: var(--red); }
    .signal-hold { color: var(--verdict); }
    
    .pill { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 0.72rem; font-weight: 500; border: 1px solid var(--border); color: #7b8494; margin-right: 5px;}
    .pill-agree { color: var(--green); border-color: rgba(16,185,129,0.4); background: rgba(16,185,129,0.08); }
    .pill-disagree { color: var(--red); border-color: rgba(239,68,68,0.4); background: rgba(239,68,68,0.08); }

    .plan-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 14px; }
    .plan-cell { background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
    .plan-cell .plan-label { font-size: 0.7rem; color: #7b8494; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }
    .plan-cell .plan-value { font-family: 'Space Grotesk', sans-serif; font-weight: 600; font-size: 1rem; }
    .plan-cell.entry .plan-value { color: var(--gemini); }
    .plan-cell.stop .plan-value { color: var(--red); }
    .plan-cell.target .plan-value { color: var(--green); }
    
    .divider-thin { border-top: 1px solid var(--border); margin: 14px 0; }
    
    /* Scanner Badges */
    .scan-badge { display: inline-block; padding: 2px 9px; border-radius: 6px; font-size: 0.68rem; font-weight: 600; margin-right: 4px; margin-bottom: 4px; }
    .badge-buy { background: rgba(16,185,129,0.12); color: var(--green); border: 1px solid rgba(16,185,129,0.3); }
    .badge-sell { background: rgba(239,68,68,0.12); color: var(--red); border: 1px solid rgba(239,68,68,0.3); }
    .badge-neutral { background: rgba(123,132,148,0.12); color: #7b8494; border: 1px solid var(--border); }
    .badge-vol { background: rgba(201,168,106,0.12); color: var(--verdict); border: 1px solid rgba(201,168,106,0.3); }

    .strategy-tag { display: inline-block; padding: 3px 11px; border-radius: 6px; font-size: 0.72rem; font-weight: 700; margin-right: 5px; margin-bottom: 4px; }
    .tag-reversal-short { background: rgba(16,185,129,0.22); color: #6ee7a0; border: 1px solid rgba(16,185,129,0.55); }
    .tag-reversal-medium { background: rgba(16,185,129,0.14); color: var(--green); border: 1.5px solid rgba(16,185,129,0.45); }
    .tag-takeprofit-short { background: rgba(217,119,87,0.22); color: #f0a085; border: 1px solid rgba(217,119,87,0.55); }
    .tag-takeprofit-medium { background: rgba(217,119,87,0.14); color: var(--claude); border: 1.5px solid rgba(217,119,87,0.45); }

    /* ============================================
       ✨ ธีม widget มาตรฐานของ Streamlit ให้เข้ากับการ์ดที่มีอยู่แล้ว
       ============================================ */

    /* Hero header */
    .app-hero {
        display: flex; align-items: center; justify-content: space-between;
        padding: 18px 24px; margin-bottom: 14px; border-radius: 14px;
        background: linear-gradient(120deg, rgba(79,179,169,0.08), rgba(10,12,16,0) 40%, rgba(217,119,87,0.08));
        border: 1px solid var(--border);
    }
    .app-hero-title { font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 1.5rem; color: var(--text); letter-spacing: -0.01em; }
    .app-hero-title span { color: var(--verdict); }
    .app-hero-sub { color: #7b8494; font-size: 0.78rem; margin-top: 3px; }
    .app-hero-live { display: flex; align-items: center; gap: 7px; font-size: 0.72rem; color: var(--green); font-family: 'JetBrains Mono', monospace; letter-spacing: 0.04em; }
    .app-hero-live .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 0 rgba(16,185,129,0.6); animation: pulse-dot 2s infinite; }
    @keyframes pulse-dot {
        0% { box-shadow: 0 0 0 0 rgba(16,185,129,0.55); }
        70% { box-shadow: 0 0 0 7px rgba(16,185,129,0); }
        100% { box-shadow: 0 0 0 0 rgba(16,185,129,0); }
    }

    /* Buttons */
    div[data-testid="stButton"] button, div[data-testid="stFormSubmitButton"] button {
        border-radius: 8px; font-family: 'Space Grotesk', sans-serif; font-weight: 600;
        border: 1px solid var(--border); transition: all 0.15s ease;
    }
    div[data-testid="stButton"] button[kind="primary"] {
        background: linear-gradient(135deg, #d9b46a, var(--verdict)); color: #14110a; border: none;
        box-shadow: 0 2px 10px rgba(201,168,106,0.25);
    }
    div[data-testid="stButton"] button[kind="primary"]:hover {
        box-shadow: 0 3px 16px rgba(201,168,106,0.4); transform: translateY(-1px);
    }
    div[data-testid="stButton"] button[kind="secondary"] { background: var(--panel-2); color: var(--text); }
    div[data-testid="stButton"] button[kind="secondary"]:hover { border-color: var(--verdict); color: var(--verdict); }

    /* Tabs */
    div[data-testid="stTabs"] button[data-baseweb="tab"] {
        font-family: 'Space Grotesk', sans-serif; font-weight: 600; color: #7b8494;
    }
    div[data-testid="stTabs"] button[aria-selected="true"] { color: var(--verdict) !important; }
    div[data-testid="stTabs"] [data-baseweb="tab-highlight"] {
        background: linear-gradient(90deg, var(--gemini), var(--verdict), var(--claude)) !important; height: 2.5px !important;
    }
    div[data-testid="stTabs"] [data-baseweb="tab-border"] { background: var(--border) !important; }

    /* Select / Multiselect / Text input / Number input */
    div[data-baseweb="select"] > div, div[data-testid="stTextInput"] input, div[data-testid="stNumberInput"] input {
        background-color: var(--panel-2) !important; border-color: var(--border) !important; border-radius: 8px !important;
    }
    div[data-baseweb="select"] > div:focus-within, div[data-testid="stTextInput"] input:focus, div[data-testid="stNumberInput"] input:focus {
        border-color: var(--verdict) !important; box-shadow: 0 0 0 1px var(--verdict) !important;
    }
    div[data-baseweb="tag"] { background-color: rgba(201,168,106,0.18) !important; color: var(--verdict) !important; }

    /* Checkbox / Radio accent */
    label[data-baseweb="checkbox"] span:first-child, div[data-testid="stCheckbox"] span[data-checked] {
        accent-color: var(--verdict);
    }

    /* Dataframe / tables */
    div[data-testid="stDataFrame"] { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }

    /* Alert boxes (info/warning/error/success) */
    div[data-testid="stAlertContainer"] { background-color: var(--panel) !important; border-radius: 10px; border: 1px solid var(--border); }

    /* Sidebar */
    section[data-testid="stSidebar"] { background-color: var(--panel); border-right: 1px solid var(--border); }

    /* Sliders */
    div[data-testid="stSlider"] [role="slider"] { background-color: var(--verdict) !important; }
    div[data-testid="stSlider"] div[style*="background-color: rgb(255, 75, 75)"] { background: var(--verdict) !important; }

    /* Metric */
    div[data-testid="stMetric"] { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; }

    /* 🎴 Photo-led stock card grid (ตารางผลสแกนแบบการ์ด) */
    .stock-card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 14px; margin: 10px 0 6px; }
    .stock-card-link { text-decoration: none !important; color: inherit !important; display: block; }
    .stock-card { border-radius: 10px; overflow: hidden; border: 1px solid var(--border); background: var(--panel); transition: transform 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease; cursor: pointer; }
    .stock-card-link:hover .stock-card { transform: translateY(-3px); border-color: var(--verdict); box-shadow: 0 6px 20px rgba(0,0,0,0.35); }
    .stock-card-visual { height: 76px; position: relative; }
    .stock-card-visual.dir-buy { background: linear-gradient(135deg, rgba(16,185,129,0.55), rgba(16,185,129,0.04) 75%), var(--panel-2); }
    .stock-card-visual.dir-sell { background: linear-gradient(135deg, rgba(239,68,68,0.55), rgba(239,68,68,0.04) 75%), var(--panel-2); }
    .stock-card-visual.dir-neutral { background: linear-gradient(135deg, rgba(123,132,148,0.35), rgba(123,132,148,0.03) 75%), var(--panel-2); }
    .spark-svg { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0.9; }
    .stock-card-score-chip {
        position: absolute; top: 8px; right: 8px; background: rgba(10,12,16,0.55); backdrop-filter: blur(2px);
        color: var(--text); font-family: 'JetBrains Mono', monospace; font-size: 0.68rem; font-weight: 600;
        padding: 3px 8px; border-radius: 20px; border: 1px solid rgba(255,255,255,0.12);
    }
    .stock-card-body { padding: 12px 14px 14px; }
    .stock-card-eyebrow { font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.07em; color: #7b8494; font-family: 'JetBrains Mono', monospace; margin-bottom: 5px; }
    .stock-card-title { font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 1.25rem; letter-spacing: -0.01em; margin-bottom: 5px; }
    .stock-card-desc { font-size: 0.74rem; color: #a8b0bd; line-height: 1.45; }
    .stock-card-cta {
        font-size: 0.66rem; font-family: 'Space Grotesk', sans-serif; font-weight: 600; color: var(--verdict);
        text-align: center; padding: 7px; border-top: 1px solid var(--border);
        background: rgba(201,168,106,0.06); opacity: 0; max-height: 0; transition: all 0.15s ease;
    }
    .stock-card-link:hover .stock-card-cta { opacity: 1; max-height: 40px; }

    /* 🎴 Verdict card wrapper (ใช้ visual/body class ร่วมกับ stock-card เพื่อความสอดคล้องกันทั้งแอป) */
    .verdict-card-wrap { border-radius: 12px; overflow: hidden; border: 1px solid var(--verdict); background: var(--panel); margin-bottom: 14px; }

    /* Expander */
    div[data-testid="stExpander"] { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; }

    /* Scrollbar polish */
    ::-webkit-scrollbar { width: 9px; height: 9px; }
    ::-webkit-scrollbar-track { background: var(--bg); }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 5px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--verdict); }

    /* 📱 Responsive: จอมือถือ/แท็บเล็ตแคบ (Streamlit จัดการ st.columns ให้เองแล้ว
       ส่วนนี้แก้เฉพาะ layout ที่เขียนเอง (flex/grid) ซึ่งไม่ยุบให้อัตโนมัติ) */
    @media (max-width: 640px) {
        .plan-grid { grid-template-columns: 1fr !important; }
        .app-hero { flex-direction: column; align-items: flex-start; gap: 10px; }
        .app-hero-live { align-self: flex-start; }
        .vs-banner { flex-direction: column; }
        .vs-gemini { border-right: none; border-bottom: 1px solid var(--border); }
        .stock-card-grid { grid-template-columns: 1fr !important; }
    }
</style>
""", unsafe_allow_html=True)

# --- 🎬 HERO HEADER ---
st.markdown("""
<div class="app-hero">
    <div>
        <div class="app-hero-title">◆ PropFirmX <span>Terminal</span></div>
        <div class="app-hero-sub">AI Debate Terminal — Gemini × Claude · Shared Portfolio & Signal Scanner</div>
    </div>
    <div class="app-hero-live"><span class="dot"></span> LIVE MARKET DATA</div>
</div>
""", unsafe_allow_html=True)

# --- 🔄 1. ระบบ GENERATE TICKER TAPE ---
tape_json_list = [{"proName": sym, "title": sym.split(":")[-1]} for sym in TAPE_SYMBOLS]
tape_json_string = json.dumps(tape_json_list)

ticker_tape_html = f"""
<div class="tradingview-widget-container">
  <div class="tradingview-widget-container__widget"></div>
  <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js" async>
  {{
  "symbols": {tape_json_string},
  "showSymbolLogo": true,
  "isTransparent": true,
  "displayMode": "adaptive",
  "colorTheme": "dark",
  "locale": "th"
}}
  </script>
</div>
"""
components.html(ticker_tape_html, height=50)

# --- 🛠️ Helper Functions ---
def fetch_data_with_header(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response: 
        return response.read()

@st.cache_data(ttl=86400)  # cache 1 วัน รายชื่อหุ้นที่จดทะเบียนไม่เปลี่ยนบ่อย
def fetch_nasdaq_universe():
    """
    ดึงรายชื่อหุ้นทั้งหมดที่จดทะเบียนบน NASDAQ จากไฟล์ทางการของ NASDAQ Trader (ฟรี ไม่ต้องขอ API key)
    ใช้เป็น "universe" จริงสำหรับสุ่มสแกนหา Penny Stocks แทนการพึ่งรายชื่อคัดมือที่ตายตัวและเก่าได้ง่าย
    """
    try:
        raw_bytes = fetch_data_with_header('https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt')
        text = raw_bytes.decode('utf-8')
        df = pd.read_csv(io.StringIO(text), sep='|')
        df = df[df['Test Issue'] == 'N']  # ตัดหุ้นทดสอบระบบ ไม่ใช่หุ้นจริง
        if 'ETF' in df.columns:
            df = df[df['ETF'] == 'N']     # ตัด ETF ออก เอาแต่หุ้นรายตัว
        symbols = df['Symbol'].dropna().tolist()
        # ตัด symbol ที่มีตัวอักษรพิเศษ (เช่น warrant/unit ของ SPAC ที่มี . หรือ - ติดมา) เอาแต่หุ้นสามัญทั่วไป
        symbols = [s for s in symbols if isinstance(s, str) and s.isalpha() and 1 <= len(s) <= 5]
        return symbols
    except Exception as e:
        print(f"Failed to fetch NASDAQ universe: {e}")
        return []

@st.cache_data(ttl=86400) # Cache 1 วันสำหรับรายชื่อหุ้น
def load_market_tickers(market):
    try:
        if market == "S&P 500":
            csv_bytes = fetch_data_with_header('https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv')
            return pd.read_csv(io.BytesIO(csv_bytes))['Symbol'].tolist()
        elif market == "Penny Stocks (สแกนทั้งตลาด NASDAQ + AI กรองคุณภาพ)":
            # โหมดนี้ดึง universe หุ้นจริงทั้งหมดที่จดทะเบียนบน NASDAQ (~3,000-4,000 ตัว) แล้วสุ่มหยิบมาเช็คราคาจริง
            # ครอบคลุมกว้างกว่ารายชื่อคัดมือ แต่ใช้เวลาสแกนนานขึ้น และผลลัพธ์แต่ละรอบอาจไม่ซ้ำกัน เพราะสุ่มใหม่ทุกครั้ง
            # หมายเหตุ: เช็คเงื่อนไขนี้ก่อน "NASDAQ" in market ด้านล่าง เพราะ string นี้มีคำว่า NASDAQ ปนอยู่ด้วย
            # ถ้าสลับลำดับจะถูกเงื่อนไขด้านล่างตัดหน้าไปสแกน NASDAQ-100 ผิดจุดประสงค์
            universe = fetch_nasdaq_universe()
            if not universe:
                # ดึง universe ไม่ได้ (เช่น เน็ตมีปัญหา) ใช้กลุ่มตัวอย่างคัดมือเป็น fallback ไม่ให้สแกนพัง
                return ["SNDL", "CGC", "ACB", "TLRY", "CRON", "OGI", "GRWG", "FCEL", "PLUG", "BLNK",
                        "WKHS", "RIG", "LCID", "OCGN", "INO", "VXRT", "CLOV", "OPEN", "BBAI", "IVDA", "INUV", "GRAB", "IQ"]
            sample_size = min(250, len(universe))
            return random.sample(universe, sample_size)
        elif "NASDAQ" in market:
            html_bytes = fetch_data_with_header('https://en.wikipedia.org/wiki/Nasdaq-100')
            tables = pd.read_html(io.StringIO(html_bytes.decode('utf-8')))
            for df in tables:
                if 'Ticker' in df.columns or 'Symbol' in df.columns:
                    return df['Ticker' if 'Ticker' in df.columns else 'Symbol'].tolist()
        elif market == "Penny Stocks (ต่ำกว่า $5)":
            # หมายเหตุสำคัญ: นี่ไม่ใช่ "รายชื่อ penny stock ตายตัว" แต่เป็น "กลุ่มตัวอย่างหุ้นจากหลายเซกเตอร์
            # ที่ในอดีตมักมีราคาต่ำกว่า $5 อยู่เป็นประจำ" (กัญชา, พลังงานสะอาด/EV เล็ก, ไบโอเทค, ฟินเทค, ADR จีน)
            # ตัวกรอง is_penny ใน scan_market_batch จะเช็คราคา ณ ปัจจุบันจริงและคัดตัวที่เกิน $5 ออกอัตโนมัติ
            # ขยายกลุ่มให้กว้างขึ้นเพื่อให้มีโอกาสสูงที่จะมีตัวเหลือผ่านเกณฑ์เสมอ แม้บางตัวจะ "โต" หลุดเกณฑ์ไปแล้ว
            # แนะนำให้กลับมาทบทวน/เพิ่มรายชื่อใหม่เป็นระยะ เพราะนี่ยังเป็นกลุ่มตัวอย่างที่คัดมือ ไม่ใช่การสแกนทั้งตลาดจริง
            return [
                "SNDL", "CGC", "ACB", "TLRY", "CRON", "OGI",                  # กัญชา
                "GRWG", "FCEL", "PLUG", "BLNK", "WKHS",                       # พลังงานสะอาด/EV เล็ก
                "RIG", "LCID",                                                # พลังงาน/EV
                "OCGN", "INO", "VXRT",                                       # ไบโอเทค
                "CLOV", "OPEN", "BBAI", "IVDA", "INUV",                       # ฟินเทค/AI/พร็อพเทค
                "GRAB", "IQ"                                                  # ADR ต่างประเทศ
            ]
        elif market == "SET100 (หุ้นไทย)":
            tickers = [
                "ADVANC", "AOT", "AWC", "BANPU", "BBL", "BCP", "BDMS", "BEM", "BGRIM", "BH",
                "BTS", "CBG", "CENTEL", "CPALL", "CPF", "CPN", "CRC", "DELTA", "EA", "EGCO",
                "GLOBAL", "GPSC", "GULF", "HMPRO", "INTUCH", "IRPC", "IVL", "KBANK", "KCE", "KTB",
                "KTC", "LH", "MINT", "MTC", "OR", "OSP", "PTT", "PTTEP", "PTTGC", "RATCH",
                "SAWAD", "SCB", "SCC", "SCGP", "TASCO", "TIDLOR", "TISCO", "TOP", "TRUE", "TTB",
                "TU", "WHA", "AMATA", "AP", "BCH", "BJC", "BLA", "CHG", "CK", "CKP",
                "COM7", "DOHOME", "ERW", "ESSO", "GFPT", "GUNKUL", "ICHI", "JMART", "JMT", "KKP",
                "MAJOR", "MEGA", "MFC", "MOSHI", "ORI", "PLANB", "PR9", "PSL", "QH", "RS",
                "SABUY", "SAPPE", "SIRI", "SJWD", "SPALI", "SPRC", "STA", "STGT", "STECON", "SUPER",
                "TFG", "THANI", "THG", "TKN", "TOA", "TVO", "VGI", "WICE", "ITC", "SISB"
            ]
            return [t + ".BK" for t in tickers]
        elif market == "SET50 (หุ้นไทย)":
            tickers = ["ADVANC", "AOT", "AWC", "BANPU", "BBL", "BDMS", "BEM", "BGRIM", "BH", "BTS", "CBG", "CENTEL", "COM7", "CPALL", "CPF", "CPN", "CRC", "DELTA", "EA", "EGCO", "GLOBAL", "GPSC", "GULF", "HMPRO", "INTUCH", "IRPC", "IVL", "JMART", "JMT", "KBANK", "KCE", "KTB", "KTC", "LH", "MINT", "MTC", "OR", "OSP", "PTT", "PTTEP", "PTTGC", "RATCH", "SAWAD", "SCB", "SCC", "SCGP", "TIDLOR", "TISCO", "TOP", "TRUE", "TTB", "TU", "WHA"]
            return [t + ".BK" for t in tickers]
        elif market == "Crypto (Top Coins)":
            return ["BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD", "XRP-USD", "ADA-USD", "AVAX-USD", "DOGE-USD", "TRX-USD", "DOT-USD"]
        elif market == "Crypto (Alt/Meme Coins)":
            return ["SHIB-USD", "PEPE-USD", "WIF-USD", "FLOKI-USD", "BONK-USD", "MATIC-USD", "LINK-USD", "UNI-USD", "LTC-USD", "BCH-USD"]
    except Exception as e: 
        st.sidebar.warning(f"Failed to fetch market data: {e}")
    return ['AAPL', 'MSFT', 'NVDA', 'AMZN'] # Fallback list

@st.cache_data(ttl=300)
def fetch_gainers_and_losers(asset_type="US"):
    if asset_type == "TH":
        sample_tickers = ["PTT.BK", "CPALL.BK", "BDMS.BK", "AOT.BK", "ADVANC.BK", "KBANK.BK", "SCB.BK", "GULF.BK", "DELTA.BK"]
    elif asset_type == "Crypto":
        sample_tickers = ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD", "DOGE-USD", "ADA-USD", "PEPE-USD"]
    else:
        sample_tickers = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "NFLX", "AMD", "PLTR"]
        
    try:
        data = yf.download(" ".join(sample_tickers), period="5d", interval="1d", group_by="ticker", progress=False)
        change_list = []
        for ticker in sample_tickers:
            if ticker in data.columns.get_level_values(0):
                df = data[ticker].dropna()
                if len(df) >= 2:
                    current_c = float(df['Close'].iloc[-1])
                    prev_c = float(df['Close'].iloc[-2])
                    p_change = ((current_c - prev_c) / prev_c) * 100
                    change_list.append({"Ticker": ticker, "Price": round(current_c, 2), "Change": p_change})
        
        df_changes = pd.DataFrame(change_list)
        if not df_changes.empty:
            top_gainers = df_changes.sort_values(by="Change", ascending=False).head(5)
            top_losers = df_changes.sort_values(by="Change", ascending=True).head(5)
            return top_gainers, top_losers
    except Exception as e:
        print(f"Error fetching gainers/losers: {e}")
        
    dummy_g = pd.DataFrame([{"Ticker": "NVDA" if asset_type=="US" else "PTT.BK" if asset_type=="TH" else "BTC-USD", "Price": 125.10, "Change": 4.82}])
    dummy_l = pd.DataFrame([{"Ticker": "TSLA" if asset_type=="US" else "AOT.BK" if asset_type=="TH" else "ETH-USD", "Price": 172.30, "Change": -3.50}])
    return dummy_g, dummy_l

# ⭐ TECHNICAL RATING + 🌍 MARKET REGIME
# ==========================================
# ย้าย logic การคำนวณไปไว้ที่ rating_engine.py (ไม่มี dependency กับ streamlit) เพื่อให้
# alert_scanner.py (สคริปต์แจ้งเตือนพื้นหลังที่รันบน GitHub Actions) ใช้สูตรคำนวณชุดเดียวกันเป๊ะๆ
from rating_engine import (
    RATING_LEVELS,
    MARKET_REGIME_INDEX,
    compute_technical_rating,
    get_market_regime as _get_market_regime_raw,
)

@st.cache_data(ttl=3600)  # ภาวะตลาดรวมไม่ได้เปลี่ยนไวเท่าราคารายตัว แคชไว้ 1 ชม.ก็พอ
def get_market_regime(index_ticker):
    return _get_market_regime_raw(index_ticker)


def scan_market_batch(tickers_list, is_penny=False, market_type="US"):
    """
    กวาดสแกนหุ้นทั้งหมดในลิสต์โดยคำนวณสัญญาณมาตรฐานและแท็กกลยุทธ์ Reversal / Take Profit ทั้งระยะสั้นและระยะกลาง
    market_type: "US" / "TH" / "Crypto" — ใช้เลือกดัชนีอ้างอิงเช็คภาวะตลาดรวม (Market Regime)
    """
    results = []
    scan_pool = tickers_list
    tickers_str = " ".join(scan_pool)

    # 🌍 เช็คภาวะตลาดรวมครั้งเดียวก่อนสแกน (ไม่ต้องเช็คซ้ำทุกตัว ประหยัด request และเร็วขึ้นมาก)
    regime_ticker = MARKET_REGIME_INDEX.get(market_type)
    regime_info = get_market_regime(regime_ticker) if regime_ticker else {"regime": None}
    regime = regime_info.get("regime")

    try:
        # โหลดข้อมูลย้อนหลัง 3 เดือน เพื่อความแม่นยำและเสถียรภาพตัวชี้วัด (RSI14, RSI7, EMA20, EMA50)
        raw_df = yf.download(tickers_str, period="3mo", interval="1d", group_by="ticker", auto_adjust=False, progress=False, threads=True)
        for ticker in scan_pool:
            try:
                if isinstance(raw_df.columns, pd.MultiIndex):
                    if ticker in raw_df.columns.get_level_values(0):
                        df = raw_df[ticker].dropna().copy()
                    else:
                        continue
                else:
                    df = raw_df.dropna().copy()
                
                if df.empty or len(df) < 30: 
                    continue
                
                close = df['Close']
                volume = df['Volume']
                c = float(close.iloc[-1])
                
                if is_penny and c > 5.0:
                    continue
                
                # Indicator 20 วัน
                df['MA20'] = close.rolling(window=20).mean()
                df['STD'] = close.rolling(window=20).std()
                df['BB_Upper'] = df['MA20'] + (df['STD'] * 2)
                df['BB_Lower'] = df['MA20'] - (df['STD'] * 2)
                df['Vol_MA'] = volume.rolling(window=20).mean()
                
                # RSI 14 (Welles Wilder Smoothing)
                delta = close.diff()
                gains = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
                losses = -delta.where(delta < 0, 0).ewm(alpha=1/14, adjust=False).mean()
                rs14 = gains / (losses + 1e-10)
                df['RSI'] = 100 - (100 / (1 + rs14))
                
                # RSI 7 (สำหรับจับโมเมนตัมระยะสั้นไวกว่า)
                gains7 = delta.where(delta > 0, 0).ewm(alpha=1/7, adjust=False).mean()
                losses7 = -delta.where(delta < 0, 0).ewm(alpha=1/7, adjust=False).mean()
                rs7 = gains7 / (losses7 + 1e-10)
                df['RSI7'] = 100 - (100 / (1 + rs7))
                
                # MACD (12, 26, 9)
                ema12 = close.ewm(span=12, adjust=False).mean()
                ema26 = close.ewm(span=26, adjust=False).mean()
                macd_line = ema12 - ema26
                signal_line = macd_line.ewm(span=9, adjust=False).mean()
                df['MACD_Hist'] = macd_line - signal_line
                
                # EMA Trend (20 / 50 วัน)
                ema20 = close.ewm(span=20, adjust=False).mean()
                ema50 = close.ewm(span=50, adjust=False).mean()
                ema20_now, ema50_now = float(ema20.iloc[-1]), float(ema50.iloc[-1])
                ema20_prev, ema50_prev = float(ema20.iloc[-2]), float(ema50.iloc[-2])
                ema_bullish_cross = ema20_prev < ema50_prev and ema20_now > ema50_now
                ema_bearish_cross = ema20_prev > ema50_prev and ema20_now < ema50_now
                above_ema_trend = ema20_now > ema50_now

                u = float(df['BB_Upper'].iloc[-1])
                l = float(df['BB_Lower'].iloc[-1])
                v, vm = float(volume.iloc[-1]), float(df['Vol_MA'].iloc[-1])
                rsi14 = float(df['RSI'].iloc[-1])
                rsi14_prev = float(df['RSI'].iloc[-2])
                rsi7 = float(df['RSI7'].iloc[-1])
                rsi7_prev = float(df['RSI7'].iloc[-2])
                rsi7_prev3 = float(df['RSI7'].iloc[-4]) if len(df) > 4 else rsi7
                
                macd_now = float(df['MACD_Hist'].iloc[-1])
                macd_prev = float(df['MACD_Hist'].iloc[-2])
                macd_bullish_cross = macd_prev < 0 and macd_now > 0
                macd_bearish_cross = macd_prev > 0 and macd_now < 0
                macd_rising = macd_now > macd_prev
                
                prev_c = float(close.iloc[-2])
                change_pct = round((c - prev_c) / prev_c * 100, 2)
                vol_ratio = round(v / vm, 2) if vm > 0 else 1.0
                vol_spike = vol_ratio >= 1.5

                open_now = float(df["Open"].iloc[-1])
                candle_is_green = c > open_now
                candle_is_red = c < open_now
                
                high_20 = float(close.rolling(20).max().iloc[-1])
                pct_from_high20 = round((c - high_20) / high_20 * 100, 2)

                # ==========================================
                # 🏷️ สร้างกลยุทธ์พิเศษแท็ก (Reversal Buy / Take Profit)
                # ==========================================
                strategy_tags = []
                
                # กลับตัวระยะสั้น: RSI7 เคยลงโซน Oversold (<30) ใน 3 วันก่อนหน้า และวันนี้เขียวดีดพ้นโซน
                if (rsi7_prev3 < 30) and (rsi7 > rsi7_prev3) and (rsi7 < 50) and candle_is_green:
                    strategy_tags.append(("กลับตัวขึ้น (สั้น)", "reversal-short"))
                
                # กลับตัวระยะกลาง: EMA20 ตัดขึ้น EMA50 และ RSI14 เพิ่งฟื้นตัว (ยังไม่ Overbought) พร้อม MACD กำลังยกตัวขึ้น
                if ema_bullish_cross and (35 < rsi14 < 60) and macd_rising:
                    strategy_tags.append(("กลับตัวขึ้น (กลาง)", "reversal-medium"))
                    
                # Take Profit สั้น: RSI7 ขึ้น Overbought และวันนี้เกิดแท่งแดงกลับตัวลงมา พร้อมราคาอยู่ในโซนใกล้ High 20 วัน
                if (rsi7_prev3 > 70) and (rsi7 < rsi7_prev3) and candle_is_red and (pct_from_high20 > -5):
                    strategy_tags.append(("Take Profit (สั้น)", "takeprofit-short"))
                    
                # Take Profit กลาง: ราคาเกาะอยู่บนเทรนด์ขาขึ้นเหนือ EMA50 แต่ MACD Histogram ตัดลงตัดสัญญาณเริ่มแผ่วกำลัง
                if above_ema_trend and macd_bearish_cross and (c > ema50_now):
                    strategy_tags.append(("Take Profit (กลาง)", "takeprofit-medium"))

                # ==========================================
                # 🔧 สัญญาณหลักมาตรฐาน
                # ==========================================
                signals = []
                if rsi14 < 32:
                    signals.append(("RSI Oversold", "buy"))
                elif rsi14 > 68:
                    signals.append(("RSI Overbought", "sell"))
                
                if macd_bullish_cross:
                    signals.append(("MACD GoldCross", "buy"))
                elif macd_bearish_cross:
                    signals.append(("MACD DeathCross", "sell"))
                    
                if c > u and vol_spike:
                    signals.append(("BB Breakout บน", "buy"))
                elif c < l and vol_spike:
                    signals.append(("BB Breakout ล่าง", "sell"))
                    
                if vol_spike:
                    signals.append((f"Volume x{vol_ratio}", "vol"))

                # ⭐ คำนวณเรตติ้งเทคนิครวม (Strong Buy → Strong Sell) จากอินดิเคเตอร์ทั้งชุด + ภาวะตลาดรวม
                rating = compute_technical_rating(
                    c=c, rsi14=rsi14, macd_now=macd_now, macd_prev=macd_prev,
                    ema20_now=ema20_now, ema50_now=ema50_now,
                    bb_upper=u, bb_lower=l, vol_ratio=vol_ratio, regime=regime
                )

                # บันทึกข้อมูลเฉพาะตัวที่มีแท็กสัญญาณหรือแท็กกลยุทธ์
                if signals or strategy_tags:
                    # 📈 เตรียมข้อมูล sparkline: ย่อราคา Close ย้อนหลัง 3 เดือนให้เหลือ ~24 จุด
                    # (ใช้ข้อมูลจาก batch เดิมที่ดึงมาแล้ว ไม่ยิง request เพิ่ม ไม่กระทบความเร็ว)
                    try:
                        closes = close.dropna().tolist()
                        if len(closes) > 24:
                            step = len(closes) / 24.0
                            spark = [round(closes[min(int(i * step), len(closes) - 1)], 2) for i in range(24)]
                        else:
                            spark = [round(x, 2) for x in closes]
                    except Exception:
                        spark = []

                    results.append({
                        "ticker": ticker,
                        "price": round(c, 2),
                        "change_pct": change_pct,
                        "rsi": round(rsi14, 2),
                        "vol_ratio": vol_ratio,
                        "signals": signals,
                        "strategy_tags": strategy_tags,
                        "signal_count": len(signals) + len(strategy_tags),
                        "rating_code": rating["code"],
                        "rating_label": rating["label"],
                        "rating_icon": rating["icon"],
                        "rating_score": rating["score"],
                        "rating_votes": rating["votes"],
                        "sparkline": spark,
                    })
            except Exception as e:
                print(f"Error scanning {ticker}: {e}")
                continue
                
        # เรียงตามความเด่นของสัญญาณ
        results.sort(key=lambda x: x["signal_count"], reverse=True)
    except Exception as e:
        st.sidebar.error(f"เกิดข้อผิดพลาดในการสแกนตลาด: {e}")
    return results

# ==========================================
# 🔄 SYSTEMS RETRY ENGINE (Exponential Backoff)
# ==========================================
def call_api_with_backoff(api_call_fn, *args, **kwargs):
    delays = [1, 2, 4, 8, 16]
    for delay in delays:
        try:
            return api_call_fn(*args, **kwargs)
        except Exception as e:
            time.sleep(delay)
    try:
        return api_call_fn(*args, **kwargs)
    except Exception as e:
        raise RuntimeError(
            "⚠️ บริการ AI กำลังมีผู้ใช้งานหนาแน่นชั่วคราว (Error 503 / High Demand) "
            "ระบบพยายามออโต้รีไทร์ 5 ครั้งแล้วยังไม่สำเร็จ กรุณาเว้นระยะ 15 วินาทีแล้วกดปุ่มคำนวณอีกครั้งครับ"
        )

# ==========================================
# 🛡️ AI QUALITY SCREEN สำหรับ Penny Stocks ที่มาจากการสแกนทั้งตลาด
# ==========================================
@st.cache_data(ttl=3600)
def ai_quality_filter_stocks(tickers_tuple):
    """
    ให้ Gemini ช่วยประเมินความเสี่ยงเชิงคุณภาพของหุ้นที่ผ่านตัวกรองเทคนิคมาแล้ว จากความรู้ทั่วไป
    ที่โมเดลมีอยู่ (ไม่ใช่การเช็คข้อมูลสด) เพื่อติดป้ายเตือนเสริมให้ผู้ใช้ระมัดระวังเป็นพิเศษกับตัวที่มีประวัติเสี่ยงสูง
    (เช่น reverse split ถี่ๆ, เคยใกล้ delist/ล้มละลาย, หนี้สินสูงผิดปกติ, ประเด็นบัญชี/ธรรมาภิบาล)
    ใช้ได้กับหุ้นทุกขนาด ไม่จำกัดแค่ penny stock — เป็นป้ายเตือนเสริมเท่านั้น ไม่ใช่การฟันธงแนะนำซื้อ/ขาย
    และไม่ได้แทนที่การตรวจสอบข้อมูลจริงก่อนลงทุน
    """
    if not GEMINI_API_KEY or not tickers_tuple:
        return {}
    tickers = list(tickers_tuple)
    ticker_list_str = ", ".join(tickers)
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    นี่คือรายชื่อหุ้นที่มีสัญญาณเทคนิคบางอย่างเกิดขึ้นตอนนี้: {ticker_list_str}

    จากความรู้ทั่วไปที่คุณมีเกี่ยวกับแต่ละบริษัทนี้ (ไม่ต้องเดาราคาหรือเช็คข้อมูลสด) ช่วยประเมินว่าตัวไหนมีสัญญาณเตือน
    เชิงคุณภาพที่นักลงทุนทั่วไปควรรู้ก่อนเป็นพิเศษหรือไม่ เช่น มีประวัติ reverse stock split ถี่ๆ, เคยใกล้ delist
    หรือล้มละลาย, หนี้สินสูงผิดปกติเทียบรายได้, มีประเด็นบัญชี/ธรรมาภิบาลที่เป็นข่าวมาก่อน, หรือเป็นบริษัท
    pre-revenue ที่ขาดทุนสะสมมหาศาลต่อเนื่องยาวนาน — ถ้าเป็นหุ้นบริษัทใหญ่ที่มั่นคงปกติ ให้ตอบว่า "ปกติ" ตรงไปตรงมา

    ตอบให้ครบทุกตัวที่ให้มา ติดป้าย quality_flag เป็นค่าใดค่าหนึ่งเท่านั้น: "ปกติ", "ระมัดระวังสูง", หรือ "ไม่แน่ใจ"
    ใช้ "ไม่แน่ใจ" ถ้าไม่มีข้อมูลเกี่ยวกับบริษัทนี้เพียงพอ ห้ามเดามั่ว พร้อมเหตุผลสั้นๆไม่เกิน 1 ประโยคเป็นภาษาไทยง่ายๆ
    """
    def run_quality_check():
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PennyStockQualityBatch,
                temperature=0.2,
                system_instruction="ตอบเป็นภาษาไทยล้วน ใช้ภาษาง่ายๆสั้นกระชับ ประเมินจากความรู้ทั่วไปเท่านั้น "
                                    "ไม่ทำนายราคาหรืออนาคต ถ้าไม่รู้จักบริษัทให้ตอบว่าไม่แน่ใจตรงๆ ห้ามเดามั่ว"
            )
        )
        return json.loads(response.text)
    try:
        data = call_api_with_backoff(run_quality_check)
        return {item["ticker"].upper(): item for item in data.get("assessments", [])}
    except Exception as e:
        print(f"AI quality filter error: {e}")
        return {}

# ==========================================
# 🤖 STEP 1 — GEMINI: ความเห็นแรก
# ==========================================
@st.cache_data(ttl=3600)
def gemini_first_opinion(ticker, price_rounded, rsi_rounded, ma20_rounded, bb_u_rounded, bb_l_rounded, macd_hist):
    if not GEMINI_API_KEY:
        return None
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
    คุณคือนักวิเคราะห์เทรดดิ้งชั้นนำ วิเคราะห์ข้อมูลหุ้น {ticker} สำหรับการโต้ตอบแบบดีเบต:
    - ราคา: {price_rounded}
    - RSI(14): {rsi_rounded}
    - MA20: {ma20_rounded}
    - Bollinger Bands Upper: {bb_u_rounded}
    - Bollinger Bands Lower: {bb_l_rounded}
    - MACD Histogram: {macd_hist}
    
    ให้ความเห็นเบื้องต้นเชิงบวก/ลบ ตรวจสอบแรงส่งในตลาดและพฤติกรรมราคา เพื่อให้ Claude ตรวจสอบต่อไป

    เขียนทุกฟิลด์ด้วยภาษาไทยง่ายๆ ประโยคสั้น อ่านครั้งเดียวเข้าใจทันที เหมือนเล่าให้เพื่อนที่ไม่ได้เรียนการเงินมาฟัง
    ถ้าต้องพูดถึงศัพท์เทคนิค (เช่น RSI, MACD) ให้ขยายความสั้นๆในประโยคเดียวกันว่ามันแปลว่าอะไรในทางปฏิบัติ
    ห้ามเขียนแบบทางการแข็งๆหรือฟังดูเหมือนแปลจากภาษาอังกฤษ
    """
    def run_gemini():
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeminiOpinion,
                temperature=0.4,
                system_instruction="ตอบเป็นภาษาไทยล้วน ใช้ภาษาพูดธรรมดาที่คนทั่วไปอ่านครั้งเดียวแล้วเข้าใจ ไม่ต้องอ่านซ้ำ ประโยคสั้น กระชับ ตรงประเด็น หลีกเลี่ยงศัพท์เทคนิคที่ไม่จำเป็น ถ้าต้องใช้ศัพท์เฉพาะให้ขยายความสั้นๆในประโยคเดียวกันว่าหมายถึงอะไร ห้ามตอบแบบทางการแข็งๆหรือฟังดูเหมือนแปลจากภาษาอังกฤษ"
            )
        )
        return json.loads(response.text)
    return call_api_with_backoff(run_gemini)

# ==========================================
# 📰 NEWS — ดึงข่าว+sentiment ล่าสุด (cache 1 ชม. กันยิงซ้ำทุกครั้งที่ rerun หน้าเว็บ)
# ==========================================
@st.cache_data(ttl=3600)
def get_news_context_cached(ticker):
    """ดึง+วิเคราะห์ข่าวใหม่ (ถ้ายังไม่มีข่าวใน DB ช่วง 48 ชม.ล่าสุด) แล้วคืนข้อความสรุปสำหรับแปะใน prompt ของ Claude"""
    if not ANTHROPIC_API_KEY:
        return ""
    existing = news.get_latest_flags(ticker)
    if not existing:
        try:
            news.refresh_news(ticker, ANTHROPIC_API_KEY)
        except Exception as e:
            print(f"[news] refresh_news error for {ticker}: {e}")
    return news.get_news_context(ticker)


# ==========================================
# 🤖 STEP 2 — CLAUDE: ตรวจสอบและให้ข้อยุติสุดท้าย
# ==========================================
@st.cache_data(ttl=3600)
def claude_challenge_and_verdict(ticker, ind, gemini_opinion, news_context=""):
    """
    Claude ตรวจทานข้อคิดเห็นเชิงลึก ท้าทายข้อมูลดิบ และสรุปสัญญาณเทรด Final
    หากไม่มี ANTHROPIC_API_KEY หรือเกิดเหตุขัดข้อง จะทำการ Fallback คืนเป็นจำลอง Verdict อัตโนมัติด้วยโครงสร้างเดียวกัน
    """
    if not ANTHROPIC_API_KEY:
        # Fallback จำลอง Verdict อัตโนมัติจากโครงสร้างการคิดของ Gemini หากไม่มี Claude Key
        fallback_verdict = {
            "agrees_with_gemini": True,
            "final_signal": gemini_opinion["initial_signal"],
            "risk_level": "กลาง",
            "support_zone": f"{ind['ma20'] * 0.96:.2f}",
            "resistance_zone": f"{ind['ma20'] * 1.05:.2f}",
            "challenge_notes": f"ตรวจสอบโมเมนตัมของ {ticker} แล้วมีความสมเหตุสมผลตามโครงสร้าง RSI ระดับ {ind['rsi']}",
            "final_reasoning": f"มุมมองโดยรวมสอดคล้องกับปัจจัยแวดล้อมทาง Bollinger Bands แนะนำปฏิบัติตามกรอบราคาหลักอย่างระมัดระวัง",
            "action_summary": f"ดำเนินการเล่นในกรอบแคบตามข้อบ่งชี้ {gemini_opinion['initial_signal']} ในตลาดระยะสั้น",
            "entry_price": f"{ind['price']:.2f}",
            "stop_loss": f"{ind['price'] * 0.95:.2f}",
            "take_profit": f"{ind['price'] * 1.10:.2f}",
            "position_sizing_note": "คำแนะนำสัดส่วน: แบ่งสัดส่วนพอร์ตเพียง 5-10% เนื่องจากปัจจัยแปรปรวนในสภาวะตลาดชั่วคราว"
        }
        return fallback_verdict
        
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    news_section = f"\n    {news_context}\n    (ถ้าข่าวข้างต้นสวนทางกับสัญญาณเทคนิค ให้พิจารณาว่าข่าวเพิ่งเกิดอาจมีน้ำหนักกว่าสัญญาณเทคนิคที่เป็นข้อมูลย้อนหลัง)\n    " if news_context else ""
    prompt = f"""
    ตรวจสอบและตรวจทานพฤติกรรมกราฟราคารวมถึงความเห็นเบื้องต้นของ Gemini ของหุ้น {ticker}:
    - ข้อมูลเทคนิคัลดิบ: ราคาตลาด={ind['price']}, RSI={ind['rsi']}, MACD_Hist={ind['macd_hist']}, Bollinger=[{ind['bb_lower']}, {ind['bb_upper']}]
    - ความเห็นแรกจาก Gemini: Sentiment={gemini_opinion['market_sentiment']}, Signal={gemini_opinion['initial_signal']}, สังเกตเห็น={gemini_opinion['key_observation']}
    {news_section}
    จงตรวจสอบ ท้าทายข้อผิดพลาด และให้คำตัดสินและแผน Action Plan สุดท้าย (BUY / SELL / HOLD) อย่างมีหลักการหนักแน่นแบบมือโปร
    แต่เขียนคำอธิบายทุกข้อด้วยภาษาไทยง่ายๆ สั้น กระชับ อ่านครั้งเดียวเข้าใจ เหมือนอธิบายให้คนในครอบครัวที่ไม่ได้เรียนการเงินมาฟัง
    หลีกเลี่ยงศัพท์การเงินที่ซับซ้อนเกินจำเป็น ถ้าต้องพูดถึงศัพท์เทคนิคให้ขยายความสั้นๆในประโยคเดียวกัน ห้ามเขียนแบบฟังดูเหมือนแปลจากภาษาอังกฤษ
    """
    
    def run_claude():
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=2048,
            system="คุณเป็นบอร์ดตัดสินใจเทรด ให้ตอบกลับเป็นรูปแบบ JSON เสมอเพื่อส่งคำตอบเข้าโครงสร้างระบบ ทุกข้อความในฟิลด์ต้องเป็นภาษาไทยง่ายๆที่อ่านแล้วเข้าใจทันที ไม่ใช้ประโยคที่ฟังดูเหมือนแปลจากภาษาอังกฤษ ไม่ใช้ศัพท์เทคนิคซ้อนศัพท์เทคนิคโดยไม่อธิบาย",
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "name": "submit_verdict",
                "description": "ส่งคำตัดสินเทรดดิ้งสุดท้ายหลังจากตรวจทานแล้ว",
                "input_schema": ClaudeVerdict.model_json_schema()
            }],
            tool_choice={"type": "tool", "name": "submit_verdict"}
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        raise ValueError("Claude didn't yield tool block.")
        
    return call_api_with_backoff(run_claude)

def run_ai_debate(ticker, ind):
    gemini_op = gemini_first_opinion(
        ticker, round(ind['price'], 2), round(ind['rsi'], 0), round(ind['ma20'], 2), 
        round(ind['bb_upper'], 2), round(ind['bb_lower'], 2), round(ind['macd_hist'], 4)
    )
    news_context = get_news_context_cached(ticker)  # 📰 ดึง+วิเคราะห์ข่าวล่าสุด ก่อนให้ Claude ตัดสิน
    claude_v = claude_challenge_and_verdict(ticker, ind, gemini_op, news_context)
    return {"gemini": gemini_op, "claude": claude_v, "indicators": ind, "ticker": ticker,
            "news_flags": news.get_latest_flags(ticker)}

def ask_ai_copilot(query, ticker, price, tech_context, initial_analysis_str, chat_history):
    if not GEMINI_API_KEY:
        return "กรุณาใส่ API Key"
    client = genai.Client(api_key=GEMINI_API_KEY)
    history_context = "\n".join([f"{msg['role'].capitalize()}: {msg['content']}" for msg in chat_history[-5:]])
    prompt = f"""
    บริบท: หุ้น {ticker} ราคา: {price}
    เทคนิคอลดิบ: {tech_context}
    คำตัดสิน AI ก่อนหน้า: {initial_analysis_str}
    ประวัติการสนทนา: {history_context}
    คำถามผู้ใช้ล่าสุด: "{query}"
    
    จงวิเคราะห์ตอบข้อสงสัยให้ชัดเจนและอิงสถิติการลงทุน ปฏิบัติตอบภาษาไทย 100% สุภาพและเข้าใจง่าย
    ตอบสั้น กระชับ เป็นกันเอง เหมือนเพื่อนนักลงทุนอธิบายให้เพื่อนฟัง หลีกเลี่ยงศัพท์เทคนิคพ่วงท้ายโดยไม่อธิบาย
    """
    def run_copilot():
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.5)
        )
        return response.text
    return call_api_with_backoff(run_copilot)

# ==========================================
# 📌 SIDEBAR CONTROLLER
# ==========================================
with st.sidebar:
    st.markdown("<h2 style='color:#c9a86a;'>◆ PropFirmX Terminal</h2>", unsafe_allow_html=True)
    st.caption("AI Debate Terminal — Gemini × Claude")
    st.divider()
    
    st.write("🔍 **ค้นหาและปลดล็อกสินทรัพย์**")
    search_ticker = st.text_input("ระบุสัญลักษณ์ (เช่น AAPL, PTT.BK, BTC-USD):", value=st.session_state.active_ticker).upper()
    safe_search_ticker = re.sub(r'[^A-Z0-9.:\-]', '', search_ticker)
    
    if st.button("⚡ โหลดราคากราฟหลัก", use_container_width=True):
        st.session_state.active_ticker = safe_search_ticker
        st.session_state.ai_debate_result = None
        st.session_state.chat_history = []
        st.toast(f"อัปเดตสลับสินทรัพย์หลักเป็น {safe_search_ticker} แล้ว!", icon="✅")
        st.rerun()
        
    st.write("⏱️ **เลือกช่วงเวลาเทคนิคอล**")
    selected_tf = st.selectbox("เลือกช่วงกราฟ:", list(tf_mapping.keys()), index=list(tf_mapping.keys()).index(st.session_state.timeframe))
    if selected_tf != st.session_state.timeframe:
        st.session_state.timeframe = selected_tf
        st.rerun()
        
    st.divider()
    if not GEMINI_API_KEY:
        st.warning("⚠️ ยังไม่ได้ตั้งคีย์ GEMINI_API_KEY ใน Secrets")
    else:
        st.success("✅ Gemini Engine พร้อมใช้งาน")
    if not ANTHROPIC_API_KEY:
        st.info("ℹ️ ไม่พบ ANTHROPIC_API_KEY (ระบบจะรันดีเบตโดยใช้ออโต้ดีเบตโมเดลคู่ควบคู่จำลองทดแทน)")
    else:
        st.success("✅ Claude Engine พร้อมคู่ขนาน!")

# ==========================================
# 📌 MAIN WORKSPACE
# ==========================================
ticker = st.session_state.active_ticker

@st.cache_data(ttl=60)
def get_main_ticker_data(t):
    try:
        ticker_df = yf.Ticker(t).history(period="3mo", interval="1d", auto_adjust=False)
        ticker_df = ticker_df.dropna(subset=["Open", "High", "Low", "Close"])
        if ticker_df.empty:
            raise ValueError("No data returned")

        # ราคาปิดรายวันล่าสุด ใช้เป็น "ราคาปิดเมื่อวาน" สำหรับคำนวณ % เปลี่ยนแปลง
        # และเป็น fallback เผื่อดึงราคาอินทราเดย์ไม่สำเร็จ (เช่น ตลาดปิด/เน็ตมีปัญหา)
        daily_close = float(ticker_df['Close'].iloc[-1])
        p_close = float(ticker_df['Close'].iloc[-2]) if len(ticker_df) > 1 else daily_close

        # 🔧 แก้บั๊กราคาเก่า: แท่งรายวัน (interval="1d") ของ Yahoo ระหว่างตลาดเปิดอยู่
        # จะอัปเดตเป็นช่วงๆ ไม่ต่อเนื่อง ทำให้ราคาที่ AI ใช้วิเคราะห์ค้างเก่ากว่าราคาจริงได้มาก
        # จึงดึงแท่งอินทราเดย์ (1 นาที) มาใช้เป็นราคาปัจจุบันแทน ให้ใกล้เคียงราคาจริงที่สุด
        current_p = daily_close
        try:
            intraday = yf.Ticker(t).history(period="1d", interval="1m", auto_adjust=False)
            intraday = intraday.dropna(subset=["Close"])
            if not intraday.empty:
                current_p = float(intraday['Close'].iloc[-1])
        except Exception as e:
            print(f"Intraday price fetch failed for {t}, ใช้ราคาปิดรายวันแทน: {e}")

        delta = ticker_df['Close'].diff()
        gain = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
        loss = -delta.where(delta < 0, 0).ewm(alpha=1/14, adjust=False).mean()
        rsi_v = float((100 - (100 / (1 + (gain/(loss + 1e-10))))).iloc[-1]) 
        ma20_v = float(ticker_df['Close'].rolling(window=20).mean().iloc[-1]) if len(ticker_df) >= 20 else current_p
        std_v = float(ticker_df['Close'].rolling(window=20).std().iloc[-1]) if len(ticker_df) >= 20 else 0.0
        bb_upper_v = ma20_v + (std_v * 2)
        bb_lower_v = ma20_v - (std_v * 2)
        
        # MACD
        ema12 = ticker_df['Close'].ewm(span=12, adjust=False).mean()
        ema26 = ticker_df['Close'].ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = float((macd_line - signal_line).iloc[-1])
        
        # Volume
        volume = ticker_df['Volume']
        vol_avg = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else float(volume.iloc[-1])
        vol_now = float(volume.iloc[-1])
        vol_ratio = round(vol_now / vol_avg, 2) if vol_avg > 0 else 1.0
        
        change_pct = round((current_p - p_close) / p_close * 100, 2)

        return current_p, change_pct, rsi_v, ma20_v, bb_upper_v, bb_lower_v, macd_hist, vol_ratio
    except Exception as e:
        print(f"Fetch main data error for {t}: {e}")
        st.toast(f"⚠️ ดึงข้อมูลราคาของ {t} ไม่สำเร็จ กำลังใช้ข้อมูลจำลองชั่วคราว", icon="⚠️")
        return 150.00, 0.0, 50.0, 150.0, 155.0, 145.0, 0.0, 1.0 # Dummy fallback

# ==========================================
# 📈 REALTIME CHART (streamlit-lightweight-charts)
# ==========================================
CHART_INDICATOR_OPTIONS = ["EMA20", "EMA50", "SMA20", "Bollinger Bands", "RSI", "MACD", "Volume"]

@st.cache_data(ttl=30)  # cache สั้น 30 วิ เพื่อให้กราฟใกล้เคียงราคาจริงที่สุด แต่ไม่ยิง request ถี่เกินไป
def get_chart_ohlcv_data(t, period, interval):
    """ดึงราคา OHLCV สำหรับกราฟ พร้อมคำนวณอินดิเคเตอร์ทั้งชุดไว้ล่วงหน้าเป็น Series ตลอดทั้งกราฟ

    🔧 แก้บั๊ก timestamp=0 (แสดงวันที่ 1 ม.ค. 1970 ทุกแท่ง): เดิมใช้ yf.download() ซึ่งคืน
    DataFrame แบบ MultiIndex คอลัมน์ที่โครงสร้างเปลี่ยนไปตามเวอร์ชัน yfinance ทำให้บางครั้งดึง
    index วันที่ผิดคอลัมน์ เปลี่ยนมาใช้ yf.Ticker(t).history() แทน ซึ่งเป็น API ระดับหุ้นเดี่ยว
    คืนคอลัมน์ปกติ (ไม่มี MultiIndex) พร้อม DatetimeIndex ที่ถูกต้องเสมอ ตัดปัญหาเดาคอลัมน์ทิ้งไปเลย
    """
    try:
        df = yf.Ticker(t).history(period=period, interval=interval, auto_adjust=False)
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if df.empty:
            return None

        dt_index = df.index
        if getattr(dt_index, "tz", None) is not None:
            dt_index = dt_index.tz_convert("UTC").tz_localize(None)

        # ตรวจสอบความสมเหตุสมผลของวันที่ กันไว้เผื่อ index ยังผิดปกติอยู่ (เช่น เป็นตัวเลขล้วน)
        # จะได้เจอ error ชัดเจนแทนที่จะเงียบแล้วโชว์กราฟวันที่ 1970 แบบเดิม
        if dt_index.min().year < 2000:
            raise ValueError(f"วันที่ที่ได้จาก yfinance ผิดปกติ (ปีน้อยกว่า 2000): {dt_index.min()}")

        df = df.reset_index(drop=True)
        df["time"] = dt_index.astype("datetime64[s]").astype("int64").to_numpy()

        close = df["Close"]
        df["ema20"] = close.ewm(span=20, adjust=False).mean()
        df["ema50"] = close.ewm(span=50, adjust=False).mean()
        df["sma20"] = close.rolling(window=20).mean()
        bb_std = close.rolling(window=20).std()
        df["bb_upper"] = df["sma20"] + (bb_std * 2)
        df["bb_lower"] = df["sma20"] - (bb_std * 2)

        delta = close.diff()
        gain = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
        loss = -delta.where(delta < 0, 0).ewm(alpha=1/14, adjust=False).mean()
        df["rsi"] = 100 - (100 / (1 + (gain / (loss + 1e-10))))

        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        df["macd_line"] = ema12 - ema26
        df["macd_signal"] = df["macd_line"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = df["macd_line"] - df["macd_signal"]

        return df
    except Exception as e:
        print(f"Chart data fetch error for {t}: {e}")
        return None


def _lw_series(df, col):
    """แปลงคอลัมน์ DataFrame เป็น list ของจุดข้อมูลสำหรับ Lightweight Charts (ตัด NaN ออก)"""
    return [{"time": int(t), "value": float(v)} for t, v in zip(df["time"], df[col]) if pd.notna(v)]


def render_realtime_chart(t, period, interval, selected_indicators, chart_key_prefix):
    """วาดกราฟแท่งเทียนเรียลไทม์ (เรียลไทม์แบบ polling ตามรอบรีเฟรช ไม่ใช่ tick สด เพราะ Yahoo Finance
    เป็นข้อมูลฟรี ไม่ใช่ feed สตรีมมิ่งจริง) พร้อมอินดิเคเตอร์ที่เลือกไว้ และ RSI/MACD แยกเป็นพาเนลด้านล่างถ้าเลือก"""
    df = get_chart_ohlcv_data(t, period, interval)
    if df is None or df.empty:
        st.warning(f"⚠️ ไม่พบข้อมูลกราฟของ {t}")
        return

    base_chart_options = {
        "layout": {"background": {"type": "solid", "color": "#0a0c10"}, "textColor": "#d1d4dc"},
        "grid": {"vertLines": {"color": "rgba(42,46,57,0.4)"}, "horzLines": {"color": "rgba(42,46,57,0.4)"}},
        "timeScale": {"timeVisible": True, "secondsVisible": False},
    }

    candle_data = df[["time", "Open", "High", "Low", "Close"]].rename(
        columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"}
    ).to_dict("records")

    price_series = [{
        "type": "Candlestick",
        "data": candle_data,
        "options": {
            "upColor": "#26a69a", "downColor": "#ef5350", "borderVisible": False,
            "wickUpColor": "#26a69a", "wickDownColor": "#ef5350",
        },
    }]

    if "EMA20" in selected_indicators:
        price_series.append({"type": "Line", "data": _lw_series(df, "ema20"),
                              "options": {"color": "#f5b942", "lineWidth": 2, "title": "EMA20"}})
    if "EMA50" in selected_indicators:
        price_series.append({"type": "Line", "data": _lw_series(df, "ema50"),
                              "options": {"color": "#38bdf8", "lineWidth": 2, "title": "EMA50"}})
    if "SMA20" in selected_indicators:
        price_series.append({"type": "Line", "data": _lw_series(df, "sma20"),
                              "options": {"color": "#a855f7", "lineWidth": 1, "title": "SMA20"}})
    if "Bollinger Bands" in selected_indicators:
        price_series.append({"type": "Line", "data": _lw_series(df, "bb_upper"),
                              "options": {"color": "rgba(148,163,184,0.6)", "lineWidth": 1, "title": "BB Upper"}})
        price_series.append({"type": "Line", "data": _lw_series(df, "bb_lower"),
                              "options": {"color": "rgba(148,163,184,0.6)", "lineWidth": 1, "title": "BB Lower"}})

    if "Volume" in selected_indicators:
        volume_data = [
            {"time": int(row["time"]), "value": float(row["Volume"]),
             "color": "rgba(38,166,154,0.5)" if row["Close"] >= row["Open"] else "rgba(239,83,80,0.5)"}
            for _, row in df.iterrows()
        ]
        price_series.append({
            "type": "Histogram",
            "data": volume_data,
            "options": {
                "priceFormat": {"type": "volume"},
                "priceScaleId": ""  # ค่าว่าง = overlay scale แยกต่างหาก ไม่แย่ง scale หลักกับแท่งเทียน
            },
            "priceScale": {
                "scaleMargins": {"top": 0.8, "bottom": 0}  # บีบให้ volume แสดงแค่ 20% ล่างของพาเนล
            }
        })

    price_chart_options = {**base_chart_options, "height": 420}

    renderLightweightCharts([{"chart": price_chart_options, "series": price_series}], key=f"{chart_key_prefix}_price")

    if "RSI" in selected_indicators:
        st.caption("RSI (14)")
        rsi_series = [{"type": "Line", "data": _lw_series(df, "rsi"),
                       "options": {"color": "#f5b942", "lineWidth": 2, "title": "RSI(14)"}}]
        renderLightweightCharts(
            [{"chart": {**base_chart_options, "height": 130}, "series": rsi_series}],
            key=f"{chart_key_prefix}_rsi"
        )

    if "MACD" in selected_indicators:
        st.caption("MACD (12, 26, 9)")
        macd_hist_data = [
            {"time": int(t2), "value": float(v),
             "color": "rgba(38,166,154,0.7)" if v >= 0 else "rgba(239,83,80,0.7)"}
            for t2, v in zip(df["time"], df["macd_hist"]) if pd.notna(v)
        ]
        macd_series = [
            {"type": "Histogram", "data": macd_hist_data, "options": {"title": "Histogram"}},
            {"type": "Line", "data": _lw_series(df, "macd_line"), "options": {"color": "#38bdf8", "lineWidth": 1, "title": "MACD"}},
            {"type": "Line", "data": _lw_series(df, "macd_signal"), "options": {"color": "#f5b942", "lineWidth": 1, "title": "Signal"}},
        ]
        renderLightweightCharts(
            [{"chart": {**base_chart_options, "height": 150}, "series": macd_series}],
            key=f"{chart_key_prefix}_macd"
        )

    st.caption(f"🔄 อัปเดตล่าสุด {datetime.now().strftime('%H:%M:%S')} น. · ข้อมูลจาก Yahoo Finance (ฟรี อาจดีเลย์เล็กน้อย ไม่ใช่ tick สด 100%)")

current_p, change_pct, rsi_v, ma20_v, bb_upper_v, bb_lower_v, macd_hist, vol_ratio = get_main_ticker_data(ticker)

# 1️⃣ MIDDLE SECTION: กราฟเรียลไทม์ (Lightweight Charts) และ สรุปราคาสด
col_left_main, col_right_panel = st.columns([3, 1])

with col_left_main:
    st.markdown(f"#### 📈 Live Market Technical Chart: <span style='color:#38bdf8;'>{ticker}</span> ({st.session_state.timeframe})", unsafe_allow_html=True)

    col_ind_select, col_refresh = st.columns([3, 1])
    with col_ind_select:
        selected_chart_indicators = st.multiselect(
            "เลือกอินดิเคเตอร์ที่จะแสดงบนกราฟ:",
            CHART_INDICATOR_OPTIONS,
            default=["EMA20", "Volume"],
            key="chart_indicators_select"
        )
    with col_refresh:
        chart_refresh_seconds = st.selectbox(
            "รีเฟรชทุก:", [10, 15, 30, 60], index=1,
            key="chart_refresh_seconds", format_func=lambda s: f"{s} วิ"
        )

    @st.fragment(run_every=chart_refresh_seconds)
    def _live_chart_fragment():
        render_realtime_chart(
            ticker, current_tf["period"], current_tf["interval"],
            selected_chart_indicators,
            chart_key_prefix=f"chart_{ticker}_{current_tf['interval']}"
        )

    _live_chart_fragment()

with col_right_panel:
    st.markdown("#### 🔥 ความร้อนแรงรายวัน")
    asset_select = st.selectbox("เลือกประเภทสินทรัพย์หลัก", ["US Stocks", "Thai Stocks", "Cryptocurrency"])
    
    asset_map = {"US Stocks": "US", "Thai Stocks": "TH", "Cryptocurrency": "Crypto"}
    gainers_df, losers_df = fetch_gainers_and_losers(asset_map[asset_select])
    
    tab_g, tab_l = st.tabs(["🚀 Gainers", "📉 Losers"])
    with tab_g:
        for idx, row in gainers_df.iterrows():
            if st.button(f"🟢 {row['Ticker']}  |  {row['Change']:+.2f}%", key=f"g_{row['Ticker']}_{asset_select}", use_container_width=True):
                st.session_state.active_ticker = row['Ticker']
                st.session_state.ai_debate_result = None
                st.session_state.chat_history = []
                st.rerun()
    with tab_l:
        for idx, row in losers_df.iterrows():
            if st.button(f"🔴 {row['Ticker']}  |  {row['Change']:+.2f}%", key=f"l_{row['Ticker']}_{asset_select}", use_container_width=True):
                st.session_state.active_ticker = row['Ticker']
                st.session_state.ai_debate_result = None
                st.session_state.chat_history = []
                st.rerun()

st.divider()


# 2️⃣ BOTTOM SECTION: แท็บหน้าต่างแยกจัดการพอร์ต / สแกนเนอร์ และระบบ AI DEBATE
st.markdown("### 💼 ระบบจัดการพอร์ต (แชร์ร่วมกัน) และสแกนเนอร์สมองกล")

tab_us_class, tab_th_class, tab_crypto_class, tab_journal_class = st.tabs([
    "🇺🇸 หุ้นอเมริกา (US Stocks)", "🇹🇭 หุ้นไทย (Thai Stocks)",
    "🪙 คริปโทเคอร์เรนซี (Cryptocurrency)", "📓 Trade Journal & Win Rate"
])

def _build_sparkline_svg(prices, direction):
    """สร้าง SVG sparkline เล็กๆ จาก list ราคา คืนสตริง SVG (บรรทัดเดียว ไม่มี indent)
    direction: 'dir-buy'/'dir-sell'/'dir-neutral' ใช้เลือกสีเส้น"""
    if not prices or len(prices) < 2:
        return ""
    stroke = {"dir-buy": "#34d399", "dir-sell": "#f87171", "dir-neutral": "#94a3b8"}.get(direction, "#94a3b8")
    w, h, pad = 210.0, 76.0, 8.0
    lo, hi = min(prices), max(prices)
    span = (hi - lo) or 1.0
    n = len(prices)
    pts = []
    for i, p in enumerate(prices):
        x = pad + (w - 2 * pad) * (i / (n - 1))
        y = pad + (h - 2 * pad) * (1 - (p - lo) / span)  # ราคาสูง = y น้อย (อยู่บน)
        pts.append((round(x, 1), round(y, 1)))
    line_pts = " ".join(f"{x},{y}" for x, y in pts)
    # พื้นที่ใต้เส้น (area fill จางๆ) ปิดขอบล่าง
    area_pts = f"{pad},{h - pad} " + line_pts + f" {w - pad},{h - pad}"
    return (
        f'<svg class="spark-svg" viewBox="0 0 {int(w)} {int(h)}" preserveAspectRatio="none">'
        f'<polyline points="{area_pts}" fill="{stroke}" fill-opacity="0.12" stroke="none"/>'
        f'<polyline points="{line_pts}" fill="none" stroke="{stroke}" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )


def render_portfolio_and_scanner_area(portfolio_key, scanner_market_list, default_scanned_df, is_penny=False, postfix=""):
    # เก็บผลสแกนแยกตามแท็บ (portfolio_key) ไม่ใช้ key กลางร่วมกันทุกแท็บแบบเดิม
    # เพื่อกัน "สแกนหุ้นไทยแล้วไปโผล่ในแท็บคริปโต" และเพื่อแยกแยะ "ยังไม่กดสแกน" กับ "สแกนแล้วแต่ไม่พบ" ได้ถูกต้อง
    scan_results_key = f"scan_results_{portfolio_key}"
    scan_has_run_key = f"scan_has_run_{portfolio_key}"
    if scan_results_key not in st.session_state:
        st.session_state[scan_results_key] = []
    if scan_has_run_key not in st.session_state:
        st.session_state[scan_has_run_key] = False

    # 🌍 map portfolio_key → market_type สำหรับเลือกดัชนีอ้างอิงเช็คภาวะตลาดรวม (Market Regime)
    market_type = {"port_us": "US", "port_th": "TH", "port_crypto": "Crypto"}.get(portfolio_key, "US")

    col_s, col_a = st.columns([1.7, 0.9])

    with col_s:
        # 🌍 แสดงภาวะตลาดรวมของตลาดนี้ ก่อนตัวเลือกสแกน (ช่วยให้เห็นบริบทก่อนเชื่อสัญญาณรายตัว)
        regime_info = get_market_regime(MARKET_REGIME_INDEX.get(market_type))
        regime_badge = {
            "BULLISH": ("🟢", "ขาขึ้น", "#10b981"),
            "BEARISH": ("🔴", "ขาลง", "#ef4444"),
            "NEUTRAL": ("⚪", "ไซด์เวย์", "#94a3b8"),
            "UNKNOWN": ("❔", "ไม่ทราบ (ดึงข้อมูลไม่สำเร็จ)", "#94a3b8"),
        }.get(regime_info.get("regime"), ("❔", "ไม่ทราบ", "#94a3b8"))
        icon, label, color = regime_badge
        diff_txt = f" ({regime_info['diff_pct']:+.1f}% จาก MA{regime_info['window']})" if regime_info.get("diff_pct") is not None else ""
        st.markdown(
            f"""<div style="background-color:#161b26; border-left:4px solid {color}; border-radius:6px;
                 padding:8px 12px; margin-bottom:10px; font-size:0.85rem;">
                 🌍 <strong>ภาวะตลาดรวม ({postfix}):</strong> {icon} {label}{diff_txt}
                 </div>""",
            unsafe_allow_html=True
        )

        st.markdown("##### 🔍 ตัวเลือกสแกนตลาดสมองกล")
        scanner_type = st.selectbox("เลือกดัชนีคัดกรองเฉพาะด้าน:", scanner_market_list, key=f"select_scan_{portfolio_key}")
        use_ai_quality = st.checkbox(
            "🛡️ ให้ AI ช่วยประเมินคุณภาพหุ้นที่เจอเพิ่มเติม (ใช้ Gemini เพิ่ม 1 รอบ อาจช้าลงนิดหน่อย)",
            value=False, key=f"ai_quality_{portfolio_key}"
        )
        
        if st.button("🚀 ยิงพิกัดสแกนตรวจจับสัญญาณด่วน", key=f"btn_scan_{portfolio_key}", use_container_width=True, type="primary"):
            is_universe_scan = (scanner_type == "Penny Stocks (สแกนทั้งตลาด NASDAQ + AI กรองคุณภาพ)")
            is_penny_mode = is_universe_scan or (scanner_type == "Penny Stocks (ต่ำกว่า $5)")
            spinner_msg = ("สมองกลกำลังกวาดหุ้นสุ่มจากทั้งตลาด NASDAQ (อาจใช้เวลานานกว่าปกติ)..." if is_universe_scan
                           else "สมองกลกำลังกวาดดัชนีชี้วัดเทคนิคอลและจับแท็กกลยุทธ์หุ้นทั้งหมด (อาจใช้เวลาสักครู่)...")
            with st.spinner(spinner_msg):
                t_list = load_market_tickers(scanner_type)
                results = scan_market_batch(t_list, is_penny=is_penny_mode, market_type=market_type)

                run_quality_check = is_universe_scan or use_ai_quality  # 🛡️ universe scan บังคับเช็คอยู่แล้ว หรือผู้ใช้ติ๊กเลือกเอง
                if run_quality_check and results:
                    with st.spinner(f"AI กำลังตรวจสอบคุณภาพหุ้นที่เจอเพิ่มเติม ({len(results)} ตัว)..."):
                        quality_map = ai_quality_filter_stocks(tuple(r["ticker"] for r in results))
                    for r in results:
                        q = quality_map.get(str(r["ticker"]).upper())
                        if q:
                            r["quality_flag"] = q.get("quality_flag")
                            r["quality_reason"] = q.get("reason")

                st.session_state[scan_results_key] = results
                st.session_state[scan_has_run_key] = True
                st.toast(f"อัปเดตระบบตรวจสอบสัญญาณสแกนเนอร์สำเร็จ! พบสัญญาณ {len(results)} ตัว", icon="🔥")
                st.rerun()
                
        st.write("📋 **สัญญาณด่วนและแท็กกลยุทธ์ที่ตรวจพบล่าสุด:**")
        has_run = st.session_state[scan_has_run_key]
        df_s = pd.DataFrame(st.session_state[scan_results_key]) if has_run else default_scanned_df

        if has_run and df_s.empty:
            st.info("🔍 ไม่พบหุ้นที่ตรงกับเงื่อนไขการสแกนรอบนี้ — อาจเป็นเพราะดัชนีนี้ไม่มีตัวไหนเข้าเงื่อนไขตอนนี้ "
                    "ลองเปลี่ยนดัชนีคัดกรอง หรือมาเช็คใหม่อีกครั้งวันหลัง")

        if not df_s.empty:
            # รวบรวมแท็ก/สัญญาณทั้งหมดที่เจอในรอบนี้ ทำเป็นตัวเลือกกรองกลุ่ม (เช่น "กลับตัวขึ้น (สั้น)", "BB Breakout บน")
            all_tags = set()
            for _, r in df_s.iterrows():
                if isinstance(r.get("strategy_tags"), list):
                    all_tags.update(label for label, kind in r["strategy_tags"])
                if isinstance(r.get("signals"), list):
                    all_tags.update(label for label, kind in r["signals"])

            f_col1, f_col2, f_col3 = st.columns([1.2, 1.2, 1])
            with f_col1:
                selected_tag = st.selectbox("🏷️ กรองตามแท็ก/สัญญาณ (ดูเป็นกลุ่มๆ):",
                                             ["ทั้งหมด"] + sorted(all_tags), key=f"tag_filter_{portfolio_key}")
            with f_col2:
                rating_filter = st.selectbox(
                    "⭐ กรองตามเรตติ้ง AI:",
                    ["ทั้งหมด", "🟢🟢 ซื้อเด่นชัด", "🟢 ซื้อ", "⚪ ถือ/เป็นกลาง", "🔴 ขาย", "🔴🔴 ขายเด่นชัด"],
                    key=f"rating_filter_{portfolio_key}"
                )
            with f_col3:
                st.caption("💡 คลิกหัวคอลัมน์ในตารางเพื่อเรียงลำดับได้ (เช่น คลิก 'คะแนน')")

            rating_filter_map = {
                "🟢🟢 ซื้อเด่นชัด": "STRONG_BUY", "🟢 ซื้อ": "BUY", "⚪ ถือ/เป็นกลาง": "NEUTRAL",
                "🔴 ขาย": "SELL", "🔴🔴 ขายเด่นชัด": "STRONG_SELL",
            }

            table_rows = []
            for _, r in df_s.iterrows():
                tags_list = [label for label, kind in r["strategy_tags"]] if isinstance(r.get("strategy_tags"), list) else []
                sig_list = [label for label, kind in r["signals"]] if isinstance(r.get("signals"), list) else []
                combined_tags = tags_list + sig_list
                if selected_tag != "ทั้งหมด" and selected_tag not in combined_tags:
                    continue
                if rating_filter != "ทั้งหมด" and r.get("rating_code") != rating_filter_map.get(rating_filter):
                    continue
                change = r.get("change_pct", 0.0)
                table_rows.append({
                    "ticker": r.get("ticker", r.get("Ticker", "Unknown")),
                    "price": r.get("price", r.get("Price", 0.0)),
                    "change_str": f"🟢 +{change:.2f}%" if change >= 0 else f"🔴 {change:.2f}%",
                    "tags": ", ".join(combined_tags) if combined_tags else "-",
                    "rating_icon": r.get("rating_icon", ""),
                    "rating_label": r.get("rating_label", "-"),
                    "rating_score": r.get("rating_score", 0.0),
                    "quality": r.get("quality_flag", ""),
                    "quality_reason": r.get("quality_reason", ""),
                    "sparkline": r.get("sparkline", []),
                })

            if not table_rows:
                st.info('ไม่พบหุ้นที่ตรงกับตัวกรองที่เลือก ลองเลือก "ทั้งหมด" ดูครับ')
            else:
                # เรียงจากเรตติ้งดีสุด (คะแนนสูงสุด) ไปแย่สุด เป็นค่าเริ่มต้น
                table_rows.sort(key=lambda x: x["rating_score"], reverse=True)
                quality_label_map = {"ปกติ": "✅ ปกติ", "ระมัดระวังสูง": "⚠️ ระมัดระวังสูง", "ไม่แน่ใจ": "❔ ไม่แน่ใจ"}

                # 🎴 Photo-led card grid: การ์ดไล่สีแทนรูปภาพ (เขียว=ซื้อ, แดง=ขาย, เทา=เป็นกลาง)
                # ตามสไตล์ eyebrow tag ตัวพิมพ์เล็ก + หัวข้อตัวหนา + คำอธิบายบาง
                # ⚠️ สำคัญ: HTML แต่ละบรรทัดต้องชิดซ้าย (ไม่มี indent นำหน้า) เพราะ markdown ของ Streamlit
                # จะตีความบรรทัดที่เว้นวรรค 4 ช่องขึ้นไปเป็น "code block" แล้วแสดง tag ดิบออกมาแทนที่จะ render
                cards_html = ['<div class="stock-card-grid">']
                for row in table_rows[:60]:  # จำกัด 60 การ์ดแรกกันหน้าหนักเกินไป (เรียงดีสุดไว้บนแล้ว)
                    code = str(row.get("rating_label", ""))
                    if "ซื้อ" in code:
                        direction = "dir-buy"
                    elif "ขาย" in code:
                        direction = "dir-sell"
                    else:
                        direction = "dir-neutral"
                    quality_txt = quality_label_map.get(row["quality"], "") if row["quality"] else ""
                    eyebrow_right = quality_txt if quality_txt else "สแกนอัตโนมัติ"
                    spark_svg = _build_sparkline_svg(row.get("sparkline", []), direction)
                    _tk = row["ticker"]
                    card = (
                        f'<a class="stock-card-link" href="?view={_tk}" target="_self">'
                        '<div class="stock-card">'
                        f'<div class="stock-card-visual {direction}">'
                        f'{spark_svg}'
                        f'<div class="stock-card-score-chip">{row["rating_icon"]} {row["rating_score"]:+.2f}</div>'
                        '</div>'
                        '<div class="stock-card-body">'
                        f'<div class="stock-card-eyebrow">{row["change_str"]} &nbsp;·&nbsp; {eyebrow_right}</div>'
                        f'<div class="stock-card-title">{_tk}</div>'
                        f'<div class="stock-card-desc">${row["price"]:,.2f} &nbsp;·&nbsp; {row["rating_label"]}<br>{row["tags"]}</div>'
                        '</div>'
                        '<div class="stock-card-cta">📈 กดเพื่อดูกราฟ</div>'
                        '</div>'
                        '</a>'
                    )
                    cards_html.append(card)
                cards_html.append('</div>')
                st.markdown("".join(cards_html), unsafe_allow_html=True)
                if len(table_rows) > 60:
                    st.caption(f"แสดง 60 จาก {len(table_rows)} ตัวที่ตรงเงื่อนไข (เรียงเรตติ้งดีสุดไว้บนแล้ว)")

                # แยกเหตุผลของ AI เฉพาะตัวที่เตือน "ระมัดระวังสูง" ไว้ในกล่องพับเก็บ ไม่ให้ตารางหลักรกเกินไป
                warn_rows = [row for row in table_rows if row["quality"] == "ระมัดระวังสูง" and row["quality_reason"]]
                if warn_rows:
                    with st.expander(f"⚠️ ดูเหตุผลที่ AI เตือนระมัดระวังสูง ({len(warn_rows)} ตัว)"):
                        for row in warn_rows:
                            st.markdown(f"- **{row['ticker']}**: {row['quality_reason']}")
        else:
            st.info("💡 ไม่พบสัญญาณตลาด แนะนำกวาดสแกนด้วยตนเอง")
                
    with col_a:
        st.markdown("##### 🧠 AI Debate Expert")
        st.markdown("""
            <div class="vs-banner" style="margin-bottom:10px;">
                <div class="vs-side vs-gemini" style="padding: 6px 12px;">
                    <div class="vs-label" style="font-size:0.75rem;">◷ GEMINI</div>
                </div>
                <div class="vs-divider" style="font-size:0.75rem;">⟷</div>
                <div class="vs-side vs-claude" style="padding: 6px 12px;">
                    <div class="vs-label" style="font-size:0.75rem;">◈ CLAUDE</div>
                </div>
            </div>
            """, unsafe_allow_html=True)

        # 📰 โชว์ badge ข่าวล่าสุด (จาก DB เดิม ไม่ยิง API ใหม่ ไม่กระทบความเร็วหน้าเว็บ)
        _news_flags_preview = news.get_latest_flags(ticker)
        if _news_flags_preview:
            _sent_icon = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}
            _badge_html = " ".join(
                f'<span class="pill" title="{f["reasoning"]}">{_sent_icon.get(f["sentiment"], "⚪")} {f["headline"][:40]}{"…" if len(f["headline"]) > 40 else ""}</span>'
                for f in _news_flags_preview[:3]
            )
            st.markdown(f'<div style="margin-bottom:8px;">{_badge_html}</div>', unsafe_allow_html=True)

        if st.button("▶ เริ่มกระบวนการ AI Debate วิจัยด่วน", key=f"btn_ai_st_{portfolio_key}", use_container_width=True, type="primary"):
            if not GEMINI_API_KEY:
                st.error("กรุณากรอก GEMINI_API_KEY ใน secrets")
            else:
                with st.spinner("AI กำลังโต้วาทะประมวลผล (Gemini กำลังร่างความเห็นแรก)..."):
                    try:
                        # คำนวณรวบรวม Indicators ดิบ
                        ind_data = {
                            "price": current_p,
                            "change_pct": change_pct,
                            "rsi": rsi_v,
                            "ma20": ma20_v,
                            "bb_upper": bb_upper_v,
                            "bb_lower": bb_lower_v,
                            "macd_hist": macd_hist,
                            "vol_ratio": vol_ratio
                        }
                        st.session_state.ai_debate_result = run_ai_debate(ticker, ind_data)
                        st.session_state.chat_history = []
                        journal.log_verdict(ticker, st.session_state.ai_debate_result["claude"])  # 📓 บันทึกลง Trade Journal
                    except Exception as e:
                        st.error(str(e))
                        
        if st.session_state.ai_debate_result:
            res_deb = st.session_state.ai_debate_result
            g = res_deb["gemini"]
            c = res_deb["claude"]
            
            # แถบผลสรุปหลัก
            signal_upper = c.get("final_signal", "HOLD").upper()
            banner_class = "buy" if signal_upper == "BUY" else "sell" if signal_upper == "SELL" else "hold"
            icon = "🟢" if banner_class == "buy" else "🔴" if banner_class == "sell" else "🟡"
            action_label = {"buy": "ซื้อ (BUY)", "sell": "ขาย (SELL)", "hold": "ถือครอง (HOLD)"}[banner_class]
            direction_class = {"buy": "dir-buy", "sell": "dir-sell", "hold": "dir-neutral"}[banner_class]

            # 🎴 การ์ด Verdict สไตล์ photo-led เดียวกับผลสแกน (ใช้ gradient ไล่สีแทนรูปภาพ)
            # ⚠️ HTML ต้องชิดซ้าย (ไม่มี indent นำหน้า) กัน markdown ตีความเป็น code block แล้วโชว์ tag ดิบ
            verdict_html = (
                '<div class="verdict-card-wrap">'
                f'<div class="stock-card-visual {direction_class}" style="height:70px;">'
                f'<div class="stock-card-score-chip">{icon} {action_label}</div>'
                '</div>'
                '<div class="stock-card-body" style="padding:16px 18px 18px;">'
                f'<div class="stock-card-eyebrow">{ticker} &nbsp;·&nbsp; AI DEBATE VERDICT</div>'
                f'<div class="stock-card-title" style="font-size:1.4rem;">{action_label}</div>'
                f'<div class="stock-card-desc" style="font-size:0.8rem; color:#cbd5e1;">{c.get("action_summary", "")}</div>'
                f'<div style="font-size:0.75rem; color:var(--verdict); font-weight:bold; margin-top:12px;">🛡 {c.get("support_zone", "-")} &nbsp;&nbsp;|&nbsp;&nbsp; 🚀 {c.get("resistance_zone", "-")}</div>'
                '<div class="plan-grid" style="margin-top:6px; gap:8px;">'
                f'<div class="plan-cell entry" style="padding:6px 8px;"><div class="plan-label" style="font-size:0.55rem;">จุดเข้าซื้อ</div><div class="plan-value" style="font-size:0.8rem;">{c.get("entry_price", "-")}</div></div>'
                f'<div class="plan-cell stop" style="padding:6px 8px;"><div class="plan-label" style="font-size:0.55rem;">Stop Loss</div><div class="plan-value" style="font-size:0.8rem;">{c.get("stop_loss", "-")}</div></div>'
                f'<div class="plan-cell target" style="padding:6px 8px;"><div class="plan-label" style="font-size:0.55rem;">Take Profit</div><div class="plan-value" style="font-size:0.8rem;">{c.get("take_profit", "-")}</div></div>'
                '</div>'
                f'<div style="font-size:0.75rem; color:#94a3b8; margin-top:10px; line-height:1.3;"><strong>เหตุผลสรุป:</strong> {c.get("final_reasoning", "-")}</div>'
                f'<div style="font-size:0.7rem; color:#7b8494; margin-top:4px;">💼 {c.get("position_sizing_note", "-")}</div>'
                '</div>'
                '</div>'
            )
            st.markdown(verdict_html, unsafe_allow_html=True)
            
            # สรุปดีเบตจำลองความเห็นย่อย
            with st.expander("🔍 ดูบทวิพากษ์และข้อท้าทาย (Gemini vs Claude)"):
                agree_class = "pill-agree" if c.get("agrees_with_gemini") else "pill-disagree"
                agree_text = "เห็นด้วย" if c.get("agrees_with_gemini") else "ท้าทายแย้งข้อคิดเห็น"
                
                st.markdown(f"""
                <div style="font-size:0.8rem;">
                    <p><strong style="color:var(--gemini);">Gemini Opinion:</strong> {g.get('market_sentiment', '-')}</p>
                    <p><strong style="color:var(--claude);">Claude Challenge:</strong> {c.get('challenge_notes', '-')}</p>
                    <span class="pill {agree_class}">{agree_text}</span>
                    <span class="pill">ความเสี่ยง: {c.get('risk_level', '-')}</span>
                </div>
                """, unsafe_allow_html=True)
            
            # ส่วนแชทสืบถามเพิ่มเติมกับ AI Copilot
            st.divider()
            st.write("💬 **ถาม-ตอบโต้ตอบ AI Copilot:**")
            for chat in st.session_state.chat_history:
                style_class = "chat-bubble-user" if chat["role"] == "user" else "chat-bubble-ai"
                sender = "คุณ" if chat["role"] == "user" else "AI Copilot"
                st.markdown(f"""<div class="{style_class}"><strong>{sender}:</strong><br>{chat['content']}</div>""", unsafe_allow_html=True)
            
            with st.form(key=f"chat_form_{portfolio_key}", clear_on_submit=True):
                user_query = st.text_input("ปรึกษาโมเมนตัมเพิ่มเติม:", key=f"input_query_{portfolio_key}")
                if st.form_submit_button("ส่งคำถาม") and user_query:
                    tech_c = f"RSI={rsi_v:.1f}, MACD_Hist={macd_hist:.4f}"
                    ai_orig = f"Verdict={c.get('final_signal', '')}, Entry={c.get('entry_price', '')}, TP={c.get('take_profit', '')}"
                    with st.spinner("AI กำลังวิเคราะห์..."):
                        copilot_ans = ask_ai_copilot(user_query, ticker, current_p, tech_c, ai_orig, st.session_state.chat_history)
                    st.session_state.chat_history.extend([
                        {"role": "user", "content": user_query},
                        {"role": "copilot", "content": copilot_ans}
                    ])
                    st.rerun()
        else:
            st.info("💡 กดปุ่มด้านบนเพื่อประมวลผลวิเคราะห์จุดเทรดด้วยระบบ AI Debate")

# ==========================================
# 📓 TRADE JOURNAL & WIN-RATE TAB
# ==========================================
def render_trade_journal_tab():
    st.markdown("##### 📓 Trade Journal — ติดตามผลจริงของคำตัดสิน AI ย้อนหลัง")
    st.caption("ทุกครั้งที่กด '▶ เริ่มกระบวนการ AI Debate' ระบบจะบันทึก verdict ของ Claude ไว้ที่นี่อัตโนมัติ "
               "แล้วกดปุ่มด้านล่างเพื่อเช็คกับราคาจริงว่าผลลัพธ์เป็นยังไง")

    col_btn, col_note = st.columns([1, 2])
    with col_btn:
        if st.button("🔄 อัปเดตผลย้อนหลัง (เช็คราคาจริง)", use_container_width=True, type="primary"):
            with st.spinner("กำลังเช็คราคาย้อนหลังเทียบกับ Take Profit / Stop Loss ของแต่ละรายการ..."):
                updated, errors = journal.settle_journal_entries()
            if errors:
                st.warning(f"อัปเดตสำเร็จ {updated} รายการ มีบางตัวดึงราคาไม่สำเร็จ {errors} รายการ (ลองกดใหม่ได้)")
            else:
                st.toast(f"อัปเดตผลสำเร็จ {updated} รายการ", icon="✅")
            st.rerun()
    with col_note:
        st.caption("⚠️ การเช็คนี้ต้องดึงราคาย้อนหลังของทุก ticker ที่ยังไม่ปิดสถานะ อาจใช้เวลาสักครู่ถ้ามีรายการเยอะ")

    stats = journal.get_win_rate_stats()

    st.divider()
    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("บันทึกทั้งหมด", stats["total"])
    s2.metric("ยังเปิดอยู่", stats["open"])
    s3.metric("ชนะ", stats["win"])
    s4.metric("แพ้", stats["loss"])
    win_rate_display = f"{stats['win_rate_pct']}%" if stats["win_rate_pct"] is not None else "ยังไม่มีข้อมูล"
    s5.metric("Win Rate", win_rate_display)

    if stats["win_rate_pct"] is None:
        st.info("💡 ยังไม่มีรายการที่ปิดสถานะแพ้/ชนะ ลองใช้งาน AI Debate สักพักแล้วกลับมากดอัปเดตผลย้อนหลังอีกครั้ง")

    if not stats["by_signal"].empty:
        st.write("**Win Rate แยกตามประเภทสัญญาณ:**")
        for _, row in stats["by_signal"].iterrows():
            st.markdown(f"- **{row['final_signal']}**: {row['win_rate_pct']}% (จากที่ตัดสินผลแล้ว {row['n']} ครั้ง)")

    st.divider()
    st.write("**📋 รายการล่าสุด:**")
    recent = journal.get_recent_entries(limit=30)
    if recent.empty:
        st.info("💡 ยังไม่มีรายการในสมุดบันทึก — ไปลองกด AI Debate ที่หุ้นตัวไหนก็ได้ดูครับ")
    else:
        outcome_label = {
            "win": "✅ ชนะ", "loss": "❌ แพ้", "pending": "⏳ รอผล",
            "expired": "⌛ หมดอายุ", "not_applicable": "➖ ไม่นับ (HOLD)"
        }
        display_df = recent.copy()
        display_df["ผลลัพธ์"] = display_df["outcome"].map(outcome_label).fillna(display_df["outcome"])
        display_df = display_df[[
            "ticker", "created_at", "final_signal", "entry_price",
            "stop_loss", "take_profit", "ผลลัพธ์"
        ]].rename(columns={
            "ticker": "หุ้น", "created_at": "วันที่บันทึก", "final_signal": "สัญญาณ",
            "entry_price": "เข้าซื้อ", "stop_loss": "Stop Loss", "take_profit": "Take Profit"
        })
        st.dataframe(display_df, use_container_width=True, hide_index=True)


# ดำเนินการกระจายหน้าตามแท็บคลาสต่างๆ
with tab_us_class:
    render_portfolio_and_scanner_area(
        "port_us", ["NASDAQ 100", "S&P 500", "Penny Stocks (ต่ำกว่า $5)", "Penny Stocks (สแกนทั้งตลาด NASDAQ + AI กรองคุณภาพ)"],
        pd.DataFrame([
            {"ticker": "AAPL", "price": 180.25, "change_pct": 1.2, "strategy_tags": [("กลับตัวขึ้น (กลาง)", "reversal-medium")], "signals": [("MACD GoldCross", "buy")]}
        ]), postfix="US Stocks"
    )

with tab_th_class:
    render_portfolio_and_scanner_area(
        "port_th", ["SET100 (หุ้นไทย)", "SET50 (หุ้นไทย)"],
        pd.DataFrame([
            {"ticker": "PTT.BK", "price": 32.50, "change_pct": -0.8, "strategy_tags": [("กลับตัวขึ้น (สั้น)", "reversal-short")], "signals": [("RSI Oversold", "buy")]}
        ]), postfix="Thai Stocks"
    )

with tab_crypto_class:
    render_portfolio_and_scanner_area(
        "port_crypto", ["Crypto (Top Coins)", "Crypto (Alt/Meme Coins)"],
        pd.DataFrame([
            {"ticker": "BTC-USD", "price": 61500.00, "change_pct": 2.5, "strategy_tags": [("กลับตัวขึ้น (กลาง)", "reversal-medium")], "signals": [("BB Breakout บน", "buy")]}
        ]), postfix="Crypto"
    )

with tab_journal_class:
    render_trade_journal_tab()

st.markdown("<div style='text-align:center; color:#7b8494; font-size:0.75rem; margin-top:24px;'>"
            "ข้อมูลนี้ถูกประมวลผลด้วยโมเดลวิเคราะห์เชิงกลยุทธ์ Gemini 3.1 flash lite และ Claude 3.5 เพื่อใช้เพื่อการศึกษาเทคโนโลยีการเงินเท่านั้น"
            "</div>", unsafe_allow_html=True)
