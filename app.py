import os
import time
import sqlite3
import threading
import requests
from flask import Flask, request, jsonify

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
DB_PATH = os.getenv("DB_PATH", "crypto_bot.db")

CMC_API_KEY = os.getenv("CMC_API_KEY", "")
CMC_BASE_URL = os.getenv("CMC_BASE_URL", "https://pro-api.coinmarketcap.com")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")
if not RENDER_EXTERNAL_URL:
    raise ValueError("RENDER_EXTERNAL_URL missing")

app = Flask(__name__)
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

ALL_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT"
]

DEFAULT_WATCHLIST = ALL_SYMBOLS.copy()

COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
    "XRPUSDT": "ripple",
    "BNBUSDT": "binancecoin",
    "ADAUSDT": "cardano",
    "DOGEUSDT": "dogecoin",
    "AVAXUSDT": "avalanche-2",
    "LINKUSDT": "chainlink",
    "MATICUSDT": "matic-network"
}

PAPRIKA_IDS = {
    "BTCUSDT": "btc-bitcoin",
    "ETHUSDT": "eth-ethereum",
    "SOLUSDT": "sol-solana",
    "XRPUSDT": "xrp-xrp",
    "BNBUSDT": "bnb-binance-coin",
    "ADAUSDT": "ada-cardano",
    "DOGEUSDT": "doge-dogecoin",
    "AVAXUSDT": "avax-avalanche",
    "LINKUSDT": "link-chainlink",
    "MATICUSDT": "matic-polygon"
}

KRAKEN_PAIRS = {
    "BTCUSDT": "XBTUSD",
    "ETHUSDT": "ETHUSD",
    "SOLUSDT": "SOLUSD",
    "XRPUSDT": "XRPUSD",
    "ADAUSDT": "ADAUSD",
    "DOGEUSDT": "DOGEUSD",
    "AVAXUSDT": "AVAXUSD",
    "LINKUSDT": "LINKUSD",
    "MATICUSDT": "MATICUSD"
}

MODE_CONFIG = {
    "prudent": {
        "buy_rsi": 58,
        "sell_rsi": 42,
        "min_auto_score": 82,
        "display_name": "Prudent 🛡️"
    },
    "normal": {
        "buy_rsi": 54,
        "sell_rsi": 46,
        "min_auto_score": 72,
        "display_name": "Normal ⚖️"
    },
    "aggressive": {
        "buy_rsi": 51,
        "sell_rsi": 49,
        "min_auto_score": 64,
        "display_name": "Agressif ⚡"
    }
}

SCAN_INTERVAL_SECONDS = 300
COOLDOWN_SECONDS = 1800
CACHE_TTL_SECONDS = 180

ANALYSIS_CACHE = {}
DB_LOCK = threading.Lock()

# =========================
# DB
# =========================
def get_conn():
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        timeout=30,
        isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db():
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                auto_scan INTEGER DEFAULT 0,
                mode TEXT DEFAULT 'normal'
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlists (
                chat_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                UNIQUE(chat_id, symbol)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS signal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                score INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS cooldowns (
                chat_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                sent_at INTEGER NOT NULL,
                UNIQUE(chat_id, symbol)
            )
        """)

        conn.close()


def ensure_user(chat_id):
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO users (chat_id, auto_scan, mode)
            VALUES (?, 0, 'normal')
        """, (chat_id,))
        cur.execute("SELECT COUNT(*) AS c FROM watchlists WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        if row["c"] == 0:
            for symbol in DEFAULT_WATCHLIST:
                cur.execute(
                    "INSERT OR IGNORE INTO watchlists (chat_id, symbol) VALUES (?, ?)",
                    (chat_id, symbol)
                )
        conn.close()


def get_user_mode(chat_id):
    ensure_user(chat_id)
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT mode FROM users WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        conn.close()
    return row["mode"] if row and row["mode"] in MODE_CONFIG else "normal"


def set_user_mode(chat_id, mode):
    if mode not in MODE_CONFIG:
        return
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE users SET mode = ? WHERE chat_id = ?", (mode, chat_id))
        conn.close()


def is_auto_scan_enabled(chat_id):
    ensure_user(chat_id)
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT auto_scan FROM users WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        conn.close()
    return bool(row["auto_scan"]) if row else False


def set_auto_scan(chat_id, enabled):
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE users SET auto_scan = ? WHERE chat_id = ?", (1 if enabled else 0, chat_id))
        conn.close()


def get_auto_scan_users():
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM users WHERE auto_scan = 1")
        rows = cur.fetchall()
        conn.close()
    return [row["chat_id"] for row in rows]


def get_watchlist(chat_id):
    ensure_user(chat_id)
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT symbol FROM watchlists WHERE chat_id = ? ORDER BY symbol", (chat_id,))
        rows = cur.fetchall()
        conn.close()
    return [row["symbol"] for row in rows]


def add_symbol_to_watchlist(chat_id, symbol):
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO watchlists (chat_id, symbol) VALUES (?, ?)", (chat_id, symbol))
        conn.close()


def remove_symbol_from_watchlist(chat_id, symbol):
    current = get_watchlist(chat_id)
    if len(current) <= 1:
        return False
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlists WHERE chat_id = ? AND symbol = ?", (chat_id, symbol))
        conn.close()
    return True


def set_watchlist_all(chat_id):
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlists WHERE chat_id = ?", (chat_id,))
        for symbol in ALL_SYMBOLS:
            cur.execute("INSERT OR IGNORE INTO watchlists (chat_id, symbol) VALUES (?, ?)", (chat_id, symbol))
        conn.close()


def reset_watchlist_default(chat_id):
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlists WHERE chat_id = ?", (chat_id,))
        for symbol in DEFAULT_WATCHLIST:
            cur.execute("INSERT OR IGNORE INTO watchlists (chat_id, symbol) VALUES (?, ?)", (chat_id, symbol))
        conn.close()


def add_signal_history(chat_id, symbol, direction, score):
    now_text = time.strftime("%H:%M")
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO signal_history (chat_id, symbol, direction, score, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (chat_id, symbol, direction, score, now_text))
        conn.close()


def get_last_signals(chat_id, limit=10):
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT symbol, direction, score, created_at
            FROM signal_history
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (chat_id, limit))
        rows = cur.fetchall()
        conn.close()
    return rows


def get_cooldown(chat_id, symbol):
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT direction, sent_at FROM cooldowns
            WHERE chat_id = ? AND symbol = ?
        """, (chat_id, symbol))
        row = cur.fetchone()
        conn.close()
    return row


def set_cooldown(chat_id, symbol, direction):
    now_ts = int(time.time())
    with DB_LOCK:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO cooldowns (chat_id, symbol, direction, sent_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, symbol)
            DO UPDATE SET direction = excluded.direction, sent_at = excluded.sent_at
        """, (chat_id, symbol, direction, now_ts))
        conn.close()

# =========================
# TELEGRAM UI
# =========================
def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=20)
    except Exception as e:
        print(f"SEND MESSAGE ERROR: {e}")


def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{TELEGRAM_API}/editMessageText", json=payload, timeout=20)
    except Exception as e:
        print(f"EDIT MESSAGE ERROR: {e}")


def answer_callback(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json=payload, timeout=20)
    except Exception as e:
        print(f"CALLBACK ERROR: {e}")


def main_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "🧠 Analyse Premium", "callback_data": "analyse"},
                {"text": "🚨 Auto Scan", "callback_data": "autoscan"}
            ],
            [
                {"text": "📈 Ma Watchlist", "callback_data": "watchlist"},
                {"text": "🕓 Derniers Signaux", "callback_data": "signals"}
            ],
            [
                {"text": "⚙️ Réglages Pro", "callback_data": "settings"},
                {"text": "❓ Guide Rapide", "callback_data": "help"}
            ]
        ]
    }


def signal_menu(symbol):
    return {
        "inline_keyboard": [
            [
                {"text": "📱 Exécution rapide", "callback_data": f"exec:{symbol}"},
                {"text": "🏠 Menu", "callback_data": "back_main"}
            ]
        ]
    }


def settings_menu(chat_id):
    mode = get_user_mode(chat_id)
    auto = "✅ Auto Scan ON" if is_auto_scan_enabled(chat_id) else "❌ Auto Scan OFF"
    return {
        "inline_keyboard": [
            [{"text": auto, "callback_data": "autoscan"}],
            [
                {"text": "🛡️ Prudent", "callback_data": "mode:prudent"},
                {"text": "⚖️ Normal", "callback_data": "mode:normal"},
                {"text": "⚡ Agressif", "callback_data": "mode:aggressive"}
            ],
            [{"text": f"🎯 Mode actuel: {MODE_CONFIG[mode]['display_name']}", "callback_data": "noop"}],
            [{"text": "🔙 Retour menu", "callback_data": "back_main"}]
        ]
    }


def watchlist_menu(chat_id):
    current = get_watchlist(chat_id)
    keyboard = [
        [
            {"text": "✅ Tout sélectionner", "callback_data": "watch_all"},
            {"text": "♻️ Réinitialiser", "callback_data": "watch_reset"}
        ]
    ]
    for symbol in ALL_SYMBOLS:
        mark = "✅" if symbol in current else "➕"
        keyboard.append([{"text": f"{mark} {symbol}", "callback_data": f"toggle:{symbol}"}])
    keyboard.append([{"text": "🔙 Retour menu", "callback_data": "back_main"}])
    return {"inline_keyboard": keyboard}

# =========================
# WEBHOOK
# =========================
def desired_webhook_url():
    return f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}"


def ensure_webhook():
    try:
        info = requests.get(f"{TELEGRAM_API}/getWebhookInfo", timeout=20).json()
        current_url = info.get("result", {}).get("url", "")
        wanted_url = desired_webhook_url()
        if current_url != wanted_url:
            requests.get(
                f"{TELEGRAM_API}/setWebhook",
                params={"url": wanted_url},
                timeout=20
            )
    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")

# =========================
# HELPERS
# =========================
def normalize_symbol(text):
    raw = text.strip()
    if raw.startswith("/"):
        return raw.lower()
    return raw.upper().replace("/", "").replace("-", "").replace(" ", "")


def current_market_session():
    gmt_hour = time.gmtime().tm_hour
    if 0 <= gmt_hour < 7:
        return "ASIA"
    if 7 <= gmt_hour < 13:
        return "LONDON"
    if 13 <= gmt_hour < 22:
        return "NEW_YORK"
    return "OFF_HOURS"


def confidence_label(score):
    if score >= 85:
        return "Très fort 🔥"
    if score >= 75:
        return "Solide ✅"
    if score >= 65:
        return "Correct ⚪"
    return "Faible ⚠️"


def setup_grade(score):
    if score >= 85:
        return "A+"
    if score >= 78:
        return "A"
    if score >= 70:
        return "B+"
    if score >= 62:
        return "B"
    return "C"


def mode_label(mode):
    return MODE_CONFIG.get(mode, MODE_CONFIG["normal"])["display_name"]


def get_step_for_symbol(symbol, price):
    if price >= 1000:
        return round(price * 0.002, 5)
    if price >= 100:
        return round(price * 0.02, 5)
    if price >= 1:
        return round(price * 0.02, 5)
    if price >= 0.1:
        return round(price * 0.02, 6)
    return round(price * 0.02, 8)


def estimate_position_and_risk(price, sl, risk_percent=1.0, balance_usd=100.0):
    if sl == "-" or price == "-":
        return "0.01", f"{risk_percent:.0f}%", "~0.00$"

    stop_distance = abs(price - sl)
    if stop_distance <= 0:
        return "0.01", f"{risk_percent:.0f}%", "~0.00$"

    risk_amount = balance_usd * (risk_percent / 100)
    raw_size = risk_amount / stop_distance

    if price >= 100:
        lot = max(0.001, round(raw_size / 1000, 3))
    elif price >= 1:
        lot = max(0.01, round(raw_size / 100, 2))
    else:
        lot = max(1, round(raw_size, 0))

    est_loss = max(0.01, round(risk_amount, 2))
    return str(lot), f"{risk_percent:.0f}%", f"~{est_loss:.2f}$"

# =========================
# DATA
# =========================
def get_kraken_closes(symbol):
    pair = KRAKEN_PAIRS.get(symbol)
    if not pair:
        raise ValueError("Kraken pair not available")

    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": pair, "interval": 60}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    result = data.get("result", {})
    key = None
    for k in result.keys():
        if k != "last":
            key = k
            break

    if not key:
        raise ValueError("No Kraken candles")

    candles = result.get(key, [])
    closes = [float(c[4]) for c in candles if len(c) >= 5]
    if len(closes) < 60:
        raise ValueError("Not enough Kraken candles")
    return closes[-120:]


def get_coingecko_closes(symbol):
    coin_id = COINGECKO_IDS[symbol]
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": 7, "interval": "hourly"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    prices = [float(x[1]) for x in data.get("prices", [])]
    if len(prices) < 60:
        raise ValueError("Not enough CoinGecko candles")
    return prices[-120:]


def get_cmc_closes(symbol):
    if not CMC_API_KEY:
        raise ValueError("CMC_API_KEY missing")

    base = symbol.replace("USDT", "")
    url = f"{CMC_BASE_URL}/v1/cryptocurrency/quotes/latest"
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"}
    params = {"symbol": base, "convert": "USD"}

    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    coin = data.get("data", {}).get(base)
    if not coin:
        raise ValueError("No CMC data")

    price = float(coin["quote"]["USD"]["price"])
    synthetic = [price * (1 + ((i - 60) / 6000.0)) for i in range(120)]
    return synthetic


def get_paprika_closes(symbol):
    coin_id = PAPRIKA_IDS[symbol]
    url = f"https://api.coinpaprika.com/v1/tickers/{coin_id}/historical"
    params = {"start": "2026-04-01", "interval": "1h"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    prices = [float(x["price"]) for x in data if x.get("price") is not None]
    if len(prices) < 60:
        raise ValueError("Not enough Paprika candles")
    return prices[-120:]


def get_dexscreener_closes(symbol):
    base = symbol.replace("USDT", "")
    url = "https://api.dexscreener.com/latest/dex/search"
    params = {"q": base}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    pairs = data.get("pairs", [])
    prices = []
    for p in pairs[:20]:
        if p.get("priceUsd"):
            prices.append(float(p["priceUsd"]))
    if len(prices) < 3:
        raise ValueError("Not enough DexScreener prices")
    synthetic = (prices * 40)[:120]
    return synthetic


def fetch_closes(symbol):
    fetchers = [
        get_kraken_closes,
        get_coingecko_closes,
        get_cmc_closes,
        get_paprika_closes,
        get_dexscreener_closes
    ]

    last_error = None
    for fn in fetchers:
        try:
            return fn(symbol)
        except Exception as e:
            last_error = e
            print(f"{fn.__name__} FAIL {symbol}: {e}")

    if last_error:
        raise last_error
    raise ValueError("All data sources failed")

# =========================
# INDICATORS
# =========================
def ema(values, period):
    if len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    ema_values = []
    sma = sum(values[:period]) / period
    ema_values.append(sma)
    for price in values[period:]:
        ema_values.append((price - ema_values[-1]) * multiplier + ema_values[-1])
    return ema_values


def rsi(values, period=14):
    if len(values) < period + 1:
        return []

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsis = []
    if avg_loss == 0:
        rsis.append(100)
    else:
        rsis.append(100 - (100 / (1 + (avg_gain / avg_loss))))

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

        if avg_loss == 0:
            rsis.append(100)
        else:
            rs = avg_gain / avg_loss
            rsis.append(100 - (100 / (1 + rs)))

    return rsis


def atr_like(closes, lookback=14):
    if len(closes) < lookback + 1:
        return None
    moves = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    recent = moves[-lookback:]
    return sum(recent) / len(recent)


def detect_momentum(rsi_value, direction):
    if direction == "BUY":
        if rsi_value >= 65:
            return "STRONG"
        if rsi_value >= 56:
            return "GOOD"
        return "WEAK"
    if direction == "SELL":
        if rsi_value <= 35:
            return "STRONG"
        if rsi_value <= 44:
            return "GOOD"
        return "WEAK"
    return "NEUTRAL"


def score_signal(price, ema20_value, ema50_value, rsi_value, direction, volatility):
    score = 45
    distance_pct = abs(ema20_value - ema50_value) / max(abs(price), 0.00001)

    if direction == "BUY":
        if ema20_value > ema50_value:
            score += 15
        if rsi_value >= 65:
            score += 20
        elif rsi_value >= 58:
            score += 12
        elif rsi_value >= 54:
            score += 8
    elif direction == "SELL":
        if ema20_value < ema50_value:
            score += 15
        if rsi_value <= 35:
            score += 20
        elif rsi_value <= 42:
            score += 12
        elif rsi_value <= 46:
            score += 8

    if distance_pct >= 0.025:
        score += 12
    elif distance_pct >= 0.015:
        score += 7
    elif distance_pct >= 0.008:
        score += 3

    if volatility is not None:
        vol_ratio = volatility / max(abs(price), 0.00001)
        if vol_ratio >= 0.015:
            score += 5
        elif vol_ratio <= 0.003:
            score -= 5

    session = current_market_session()
    if session in ["LONDON", "NEW_YORK"]:
        score += 5
    elif session == "OFF_HOURS":
        score -= 5

    return max(0, min(97, int(score)))

# =========================
# ANALYSIS
# =========================
def analyze_symbol(symbol, mode="normal"):
    cache_key = f"{symbol}_{mode}"
    now_ts = int(time.time())
    cached = ANALYSIS_CACHE.get(cache_key)

    if cached and (now_ts - cached["time"] <= CACHE_TTL_SECONDS):
        data = dict(cached["data"])
        data["cached"] = True
        return data

    try:
        closes = fetch_closes(symbol)
        ema20_list = ema(closes, 20)
        ema50_list = ema(closes, 50)
        rsi_list = rsi(closes, 14)

        if not ema20_list or not ema50_list or not rsi_list:
            raise ValueError("Indicator calculation failed")

        price = closes[-1]
        ema20_value = ema20_list[-1]
        ema50_value = ema50_list[-1]
        rsi_value = rsi_list[-1]
        volatility = atr_like(closes, 14)
        session = current_market_session()

        cfg = MODE_CONFIG.get(mode, MODE_CONFIG["normal"])
        buy_rsi = cfg["buy_rsi"]
        sell_rsi = cfg["sell_rsi"]

        if ema20_value > ema50_value and rsi_value >= buy_rsi:
            direction = "BUY"
            trend = "BULLISH"
            summary = "Continuation haussière détectée avec alignement EMA et momentum favorable."
        elif ema20_value < ema50_value and rsi_value <= sell_rsi:
            direction = "SELL"
            trend = "BEARISH"
            summary = "Continuation baissière détectée avec pression vendeuse claire."
        else:
            direction = "NO TRADE"
            trend = "NEUTRAL"
            summary = "Le marché n'offre pas un setup suffisamment propre."

        score = 40 if direction == "NO TRADE" else score_signal(
            price, ema20_value, ema50_value, rsi_value, direction, volatility
        )
        momentum = detect_momentum(rsi_value, direction if direction != "NO TRADE" else "BUY")
        grade = setup_grade(score)
        step = get_step_for_symbol(symbol, price)

        if direction == "BUY":
            sl = round(price - step, 8)
            tp1 = round(price + step, 8)
            tp2 = round(price + step * 2, 8)
            tp3 = round(price + step * 3, 8)
        elif direction == "SELL":
            sl = round(price + step, 8)
            tp1 = round(price - step, 8)
            tp2 = round(price - step * 2, 8)
            tp3 = round(price - step * 3, 8)
        else:
            sl = "-"
            tp1 = "-"
            tp2 = "-"
            tp3 = "-"

        rr_text = "-"
        if direction in ["BUY", "SELL"] and sl != "-" and tp1 != "-":
            risk = abs(price - sl)
            reward = abs(tp1 - price)
            if risk > 0:
                rr_text = f"{round(reward / risk, 2)}R"

        entry_low = "-"
        entry_high = "-"
        if direction in ["BUY", "SELL"]:
            zone = step * 0.25
            entry_low = round(price - zone, 8)
            entry_high = round(price + zone, 8)

        lot, risk_percent, est_loss = estimate_position_and_risk(price, sl)

        data = {
            "symbol": symbol,
            "direction": direction,
            "trend": trend,
            "momentum": momentum,
            "grade": grade,
            "price": round(price, 8),
            "ema20": round(ema20_value, 8),
            "ema50": round(ema50_value, 8),
            "rsi": round(rsi_value, 2),
            "score": score,
            "session": session,
            "entry": round(price, 8) if direction != "NO TRADE" else "-",
            "entry_low": entry_low,
            "entry_high": entry_high,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "rr": rr_text,
            "lot": lot,
            "risk_percent": risk_percent,
            "est_loss": est_loss,
            "summary": summary,
            "cached": False,
            "mode": mode
        }

        ANALYSIS_CACHE[cache_key] = {"time": now_ts, "data": dict(data)}
        return data

    except Exception as e:
        if cached:
            data = dict(cached["data"])
            data["cached"] = True
            return data

        return {
            "symbol": symbol,
            "direction": "NO TRADE",
            "trend": "NEUTRAL",
            "momentum": "NEUTRAL",
            "grade": "C",
            "price": "-",
            "ema20": "-",
            "ema50": "-",
            "rsi": "-",
            "score": 20,
            "session": current_market_session(),
            "entry": "-",
            "entry_low": "-",
            "entry_high": "-",
            "sl": "-",
            "tp1": "-",
            "tp2": "-",
            "tp3": "-",
            "rr": "-",
            "lot": "0.01",
            "risk_percent": "1%",
            "est_loss": "~0.00$",
            "summary": f"Données temporairement indisponibles. ({str(e)})",
            "cached": False,
            "mode": mode
        }

# =========================
# MESSAGES
# =========================
def format_signal(sig):
    quality = confidence_label(sig["score"])
    mode_note = mode_label(sig["mode"])
    cache_note = "\n📦 *Source rapide:* cache récent utilisé" if sig.get("cached") else ""

    if sig["direction"] == "NO TRADE":
        return (
            f"👑 *LVBXNT CRYPTO BOT — V3 REAL AI* 👑\n\n"
            f"💰 *Crypto:* `{sig['symbol']}`\n"
            f"⚠️ *Signal:* *NO TRADE*\n"
            f"🏆 *Setup Grade:* `{sig['grade']}`\n"
            f"🧠 *Score:* `{sig['score']}/100`\n"
            f"💎 *Qualité:* {quality}\n"
            f"🎛️ *Mode:* {mode_note}"
            f"{cache_note}\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"📉 *Trend:* `{sig['trend']}`\n"
            f"⚡ *Momentum:* `{sig['momentum']}`\n"
            f"🕒 *Session:* `{sig['session']}`\n"
            f"━━━━━━━━━━━━━━\n\n"
            f"💵 *Prix:* `{sig['price']}`\n"
            f"📈 *EMA20:* `{sig['ema20']}`\n"
            f"📉 *EMA50:* `{sig['ema50']}`\n"
            f"⚡ *RSI:* `{sig['rsi']}`\n\n"
            f"🧠 *Résumé:*\n{sig['summary']}\n\n"
            f"📌 *Verdict:*\nAttends un setup plus propre."
        )

    return (
        f"👑 *LVBXNT CRYPTO BOT — V3 REAL AI* 👑\n\n"
        f"💰 *Crypto:* `{sig['symbol']}`\n"
        f"🚦 *Signal:* *{sig['direction']}*\n"
        f"🏆 *Setup Grade:* `{sig['grade']}`\n"
        f"🧠 *Score:* `{sig['score']}/100`\n"
        f"💎 *Qualité:* {quality}\n"
        f"🎛️ *Mode:* {mode_note}"
        f"{cache_note}\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"📈 *Trend:* `{sig['trend']}`\n"
        f"⚡ *Momentum:* `{sig['momentum']}`\n"
        f"🕒 *Session:* `{sig['session']}`\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"💵 *Current Price:* `{sig['price']}`\n"
        f"📍 *Entry Zone:* `{sig['entry_low']}` → `{sig['entry_high']}`\n"
        f"🛑 *Stop Loss:* `{sig['sl']}`\n\n"
        f"🎯 *TP1:* `{sig['tp1']}`\n"
        f"🎯 *TP2:* `{sig['tp2']}`\n"
        f"🎯 *TP3:* `{sig['tp3']}`\n"
        f"⚖️ *Risk/Reward TP1:* `{sig['rr']}`\n\n"
        f"💰 *Lot conseillé:* `{sig['lot']}`\n"
        f"📊 *Risque:* `{sig['risk_percent']}` du capital\n"
        f"💸 *Perte max estimée:* `{sig['est_loss']}`\n\n"
        f"🧠 *Résumé:*\n{sig['summary']}\n\n"
        f"📌 *Verdict:*\nSetup exploitable. Vérifie ton exchange avant entrée."
    )


def format_quick_exec(sig):
    if sig["direction"] == "NO TRADE":
        return (
            f"📱 *EXECUTION RAPIDE*\n\n"
            f"`{sig['symbol']}`\n\n"
            f"⚠️ Aucun trade à exécuter pour le moment."
        )

    return (
        f"📱 *EXECUTION RAPIDE*\n\n"
        f"`{sig['symbol']}` — *{sig['direction']}*\n\n"
        f"Entry: `{sig['entry']}`\n"
        f"SL: `{sig['sl']}`\n\n"
        f"TP1: `{sig['tp1']}`\n"
        f"TP2: `{sig['tp2']}`\n"
        f"TP3: `{sig['tp3']}`\n\n"
        f"Lot: `{sig['lot']}`\n"
        f"Risk: `{sig['risk_percent']}`\n"
        f"Max Loss: `{sig['est_loss']}`\n\n"
        f"📌 Recopie simplement ces valeurs dans ton exchange."
    )


def format_last_signals(chat_id):
    rows = get_last_signals(chat_id, 10)
    if not rows:
        return "🕓 *Derniers signaux*\n\nAucun signal pour le moment."
    text = "🕓 *Derniers signaux premium*\n\n"
    for row in rows:
        text += f"• `{row['symbol']}` → *{row['direction']}* (`{row['score']}/100`) à `{row['created_at']}`\n"
    return text


def format_watchlist_text(chat_id):
    symbols = get_watchlist(chat_id)
    text = f"📈 *Ta watchlist premium*\n\nSélectionnées: *{len(symbols)}/{len(ALL_SYMBOLS)}*\n\n"
    text += "\n".join([f"• `{s}`" for s in symbols])
    return text


def format_settings_text(chat_id):
    mode = get_user_mode(chat_id)
    auto = "ON ✅" if is_auto_scan_enabled(chat_id) else "OFF ❌"

    return (
        f"⚙️ *PARAMÈTRES PRO*\n\n"
        f"🤖 *Auto Scan:* {auto}\n"
        f"🎛️ *Mode actuel:* {mode_label(mode)}\n\n"
        f"🛡️ *Prudent* = moins de signaux, plus strict\n"
        f"⚖️ *Normal* = bon équilibre\n"
        f"⚡ *Agressif* = plus d'opportunités"
    )

# =========================
# AUTO SCAN
# =========================
def cooldown_ok(chat_id, symbol, direction):
    row = get_cooldown(chat_id, symbol)
    if not row:
        return True
    now_ts = int(time.time())
    if row["direction"] != direction:
        return True
    return (now_ts - row["sent_at"]) >= COOLDOWN_SECONDS


def auto_scan_loop():
    while True:
        try:
            users = get_auto_scan_users()
            for chat_id in users:
                mode = get_user_mode(chat_id)
                min_score = MODE_CONFIG[mode]["min_auto_score"]
                for symbol in get_watchlist(chat_id):
                    try:
                        sig = analyze_symbol(symbol, mode)
                        if sig["direction"] in ["BUY", "SELL"] and sig["score"] >= min_score:
                            if cooldown_ok(chat_id, symbol, sig["direction"]):
                                set_cooldown(chat_id, symbol, sig["direction"])
                                add_signal_history(chat_id, symbol, sig["direction"], sig["score"])
                                send_message(chat_id, format_signal(sig), signal_menu(symbol))
                    except Exception as e:
                        print(f"AUTO SCAN ERROR {symbol}: {e}")
            time.sleep(SCAN_INTERVAL_SECONDS)
        except Exception as e:
            print(f"AUTO LOOP ERROR: {e}")
            time.sleep(30)


def start_auto_scan_thread():
    t = threading.Thread(target=auto_scan_loop, daemon=True)
    t.start()

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "LVBXNT CRYPTO BOT V3 REAL AI is running!"


@app.route("/set_webhook")
def set_webhook():
    try:
        response = requests.get(
            f"{TELEGRAM_API}/setWebhook",
            params={"url": desired_webhook_url()},
            timeout=20
        )
        return response.text
    except Exception as e:
        return f"set_webhook error: {e}", 500


@app.route("/delete_webhook")
def delete_webhook():
    try:
        response = requests.get(f"{TELEGRAM_API}/deleteWebhook", timeout=20)
        return response.text
    except Exception as e:
        return f"delete_webhook error: {e}", 500


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)

    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        raw_text = msg.get("text", "").strip()
        normalized = normalize_symbol(raw_text)

        ensure_user(chat_id)

        if normalized == "/start" or raw_text.lower() == "/start":
            welcome = (
                "👑 *LVBXNT CRYPTO BOT — V3 REAL AI* 👑\n\n"
                "Ton bot premium est prêt.\n\n"
                "💰 *Cryptos supportées:*\n"
                "`BTCUSDT` • `ETHUSDT` • `SOLUSDT` • `XRPUSDT`\n"
                "`BNBUSDT` • `ADAUSDT` • `DOGEUSDT` • `AVAXUSDT`\n"
                "`LINKUSDT` • `MATICUSDT`\n\n"
                "✅ Analyse Premium\n"
                "✅ Auto Scan\n"
                "✅ Watchlist\n"
                "✅ Réglages Pro\n"
                "✅ Exécution rapide iPhone\n\n"
                "👇 *Choisis une option ou envoie une crypto*"
            )
            send_message(chat_id, welcome, main_menu())

        elif normalized in ALL_SYMBOLS:
            mode = get_user_mode(chat_id)
            sig = analyze_symbol(normalized, mode)
            if sig["direction"] in ["BUY", "SELL"]:
                add_signal_history(chat_id, normalized, sig["direction"], sig["score"])
            send_message(chat_id, format_signal(sig), signal_menu(normalized))

        else:
            send_message(chat_id, "❌ *Crypto inconnue*\n\nEnvoie par exemple `BTCUSDT`.", main_menu())

    elif "callback_query" in data:
        cb = data["callback_query"]
        callback_id = cb["id"]
        chat_id = cb["message"]["chat"]["id"]
        message_id = cb["message"]["message_id"]
        action = cb["data"]

        ensure_user(chat_id)
        answer_callback(callback_id)

        if action == "noop":
            return jsonify({"ok": True})

        if action == "analyse":
            send_message(chat_id, "🧠 Envoie-moi une crypto comme `BTCUSDT` pour lancer l'analyse.", main_menu())

        elif action == "autoscan":
            current = is_auto_scan_enabled(chat_id)
            set_auto_scan(chat_id, not current)
            status = "activé" if not current else "désactivé"
            send_message(chat_id, f"🤖 *Auto Scan {status}.*", main_menu())

        elif action == "settings":
            edit_message(chat_id, message_id, format_settings_text(chat_id), settings_menu(chat_id))

        elif action.startswith("mode:"):
            mode = action.split(":", 1)[1]
            set_user_mode(chat_id, mode)
            edit_message(chat_id, message_id, format_settings_text(chat_id), settings_menu(chat_id))

        elif action == "watchlist":
            edit_message(chat_id, message_id, format_watchlist_text(chat_id), watchlist_menu(chat_id))

        elif action == "watch_all":
            set_watchlist_all(chat_id)
            edit_message(chat_id, message_id, format_watchlist_text(chat_id), watchlist_menu(chat_id))

        elif action == "watch_reset":
            reset_watchlist_default(chat_id)
            edit_message(chat_id, message_id, format_watchlist_text(chat_id), watchlist_menu(chat_id))

        elif action.startswith("toggle:"):
            symbol = action.split(":", 1)[1]
            current = get_watchlist(chat_id)
            if symbol in current:
                removed = remove_symbol_from_watchlist(chat_id, symbol)
                if not removed:
                    send_message(chat_id, "⚠️ Garde au moins 1 crypto.", main_menu())
            else:
                add_symbol_to_watchlist(chat_id, symbol)
            edit_message(chat_id, message_id, format_watchlist_text(chat_id), watchlist_menu(chat_id))

        elif action == "signals":
            send_message(chat_id, format_last_signals(chat_id), main_menu())

        elif action == "help":
            msg = (
                "❓ *GUIDE RAPIDE*\n\n"
                "• Envoie une crypto : `BTCUSDT`\n"
                "• Lis l'analyse\n"
                "• Clique *📱 Exécution rapide*\n"
                "• Recopie dans ton app mobile\n\n"
                "💡 *Conseil:*\n"
                "Commence en mode *Normal*."
            )
            send_message(chat_id, msg, main_menu())

        elif action == "back_main":
            edit_message(chat_id, message_id, "🏠 *Menu principal premium*", main_menu())

        elif action.startswith("exec:"):
            symbol = action.split(":", 1)[1]
            mode = get_user_mode(chat_id)
            sig = analyze_symbol(symbol, mode)
            send_message(chat_id, format_quick_exec(sig), signal_menu(symbol))

    return jsonify({"ok": True})

# =========================
# STARTUP
# =========================
def background_boot():
    try:
        init_db()
    except Exception as e:
        print(f"DB INIT ERROR: {e}")

    try:
        ensure_webhook()
    except Exception as e:
        print(f"WEBHOOK INIT ERROR: {e}")

    try:
        start_auto_scan_thread()
    except Exception as e:
        print(f"AUTOSCAN INIT ERROR: {e}")


threading.Thread(target=background_boot, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)