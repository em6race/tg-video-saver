from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

ALERT_REFRESH_COOLDOWN = 3   # Секунди кулдауну для ручного оновлення
POWER_REFRESH_COOLDOWN = 3   # Секунди кулдауну для ручного оновлення графіка світла

def get_outage_inline_keyboard(notifications_enabled: bool = True, view_day: str = "today") -> InlineKeyboardMarkup:
    """Повертає інлайн-кнопки для графіка відключень світла (Yasno/DTEK)."""
    day_btn = (
        InlineKeyboardButton("📅 На завтра", callback_data="switch_power_day:tomorrow")
        if view_day == "today"
        else InlineKeyboardButton("📅 На сьогодні", callback_data="switch_power_day:today")
    )
    notif_btn = (
        InlineKeyboardButton("🔔 Сповіщення: УВІМК", callback_data="toggle_power_notif")
        if notifications_enabled
        else InlineKeyboardButton("🔕 Сповіщення: ВИМК", callback_data="toggle_power_notif")
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Оновити", callback_data="refresh_power_status"),
            day_btn
        ],
        [
            notif_btn
        ],
        [
            InlineKeyboardButton("⚙️ Змінити групу / адресу", callback_data="open_power_menu")
        ],
        [
            InlineKeyboardButton("❌ Закрити", callback_data="close_power_card")
        ]
    ])

def get_alert_inline_keyboard(sound_enabled: bool = True, putin_enabled: bool = True) -> InlineKeyboardMarkup:
    """Повертає інлайн-кнопки швидкого ручного оновлення, карти, звуку та статусу путіна."""
    sound_btn = (
        InlineKeyboardButton("🔔 Звук: УВІМК", callback_data="toggle_sound")
        if sound_enabled
        else InlineKeyboardButton("🔕 Звук: ВИМК", callback_data="toggle_sound")
    )
    putin_btn = (
        InlineKeyboardButton("☠️ Путін: УВІМК", callback_data="toggle_putin")
        if putin_enabled
        else InlineKeyboardButton("☠️ Путін: ВИМК", callback_data="toggle_putin")
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Оновити", callback_data="refresh_alert_status"),
            InlineKeyboardButton("🗺️ Карта", callback_data="alerts_map_refresh")
        ],
        [
            sound_btn,
            putin_btn
        ],
        [
            InlineKeyboardButton("📍 Змінити місто", callback_data="open_alerts")
        ]
    ])

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Повертає адаптивну меню-клавіатуру внизу екрана Telegram залежно від конфігурації плагінів."""
    import os
    alerts_on = os.getenv("ENABLE_ALERTS_PLUGIN", "true").strip().lower() in ("1", "true", "yes", "on")
    outages_on = os.getenv("ENABLE_OUTAGES_PLUGIN", "false").strip().lower() in ("1", "true", "yes", "on")

    if outages_on and not alerts_on:
        return ReplyKeyboardMarkup([
            [
                KeyboardButton("💡 Стан світла"),
                KeyboardButton("⚡ Графік відключень")
            ],
            [
                KeyboardButton("⚙️ Налаштувати групу/адресу"),
                KeyboardButton("❓ Довідка")
            ]
        ], resize_keyboard=True)
    elif alerts_on and not outages_on:
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
    elif alerts_on and outages_on:
        return ReplyKeyboardMarkup([
            [
                KeyboardButton("📊 Статус тривоги"),
                KeyboardButton("💡 Стан світла")
            ],
            [
                KeyboardButton("🗺️ Карта тривог"),
                KeyboardButton("⚡ Графік відключень")
            ],
            [
                KeyboardButton("🚨 Налаштувати тривоги"),
                KeyboardButton("⚙️ Налаштувати групу/адресу")
            ],
            [
                KeyboardButton("❓ Довідка")
            ]
        ], resize_keyboard=True)
    else:
        return ReplyKeyboardMarkup([
            [
                KeyboardButton("❓ Довідка")
            ]
        ], resize_keyboard=True)

