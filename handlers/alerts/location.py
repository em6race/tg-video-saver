import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes

from database import (
    set_awaiting_location,
    save_user_location,
    get_user_subscription,
    set_user_last_alert_msg_id,
)
from neptun_alerts import (
    reverse_geocode,
    fetch_neptun_data,
    evaluate_user_alert_state,
    format_alert_message,
    make_threat_signature,
)
from keyboards import get_alert_inline_keyboard
from handlers.common import check_access
from handlers.alerts.state import (
    _user_last_alert_state,
    _user_last_threat_signature,
)
from handlers.alerts.monitoring import repost_alert_message_to_bottom

logger = logging.getLogger(__name__)

async def prompt_location_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, geo: dict) -> None:
    """Виводить результат пошуку населеного пункту і запитує підтвердження користувача."""
    user_id = update.effective_user.id
    context.user_data["pending_geo"] = geo

    oblast = geo.get("oblast") or ""
    raion = geo.get("raion") or ""
    locality = geo.get("locality") or ""
    is_ukraine = geo.get("is_ukraine", True)
    country_name = geo.get("country_name", "")

    if not is_ukraine:
        msg_text = (
            f"🌍 **Виявлено локацію за межами України ({country_name or 'Закордон'}):**\n\n"
            f"📍 **Населений пункт:** {locality} ({oblast})\n\n"
            "⚠️ *Система моніторингу NEPTUN відстежує повітряний простір тільки в межах України 🇺🇦.*\n\n"
            "Якщо ви перебуваєте за кордоном, але хочете відстежувати тривоги для вашого рідного міста в Україні (для батьків чи друзів), "
            "будь ласка, надішліть назву українського міста (наприклад: `Київ`, `Харків`, `Одеса`) або оберіть область зі списку."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗺️ Обрати область в Україні", callback_data="alerts_manual_page_0")]
        ])
        await update.effective_message.reply_text(msg_text, reply_markup=kb, parse_mode="Markdown")
        return

    loc_parts = [p for p in [locality, raion, oblast] if p]
    unique_loc = []
    for p in loc_parts:
        if p not in unique_loc:
            unique_loc.append(p)
    loc_display = ", ".join(unique_loc)

    msg_text = (
        "📍 **Знайдено населений пункт:**\n\n"
        f"🏘 **Населений пункт:** {locality or '—'}\n"
        f"🏛 **Район:** {raion or '—'}\n"
        f"🗺 **Область:** {oblast or '—'}\n\n"
        f"👉 **Регіон для сповіщень:** `{loc_display}`\n\n"
        "Бажаєте підтвердити та встановити цю локацію для моніторингу тривог?"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Підтвердити та встановити", callback_data="confirm_pending_loc")],
        [InlineKeyboardButton("❌ Ні, шукати інше місто", callback_data="cancel_pending_loc")],
        [InlineKeyboardButton("🏠 В меню", callback_data="open_alerts")]
    ])

    await update.effective_message.reply_text(msg_text, reply_markup=kb, parse_mode="Markdown")

async def save_and_confirm_location(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, geo: dict) -> None:
    """Зберігає локацію користувача, відправляє підтвердження та поточний звіт."""
    oblast = geo.get("oblast") or ""
    raion = geo.get("raion") or ""
    locality = geo.get("locality") or ""
    lat = geo.get("lat")
    lon = geo.get("lon")

    await save_user_location(user_id, chat_id, oblast, raion, locality, lat, lon)
    await set_awaiting_location(user_id, chat_id, False)

    loc_parts = [p for p in [locality, raion, oblast] if p]
    unique_loc = []
    for p in loc_parts:
        if p not in unique_loc:
            unique_loc.append(p)
    loc_display = ", ".join(unique_loc)

    msg = update.effective_message
    await msg.reply_text(
        f"✅ **Локацію успішно збережено!**\n\n"
        f"📍 **Ваш регіон:** {loc_display}\n\n"
        "Тепер бот цілодобово моніторитиме повітряний простір для вашої локації і надсилатиме сповіщення про:\n"
        "🔴 **Червоний рівень** — ракетна/дронова тривога (пройдіть в укриття!)\n"
        "🟡 **Жовтий рівень** — загроза артобстрілу / підвищена небезпека\n"
        "🛸 **Повітряні цілі поруч** — тип (БпЛА/ракета), курс та відстань у км\n"
        "🟢 **Відбій тривоги** (повідомлення оновлюється на спокійний зелений статус)\n\n"
        "🧹 *У чаті підтримується лише одне повідомлення тривоги, яке автоматично оновлюється та очищується.*",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )

    try:
        alerts_data, threats_data = await fetch_neptun_data()
        sub = await get_user_subscription(user_id)
        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
        _user_last_alert_state[user_id] = current_state["level"]
        _user_last_threat_signature[user_id] = make_threat_signature(current_state)

        await repost_alert_message_to_bottom(
            bot=context.bot,
            chat_id=chat_id,
            user_id=user_id,
            sub=sub,
            current_state=current_state,
            is_manual_check=True
        )
    except Exception as e:
        logger.warning(f"Failed to fetch initial alert state: {e}")

async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    location = update.message.location
    if not location:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    sub = await get_user_subscription(user_id)
    if not sub or not sub.get("awaiting_location"):
        await update.message.reply_text(
            "⚠️ Щоб прив'язати або оновити геолокацію для тривог, спочатку активуйте цю функцію командою **/alerts** і натисніть кнопку передачі геопозиції!",
            parse_mode="Markdown"
        )
        return

    lat = location.latitude
    lon = location.longitude

    status_msg = await update.message.reply_text("⏳ Визначаю вашу область та район за координатами...")

    try:
        geo = await reverse_geocode(lat, lon)
        await status_msg.delete()
        await prompt_location_confirmation(update, context, geo)
    except Exception as e:
        logger.error(f"Error handling location: {e}")
        await status_msg.edit_text(
            f"❌ Не вдалося точно визначити область за GPS: `{e}`\n\n"
            "Будь ласка, оберіть вашу область вручну або напишіть назву міста через /alerts.",
            parse_mode="Markdown"
        )

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await set_awaiting_location(user_id, chat_id, False)

    sub = await get_user_subscription(user_id)
    if sub and sub.get("oblast"):
        try:
            alerts_data, threats_data = await fetch_neptun_data()
            current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
            _user_last_alert_state[user_id] = current_state["level"]
            _user_last_threat_signature[user_id] = make_threat_signature(current_state)

            await repost_alert_message_to_bottom(
                bot=context.bot,
                chat_id=chat_id,
                user_id=user_id,
                sub=sub,
                current_state=current_state,
                is_manual_check=True
            )
            return
        except Exception as e:
            logger.warning(f"Failed to repost status card on cancel_handler: {e}")

    await update.message.reply_text("Дію скасовано 👍", reply_markup=ReplyKeyboardRemove())
