from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

ALERT_REFRESH_COOLDOWN = 3   # Секунди кулдауну для ручного оновлення

def get_alert_inline_keyboard() -> InlineKeyboardMarkup:
    """Повертає інлайн-кнопки швидкого ручного оновлення та карти під повідомленням тривоги."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Оновити статус", callback_data="refresh_alert_status"),
            InlineKeyboardButton("🗺️ Карта тривог", callback_data="alerts_map_refresh")
        ]
    ])

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Повертає постійну меню-клавіатуру внизу екрана Telegram."""
    return ReplyKeyboardMarkup([
        [
            KeyboardButton("📊 Статус тривоги"),
            KeyboardButton("🗺️ Карта тривог")
        ],
        [
            KeyboardButton("🚨 Налаштувати тривоги"),
            KeyboardButton("❓ Довідка")
        ],
        [
            KeyboardButton("📍 Надіслати мою геопозицію", request_location=True)
        ]
    ], resize_keyboard=True)
