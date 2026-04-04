import os
import math
import time
import json
import uuid
import hmac
import html
import sqlite3
import logging
import hashlib
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, request, jsonify

# =========================================================
# LVBXNT_Crypto_Bot - V1
# Single-file premium crypto Telegram bot for Render
# =========================================================

# -----------------------------
# CONFIG
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()  # optional
SCAN_SECRET = os.getenv("SCAN_SECRET", "").strip()        # required for /run_scan
DATABASE_PATH = os.getenv("DATABASE_PATH", "crypto_bot.db")
PORT = int(os.getenv("PORT", "10000"))

BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://api.binance.com")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

DEFAULT_INTERVAL = "1h"
DEFAULT_LIMIT = 120
DEFAULT_RISK_PERCENT = 1.0
SIGNAL_COOLDOWN_SECONDS = 60 * 60 * 6  # 6 hours
AUTO_SCAN_COOLDOWN_SECONDS = 60 * 15   # 15 minutes
REQUEST_TIMEOUT = 20

SUPPORTED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT"
]

DEFAULT_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"
]

MODE_CONFIG = {
    "prudent": {
        "label": "Prudent",
        "min_score_signal": 78,
        "min_score_autoscan": 82,
        "rsi_buy_min": 54,
        "rsi_sell_max": 46,
    },
    "normal": {
        "label": "Normal",
        "min_score_signal": 68,
        "min_score_autoscan": 72,
        "rsi_buy_min": 51,
        "rsi_sell_max": 49,
    },
    "agressif": {
        "label": "Agressif",
        "min_score_signal": 58,
        "min_score_autoscan": 62,
        "rsi_buy_min": 48,
        "rsi_sell_max": 52,
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("LVBXNT_Crypto_Bot")

app = Flask(__name__)
SCAN_LOCK = threading.Lock()


# =========================================================
# DATABASE
# =========================================================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db() -> None:
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            mode TEXT DEFAULT 'normal',
            auto_scan_enabled INTEGER DEFAULT 0,
            created_at INTEGER,
            updated_at INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            UNIQUE(chat_id, symbol)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            signal_id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            score INTEGER NOT NULL,
            grade TEXT NOT NULL,
            mode TEXT NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            tp1 REAL,
            tp2 REAL,
            tp3 REAL,
            market_price REAL,
            trend TEXT,
            momentum TEXT,
            summary TEXT,
            rationale_json TEXT,
            is_auto_scan INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()
    conn.close()


def now_ts() -> int:
    return int(time.time())


def set_meta(key: str, value: str) -> None:
    conn = db_connect()
    conn.execute("""
        INSERT INTO bot_meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    conn.commit()
    conn.close()


def get_meta(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = db_connect()
    row = conn.execute("SELECT value FROM bot_meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


# =========================================================
# TELEGRAM HELPERS
# =========================================================
def telegram_request(method: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")
    url = f"{TELEGRAM_API_BASE}/{method}"
    resp = requests.post(url, json=payload or {}, timeout=REQUEST_TIMEOUT)
    try:
        data = resp.json()
    except Exception:
        resp.raise_for_status()
        raise
    if not data.get("ok", False):
        logger.warning("Telegram API error on %s: %s", method, data)
    return data


def send_message(
    chat_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = True
) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram_request("sendMessage", payload)


def edit_message(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = True
) -> None:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram_request("editMessageText", payload)


def answer_callback(callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
    telegram_request("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text[:200],
        "show_alert": show_alert
    })


def set_webhook() -> Dict[str, Any]:
    if not RENDER_EXTERNAL_URL:
        return {"ok": False, "description": "RENDER_EXTERNAL_URL manquant"}
    webhook_url = f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}"
    payload: Dict[str, Any] = {"url": webhook_url}
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET
    return telegram_request("setWebhook", payload)


def delete_webhook() -> Dict[str, Any]:
    return telegram_request("deleteWebhook", {})


# =========================================================
# UI / BUTTONS
# =========================================================
def inline_keyboard(rows: List[List[Tuple[str, str]]]) -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


def main_menu_keyboard() -> Dict[str, Any]:
    return inline_keyboard([
        [("🧠 Analyse Premium", "menu:analyse"), ("🚨 Auto Scan", "menu:autoscan")],
        [("📈 Ma Watchlist", "menu:watchlist"), ("🕓 Derniers Signaux", "menu:history")],
        [("⚙️ Réglages Pro", "menu:settings"), ("❓ Guide Rapide", "menu:guide")],
        [("🏠 Menu", "menu:home")]
    ])


def home_text() -> str:
    return (
        "💎 <b>LVBXNT_Crypto_Bot</b>\n"
        "Bot crypto premium — propre, lisible et rapide.\n\n"
        "🎯 <b>Ce que je fais :</b>\n"
        "• Analyse crypto premium\n"
        "• Signaux BUY / SELL / NO TRADE\n"
        "• Entry / SL / TP1 / TP2 / TP3\n"
        "• Score qualité du setup\n"
        "• Watchlist personnalisée\n"
        "• Auto Scan intelligent\n"
        "• Exécution rapide iPhone\n\n"
        "📌 <b>Utilisation rapide :</b>\n"
        "• Tape <code>/analyze BTCUSDT</code>\n"
        "• Ou utilise les boutons ci-dessous"
    )


def watchlist_keyboard(chat_id: int) -> Dict[str, Any]:
    watchlist = get_watchlist(chat_id)
    rows: List[List[Tuple[str, str]]] = []

    for symbol in SUPPORTED_SYMBOLS:
        selected = "✅" if symbol in watchlist else "➕"
        rows.append([(f"{selected} {symbol}", f"watch:toggle:{symbol}")])

    rows.append([("✅ Tout sélectionner", "watch:all"), ("♻️ Défaut", "watch:default")])
    rows.append([("🗑️ Réinitialiser", "watch:reset"), ("🏠 Menu", "menu:home")])

    return inline_keyboard(rows)


def settings_keyboard(chat_id: int) -> Dict[str, Any]:
    user = get_or_create_user(chat_id)
    current_mode = user["mode"]

    rows = []
    for mode_key, cfg in MODE_CONFIG.items():
        prefix = "✅" if current_mode == mode_key else "⚪"
        rows.append([(f"{prefix} {cfg['label']}", f"mode:set:{mode_key}")])

    rows.append([("🏠 Menu", "menu:home")])
    return inline_keyboard(rows)


def analysis_keyboard(signal_id: str) -> Dict[str, Any]:
    return inline_keyboard([
        [("📱 Exécution rapide", f"signal:quick:{signal_id}")],
        [("🕓 Sauver dans l’historique", f"signal:history:{signal_id}")],
        [("🏠 Menu", "menu:home")]
    ])


# =========================================================
# USER / WATCHLIST
# =========================================================
def get_or_create_user(chat_id: int, username: str = "", first_name: str = "") -> sqlite3.Row:
    conn = db_connect()
    row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    if not row:
        ts = now_ts()
        conn.execute("""
            INSERT INTO users(chat_id, username, first_name, mode, auto_scan_enabled, created_at, updated_at)
            VALUES (?, ?, ?, 'normal', 0, ?, ?)
        """, (chat_id, username, first_name, ts, ts))
        for symbol in DEFAULT_WATCHLIST:
            conn.execute("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES (?, ?)", (chat_id, symbol))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    else:
        conn.execute("""
            UPDATE users SET username = ?, first_name = ?, updated_at = ?
            WHERE chat_id = ?
        """, (username, first_name, now_ts(), chat_id))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row


def set_user_mode(chat_id: int, mode: str) -> None:
    conn = db_connect()
    conn.execute("UPDATE users SET mode = ?, updated_at = ? WHERE chat_id = ?", (mode, now_ts(), chat_id))
    conn.commit()
    conn.close()


def toggle_auto_scan(chat_id: int) -> bool:
    conn = db_connect()
    row = conn.execute("SELECT auto_scan_enabled FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    current = int(row["auto_scan_enabled"]) if row else 0
    new_value = 0 if current == 1 else 1
    conn.execute("UPDATE users SET auto_scan_enabled = ?, updated_at = ? WHERE chat_id = ?",
                 (new_value, now_ts(), chat_id))
    conn.commit()
    conn.close()
    return bool(new_value)


def get_watchlist(chat_id: int) -> List[str]:
    conn = db_connect()
    rows = conn.execute("SELECT symbol FROM watchlist WHERE chat_id = ? ORDER BY symbol ASC", (chat_id,)).fetchall()
    conn.close()
    return [r["symbol"] for r in rows]


def reset_watchlist(chat_id: int) -> None:
    conn = db_connect()
    conn.execute("DELETE FROM watchlist WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def set_default_watchlist(chat_id: int) -> None:
    conn = db_connect()
    conn.execute("DELETE FROM watchlist WHERE chat_id = ?", (chat_id,))
    for symbol in DEFAULT_WATCHLIST:
        conn.execute("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES (?, ?)", (chat_id, symbol))
    conn.commit()
    conn.close()


def set_all_watchlist(chat_id: int) -> None:
    conn = db_connect()
    conn.execute("DELETE FROM watchlist WHERE chat_id = ?", (chat_id,))
    for symbol in SUPPORTED_SYMBOLS:
        conn.execute("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES (?, ?)", (chat_id, symbol))
    conn.commit()
    conn.close()


def toggle_watch_symbol(chat_id: int, symbol: str) -> bool:
    conn = db_connect()
    row = conn.execute("SELECT 1 FROM watchlist WHERE chat_id = ? AND symbol = ?", (chat_id, symbol)).fetchone()
    if row:
        conn.execute("DELETE FROM watchlist WHERE chat_id = ? AND symbol = ?", (chat_id, symbol))
        conn.commit()
        conn.close()
        return False
    conn.execute("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES (?, ?)", (chat_id, symbol))
    conn.commit()
    conn.close()
    return True


# =========================================================
# BINANCE DATA
# =========================================================
def fetch_klines(symbol: str, interval: str = DEFAULT_INTERVAL, limit: int = DEFAULT_LIMIT) -> List[Dict[str, float]]:
    url = f"{BINANCE_BASE_URL}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    raw = resp.json()

    candles = []
    for item in raw:
        candles.append({
            "open_time": int(item[0]),
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
            "close_time": int(item[6]),
        })
    return candles


# =========================================================
# INDICATORS
# =========================================================
def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    result = [values[0]]
    for price in values[1:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def rsi(values: List[float], period: int = 14) -> List[float]:
    if len(values) < period + 1:
        return [50.0 for _ in values]

    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsis = [50.0] * period
    rs = avg_gain / avg_loss if avg_loss != 0 else 999.0
    rsis.append(100 - (100 / (1 + rs)))

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 999.0
        rsis.append(100 - (100 / (1 + rs)))

    while len(rsis) < len(values):
        rsis.insert(0, 50.0)
    return rsis[:len(values)]


def atr(candles: List[Dict[str, float]], period: int = 14) -> List[float]:
    if len(candles) < 2:
        return [0.0 for _ in candles]

    trs = [0.0]
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)

    result = []
    first = sum(trs[1:period + 1]) / period if len(trs) > period else sum(trs[1:]) / max(1, len(trs) - 1)
    for i in range(period):
        result.append(first)
    prev_atr = first

    for i in range(period, len(trs)):
        prev_atr = ((prev_atr * (period - 1)) + trs[i]) / period
        result.append(prev_atr)

    while len(result) < len(candles):
        result.insert(0, first)
    return result[:len(candles)]


# =========================================================
# ANALYSIS ENGINE
# =========================================================
def safe_round(price: float) -> float:
    if price >= 1000:
        return round(price, 2)
    if price >= 100:
        return round(price, 3)
    if price >= 1:
        return round(price, 4)
    if price >= 0.1:
        return round(price, 5)
    if price >= 0.01:
        return round(price, 6)
    return round(price, 8)


def grade_from_score(score: int) -> str:
    if score >= 90:
        return "A+"
    if score >= 84:
        return "A"
    if score >= 76:
        return "B+"
    if score >= 68:
        return "B"
    if score >= 60:
        return "C"
    return "D"


def risk_reward(entry: float, stop: float, target: float, signal_type: str) -> float:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        return 0.0
    return reward / risk


def analyze_symbol(symbol: str, mode: str = "normal", interval: str = DEFAULT_INTERVAL) -> Dict[str, Any]:
    if symbol not in SUPPORTED_SYMBOLS:
        return {"ok": False, "error": f"Symbole non supporté: {symbol}"}

    candles = fetch_klines(symbol, interval=interval, limit=DEFAULT_LIMIT)
    if len(candles) < 60:
        return {"ok": False, "error": "Pas assez de données"}

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    rsi14 = rsi(closes, 14)
    atr14 = atr(candles, 14)

    price = closes[-1]
    prev_price = closes[-2]
    current_ema20 = ema20[-1]
    prev_ema20 = ema20[-2]
    current_ema50 = ema50[-1]
    current_rsi = rsi14[-1]
    current_atr = atr14[-1] if atr14[-1] > 0 else max(price * 0.008, 0.0001)

    ema_spread_pct = abs(current_ema20 - current_ema50) / price * 100
    price_vs_ema20_pct = (price - current_ema20) / price * 100
    ema20_slope = current_ema20 - prev_ema20
    recent_high = max(highs[-20:])
    recent_low = min(lows[-20:])

    momentum_up = price > prev_price and closes[-1] > closes[-3] > closes[-5]
    momentum_down = price < prev_price and closes[-1] < closes[-3] < closes[-5]

    trend_bull = price > current_ema20 > current_ema50 and ema20_slope > 0
    trend_bear = price < current_ema20 < current_ema50 and ema20_slope < 0

    cfg = MODE_CONFIG.get(mode, MODE_CONFIG["normal"])
    reasons = []
    score = 0

    # Trend score (30)
    if trend_bull or trend_bear:
        score += 22
        reasons.append("Tendance claire")
        if ema_spread_pct >= 0.25:
            score += 8
            reasons.append("EMA20/EMA50 bien espacées")
    else:
        reasons.append("Tendance peu claire")

    # Momentum score (20)
    if momentum_up or momentum_down:
        score += 14
        reasons.append("Momentum présent")
        if abs(price - prev_price) / price * 100 >= 0.25:
            score += 6
            reasons.append("Impulsion exploitable")
    else:
        reasons.append("Momentum faible")

    # RSI score (15)
    if 52 <= current_rsi <= 68 or 32 <= current_rsi <= 48:
        score += 12
        reasons.append("RSI propre")
    elif 49 <= current_rsi <= 72 or 28 <= current_rsi <= 51:
        score += 7
        reasons.append("RSI acceptable")
    else:
        reasons.append("RSI extrême ou peu utile")

    # Structure score (20)
    if ema_spread_pct >= 0.18:
        score += 8
        reasons.append("Structure correcte")
    if abs(price_vs_ema20_pct) <= 1.2:
        score += 6
        reasons.append("Prix encore exploitable")
    if (recent_high - recent_low) / price * 100 >= 1.0:
        score += 6
        reasons.append("Amplitude suffisante")

    # RR potential score (15)
    rr_candidate = 0.0
    candidate_signal = "NO TRADE"

    if trend_bull and momentum_up and current_rsi >= cfg["rsi_buy_min"]:
        candidate_signal = "BUY"
        entry = price
        stop = min(current_ema20 - current_atr * 1.2, recent_low - current_atr * 0.3)
        risk = max(entry - stop, current_atr * 0.8)
        stop = entry - risk
        tp1 = entry + risk * 1.2
        tp2 = entry + risk * 2.0
        tp3 = entry + risk * 3.0
        rr_candidate = risk_reward(entry, stop, tp2, "BUY")

    elif trend_bear and momentum_down and current_rsi <= cfg["rsi_sell_max"]:
        candidate_signal = "SELL"
        entry = price
        stop = max(current_ema20 + current_atr * 1.2, recent_high + current_atr * 0.3)
        risk = max(stop - entry, current_atr * 0.8)
        stop = entry + risk
        tp1 = entry - risk * 1.2
        tp2 = entry - risk * 2.0
        tp3 = entry - risk * 3.0
        rr_candidate = risk_reward(entry, stop, tp2, "SELL")
    else:
        entry = price
        stop = price
        tp1 = price
        tp2 = price
        tp3 = price

    if rr_candidate >= 1.8:
        score += 15
        reasons.append("Risk/Reward fort")
    elif rr_candidate >= 1.3:
        score += 10
        reasons.append("Risk/Reward correct")
    elif rr_candidate > 0:
        score += 5
        reasons.append("Risk/Reward faible")

    score = min(100, max(0, int(round(score))))
    grade = grade_from_score(score)

    min_score = cfg["min_score_signal"]
    if candidate_signal == "BUY" and score < min_score:
        candidate_signal = "NO TRADE"
    if candidate_signal == "SELL" and score < min_score:
        candidate_signal = "NO TRADE"

    if ema_spread_pct < 0.08:
        candidate_signal = "NO TRADE"
        reasons.append("Marché trop compressé")
    if 47 <= current_rsi <= 53 and not (trend_bull or trend_bear):
        candidate_signal = "NO TRADE"
        reasons.append("Zone neutre")
    if abs(price_vs_ema20_pct) > 2.5:
        candidate_signal = "NO TRADE"
        reasons.append("Prix trop étendu")
    if current_atr / price * 100 < 0.25:
        reasons.append("Volatilité faible")

    trend_label = "Haussière" if trend_bull else "Baissière" if trend_bear else "Neutre"
    momentum_label = "Haussier" if momentum_up else "Baissier" if momentum_down else "Faible"

    if candidate_signal == "NO TRADE":
        entry = price
        stop = price
        tp1 = price
        tp2 = price
        tp3 = price

    summary = build_summary(
        signal_type=candidate_signal,
        trend=trend_label,
        momentum=momentum_label,
        rsi_value=current_rsi,
        ema_spread_pct=ema_spread_pct,
        score=score
    )

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": interval,
        "signal_type": candidate_signal,
        "score": score,
        "grade": grade,
        "mode": mode,
        "market_price": price,
        "entry_price": safe_round(entry),
        "stop_loss": safe_round(stop),
        "tp1": safe_round(tp1),
        "tp2": safe_round(tp2),
        "tp3": safe_round(tp3),
        "trend": trend_label,
        "momentum": momentum_label,
        "summary": summary,
        "rationale": {
            "rsi": round(current_rsi, 2),
            "ema20": safe_round(current_ema20),
            "ema50": safe_round(current_ema50),
            "atr": safe_round(current_atr),
            "ema_spread_pct": round(ema_spread_pct, 3),
            "price_vs_ema20_pct": round(price_vs_ema20_pct, 3),
            "reasons": reasons[:8],
        },
    }


def build_summary(signal_type: str, trend: str, momentum: str, rsi_value: float, ema_spread_pct: float, score: int) -> str:
    if signal_type == "BUY":
        return (
            f"Setup acheteur détecté : tendance {trend.lower()}, momentum {momentum.lower()}, "
            f"RSI à {rsi_value:.1f}, structure EMA propre ({ema_spread_pct:.2f}% d’écart). "
            f"Score global {score}/100."
        )
    if signal_type == "SELL":
        return (
            f"Setup vendeur détecté : tendance {trend.lower()}, momentum {momentum.lower()}, "
            f"RSI à {rsi_value:.1f}, structure EMA propre ({ema_spread_pct:.2f}% d’écart). "
            f"Score global {score}/100."
        )
    return (
        f"Pas de setup assez propre pour l’instant : tendance {trend.lower()}, momentum {momentum.lower()}, "
        f"RSI à {rsi_value:.1f}. Le filtre premium préfère attendre."
    )


# =========================================================
# SIGNAL STORAGE
# =========================================================
def save_signal(chat_id: int, analysis: Dict[str, Any], is_auto_scan: bool = False) -> str:
    signal_id = str(uuid.uuid4())
    conn = db_connect()
    conn.execute("""
        INSERT INTO signals(
            signal_id, chat_id, symbol, timeframe, signal_type, score, grade, mode,
            entry_price, stop_loss, tp1, tp2, tp3, market_price, trend, momentum,
            summary, rationale_json, is_auto_scan, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal_id,
        chat_id,
        analysis["symbol"],
        analysis["timeframe"],
        analysis["signal_type"],
        analysis["score"],
        analysis["grade"],
        analysis["mode"],
        analysis["entry_price"],
        analysis["stop_loss"],
        analysis["tp1"],
        analysis["tp2"],
        analysis["tp3"],
        analysis["market_price"],
        analysis["trend"],
        analysis["momentum"],
        analysis["summary"],
        json.dumps(analysis["rationale"], ensure_ascii=False),
        1 if is_auto_scan else 0,
        now_ts()
    ))
    conn.commit()
    conn.close()
    return signal_id


def get_signal(signal_id: str) -> Optional[sqlite3.Row]:
    conn = db_connect()
    row = conn.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
    conn.close()
    return row


def get_recent_signals(chat_id: int, limit: int = 8) -> List[sqlite3.Row]:
    conn = db_connect()
    rows = conn.execute("""
        SELECT * FROM signals
        WHERE chat_id = ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (chat_id, limit)).fetchall()
    conn.close()
    return rows


def recently_sent_same_signal(chat_id: int, symbol: str, signal_type: str) -> bool:
    conn = db_connect()
    row = conn.execute("""
        SELECT created_at FROM signals
        WHERE chat_id = ? AND symbol = ? AND signal_type = ?
        ORDER BY created_at DESC
        LIMIT 1
    """, (chat_id, symbol, signal_type)).fetchone()
    conn.close()
    if not row:
        return False
    return (now_ts() - int(row["created_at"])) < SIGNAL_COOLDOWN_SECONDS


# =========================================================
# FORMATTERS
# =========================================================
def signal_emoji(signal_type: str) -> str:
    return {
        "BUY": "🟢",
        "SELL": "🔴",
        "NO TRADE": "⚪"
    }.get(signal_type, "⚪")


def format_signal_message(signal_id: str, row_or_analysis: Any, is_preview: bool = False) -> str:
    if isinstance(row_or_analysis, sqlite3.Row):
        signal_type = row_or_analysis["signal_type"]
        score = row_or_analysis["score"]
        grade = row_or_analysis["grade"]
        symbol = row_or_analysis["symbol"]
        timeframe = row_or_analysis["timeframe"]
        trend = row_or_analysis["trend"]
        momentum = row_or_analysis["momentum"]
        entry = row_or_analysis["entry_price"]
        sl = row_or_analysis["stop_loss"]
        tp1 = row_or_analysis["tp1"]
        tp2 = row_or_analysis["tp2"]
        tp3 = row_or_analysis["tp3"]
        summary = row_or_analysis["summary"]
        mode = row_or_analysis["mode"]
        market_price = row_or_analysis["market_price"]
    else:
        signal_type = row_or_analysis["signal_type"]
        score = row_or_analysis["score"]
        grade = row_or_analysis["grade"]
        symbol = row_or_analysis["symbol"]
        timeframe = row_or_analysis["timeframe"]
        trend = row_or_analysis["trend"]
        momentum = row_or_analysis["momentum"]
        entry = row_or_analysis["entry_price"]
        sl = row_or_analysis["stop_loss"]
        tp1 = row_or_analysis["tp1"]
        tp2 = row_or_analysis["tp2"]
        tp3 = row_or_analysis["tp3"]
        summary = row_or_analysis["summary"]
        mode = row_or_analysis["mode"]
        market_price = row_or_analysis["market_price"]

    verdict = (
        "Setup premium exploitable."
        if signal_type in ("BUY", "SELL") and score >= 80 else
        "Setup correct mais sélectif."
        if signal_type in ("BUY", "SELL") and score >= 65 else
        "Pas de trade propre pour le moment."
    )

    header = "🧠 <b>Analyse Premium Crypto</b>"
    if is_preview:
        header = "🚨 <b>Auto Scan Crypto</b>"

    return (
        f"{header}\n\n"
        f"🪙 <b>Crypto :</b> {html.escape(symbol)}\n"
        f"{signal_emoji(signal_type)} <b>Signal :</b> {html.escape(signal_type)}\n"
        f"🏷️ <b>Setup Grade :</b> {html.escape(grade)}\n"
        f"📊 <b>Score :</b> {score}/100\n"
        f"⏱️ <b>Timeframe :</b> {html.escape(timeframe)}\n"
        f"⚙️ <b>Mode :</b> {html.escape(MODE_CONFIG.get(mode, MODE_CONFIG['normal'])['label'])}\n\n"
        f"📈 <b>Tendance :</b> {html.escape(trend)}\n"
        f"⚡ <b>Momentum :</b> {html.escape(momentum)}\n"
        f"💵 <b>Prix actuel :</b> {market_price}\n\n"
        f"🎯 <b>Entry :</b> {entry}\n"
        f"🛑 <b>Stop Loss :</b> {sl}\n"
        f"🥇 <b>TP1 :</b> {tp1}\n"
        f"🥈 <b>TP2 :</b> {tp2}\n"
        f"🥉 <b>TP3 :</b> {tp3}\n\n"
        f"📝 <b>Résumé :</b>\n{html.escape(summary)}\n\n"
        f"✅ <b>Verdict :</b> {html.escape(verdict)}\n"
        f"🆔 <code>{signal_id}</code>"
    )


def format_quick_execution(row: sqlite3.Row) -> str:
    signal_type = row["signal_type"]
    entry = float(row["entry_price"])
    sl = float(row["stop_loss"])
    tp1 = float(row["tp1"])
    tp2 = float(row["tp2"])
    tp3 = float(row["tp3"])
    risk_pct = compute_trade_risk_percent(entry, sl)

    return (
        f"📱 <b>Exécution rapide</b>\n\n"
        f"🪙 <b>{html.escape(row['symbol'])}</b>\n"
        f"{signal_emoji(signal_type)} <b>{html.escape(signal_type)}</b>\n"
        f"🎯 Entry: <code>{entry}</code>\n"
        f"🛑 SL: <code>{sl}</code>\n"
        f"🥇 TP1: <code>{tp1}</code>\n"
        f"🥈 TP2: <code>{tp2}</code>\n"
        f"🥉 TP3: <code>{tp3}</code>\n"
        f"⚠️ Risque estimé: <b>{risk_pct:.2f}%</b>\n"
        f"💡 Taille conseillée: risque fixe de <b>{DEFAULT_RISK_PERCENT:.1f}%</b> max par trade\n\n"
        f"📌 Copie rapide dans ton app mobile : sens + entry + SL + TP"
    )


def compute_trade_risk_percent(entry: float, stop: float) -> float:
    if entry == 0:
        return 0.0
    return abs(entry - stop) / entry * 100


def format_history(rows: List[sqlite3.Row]) -> str:
    if not rows:
        return (
            "🕓 <b>Derniers signaux</b>\n\n"
            "Aucun signal enregistré pour l’instant."
        )

    lines = ["🕓 <b>Derniers signaux</b>\n"]
    for row in rows:
        emoji = signal_emoji(row["signal_type"])
        local_time = time.strftime("%d/%m %H:%M", time.localtime(row["created_at"]))
        lines.append(
            f"{emoji} <b>{row['symbol']}</b> • {row['signal_type']} • "
            f"Score {row['score']} • {row['grade']} • {local_time}"
        )
    return "\n".join(lines)


def format_watchlist(chat_id: int) -> str:
    watchlist = get_watchlist(chat_id)
    if not watchlist:
        body = "Aucune crypto sélectionnée."
    else:
        body = "\n".join([f"• {symbol}" for symbol in watchlist])

    return (
        "📈 <b>Ma Watchlist</b>\n\n"
        f"{body}\n\n"
        "Choisis les cryptos à suivre avec les boutons ci-dessous."
    )


def format_settings(chat_id: int) -> str:
    user = get_or_create_user(chat_id)
    mode = user["mode"]
    auto_scan_enabled = bool(user["auto_scan_enabled"])

    return (
        "⚙️ <b>Réglages Pro</b>\n\n"
        f"🎛️ <b>Mode actuel :</b> {MODE_CONFIG[mode]['label']}\n"
        f"🚨 <b>Auto Scan :</b> {'Activé' if auto_scan_enabled else 'Désactivé'}\n\n"
        "🛡️ <b>Prudent</b> : moins de signaux, plus sélectif\n"
        "⚖️ <b>Normal</b> : équilibre qualité / fréquence\n"
        "🔥 <b>Agressif</b> : plus d’opportunités, moins strict"
    )


def format_guide() -> str:
    return (
        "❓ <b>Guide Rapide</b>\n\n"
        "1. Utilise <code>/analyze BTCUSDT</code>\n"
        "2. Lis le signal BUY / SELL / NO TRADE\n"
        "3. Vérifie le score et le grade\n"
        "4. Utilise <b>📱 Exécution rapide</b> pour recopier vite\n"
        "5. Active l’Auto Scan si tu veux surveiller ta watchlist\n\n"
        "📌 Conseils :\n"
        "• Ne risque pas trop sur un seul trade\n"
        "• Un score élevé = setup plus propre\n"
        "• NO TRADE = discipline premium"
    )


# =========================================================
# AUTO SCAN
# =========================================================
def run_auto_scan(force: bool = False) -> Dict[str, Any]:
    if not SCAN_LOCK.acquire(blocking=False):
        return {"ok": False, "message": "Scan déjà en cours"}

    try:
        last_scan = int(get_meta("last_auto_scan_ts", "0") or "0")
        if not force and (now_ts() - last_scan) < AUTO_SCAN_COOLDOWN_SECONDS:
            return {"ok": True, "message": "Cooldown actif, scan ignoré"}

        conn = db_connect()
        users = conn.execute("SELECT * FROM users WHERE auto_scan_enabled = 1").fetchall()
        conn.close()

        alerts_sent = 0

        for user in users:
            chat_id = int(user["chat_id"])
            mode = user["mode"]
            cfg = MODE_CONFIG.get(mode, MODE_CONFIG["normal"])
            symbols = get_watchlist(chat_id)

            for symbol in symbols:
                try:
                    analysis = analyze_symbol(symbol, mode=mode, interval=DEFAULT_INTERVAL)
                    if not analysis.get("ok"):
                        continue

                    signal_type = analysis["signal_type"]
                    score = int(analysis["score"])

                    if signal_type not in ("BUY", "SELL"):
                        continue
                    if score < cfg["min_score_autoscan"]:
                        continue
                    if recently_sent_same_signal(chat_id, symbol, signal_type):
                        continue

                    signal_id = save_signal(chat_id, analysis, is_auto_scan=True)
                    send_message(
                        chat_id,
                        format_signal_message(signal_id, analysis, is_preview=True),
                        reply_markup=analysis_keyboard(signal_id)
                    )
                    alerts_sent += 1

                except Exception as e:
                    logger.exception("Auto scan failed for %s / %s: %s", chat_id, symbol, e)

        set_meta("last_auto_scan_ts", str(now_ts()))
        return {"ok": True, "message": f"Scan terminé, {alerts_sent} alerte(s) envoyée(s)"}
    finally:
        SCAN_LOCK.release()


# =========================================================
# COMMANDS / TEXT HANDLERS
# =========================================================
def handle_start(chat_id: int, user_info: Dict[str, Any]) -> None:
    get_or_create_user(
        chat_id=chat_id,
        username=user_info.get("username", ""),
        first_name=user_info.get("first_name", "")
    )
    send_message(chat_id, home_text(), reply_markup=main_menu_keyboard())


def handle_menu(chat_id: int) -> None:
    send_message(chat_id, home_text(), reply_markup=main_menu_keyboard())


def handle_analyze(chat_id: int, symbol: str) -> None:
    user = get_or_create_user(chat_id)
    mode = user["mode"]
    symbol = symbol.upper().replace("/", "").replace(" ", "")

    if symbol not in SUPPORTED_SYMBOLS:
        send_message(
            chat_id,
            "⚠️ Symbole non supporté.\n\n"
            f"✅ Symboles disponibles :\n" + "\n".join([f"• {s}" for s in SUPPORTED_SYMBOLS])
        )
        return

    send_message(chat_id, f"⏳ Analyse en cours sur <b>{html.escape(symbol)}</b>...")

    try:
        analysis = analyze_symbol(symbol, mode=mode, interval=DEFAULT_INTERVAL)
        if not analysis.get("ok"):
            send_message(chat_id, f"❌ Impossible d’analyser {html.escape(symbol)}")
            return

        signal_id = save_signal(chat_id, analysis, is_auto_scan=False)
        send_message(
            chat_id,
            format_signal_message(signal_id, analysis, is_preview=False),
            reply_markup=analysis_keyboard(signal_id)
        )
    except Exception as e:
        logger.exception("Analyze failed: %s", e)
        send_message(chat_id, "❌ Erreur pendant l’analyse. Réessaie dans un instant.")


def handle_text_message(chat_id: int, text: str) -> None:
    text_clean = (text or "").strip()
    if not text_clean:
        return

    upper = text_clean.upper().replace("/", "").replace(" ", "")
    if upper in SUPPORTED_SYMBOLS:
        handle_analyze(chat_id, upper)
        return

    if text_clean.startswith("/analyze"):
        parts = text_clean.split()
        if len(parts) < 2:
            send_message(chat_id, "Utilise : <code>/analyze BTCUSDT</code>")
            return
        handle_analyze(chat_id, parts[1])
        return

    if text_clean in ["/start", "/menu"]:
        handle_menu(chat_id)
        return

    if text_clean == "/scan":
        result = run_auto_scan(force=True)
        send_message(chat_id, f"🚨 {html.escape(result['message'])}")
        return

    send_message(
        chat_id,
        "Je n’ai pas compris.\n\n"
        "✅ Essaie par exemple :\n"
        "<code>/analyze BTCUSDT</code>\n"
        "ou clique sur le menu ci-dessous.",
        reply_markup=main_menu_keyboard()
    )


# =========================================================
# CALLBACK HANDLERS
# =========================================================
def handle_callback(callback_query: Dict[str, Any]) -> None:
    callback_id = callback_query["id"]
    data = callback_query.get("data", "")
    message = callback_query.get("message", {})
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]

    get_or_create_user(chat_id)

    try:
        if data == "menu:home":
            answer_callback(callback_id)
            edit_message(chat_id, message_id, home_text(), reply_markup=main_menu_keyboard())
            return

        if data == "menu:analyse":
            answer_callback(callback_id)
            edit_message(
                chat_id,
                message_id,
                "🧠 <b>Analyse Premium</b>\n\n"
                "Tape simplement une crypto supportée comme :\n"
                "<code>BTCUSDT</code>\n"
                "ou\n"
                "<code>/analyze ETHUSDT</code>\n\n"
                "✅ Paires supportées :\n" + "\n".join([f"• {s}" for s in SUPPORTED_SYMBOLS]),
                reply_markup=inline_keyboard([[("🏠 Menu", "menu:home")]])
            )
            return

        if data == "menu:autoscan":
            enabled = toggle_auto_scan(chat_id)
            answer_callback(callback_id, f"Auto Scan {'activé' if enabled else 'désactivé'}")
            edit_message(
                chat_id,
                message_id,
                format_settings(chat_id),
                reply_markup=settings_keyboard(chat_id)
            )
            return

        if data == "menu:watchlist":
            answer_callback(callback_id)
            edit_message(chat_id, message_id, format_watchlist(chat_id), reply_markup=watchlist_keyboard(chat_id))
            return

        if data == "menu:history":
            answer_callback(callback_id)
            edit_message(
                chat_id,
                message_id,
                format_history(get_recent_signals(chat_id)),
                reply_markup=inline_keyboard([[("🏠 Menu", "menu:home")]])
            )
            return

        if data == "menu:settings":
            answer_callback(callback_id)
            edit_message(chat_id, message_id, format_settings(chat_id), reply_markup=settings_keyboard(chat_id))
            return

        if data == "menu:guide":
            answer_callback(callback_id)
            edit_message(chat_id, message_id, format_guide(), reply_markup=inline_keyboard([[("🏠 Menu", "menu:home")]]))
            return

        if data.startswith("mode:set:"):
            mode = data.split(":")[-1]
            if mode not in MODE_CONFIG:
                answer_callback(callback_id, "Mode invalide", True)
                return
            set_user_mode(chat_id, mode)
            answer_callback(callback_id, f"Mode {MODE_CONFIG[mode]['label']} activé")
            edit_message(chat_id, message_id, format_settings(chat_id), reply_markup=settings_keyboard(chat_id))
            return

        if data.startswith("watch:toggle:"):
            symbol = data.split(":")[-1]
            if symbol not in SUPPORTED_SYMBOLS:
                answer_callback(callback_id, "Symbole invalide", True)
                return
            added = toggle_watch_symbol(chat_id, symbol)
            answer_callback(callback_id, f"{symbol} {'ajouté' if added else 'retiré'}")
            edit_message(chat_id, message_id, format_watchlist(chat_id), reply_markup=watchlist_keyboard(chat_id))
            return

        if data == "watch:all":
            set_all_watchlist(chat_id)
            answer_callback(callback_id, "Toute la watchlist a été sélectionnée")
            edit_message(chat_id, message_id, format_watchlist(chat_id), reply_markup=watchlist_keyboard(chat_id))
            return

        if data == "watch:default":
            set_default_watchlist(chat_id)
            answer_callback(callback_id, "Watchlist par défaut restaurée")
            edit_message(chat_id, message_id, format_watchlist(chat_id), reply_markup=watchlist_keyboard(chat_id))
            return

        if data == "watch:reset":
            reset_watchlist(chat_id)
            answer_callback(callback_id, "Watchlist réinitialisée")
            edit_message(chat_id, message_id, format_watchlist(chat_id), reply_markup=watchlist_keyboard(chat_id))
            return

        if data.startswith("signal:quick:"):
            signal_id = data.split(":")[-1]
            row = get_signal(signal_id)
            if not row:
                answer_callback(callback_id, "Signal introuvable", True)
                return
            answer_callback(callback_id)
            send_message(chat_id, format_quick_execution(row), reply_markup=inline_keyboard([[("🏠 Menu", "menu:home")]]))
            return

        if data.startswith("signal:history:"):
            signal_id = data.split(":")[-1]
            row = get_signal(signal_id)
            if not row:
                answer_callback(callback_id, "Signal introuvable", True)
                return
            answer_callback(callback_id, "Signal déjà enregistré dans l’historique")
            return

        answer_callback(callback_id, "Action non reconnue", True)

    except Exception as e:
        logger.exception("Callback error: %s", e)
        answer_callback(callback_id, "Erreur, réessaie.", True)


# =========================================================
# UPDATE PROCESSOR
# =========================================================
def verify_telegram_secret(req) -> bool:
    if not WEBHOOK_SECRET:
        return True
    incoming_secret = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return hmac.compare_digest(incoming_secret, WEBHOOK_SECRET)


def process_update(update: Dict[str, Any]) -> None:
    # Opportunistic autoscan
    try:
        run_auto_scan(force=False)
    except Exception:
        logger.exception("Opportunistic autoscan failed")

    if "message" in update:
        message = update["message"]
        chat_id = message["chat"]["id"]
        from_user = message.get("from", {})
        get_or_create_user(
            chat_id=chat_id,
            username=from_user.get("username", ""),
            first_name=from_user.get("first_name", "")
        )

        text = message.get("text", "")
        handle_text_message(chat_id, text)
        return

    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return


# =========================================================
# FLASK ROUTES
# =========================================================
@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "ok": True,
        "bot": "LVBXNT_Crypto_Bot",
        "status": "running"
    })


@app.route("/set_webhook", methods=["GET"])
def route_set_webhook():
    result = set_webhook()
    return jsonify(result)


@app.route("/delete_webhook", methods=["GET"])
def route_delete_webhook():
    result = delete_webhook()
    return jsonify(result)


@app.route("/run_scan", methods=["GET", "POST"])
def route_run_scan():
    if not SCAN_SECRET:
        return jsonify({"ok": False, "error": "SCAN_SECRET manquant"}), 500

    incoming = request.args.get("secret", "") or request.headers.get("X-Scan-Secret", "")
    if not hmac.compare_digest(incoming, SCAN_SECRET):
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    result = run_auto_scan(force=True)
    return jsonify(result)


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    if not verify_telegram_secret(request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    update = request.get_json(silent=True) or {}
    try:
        process_update(update)
    except Exception as e:
        logger.exception("process_update failed: %s", e)

    return jsonify({"ok": True})


# =========================================================
# STARTUP
# =========================================================
def startup() -> None:
    init_db()
    logger.info("Database initialized")

    if BOT_TOKEN and RENDER_EXTERNAL_URL:
        try:
            result = set_webhook()
            logger.info("Webhook setup result: %s", result)
        except Exception as e:
            logger.warning("Webhook auto-setup failed: %s", e)
    else:
        logger.warning("BOT_TOKEN or RENDER_EXTERNAL_URL missing; webhook not auto-set")


startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
