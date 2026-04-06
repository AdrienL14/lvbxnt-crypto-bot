# 🔥 SEULEMENT PARTIE INLINE / CALLBACK REMASTER

# =========================
# KEYBOARDS
# =========================

def main_keyboard(chat_id):
    auto = "🚨 Auto Scan ON" if is_auto_scan_enabled(chat_id) else "🚨 Auto Scan OFF"
    return {
        "inline_keyboard": [
            [
                {"text": "🧠 Analyse Premium", "callback_data": "analyse"},
                {"text": auto, "callback_data": "autoscan"}
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


def settings_keyboard(chat_id):
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
            [{"text": f"🎯 Mode actuel: {mode}", "callback_data": "noop"}],
            [{"text": "🔙 Retour menu", "callback_data": "back_main"}]
        ]
    }


def watchlist_keyboard(chat_id):
    current = get_watchlist(chat_id)
    keyboard = [
        [
            {"text": "🔥 Tout sélectionner", "callback_data": "watch_all"},
            {"text": "♻️ Reset", "callback_data": "watch_reset"}
        ]
    ]

    for symbol in SUPPORTED:
        mark = "✅" if symbol in current else "➕"
        keyboard.append([
            {"text": f"{mark} {symbol}", "callback_data": f"toggle:{symbol}"}
        ])

    keyboard.append([{"text": "🔙 Retour menu", "callback_data": "back_main"}])
    return {"inline_keyboard": keyboard}


# =========================
# CALLBACK HANDLER (PRO)
# =========================

def handle_callback(chat_id, message_id, callback_id, action):
    ensure_user(chat_id)
    answer_callback(callback_id)

    if action == "noop":
        return

    # =========================
    # MENU
    # =========================
    if action == "back_main":
        edit_message(chat_id, message_id, "🏠 Menu principal", main_keyboard(chat_id))

    elif action == "analyse":
        send_message(chat_id, "📩 Envoie une crypto (ex: BTCUSDT)", main_keyboard(chat_id))

    # =========================
    # AUTO SCAN (PRO CLEAN)
    # =========================
    elif action == "autoscan":
        current = is_auto_scan_enabled(chat_id)
        set_auto_scan(chat_id, not current)

        status = "activé" if not current else "désactivé"
        edit_message(
            chat_id,
            message_id,
            f"🤖 Auto Scan {status}",
            main_keyboard(chat_id)
        )

    # =========================
    # SETTINGS
    # =========================
    elif action == "settings":
        edit_message(chat_id, message_id, format_settings_text(chat_id), settings_keyboard(chat_id))

    elif action.startswith("mode:"):
        mode = action.split(":", 1)[1]
        set_user_mode(chat_id, mode)
        edit_message(chat_id, message_id, format_settings_text(chat_id), settings_keyboard(chat_id))

    # =========================
    # WATCHLIST
    # =========================
    elif action == "watchlist":
        edit_message(chat_id, message_id, format_watchlist_text(chat_id), watchlist_keyboard(chat_id))

    elif action == "watch_all":
        select_all_watchlist(chat_id)
        edit_message(chat_id, message_id, format_watchlist_text(chat_id), watchlist_keyboard(chat_id))

    elif action == "watch_reset":
        reset_watchlist(chat_id)
        edit_message(chat_id, message_id, format_watchlist_text(chat_id), watchlist_keyboard(chat_id))

    elif action.startswith("toggle:"):
        symbol = action.split(":", 1)[1]
        current = get_watchlist(chat_id)

        if symbol in current:
            remove_from_watchlist(chat_id, symbol)
        else:
            add_to_watchlist(chat_id, symbol)

        edit_message(chat_id, message_id, format_watchlist_text(chat_id), watchlist_keyboard(chat_id))

    # =========================
    # SIGNAL HISTORY
    # =========================
    elif action == "signals":
        send_message(chat_id, format_last_signals(chat_id), main_keyboard(chat_id))

    # =========================
    # EXECUTION
    # =========================
    elif action.startswith("exec:"):
        symbol = action.split(":", 1)[1]
        mode = get_user_mode(chat_id)
        sig = analyze_symbol(symbol, mode)
        send_message(chat_id, format_execution(sig), signal_menu(symbol))