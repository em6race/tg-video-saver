import os
import html
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

ALERT_REFRESH_COOLDOWN = 3   # Секунди кулдауну для ручного оновлення

def get_alert_inline_keyboard(sound_enabled: bool = True, putin_enabled: bool = True) -> InlineKeyboardMarkup:
    """Повертає головну інлайн-клавіатуру для картки статусу тривог."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Оновити", callback_data="refresh_alert_status"),
            InlineKeyboardButton("🗺️ Карта", callback_data="alerts_map_refresh")
        ],
        [
            InlineKeyboardButton("📜 Історія", callback_data="alerts_history"),
            InlineKeyboardButton("⚙️ Меню", callback_data="alerts_settings_menu")
        ]
    ])

def get_alert_settings_keyboard(sound_enabled: bool = True, putin_enabled: bool = True) -> InlineKeyboardMarkup:
    """Повертає інлайн-кнопки меню налаштувань (місто, звук, путін, назад до статусу)."""
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
            InlineKeyboardButton("📍 Змінити місто / локацію", callback_data="open_alerts")
        ],
        [
            sound_btn,
            putin_btn
        ],
        [
            InlineKeyboardButton("🔙 Назад до статусу", callback_data="back_to_status")
        ]
    ])

def build_alert_settings_view(sub: dict | None) -> tuple[str, InlineKeyboardMarkup]:
    """Формує текст та клавіатуру окремого вікна меню налаштувань."""
    sound_enabled = sub.get("sound_enabled", True) if sub else True
    putin_enabled = sub.get("putin_check_enabled", True) if sub else True

    oblast = (sub.get("oblast") or "") if sub else ""
    raion = (sub.get("raion") or "") if sub else ""
    locality = (sub.get("locality") or "") if sub else ""

    loc_parts = [p for p in [locality, raion, oblast] if p]
    unique_loc = []
    for p in loc_parts:
        p_clean = p.replace(" область", " обл.").replace(" район", " р-н")
        if p_clean not in unique_loc:
            unique_loc.append(p_clean)
    loc_display = html.escape(", ".join(unique_loc)) if unique_loc else "не встановлено"

    sound_str = "🔔 Увімкнено (зі звуком)" if sound_enabled else "🔕 Вимкнено (тихий режим)"
    putin_str = "✅ Відображається в картці" if putin_enabled else "❌ Приховано з картки"

    text = (
        "⚙️ <b>Меню налаштувань моніторингу:</b>\n\n"
        f"• 📍 <b>Локація:</b> {loc_display}\n"
        f"• 🔔 <b>Звук сповіщень:</b> {sound_str}\n"
        f"• ☠️ <b>Статус путіна:</b> {putin_str}\n\n"
        "<i>Оберіть параметри або змініть місто кнопками нижче:</i>"
    )
    kb = get_alert_settings_keyboard(sound_enabled, putin_enabled)
    return text, kb

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Повертає адаптивну меню-клавіатуру внизу екрана Telegram залежно від конфігурації плагінів."""
    alerts_on = os.getenv("ENABLE_ALERTS_PLUGIN", "true").strip().lower() in ("1", "true", "yes", "on")

    if alerts_on:
        return ReplyKeyboardMarkup([
            [
                KeyboardButton("📊 Статус тривоги"),
                KeyboardButton("🗺️ Карта тривог")
            ],
            [
                KeyboardButton("📜 Історія тривог"),
                KeyboardButton("🚨 Налаштувати тривоги")
            ],
            [
                KeyboardButton("📍 Надіслати мою геопозицію", request_location=True),
                KeyboardButton("❓ Довідка")
            ]
        ], resize_keyboard=True)
    else:
        return ReplyKeyboardMarkup([
            [
                KeyboardButton("❓ Довідка")
            ]
        ], resize_keyboard=True)
