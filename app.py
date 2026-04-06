import os
import time
import json
import uuid
import hmac
import html
import math
import sqlite3
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, request, jsonify

# =========================================================
# LVBXNT_Crypto_Bot - V2 PRO
# Telegram + Flask + SQLite + Multi Source Market Data
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
SCAN_SECRET = os.getenv("SCAN_SECRET", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "crypto_bot.db")
PORT = int(os.getenv("PORT", "10000"))

COINGECKO_BASE = os.getenv("COINGECKO_BASE", "https://api.coingecko.com/api/v3").rstrip("/")
BINANCE_BASE = os.getenv("BINANCE_BASE", "https://api.binance.com").rstrip("/")
BYBIT_BASE = os.getenv("BYBIT_BASE", "https://api.bybit.com").rstrip("/")
DEXSCREENER_BASE = os.getenv("DEXSCREENER_BASE", "https://api.dexscreener.com/latest/dex").rstrip("/")
STOCKTWITS_BASE = os.getenv("STOCKTWITS_BASE", "https://api.stocktwits.com/api/2").rstrip("/")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

REQUEST_TIMEOUT = 20
DEFAULT_INTERVAL = "1h"
DEFAULT_RISK_PERCENT = 1.0
DEFAULT_LOOKBACK = 120
SIGNAL_COOLDOWN_SECONDS = 60 * 60 * 6
AUTO_SCAN_COOLDOWN_SECONDS = 60 * 15

SUPPORTED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT"
]

DEFAULT_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"
]

SYMBOL_META = {
    "BTCUSDT": {"coingecko_id": "bitcoin", "stocktwits": "BTC.X"},
    "ETHUSDT": {"coingecko_id": "ethereum", "stocktwits": "ETH.X"},
    "SOLUSDT": {"coingecko_id": "solana", "stocktwits": "SOL.X"},
    "XRPUSDT": {"coingecko_id": "ripple", "stocktwits": "XRP.X"},
    "BNBUSDT": {"coingecko_id": "binancecoin", "stocktwits": "BNB.X"},
    "ADAUSDT": {"coingecko_id": "cardano", "stocktwits": "ADA.X"},
    "DOGEUSDT": {"coingecko_id": "dogecoin", "stocktwits": "DOGE.X"},
    "AVAXUSDT": {"coingecko_id": "avalanche-2", "stocktwits": "AVAX.X"},
    "LINKUSDT": {"coingecko_id": "chainlink", "stocktwits": "LINK.X"},
    "MATICUSDT": {"coingecko_id": "matic-network", "stocktwits": "MATIC.X"},
}

MODE_CONFIG = {
    "prudent": {
        "label": "Prudent",
        "min_score_signal": 78,
        "min_score_autoscan": 82,
        "rsi_buy_min": 55,
        "rsi_sell_max": 45,
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
logger = logging.getLogger("LVBXNT_Crypto_Bot_V2_PRO")

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
            source_used TEXT,
            social_bias TEXT,
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
# TELEGRAM
# =========================================================
def telegram_request(method: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN manquant")

    resp = requests.post(
        f"{TELEGRAM_API_BASE}/{method}",
        json=payload or {},
        timeout=REQUEST_TIMEOUT
    )

    try:
        data = resp.json()
    except Exception:
        logger.error("Telegram non-JSON response on %s: %s", method, resp.text[:400])
        resp.raise_for_status()
        raise

    if not data.get("ok", False):
        logger.warning("Telegram API error on %s: %s", method, data)

    return data


def send_message(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram_request("sendMessage", payload)


def edit_message(chat_id: int, message_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram_request("editMessageText", payload)


def answer_callback(callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
    telegram_request("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text[:180],
        "show_alert": show_alert
    })


def set_webhook() -> Dict[str, Any]:
    if not RENDER_EXTERNAL_URL:
        return {"ok": False, "description": "RENDER_EXTERNAL_URL manquant"}

    payload: Dict[str, Any] = {"url": f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}"}
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET

    return telegram_request("setWebhook", payload)


# =========================================================
# UI
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


def watchlist_keyboard(chat_id: int) -> Dict[str, Any]:
    watchlist = get_watchlist(chat_id)
    rows: List[List[Tuple[str, str]]] = []

    for symbol in SUPPORTED_SYMBOLS:
        mark = "✅" if symbol in watchlist else "➕"
        rows.append([(f"{mark} {symbol}", f"watch:toggle:{symbol}")])

    rows.append([("✅ Tout sélectionner", "watch:all"), ("♻️ Défaut", "watch:default")])
    rows.append([("🗑️ Réinitialiser", "watch:reset"), ("🏠 Menu", "menu:home")])

    return inline_keyboard(rows)


def settings_keyboard(chat_id: int) -> Dict[str, Any]:
    user = get_or_create_user(chat_id)
    current_mode = user["mode"]

    rows = []
    for mode_key, cfg in MODE_CONFIG.items():
        mark = "✅" if current_mode == mode_key else "⚪"
        rows.append([(f"{mark} {cfg['label']}", f"mode:set:{mode_key}")])

    rows.append([("🏠 Menu", "menu:home")])
    return inline_keyboard(rows)


def analysis_keyboard(signal_id: str) -> Dict[str, Any]:
    return inline_keyboard([
        [("📱 Exécution rapide", f"signal:quick:{signal_id}")],
        [("🏠 Menu", "menu:home")]
    ])


def home_text() -> str:
    return (
        "💎 <b>LVBXNT_Crypto_Bot — V2 PRO</b>\n"
        "Bot crypto multi-source, premium et stable.\n\n"
        "🎯 <b>Fonctions :</b>\n"
        "• Signaux BUY / SELL / NO TRADE\n"
        "• EMA20 / EMA50 / RSI / ATR simplifié\n"
        "• Score qualité intelligent\n"
        "• Watchlist personnalisée\n"
        "• Auto Scan\n"
        "• Exécution rapide iPhone\n"
        "• Multi-source : CoinGecko / Binance / Bybit / DexScreener / Stocktwits\n\n"
        "📌 <b>Utilisation :</b>\n"
        "• Tape <code>BTCUSDT</code>\n"
        "• Ou <code>/analyze ETHUSDT</code>"
    )


def format_watchlist(chat_id: int) -> str:
    wl = get_watchlist(chat_id)
    body = "Aucune crypto sélectionnée." if not wl else "\n".join([f"• {x}" for x in wl])
    return (
        "📈 <b>Ma Watchlist</b>\n\n"
        f"{body}\n\n"
        "Choisis les cryptos à suivre avec les boutons."
    )


def format_settings(chat_id: int) -> str:
    user = get_or_create_user(chat_id)
    mode = user["mode"]
    auto_scan = "Activé" if bool(user["auto_scan_enabled"]) else "Désactivé"

    return (
        "⚙️ <b>Réglages Pro</b>\n\n"
        f"🎛️ <b>Mode :</b> {MODE_CONFIG[mode]['label']}\n"
        f"🚨 <b>Auto Scan :</b> {auto_scan}\n\n"
        "🛡️ Prudent = très sélectif\n"
        "⚖️ Normal = équilibré\n"
        "🔥 Agressif = plus d’opportunités"
    )


def format_guide() -> str:
    return (
        "❓ <b>Guide Rapide</b>\n\n"
        "1. Tape une crypto comme <code>BTCUSDT</code>\n"
        "2. Lis le signal et le score\n"
        "3. Utilise <b>📱 Exécution rapide</b>\n"
        "4. Active l’Auto Scan si tu veux surveiller ta watchlist\n\n"
        "📌 Plus le score est haut, plus le setup est propre."
    )


# =========================================================
# USERS / WATCHLIST
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
            UPDATE users SET username = ?, first_name = ?, updated_at = ? WHERE chat_id = ?
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


def set_all_watchlist(chat_id: int) -> None:
    conn = db_connect()
    conn.execute("DELETE FROM watchlist WHERE chat_id = ?", (chat_id,))
    for symbol in SUPPORTED_SYMBOLS:
        conn.execute("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES (?, ?)", (chat_id, symbol))
    conn.commit()
    conn.close()


def set_default_watchlist(chat_id: int) -> None:
    conn = db_connect()
    conn.execute("DELETE FROM watchlist WHERE chat_id = ?", (chat_id,))
    for symbol in DEFAULT_WATCHLIST:
        conn.execute("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES (?, ?)", (chat_id, symbol))
    conn.commit()
    conn.close()


def reset_watchlist(chat_id: int) -> None:
    conn = db_connect()
    conn.execute("DELETE FROM watchlist WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


# =========================================================
# MARKET HELPERS
# =========================================================
def safe_get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    resp = requests.get(
        url,
        params=params or {},
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "LVBXNT_Crypto_Bot_V2_PRO/1.0"}
    )
    resp.raise_for_status()
    return resp.json()


def interval_to_bybit(interval: str) -> str:
    mapping = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
        "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
        "1d": "D"
    }
    return mapping.get(interval, "60")


def candles_from_closes(closes: List[float]) -> List[Dict[str, float]]:
    candles: List[Dict[str, float]] = []
    if not closes:
        return candles

    prev = closes[0]
    for idx, close in enumerate(closes):
        move = abs(close - prev)
        high = max(close, prev) + move * 0.25
        low = min(close, prev) - move * 0.25
        candles.append({
            "open_time": idx,
            "open": prev,
            "high": high,
            "low": low,
            "close": close,
            "close_time": idx
        })
        prev = close
    return candles


# =========================================================
# SOURCES
# =========================================================
def fetch_from_coingecko(symbol: str) -> Dict[str, Any]:
    coin_id = SYMBOL_META[symbol]["coingecko_id"]

    # tries OHLC first
    try:
        data = safe_get_json(f"{COINGECKO_BASE}/coins/{coin_id}/ohlc", {
            "vs_currency": "usd",
            "days": 7
        })
        if isinstance(data, list) and len(data) >= 60:
            candles = [{
                "open_time": int(x[0]),
                "open": float(x[1]),
                "high": float(x[2]),
                "low": float(x[3]),
                "close": float(x[4]),
                "close_time": int(x[0]),
            } for x in data[-DEFAULT_LOOKBACK:]]
            return {"source": "CoinGecko", "candles": candles}
    except Exception as e:
        logger.warning("CoinGecko OHLC failed for %s: %s", symbol, e)

    # fallback market_chart prices
    data = safe_get_json(f"{COINGECKO_BASE}/coins/{coin_id}/market_chart", {
        "vs_currency": "usd",
        "days": 7,
        "interval": "hourly"
    })
    prices = data.get("prices", [])
    if not isinstance(prices, list) or len(prices) < 60:
        raise RuntimeError(f"CoinGecko market_chart insuffisant pour {symbol}")
    closes = [float(p[1]) for p in prices[-DEFAULT_LOOKBACK:]]
    return {"source": "CoinGecko", "candles": candles_from_closes(closes)}


def fetch_from_binance(symbol: str) -> Dict[str, Any]:
    data = safe_get_json(f"{BINANCE_BASE}/api/v3/klines", {
        "symbol": symbol,
        "interval": "1h",
        "limit": DEFAULT_LOOKBACK
    })
    if not isinstance(data, list) or len(data) < 60:
        raise RuntimeError(f"Binance insuffisant pour {symbol}")
    candles = [{
        "open_time": int(x[0]),
        "open": float(x[1]),
        "high": float(x[2]),
        "low": float(x[3]),
        "close": float(x[4]),
        "close_time": int(x[6]),
    } for x in data]
    return {"source": "Binance", "candles": candles}


def fetch_from_bybit(symbol: str) -> Dict[str, Any]:
    data = safe_get_json(f"{BYBIT_BASE}/v5/market/kline", {
        "category": "spot",
        "symbol": symbol,
        "interval": interval_to_bybit("1h"),
        "limit": DEFAULT_LOOKBACK
    })
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit erreur pour {symbol}")
    raw = data.get("result", {}).get("list", [])
    if not isinstance(raw, list) or len(raw) < 60:
        raise RuntimeError(f"Bybit insuffisant pour {symbol}")
    candles = [{
        "open_time": int(x[0]),
        "open": float(x[1]),
        "high": float(x[2]),
        "low": float(x[3]),
        "close": float(x[4]),
        "close_time": int(x[0]),
    } for x in raw]
    candles.reverse()
    return {"source": "Bybit", "candles": candles}


def fetch_from_dexscreener(symbol: str) -> Dict[str, Any]:
    query = symbol.replace("USDT", "")
    data = safe_get_json(f"{DEXSCREENER_BASE}/search", {"q": query})
    pairs = data.get("pairs", [])
    if not isinstance(pairs, list) or not pairs:
        raise RuntimeError(f"DexScreener vide pour {symbol}")

    best = None
    target_base = query
    for pair in pairs:
        base_token = pair.get("baseToken", {}).get("symbol", "").upper()
        quote_token = pair.get("quoteToken", {}).get("symbol", "").upper()
        if base_token == target_base and quote_token in ("USDT", "USDC", "USD"):
            best = pair
            break
    if best is None:
        best = pairs[0]

    price = float(best.get("priceUsd") or 0)
    if price <= 0:
        raise RuntimeError(f"DexScreener sans prix pour {symbol}")

    # synthetic history from changes
    change_h24 = float(best.get("priceChange", {}).get("h24") or 0.0)
    closes: List[float] = []
    base_factor = 1 - (change_h24 / 100.0)
    start_price = price * max(base_factor, 0.5)
    steps = DEFAULT_LOOKBACK
    for i in range(steps):
        ratio = i / max(steps - 1, 1)
        wave = math.sin(i / 6.0) * price * 0.003
        p = (start_price + (price - start_price) * ratio) + wave
        closes.append(max(p, price * 0.2))

    return {"source": "DexScreener", "candles": candles_from_closes(closes)}


def fetch_market_data(symbol: str) -> Dict[str, Any]:
    source_funcs = [
        fetch_from_coingecko,
        fetch_from_binance,
        fetch_from_bybit,
        fetch_from_dexscreener,
    ]

    errors = []
    for fn in source_funcs:
        try:
            result = fn(symbol)
            logger.info("Market source OK for %s via %s", symbol, result["source"])
            return result
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
            logger.warning("Source failed for %s via %s: %s", symbol, fn.__name__, e)

    raise RuntimeError(" | ".join(errors))


def fetch_stocktwits_bias(symbol: str) -> str:
    try:
        st_symbol = SYMBOL_META[symbol]["stocktwits"]
        data = safe_get_json(f"{STOCKTWITS_BASE}/streams/symbol/{st_symbol}.json")
        messages = data.get("messages", [])
        if not isinstance(messages, list) or not messages:
            return "Neutre"

        bull = 0
        bear = 0
        for msg in messages[:20]:
            entity = msg.get("entities", {})
            sentiment = entity.get("sentiment", {})
            basic = (sentiment.get("basic") or "").lower()
            if basic == "bullish":
                bull += 1
            elif basic == "bearish":
                bear += 1

        if bull >= bear + 3:
            return "Bullish"
        if bear >= bull + 3:
            return "Bearish"
        return "Neutre"
    except Exception as e:
        logger.warning("Stocktwits bias failed for %s: %s", symbol, e)
        return "Neutre"


# =========================================================
# INDICATORS
# =========================================================
def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def rsi(values: List[float], period: int = 14) -> List[float]:
    if len(values) < period + 2:
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

    if len(trs) <= period:
        avg = sum(trs[1:]) / max(1, len(trs) - 1)
        return [avg for _ in candles]

    result = []
    first = sum(trs[1:period + 1]) / period
    for _ in range(period):
        result.append(first)

    prev_atr = first
    for i in range(period, len(trs)):
        prev_atr = ((prev_atr * (period - 1)) + trs[i]) / period
        result.append(prev_atr)

    while len(result) < len(candles):
        result.insert(0, first)

    return result[:len(candles)]


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


# =========================================================
# ANALYSIS ENGINE
# =========================================================
def analyze_symbol(symbol: str, mode: str = "normal") -> Dict[str, Any]:
    if symbol not in SUPPORTED_SYMBOLS:
        raise RuntimeError(f"Symbole non supporté: {symbol}")

    market = fetch_market_data(symbol)
    source_used = market["source"]
    candles = market["candles"]

    if len(candles) < 60:
        raise RuntimeError(f"Pas assez de données pour {symbol}")

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    ema20_values = ema(closes, 20)
    ema50_values = ema(closes, 50)
    rsi14_values = rsi(closes, 14)
    atr14_values = atr(candles, 14)

    price = closes[-1]
    prev_price = closes[-2]
    current_ema20 = ema20_values[-1]
    prev_ema20 = ema20_values[-2]
    current_ema50 = ema50_values[-1]
    current_rsi = rsi14_values[-1]
    current_atr = atr14_values[-1] if atr14_values[-1] > 0 else max(price * 0.008, 0.0001)

    ema_spread_pct = abs(current_ema20 - current_ema50) / max(price, 1e-10) * 100
    price_vs_ema20_pct = (price - current_ema20) / max(price, 1e-10) * 100
    ema20_slope = current_ema20 - prev_ema20
    recent_high = max(highs[-20:])
    recent_low = min(lows[-20:])

    momentum_up = price > prev_price and closes[-1] > closes[-3] > closes[-5]
    momentum_down = price < prev_price and closes[-1] < closes[-3] < closes[-5]

    trend_bull = price > current_ema20 > current_ema50 and ema20_slope > 0
    trend_bear = price < current_ema20 < current_ema50 and ema20_slope < 0

    social_bias = fetch_stocktwits_bias(symbol)
    cfg = MODE_CONFIG.get(mode, MODE_CONFIG["normal"])

    reasons: List[str] = []
    score = 0

    # trend 30
    if trend_bull or trend_bear:
        score += 22
        reasons.append("Tendance claire")
        if ema_spread_pct >= 0.25:
            score += 8
            reasons.append("EMA20/EMA50 bien espacées")
    else:
        reasons.append("Tendance peu claire")

    # momentum 20
    if momentum_up or momentum_down:
        score += 14
        reasons.append("Momentum présent")
        if abs(price - prev_price) / max(price, 1e-10) * 100 >= 0.20:
            score += 6
            reasons.append("Impulsion exploitable")
    else:
        reasons.append("Momentum faible")

    # RSI 15
    if 52 <= current_rsi <= 68 or 32 <= current_rsi <= 48:
        score += 12
        reasons.append("RSI propre")
    elif 49 <= current_rsi <= 72 or 28 <= current_rsi <= 51:
        score += 7
        reasons.append("RSI acceptable")
    else:
        reasons.append("RSI extrême ou peu utile")

    # structure 20
    if ema_spread_pct >= 0.18:
        score += 8
        reasons.append("Structure correcte")
    if abs(price_vs_ema20_pct) <= 1.2:
        score += 6
        reasons.append("Prix encore exploitable")
    if (recent_high - recent_low) / max(price, 1e-10) * 100 >= 1.0:
        score += 6
        reasons.append("Amplitude suffisante")

    candidate_signal = "NO TRADE"
    entry = price
    stop = price
    tp1 = price
    tp2 = price
    tp3 = price

    if trend_bull and momentum_up and current_rsi >= cfg["rsi_buy_min"]:
        candidate_signal = "BUY"
        entry = price
        stop = min(current_ema20 - current_atr * 1.2, recent_low - current_atr * 0.3)
        risk = max(entry - stop, current_atr * 0.8)
        stop = entry - risk
        tp1 = entry + risk * 1.2
        tp2 = entry + risk * 2.0
        tp3 = entry + risk * 3.0

    elif trend_bear and momentum_down and current_rsi <= cfg["rsi_sell_max"]:
        candidate_signal = "SELL"
        entry = price
        stop = max(current_ema20 + current_atr * 1.2, recent_high + current_atr * 0.3)
        risk = max(stop - entry, current_atr * 0.8)
        stop = entry + risk
        tp1 = entry - risk * 1.2
        tp2 = entry - risk * 2.0
        tp3 = entry - risk * 3.0

    rr_candidate = 0.0
    if candidate_signal == "BUY":
        rr_candidate = abs(tp2 - entry) / max(abs(entry - stop), 1e-10)
    elif candidate_signal == "SELL":
        rr_candidate = abs(entry - tp2) / max(abs(stop - entry), 1e-10)

    # RR 15
    if rr_candidate >= 1.8:
        score += 15
        reasons.append("Risk/Reward fort")
    elif rr_candidate >= 1.3:
        score += 10
        reasons.append("Risk/Reward correct")
    elif rr_candidate > 0:
        score += 5
        reasons.append("Risk/Reward faible")

    # social bias ±5
    if social_bias == "Bullish" and candidate_signal == "BUY":
        score += 5
        reasons.append("Biais social bullish")
    elif social_bias == "Bearish" and candidate_signal == "SELL":
        score += 5
        reasons.append("Biais social bearish")
    elif social_bias == "Bullish" and candidate_signal == "SELL":
        score -= 3
    elif social_bias == "Bearish" and candidate_signal == "BUY":
        score -= 3

    score = min(100, max(0, int(round(score))))
    grade = grade_from_score(score)

    if candidate_signal in ("BUY", "SELL") and score < cfg["min_score_signal"]:
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

    if candidate_signal == "NO TRADE":
        entry = price
        stop = price
        tp1 = price
        tp2 = price
        tp3 = price

    trend_label = "Haussière" if trend_bull else "Baissière" if trend_bear else "Neutre"
    momentum_label = "Haussier" if momentum_up else "Baissier" if momentum_down else "Faible"

    if candidate_signal == "BUY":
        summary = f"Tendance haussière propre, momentum actif, score {score}/100."
    elif candidate_signal == "SELL":
        summary = f"Tendance baissière propre, momentum actif, score {score}/100."
    else:
        summary = f"Pas de setup assez propre pour l’instant. Le filtre premium préfère attendre."

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": DEFAULT_INTERVAL,
        "signal_type": candidate_signal,
        "score": score,
        "grade": grade,
        "mode": mode,
        "source_used": source_used,
        "social_bias": social_bias,
        "market_price": safe_round(price),
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
            "reasons": reasons[:10],
        },
    }


# =========================================================
# SIGNAL STORAGE
# =========================================================
def save_signal(chat_id: int, analysis: Dict[str, Any], is_auto_scan: bool = False) -> str:
    signal_id = str(uuid.uuid4())
    conn = db_connect()
    conn.execute("""
        INSERT INTO signals(
            signal_id, chat_id, symbol, timeframe, signal_type, score, grade, mode,
            source_used, social_bias, entry_price, stop_loss, tp1, tp2, tp3,
            market_price, trend, momentum, summary, rationale_json, is_auto_scan, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal_id,
        chat_id,
        analysis["symbol"],
        analysis["timeframe"],
        analysis["signal_type"],
        analysis["score"],
        analysis["grade"],
        analysis["mode"],
        analysis.get("source_used", "Unknown"),
        analysis.get("social_bias", "Neutre"),
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
        SELECT * FROM signals WHERE chat_id = ? ORDER BY created_at DESC LIMIT ?
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
# FORMAT
# =========================================================
def signal_emoji(signal_type: str) -> str:
    return {"BUY": "🟢", "SELL": "🔴", "NO TRADE": "⚪"}.get(signal_type, "⚪")


def compute_trade_risk_percent(entry: float, stop: float) -> float:
    if entry == 0:
        return 0.0
    return abs(entry - stop) / entry * 100


def format_signal_message(signal_id: str, row_or_analysis: Any, is_preview: bool = False) -> str:
    data = dict(row_or_analysis) if isinstance(row_or_analysis, sqlite3.Row) else row_or_analysis

    header = "🚨 <b>Auto Scan Crypto</b>" if is_preview else "🧠 <b>Analyse Premium Crypto</b>"
    signal_type = data["signal_type"]
    score = data["score"]

    verdict = (
        "Setup premium exploitable."
        if signal_type in ("BUY", "SELL") and score >= 80 else
        "Setup correct mais sélectif."
        if signal_type in ("BUY", "SELL") and score >= 65 else
        "Pas de trade propre pour le moment."
    )

    return (
        f"{header}\n\n"
        f"🪙 <b>Crypto :</b> {html.escape(str(data['symbol']))}\n"
        f"{signal_emoji(signal_type)} <b>Signal :</b> {html.escape(str(signal_type))}\n"
        f"🏷️ <b>Grade :</b> {html.escape(str(data['grade']))}\n"
        f"📊 <b>Score :</b> {score}/100\n"
        f"⏱️ <b>Timeframe :</b> {html.escape(str(data['timeframe']))}\n"
        f"⚙️ <b>Mode :</b> {html.escape(MODE_CONFIG.get(data['mode'], MODE_CONFIG['normal'])['label'])}\n"
        f"🌐 <b>Source :</b> {html.escape(str(data.get('source_used', 'Unknown')))}\n"
        f"🧠 <b>Sentiment :</b> {html.escape(str(data.get('social_bias', 'Neutre')))}\n\n"
        f"📈 <b>Tendance :</b> {html.escape(str(data['trend']))}\n"
        f"⚡ <b>Momentum :</b> {html.escape(str(data['momentum']))}\n"
        f"💵 <b>Prix actuel :</b> {data['market_price']}\n\n"
        f"🎯 <b>Entry :</b> {data['entry_price']}\n"
        f"🛑 <b>Stop Loss :</b> {data['stop_loss']}\n"
        f"🥇 <b>TP1 :</b> {data['tp1']}\n"
        f"🥈 <b>TP2 :</b> {data['tp2']}\n"
        f"🥉 <b>TP3 :</b> {data['tp3']}\n\n"
        f"📝 <b>Résumé :</b>\n{html.escape(str(data['summary']))}\n\n"
        f"✅ <b>Verdict :</b> {html.escape(verdict)}\n"
        f"🆔 <code>{signal_id}</code>"
    )


def format_quick_execution(row: sqlite3.Row) -> str:
    entry = float(row["entry_price"])
    sl = float(row["stop_loss"])
    risk_pct = compute_trade_risk_percent(entry, sl)

    return (
        f"📱 <b>Exécution rapide</b>\n\n"
        f"🪙 <b>{html.escape(row['symbol'])}</b>\n"
        f"{signal_emoji(row['signal_type'])} <b>{html.escape(row['signal_type'])}</b>\n"
        f"🌐 Source: <b>{html.escape(row['source_used'] or 'Unknown')}</b>\n"
        f"🎯 Entry: <code>{entry}</code>\n"
        f"🛑 SL: <code>{row['stop_loss']}</code>\n"
        f"🥇 TP1: <code>{row['tp1']}</code>\n"
        f"🥈 TP2: <code>{row['tp2']}</code>\n"
        f"🥉 TP3: <code>{row['tp3']}</code>\n"
        f"⚠️ Risque estimé: <b>{risk_pct:.2f}%</b>\n"
        f"💡 Taille conseillée: risque max <b>{DEFAULT_RISK_PERCENT:.1f}%</b> par trade"
    )


def format_history(rows: List[sqlite3.Row]) -> str:
    if not rows:
        return "🕓 <b>Derniers signaux</b>\n\nAucun signal enregistré."

    lines = ["🕓 <b>Derniers signaux</b>\n"]
    for row in rows:
        when = time.strftime("%d/%m %H:%M", time.localtime(row["created_at"]))
        lines.append(
            f"{signal_emoji(row['signal_type'])} <b>{row['symbol']}</b> • {row['signal_type']} • "
            f"Score {row['score']} • {row['grade']} • {row['source_used']} • {when}"
        )
    return "\n".join(lines)


# =========================================================
# AUTO SCAN
# =========================================================
def run_auto_scan(force: bool = False) -> Dict[str, Any]:
    if not SCAN_LOCK.acquire(blocking=False):
        return {"ok": False, "message": "Scan déjà en cours"}

    try:
        last_scan = int(get_meta("last_auto_scan_ts", "0") or "0")
        if not force and (now_ts() - last_scan) < AUTO_SCAN_COOLDOWN_SECONDS:
            return {"ok": True, "message": "Cooldown actif"}

        conn = db_connect()
        users = conn.execute("SELECT * FROM users WHERE auto_scan_enabled = 1").fetchall()
        conn.close()

        alerts_sent = 0

        for user in users:
            chat_id = int(user["chat_id"])
            mode = user["mode"]
            cfg = MODE_CONFIG.get(mode, MODE_CONFIG["normal"])

            for symbol in get_watchlist(chat_id):
                try:
                    analysis = analyze_symbol(symbol, mode=mode)
                    if analysis["signal_type"] not in ("BUY", "SELL"):
                        continue
                    if analysis["score"] < cfg["min_score_autoscan"]:
                        continue
                    if recently_sent_same_signal(chat_id, symbol, analysis["signal_type"]):
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
        return {"ok": True, "message": f"Scan terminé, {alerts_sent} alerte(s)"}

    finally:
        SCAN_LOCK.release()


# =========================================================
# COMMANDS / TEXT
# =========================================================
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
            "✅ Paires disponibles :\n" + "\n".join([f"• {s}" for s in SUPPORTED_SYMBOLS])
        )
        return

    send_message(chat_id, f"⏳ Analyse PRO en cours sur <b>{html.escape(symbol)}</b>...")

    try:
        analysis = analyze_symbol(symbol, mode=mode)
        signal_id = save_signal(chat_id, analysis, is_auto_scan=False)
        send_message(
            chat_id,
            format_signal_message(signal_id, analysis, is_preview=False),
            reply_markup=analysis_keyboard(signal_id)
        )
    except Exception as e:
        logger.exception("Analyze failed for %s: %s", symbol, e)
        send_message(
            chat_id,
            "❌ Erreur pendant l’analyse.\n\n"
            "Vérifie que Render a fini le redéploiement puis réessaie."
        )


def handle_text_message(chat_id: int, text: str) -> None:
    t = (text or "").strip()
    if not t:
        return

    upper = t.upper().replace("/", "").replace(" ", "")
    if upper in SUPPORTED_SYMBOLS:
        handle_analyze(chat_id, upper)
        return

    if t.startswith("/analyze"):
        parts = t.split()
        if len(parts) < 2:
            send_message(chat_id, "Utilise : <code>/analyze BTCUSDT</code>")
            return
        handle_analyze(chat_id, parts[1])
        return

    if t in ["/start", "/menu"]:
        handle_menu(chat_id)
        return

    if t == "/scan":
        result = run_auto_scan(force=True)
        send_message(chat_id, f"🚨 {html.escape(result['message'])}")
        return

    send_message(
        chat_id,
        "Je n’ai pas compris.\n\n"
        "Essaie : <code>BTCUSDT</code>\n"
        "ou <code>/analyze ETHUSDT</code>",
        reply_markup=main_menu_keyboard()
    )


# =========================================================
# CALLBACKS
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
            edit_message(chat_id, message_id, format_settings(chat_id), reply_markup=settings_keyboard(chat_id))
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
            edit_message(
                chat_id,
                message_id,
                format_guide(),
                reply_markup=inline_keyboard([[("🏠 Menu", "menu:home")]])
            )
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
            send_message(
                chat_id,
                format_quick_execution(row),
                reply_markup=inline_keyboard([[("🏠 Menu", "menu:home")]])
            )
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
    incoming = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return hmac.compare_digest(incoming, WEBHOOK_SECRET)


def process_update(update: Dict[str, Any]) -> None:
    try:
        run_auto_scan(force=False)
    except Exception as e:
        logger.warning("Opportunistic autoscan skipped: %s", e)

    if "message" in update:
        message = update["message"]
        chat_id = message["chat"]["id"]
        from_user = message.get("from", {})

        get_or_create_user(
            chat_id=chat_id,
            username=from_user.get("username", ""),
            first_name=from_user.get("first_name", "")
        )
        handle_text_message(chat_id, message.get("text", ""))
        return

    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return


# =========================================================
# ROUTES
# =========================================================
@app.route("/", methods=["GET"])
def index():
    return jsonify({"ok": True, "bot": "LVBXNT_Crypto_Bot", "status": "running"})


@app.route("/set_webhook", methods=["GET"])
def route_set_webhook():
    return jsonify(set_webhook())


@app.route("/run_scan", methods=["GET", "POST"])
def route_run_scan():
    if not SCAN_SECRET:
        return jsonify({"ok": False, "error": "SCAN_SECRET manquant"}), 500

    incoming = request.args.get("secret", "") or request.headers.get("X-Scan-Secret", "")
    if not hmac.compare_digest(incoming, SCAN_SECRET):
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    return jsonify(run_auto_scan(force=True))


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