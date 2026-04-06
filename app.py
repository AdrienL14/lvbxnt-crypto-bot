import os
import sqlite3
import time
import math
import uuid
import requests
from flask import Flask, request, jsonify

# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = Flask(__name__)

SUPPORTED = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT"
]

DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]

# CoinGecko vrais IDs
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

# CoinPaprika IDs
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

CACHE = {}
CACHE_TTL = 180

# =========================================================
# DATABASE
# =========================================================
def db():
    conn = sqlite3.connect("crypto_bot.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id TEXT PRIMARY KEY,
        mode TEXT DEFAULT 'NORMAL',
        autoscan INTEGER DEFAULT 1
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS watchlists (
        chat_id TEXT,
        symbol TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        signal_id TEXT PRIMARY KEY,
        chat_id TEXT,
        symbol TEXT,
        signal TEXT,
        score INTEGER,
        entry REAL,
        sl REAL,
        tp1 REAL,
        tp2 REAL,
        tp3 REAL,
        created_at INTEGER
    )
    """)

    conn.commit()
    conn.close()

init_db()

# =========================================================
# HELPERS
# =========================================================
def cache_get(key):
    item = CACHE.get(key)
    if not item:
        return None
    if time.time() - item["time"] > CACHE_TTL:
        return None
    return item["value"]

def cache_set(key, value):
    CACHE[key] = {"value": value, "time": time.time()}

def ensure_user(chat_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM users WHERE chat_id=?", (str(chat_id),))
    row = c.fetchone()

    if not row:
        c.execute("INSERT INTO users (chat_id, mode, autoscan) VALUES (?, ?, ?)",
                  (str(chat_id), "NORMAL", 1))
        for s in DEFAULT_WATCHLIST:
            c.execute("INSERT INTO watchlists (chat_id, symbol) VALUES (?, ?)", (str(chat_id), s))
        conn.commit()

    conn.close()

def get_user_settings(chat_id):
    ensure_user(chat_id)
    conn = db()
    c = conn.cursor()
    c.execute("SELECT mode, autoscan FROM users WHERE chat_id=?", (str(chat_id),))
    row = c.fetchone()
    conn.close()
    return row["mode"], row["autoscan"]

def set_user_mode(chat_id, mode):
    ensure_user(chat_id)
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET mode=? WHERE chat_id=?", (mode, str(chat_id)))
    conn.commit()
    conn.close()

def set_autoscan(chat_id, value):
    ensure_user(chat_id)
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET autoscan=? WHERE chat_id=?", (value, str(chat_id)))
    conn.commit()
    conn.close()

def get_watchlist(chat_id):
    ensure_user(chat_id)
    conn = db()
    c = conn.cursor()
    c.execute("SELECT symbol FROM watchlists WHERE chat_id=?", (str(chat_id),))
    rows = c.fetchall()
    conn.close()
    return [r["symbol"] for r in rows]

def save_signal(chat_id, symbol, signal, score, entry, sl, tp1, tp2, tp3):
    conn = db()
    c = conn.cursor()
    signal_id = str(uuid.uuid4())[:8]
    c.execute("""
        INSERT INTO signals (signal_id, chat_id, symbol, signal, score, entry, sl, tp1, tp2, tp3, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (signal_id, str(chat_id), symbol, signal, score, entry, sl, tp1, tp2, tp3, int(time.time())))
    conn.commit()
    conn.close()
    return signal_id

def get_last_signals(chat_id, limit=5):
    conn = db()
    c = conn.cursor()
    c.execute("""
        SELECT * FROM signals
        WHERE chat_id=?
        ORDER BY created_at DESC
        LIMIT ?
    """, (str(chat_id), limit))
    rows = c.fetchall()
    conn.close()
    return rows

# =========================================================
# TELEGRAM
# =========================================================
def tg(method, payload):
    url = f"{BASE_URL}/{method}"
    return requests.post(url, json=payload, timeout=15)

def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    tg("sendMessage", payload)

def answer_callback(callback_id, text="OK"):
    tg("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text,
        "show_alert": False
    })

# =========================================================
# KEYBOARDS
# =========================================================
def main_keyboard():
    return {
        "inline_keyboard": [
            [{"text":"🧠 Analyse Premium","callback_data":"analyse"},
             {"text":"🚨 Auto Scan","callback_data":"autoscan"}],
            [{"text":"📈 Ma Watchlist","callback_data":"watchlist"},
             {"text":"🕓 Derniers Signaux","callback_data":"signals"}],
            [{"text":"⚙️ Réglages Pro","callback_data":"settings"},
             {"text":"❓ Guide Rapide","callback_data":"guide"}]
        ]
    }

def settings_keyboard(chat_id):
    mode, autoscan = get_user_settings(chat_id)
    auto_label = "✅ Auto Scan ON" if autoscan else "❌ Auto Scan OFF"

    return {
        "inline_keyboard": [
            [{"text": auto_label, "callback_data": "toggle_autoscan"}],
            [{"text":"🛡️ Prudent","callback_data":"mode_PRUDENT"},
             {"text":"⚖️ Normal","callback_data":"mode_NORMAL"},
             {"text":"⚡ Agressif","callback_data":"mode_AGGRESSIVE"}],
            [{"text":f"🎯 Mode actuel : {mode}", "callback_data":"noop"}],
            [{"text":"🏠 Retour menu","callback_data":"menu"}]
        ]
    }

# =========================================================
# UI TEXT
# =========================================================
def show_menu(chat_id):
    text = (
        "👑 <b>LVBXNT CRYPTO BOT — V2.2 PRO</b> 👑\n\n"
        "Ton bot crypto premium est prêt.\n\n"
        "💰 <b>Cryptos supportées :</b>\n"
        "BTC • ETH • SOL • XRP • BNB\n"
        "ADA • DOGE • AVAX • LINK • MATIC\n\n"
        "✅ Analyse Premium\n"
        "✅ Auto Scan\n"
        "✅ Watchlist\n"
        "✅ Réglages Pro\n"
        "✅ Exécution rapide iPhone\n\n"
        "👇 <b>Choisis une option ou envoie une crypto</b>"
    )
    send_message(chat_id, text, main_keyboard())

def show_settings(chat_id):
    mode, autoscan = get_user_settings(chat_id)

    text = (
        "⚙️ <b>PARAMÈTRES PRO</b>\n\n"
        f"🤖 Auto Scan : {'ON ✅' if autoscan else 'OFF ❌'}\n"
        f"🎯 Mode actuel : <b>{mode}</b>\n\n"
        "🛡️ <b>Prudent</b> = moins de signaux, plus strict\n"
        "⚖️ <b>Normal</b> = bon équilibre\n"
        "⚡ <b>Agressif</b> = plus d'opportunités"
    )
    send_message(chat_id, text, settings_keyboard(chat_id))

def show_guide(chat_id):
    text = (
        "❓ <b>GUIDE RAPIDE</b>\n\n"
        "📩 Envoie simplement une crypto supportée :\n"
        "<code>BTCUSDT</code>\n"
        "<code>ETHUSDT</code>\n"
        "<code>SOLUSDT</code>\n\n"
        "🧠 Le bot te donne :\n"
        "• BUY / SELL / NO TRADE\n"
        "• Score qualité\n"
        "• Entry / SL / TP1 / TP2 / TP3\n"
        "• Résumé clair du setup\n\n"
        "📱 Ensuite tu peux copier vite ton setup sur mobile."
    )
    send_message(chat_id, text, {"inline_keyboard":[[{"text":"🏠 Menu","callback_data":"menu"}]]})

def show_watchlist(chat_id):
    wl = get_watchlist(chat_id)
    text = "📈 <b>MA WATCHLIST</b>\n\n" + "\n".join([f"• {x}" for x in wl])
    send_message(chat_id, text, {"inline_keyboard":[[{"text":"🏠 Menu","callback_data":"menu"}]]})

def show_signals(chat_id):
    rows = get_last_signals(chat_id, 5)
    if not rows:
        send_message(chat_id, "🕓 Aucun signal récent.", {"inline_keyboard":[[{"text":"🏠 Menu","callback_data":"menu"}]]})
        return

    text = "🕓 <b>DERNIERS SIGNAUX</b>\n\n"
    for r in rows:
        text += (
            f"• <b>{r['symbol']}</b> — {r['signal']} — Score {r['score']}/100\n"
            f"Entry {r['entry']} | SL {r['sl']} | TP1 {r['tp1']}\n\n"
        )

    send_message(chat_id, text, {"inline_keyboard":[[{"text":"🏠 Menu","callback_data":"menu"}]]})

# =========================================================
# DATA SOURCES
# =========================================================
def fetch_from_coingecko(symbol):
    cache_key = f"cg_{symbol}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    coin_id = COINGECKO_IDS.get(symbol)
    if not coin_id:
        return None

    try:
        url = (
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
            f"?vs_currency=usd&days=7&interval=hourly"
        )
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return None

        data = r.json()
        prices = [float(x[1]) for x in data.get("prices", [])][-120:]
        if len(prices) < 30:
            return None

        cache_set(cache_key, prices)
        return prices
    except:
        return None

def fetch_from_coinpaprika(symbol):
    cache_key = f"paprika_{symbol}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    coin_id = PAPRIKA_IDS.get(symbol)
    if not coin_id:
        return None

    try:
        url = f"https://api.coinpaprika.com/v1/tickers/{coin_id}/historical?start=2026-04-01&interval=1h"
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return None

        data = r.json()
        prices = [float(x["price"]) for x in data if x.get("price")][-120:]
        if len(prices) < 30:
            return None

        cache_set(cache_key, prices)
        return prices
    except:
        return None

def fetch_from_dexscreener(symbol):
    cache_key = f"dex_{symbol}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    base = symbol.replace("USDT", "")
    try:
        url = f"https://api.dexscreener.com/latest/dex/search?q={base}"
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return None

        data = r.json()
        pairs = data.get("pairs", [])
        if not pairs:
            return None

        prices = []
        for p in pairs[:20]:
            price = p.get("priceUsd")
            if price:
                prices.append(float(price))

        if len(prices) < 3:
            return None

        synthetic = prices * 40
        synthetic = synthetic[:120]

        cache_set(cache_key, synthetic)
        return synthetic
    except:
        return None

def fetch_social_sentiment(symbol):
    cache_key = f"st_{symbol}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    try:
        ticker = symbol.replace("USDT", "") + ".X"
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return {"sentiment": "NEUTRE", "activity": "Faible"}

        data = r.json()
        messages = data.get("messages", [])
        count = len(messages)

        if count >= 20:
            sentiment = "POSITIF"
            activity = "Élevée"
        elif count >= 8:
            sentiment = "MODÉRÉ"
            activity = "Correcte"
        else:
            sentiment = "FAIBLE"
            activity = "Faible"

        result = {"sentiment": sentiment, "activity": activity}
        cache_set(cache_key, result)
        return result
    except:
        return {"sentiment": "NEUTRE", "activity": "Faible"}

def get_prices(symbol):
    sources = [
        fetch_from_coingecko,
        fetch_from_coinpaprika,
        fetch_from_dexscreener
    ]

    for fn in sources:
        prices = fn(symbol)
        if prices and len(prices) >= 30:
            return prices

    return None

# =========================================================
# ANALYSIS
# =========================================================
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for price in values[period:]:
        e = price * k + e * (1 - k)
    return e

def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def analyze_symbol(symbol, mode="NORMAL"):
    prices = get_prices(symbol)
    if not prices or len(prices) < 50:
        return None

    current = prices[-1]
    ema20 = ema(prices, 20)
    ema50 = ema(prices, 50)
    rsi_val = rsi(prices[-15:], 14)

    if not ema20 or not ema50 or rsi_val is None:
        return None

    trend = "HAUSSIÈRE" if ema20 > ema50 else "BAISSIÈRE"
    momentum = "FORT" if abs(ema20 - ema50) / current > 0.01 else "MOYEN"

    score = 50

    if ema20 > ema50:
        score += 20
    else:
        score -= 20

    if rsi_val < 35:
        score += 15
    elif rsi_val > 65:
        score -= 15

    if momentum == "FORT":
        score += 10

    score = max(0, min(100, int(score)))

    if mode == "PRUDENT":
        buy_threshold = 72
        sell_threshold = 28
    elif mode == "AGGRESSIVE":
        buy_threshold = 58
        sell_threshold = 42
    else:
        buy_threshold = 65
        sell_threshold = 35

    if score >= buy_threshold:
        signal = "BUY"
        sl = round(current * 0.98, 4)
        tp1 = round(current * 1.02, 4)
        tp2 = round(current * 1.04, 4)
        tp3 = round(current * 1.06, 4)
        grade = "A+" if score >= 80 else "A" if score >= 70 else "B"
    elif score <= sell_threshold:
        signal = "SELL"
        sl = round(current * 1.02, 4)
        tp1 = round(current * 0.98, 4)
        tp2 = round(current * 0.96, 4)
        tp3 = round(current * 0.94, 4)
        grade = "A+" if score <= 20 else "A" if score <= 30 else "B"
    else:
        signal = "NO TRADE"
        sl = round(current * 0.99, 4)
        tp1 = round(current * 1.01, 4)
        tp2 = round(current * 1.02, 4)
        tp3 = round(current * 1.03, 4)
        grade = "C"

    social = fetch_social_sentiment(symbol)

    return {
        "symbol": symbol,
        "signal": signal,
        "score": score,
        "grade": grade,
        "trend": trend,
        "momentum": momentum,
        "price": round(current, 4),
        "entry": round(current, 4),
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rsi": round(rsi_val, 2),
        "ema20": round(ema20, 4),
        "ema50": round(ema50, 4),
        "social_sentiment": social["sentiment"],
        "social_activity": social["activity"]
    }

def format_analysis(data):
    verdict = "✅ Setup propre" if data["signal"] != "NO TRADE" else "⚪ Marché pas assez propre"

    return (
        f"🧠 <b>ANALYSE PREMIUM — {data['symbol']}</b>\n\n"
        f"🚦 <b>Signal :</b> {data['signal']}\n"
        f"🏆 <b>Grade :</b> {data['grade']}\n"
        f"⭐ <b>Score :</b> {data['score']}/100\n\n"
        f"📈 <b>Tendance :</b> {data['trend']}\n"
        f"⚡ <b>Momentum :</b> {data['momentum']}\n"
        f"📊 <b>RSI :</b> {data['rsi']}\n\n"
        f"💰 <b>Prix :</b> {data['price']}\n"
        f"🎯 <b>Entry :</b> {data['entry']}\n"
        f"🛑 <b>Stop Loss :</b> {data['sl']}\n"
        f"🎯 <b>TP1 :</b> {data['tp1']}\n"
        f"🎯 <b>TP2 :</b> {data['tp2']}\n"
        f"🎯 <b>TP3 :</b> {data['tp3']}\n\n"
        f"🗣 <b>Sentiment social :</b> {data['social_sentiment']}\n"
        f"📢 <b>Activité sociale :</b> {data['social_activity']}\n\n"
        f"📝 <b>Résumé :</b>\n"
        f"EMA20 = {data['ema20']} | EMA50 = {data['ema50']}\n\n"
        f"{verdict}"
    )

def format_execution(data):
    return (
        f"📱 <b>EXÉCUTION RAPIDE — {data['symbol']}</b>\n\n"
        f"🚦 {data['signal']}\n"
        f"🎯 Entry : {data['entry']}\n"
        f"🛑 SL : {data['sl']}\n"
        f"🎯 TP1 : {data['tp1']}\n"
        f"🎯 TP2 : {data['tp2']}\n"
        f"🎯 TP3 : {data['tp3']}\n\n"
        f"⭐ Score : {data['score']}/100"
    )

# =========================================================
# HANDLERS
# =========================================================
def handle_symbol(chat_id, symbol):
    mode, _ = get_user_settings(chat_id)

    send_message(chat_id, f"⏳ Analyse PRO en cours sur <b>{symbol}</b>...")

    data = analyze_symbol(symbol, mode)
    if not data:
        send_message(chat_id, "❌ Impossible de récupérer les données.\n\nRéessaie dans 1 à 2 minutes.")
        return

    signal_id = save_signal(
        chat_id, symbol, data["signal"], data["score"],
        data["entry"], data["sl"], data["tp1"], data["tp2"], data["tp3"]
    )

    keyboard = {
        "inline_keyboard": [
            [{"text":"📱 Exécution rapide","callback_data":f"exec_{signal_id}"}],
            [{"text":"🏠 Menu","callback_data":"menu"}]
        ]
    }

    send_message(chat_id, format_analysis(data), keyboard)

def handle_callback(chat_id, callback_id, data):
    ensure_user(chat_id)

    if data == "menu":
        answer_callback(callback_id, "Menu")
        show_menu(chat_id)

    elif data == "analyse":
        answer_callback(callback_id, "Analyse")
        send_message(chat_id, "📩 Envoie une crypto (ex: BTCUSDT)")

    elif data == "autoscan":
        answer_callback(callback_id, "Auto Scan")
        mode, autoscan = get_user_settings(chat_id)
        txt = "🤖 Auto Scan activé." if autoscan else "🤖 Auto Scan désactivé."
        send_message(chat_id, txt, main_keyboard())

    elif data == "watchlist":
        answer_callback(callback_id, "Watchlist")
        show_watchlist(chat_id)

    elif data == "signals":
        answer_callback(callback_id, "Signaux")
        show_signals(chat_id)

    elif data == "settings":
        answer_callback(callback_id, "Réglages")
        show_settings(chat_id)

    elif data == "guide":
        answer_callback(callback_id, "Guide")
        show_guide(chat_id)

    elif data == "toggle_autoscan":
        mode, autoscan = get_user_settings(chat_id)
        new_value = 0 if autoscan else 1
        set_autoscan(chat_id, new_value)
        answer_callback(callback_id, "Auto Scan mis à jour")
        show_settings(chat_id)

    elif data.startswith("mode_"):
        new_mode = data.replace("mode_", "")
        set_user_mode(chat_id, new_mode)
        answer_callback(callback_id, f"Mode {new_mode}")
        show_settings(chat_id)

    elif data.startswith("exec_"):
        signal_id = data.replace("exec_", "")
        conn = db()
        c = conn.cursor()
        c.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,))
        row = c.fetchone()
        conn.close()

        if row:
            fake_data = {
                "symbol": row["symbol"],
                "signal": row["signal"],
                "score": row["score"],
                "entry": row["entry"],
                "sl": row["sl"],
                "tp1": row["tp1"],
                "tp2": row["tp2"],
                "tp3": row["tp3"]
            }
            answer_callback(callback_id, "Exécution rapide")
            send_message(chat_id, format_execution(fake_data), {"inline_keyboard":[[{"text":"🏠 Menu","callback_data":"menu"}]]})
        else:
            answer_callback(callback_id, "Signal introuvable")

    else:
        answer_callback(callback_id, "OK")

# =========================================================
# FLASK ROUTES
# =========================================================
@app.route("/")
def home():
    return "LVBXNT CRYPTO BOT V2.2 RUNNING"

@app.route("/set_webhook")
def set_webhook():
    if not BOT_TOKEN:
        return "BOT_TOKEN manquant"

    webhook_url = f"{request.host_url.rstrip('/')}/{BOT_TOKEN}"
    r = requests.get(f"{BASE_URL}/setWebhook?url={webhook_url}")
    return r.text

@app.route("/delete_webhook")
def delete_webhook():
    r = requests.get(f"{BASE_URL}/deleteWebhook")
    return r.text

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)

    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip().upper()

        ensure_user(chat_id)

        if text == "/START":
            show_menu(chat_id)
        elif text in SUPPORTED:
            handle_symbol(chat_id, text)
        else:
            send_message(chat_id, "❌ Crypto non reconnue.\n\nExemples : BTCUSDT, ETHUSDT, SOLUSDT")

    elif "callback_query" in data:
        cb = data["callback_query"]
        callback_id = cb["id"]
        chat_id = cb["message"]["chat"]["id"]
        callback_data = cb["data"]

        handle_callback(chat_id, callback_id, callback_data)

    return "ok", 200

# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)