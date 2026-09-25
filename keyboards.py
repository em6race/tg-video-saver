from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

ALERT_REFRESH_COOLDOWN = 3   # Секунди кулдауну для ручного оновлення

def get_alert_inline_keyboard(sound_enabled: bool = True) -> InlineKeyboardMarkup:
    """Повертає інлайн-кнопки швидкого ручного оновлення, карти та звукових сповіщень."""
    sound_btn = (
        InlineKeyboardButton("🔔 Звук: УВІМК", callback_data="toggle_sound")
        if sound_enabled
        else InlineKeyboardButton("🔕 Звук: ВИМК", callback_data="toggle_sound")
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Оновити", callback_data="refresh_alert_status"),
            InlineKeyboardButton("🗺️ Карта", callback_data="alerts_map_refresh")
        ],
        [
            sound_btn,
            InlineKeyboardButton("📍 Змінити місто", callback_data="open_alerts")
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
