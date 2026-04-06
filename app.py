import os
import requests
import numpy as np
from flask import Flask, request

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

COINGECKO = os.getenv("COINGECKO_BASE")
BINANCE = os.getenv("BINANCE_BASE")
BYBIT = os.getenv("BYBIT_BASE")
DEX = os.getenv("DEXSCREENER_BASE")

app = Flask(__name__)

# =========================
# TELEGRAM
# =========================
def send(chat_id, text):
    requests.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    })

# =========================
# FETCH MULTI SOURCE
# =========================
def get_price(symbol):
    symbol = symbol.lower().replace("usdt","")

    # 1 COINGECKO
    try:
        r = requests.get(f"{COINGECKO}/simple/price?ids={symbol}&vs_currencies=usd").json()
        price = r[symbol]["usd"]
        return price
    except:
        pass

    # 2 BINANCE
    try:
        r = requests.get(f"{BINANCE}/api/v3/ticker/price?symbol={symbol.upper()}USDT").json()
        return float(r["price"])
    except:
        pass

    # 3 BYBIT
    try:
        r = requests.get(f"{BYBIT}/v5/market/tickers?category=spot&symbol={symbol.upper()}USDT").json()
        return float(r["result"]["list"][0]["lastPrice"])
    except:
        pass

    # 4 DEXSCREENER
    try:
        r = requests.get(f"{DEX}/pairs/ethereum/{symbol}").json()
        return float(r["pairs"][0]["priceUsd"])
    except:
        pass

    return None

# =========================
# ANALYSE SIMPLE
# =========================
def analyze(symbol):
    prices = []

    for i in range(50):
        p = get_price(symbol)
        if p:
            prices.append(p)

    if len(prices) < 10:
        return "NO TRADE", 0

    prices = np.array(prices)

    ema20 = prices.mean()
    ema50 = prices[:25].mean()

    rsi = 50 + (prices[-1] - prices.mean())

    if prices[-1] > ema20 and rsi > 55:
        return "BUY", 85
    elif prices[-1] < ema20 and rsi < 45:
        return "SELL", 80
    else:
        return "NO TRADE", 50

# =========================
# FORMAT
# =========================
def format_msg(symbol, signal, score, price):
    return f"""
💎 <b>LVBXNT CRYPTO SIGNAL</b>

🪙 {symbol}
📊 Signal: {signal}
⭐ Score: {score}/100

💰 Prix: {price}

🎯 Entry: {price}
🛑 SL: {round(price*0.98,4)}
🥇 TP1: {round(price*1.02,4)}
🥈 TP2: {round(price*1.04,4)}
🥉 TP3: {round(price*1.06,4)}

📱 Prêt à exécuter
"""

# =========================
# ROUTE TELEGRAM
# =========================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()

    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text","").upper()

        send(chat_id, f"⏳ Analyse {text}...")

        price = get_price(text)

        if not price:
            send(chat_id, "❌ Impossible de récupérer les données")
            return "ok"

        signal, score = analyze(text)

        msg = format_msg(text, signal, score, price)
        send(chat_id, msg)

    return "ok"

# =========================
# ROOT
# =========================
@app.route("/")
def home():
    return "BOT ONLINE"

# =========================
# START
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)