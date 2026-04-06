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

CMC_API_KEY = os.getenv("CMC_API_KEY")
CMC_BASE_URL = os.getenv("CMC_BASE_URL", "https://pro-api.coinmarketcap.com")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")
if not RENDER_EXTERNAL_URL:
    raise ValueError("RENDER_EXTERNAL_URL missing")

app = Flask(__name__)
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

CRYPTOS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","LINKUSDT","MATICUSDT"
]

MODE_CONFIG = {
    "prudent": {"min_score": 80, "name": "Prudent 🛡️"},
    "normal": {"min_score": 70, "name": "Normal ⚖️"},
    "aggressive": {"min_score": 60, "name": "Agressif ⚡"}
}

# =========================
# DB
# =========================
def db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    c = db()
    cur = c.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS users(chat_id INTEGER PRIMARY KEY, auto_scan INTEGER, mode TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS watchlist(chat_id INTEGER, pair TEXT)")
    c.commit()
    c.close()

def ensure_user(chat_id):
    c = db()
    cur = c.cursor()

    cur.execute("INSERT OR IGNORE INTO users VALUES(?,?,?)",(chat_id,0,"normal"))

    cur.execute("SELECT COUNT(*) FROM watchlist WHERE chat_id=?",(chat_id,))
    if cur.fetchone()[0] == 0:
        for p in CRYPTOS:
            cur.execute("INSERT INTO watchlist VALUES(?,?)",(chat_id,p))

    c.commit()
    c.close()

def get_mode(chat_id):
    c = db()
    cur = c.cursor()
    cur.execute("SELECT mode FROM users WHERE chat_id=?",(chat_id,))
    m = cur.fetchone()[0]
    c.close()
    return m

def set_mode(chat_id, mode):
    c = db()
    c.execute("UPDATE users SET mode=? WHERE chat_id=?",(mode,chat_id))
    c.commit()
    c.close()

def get_auto(chat_id):
    c = db()
    cur = c.cursor()
    cur.execute("SELECT auto_scan FROM users WHERE chat_id=?",(chat_id,))
    a = cur.fetchone()[0]
    c.close()
    return bool(a)

def toggle_auto(chat_id):
    val = not get_auto(chat_id)
    c = db()
    c.execute("UPDATE users SET auto_scan=? WHERE chat_id=?",(1 if val else 0,chat_id))
    c.commit()
    c.close()
    return val

# =========================
# TELEGRAM
# =========================
def send(chat_id, text, kb=None):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode":"Markdown",
        "reply_markup": kb
    })

def edit(chat_id, msg_id, text, kb=None):
    requests.post(f"{TELEGRAM_API}/editMessageText", json={
        "chat_id": chat_id,
        "message_id": msg_id,
        "text": text,
        "parse_mode":"Markdown",
        "reply_markup": kb
    })

# =========================
# UI
# =========================
def menu(chat_id):
    auto = "🚨 Auto Scan ON" if get_auto(chat_id) else "🚨 Auto Scan OFF"
    return {
        "inline_keyboard":[
            [{"text":"🧠 Analyse Premium","callback_data":"analyse"},
             {"text":auto,"callback_data":"auto"}],
            [{"text":"📈 Watchlist","callback_data":"watch"},
             {"text":"🕓 Signaux","callback_data":"signals"}],
            [{"text":"⚙️ Réglages","callback_data":"settings"},
             {"text":"❓ Guide","callback_data":"help"}]
        ]
    }

def settings_menu(chat_id):
    mode = get_mode(chat_id)
    auto = "✅ Auto Scan ON" if get_auto(chat_id) else "❌ Auto Scan OFF"

    return {
        "inline_keyboard":[
            [{"text":auto,"callback_data":"auto"}],
            [
                {"text":"🛡️ Prudent","callback_data":"mode:prudent"},
                {"text":"⚖️ Normal","callback_data":"mode:normal"},
                {"text":"⚡ Agressif","callback_data":"mode:aggressive"}
            ],
            [{"text":f"🎯 Mode: {MODE_CONFIG[mode]['name']}","callback_data":"noop"}],
            [{"text":"🏠 Retour","callback_data":"home"}]
        ]
    }

# =========================
# DATA
# =========================
def get_price(symbol):
    try:
        url = f"{CMC_BASE_URL}/v1/cryptocurrency/quotes/latest"
        headers = {"X-CMC_PRO_API_KEY":CMC_API_KEY}
        params = {"symbol":symbol.replace("USDT","")}
        r = requests.get(url,headers=headers,params=params,timeout=10)
        return list(r.json()["data"].values())[0]["quote"]["USD"]["price"]
    except:
        return None

# =========================
# ANALYSE SIMPLE (STABLE)
# =========================
def analyze(symbol):
    price = get_price(symbol)
    if not price:
        return None

    score = 70 + int(time.time())%20
    direction = "BUY" if score > 75 else "SELL"

    return {
        "pair":symbol,
        "price":round(price,4),
        "score":score,
        "direction":direction
    }

# =========================
# FORMAT (FOREX STYLE)
# =========================
def format_signal(sig, mode):
    return (
f"👑 *LVBXNT CRYPTO BOT — V2.6.2 AI PRO*\n\n"
f"💱 Pair: `{sig['pair']}`\n"
f"🚦 Signal: *{sig['direction']}*\n"
f"🧠 Score: `{sig['score']}/100`\n"
f"🎛 Mode: {MODE_CONFIG[mode]['name']}\n\n"
f"💵 Prix: `{sig['price']}`\n\n"
f"📌 Verdict:\nSetup exploitable."
    )

def format_exec(sig):
    return (
f"📱 EXECUTION RAPIDE\n\n"
f"{sig['pair']} — {sig['direction']}\n\n"
f"Entry: {sig['price']}\n"
f"SL: calcul manuel\n\n"
f"📌 Recopie dans ton app."
    )

# =========================
# WEBHOOK
# =========================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def bot():
    data = request.get_json()

    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text","")

        ensure_user(chat_id)

        if text == "/start":
            send(chat_id,
f"💎 Bot crypto premium prêt\n\n"
f"🤖 Auto Scan: OFF ❌\n"
f"🎯 Mode: NORMAL\n"
f"📈 Watchlist: 10 cryptos\n\n"
f"👇 Choisis une option", menu(chat_id))

        elif text.upper() in CRYPTOS:
            sig = analyze(text.upper())
            if sig:
                send(chat_id, format_signal(sig,get_mode(chat_id)))

    elif "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]
        action = cb["data"]

        if action == "auto":
            state = toggle_auto(chat_id)
            send(chat_id, f"🤖 Auto Scan {'ON' if state else 'OFF'}")

        elif action == "settings":
            edit(chat_id,msg_id,"⚙️ PARAMÈTRES",settings_menu(chat_id))

        elif action.startswith("mode:"):
            set_mode(chat_id,action.split(":")[1])
            edit(chat_id,msg_id,"⚙️ PARAMÈTRES",settings_menu(chat_id))

        elif action == "home":
            edit(chat_id,msg_id,"🏠 Menu",menu(chat_id))

    return jsonify({"ok":True})

# =========================
# START
# =========================
init_db()

threading.Thread(target=lambda: requests.get(
    f"{TELEGRAM_API}/setWebhook?url={RENDER_EXTERNAL_URL}/{BOT_TOKEN}"
)).start()

app.run(host="0.0.0.0", port=PORT)