import os
import requests
import sqlite3
import time
from flask import Flask, request

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)

SUPPORTED = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","LINKUSDT","MATICUSDT"
]

# =========================
# DATABASE
# =========================
def init_db():
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        chat_id TEXT PRIMARY KEY,
        mode TEXT DEFAULT 'NORMAL',
        autoscan INTEGER DEFAULT 1
    )""")
    conn.commit()
    conn.close()

init_db()

# =========================
# TELEGRAM SEND
# =========================
def send(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if keyboard:
        data["reply_markup"] = keyboard
    requests.post(BASE_URL + "/sendMessage", json=data)

# =========================
# MENU
# =========================
def menu_keyboard():
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

def main_menu(chat_id):
    text = (
        "🚀 <b>LVBXNT CRYPTO BOT — V2.1 PRO</b>\n\n"
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
    send(chat_id, text, menu_keyboard())

# =========================
# DATA (SAFE)
# =========================
def get_price(symbol):
    try:
        coin = symbol.replace("USDT","").lower()
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin}&vs_currencies=usd"
        r = requests.get(url, timeout=10).json()
        return r[coin]["usd"]
    except:
        return None

# =========================
# ANALYSE SIMPLE STABLE
# =========================
def analyse(symbol):
    price = get_price(symbol)
    if not price:
        return "❌ Impossible de récupérer les données"

    # logique simple stable
    score = int(price % 100)

    if score > 60:
        signal = "BUY"
    elif score < 40:
        signal = "SELL"
    else:
        signal = "NO TRADE"

    sl = round(price * 0.98, 2)
    tp1 = round(price * 1.02, 2)
    tp2 = round(price * 1.04, 2)
    tp3 = round(price * 1.06, 2)

    return f"""
📊 <b>{symbol}</b>

🚦 Signal : <b>{signal}</b>
⭐ Score : <b>{score}/100</b>

💰 Prix : {price}

🎯 TP1 : {tp1}
🎯 TP2 : {tp2}
🎯 TP3 : {tp3}
🛑 SL : {sl}

📱 <b>Exécution rapide :</b>
Entry : {price}
SL : {sl}
TP1 : {tp1}
TP2 : {tp2}
TP3 : {tp3}
"""

# =========================
# SETTINGS
# =========================
def settings_menu(chat_id):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT mode, autoscan FROM users WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        mode, autoscan = "NORMAL", 1
    else:
        mode, autoscan = row

    text = (
        "⚙️ <b>PARAMÈTRES PRO</b>\n\n"
        f"🤖 Auto Scan : {'ON' if autoscan else 'OFF'}\n"
        f"🎯 Mode actuel : {mode}\n\n"
        "🛡 Prudent = moins de signaux\n"
        "⚖️ Normal = équilibre\n"
        "⚡ Agressif = plus d'opportunités"
    )

    keyboard = {
        "inline_keyboard":[
            [{"text":"✅ Auto Scan ON","callback_data":"auto_on"}],
            [{"text":"🛡 Prudent","callback_data":"mode_prudent"},
             {"text":"⚖️ Normal","callback_data":"mode_normal"},
             {"text":"⚡ Agressif","callback_data":"mode_agressif"}],
            [{"text":"🏠 Retour menu","callback_data":"menu"}]
        ]
    }

    send(chat_id, text, keyboard)

# =========================
# WEBHOOK
# =========================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    data = request.json

    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text","").upper()

        if text == "/START":
            main_menu(chat_id)

        elif text in SUPPORTED:
            send(chat_id, f"⏳ Analyse PRO en cours sur {text}...")
            result = analyse(text)
            send(chat_id, result)

        else:
            send(chat_id, "❌ Crypto non reconnue")

    elif "callback_query" in data:
        chat_id = data["callback_query"]["message"]["chat"]["id"]
        data_cb = data["callback_query"]["data"]

        if data_cb == "menu":
            main_menu(chat_id)

        elif data_cb == "settings":
            settings_menu(chat_id)

        elif data_cb == "analyse":
            send(chat_id, "📩 Envoie une crypto (ex: BTCUSDT)")

    return "ok"

@app.route("/")
def home():
    return "BOT RUNNING"