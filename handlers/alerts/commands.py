import os
import time
import logging
from datetime import datetime
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes

from database import (
    set_awaiting_location,
    disable_user_alerts,
    enable_user_alerts,
    get_user_subscription,
)
from neptun_alerts import (
    forward_geocode,
    fetch_neptun_data,
    evaluate_user_alert_state,
    format_alert_message,
    make_threat_signature,
    generate_alerts_map_image,
)
from keyboards import (
    get_alert_inline_keyboard,
    ALERT_REFRESH_COOLDOWN,
)
from handlers.common import check_access, delete_user_command_message
from handlers.alerts.state import (
    _user_last_alert_state,
    _user_last_threat_signature,
    _user_last_manual_refresh,
    _user_last_threat_ids,
)
from handlers.alerts.location import prompt_location_confirmation
from handlers.alerts.monitoring import repost_alert_message_to_bottom

logger = logging.getLogger(__name__)

async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    await delete_user_command_message(update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    args = context.args
    if args and len(args) > 0:
        arg_lower = args[0].lower().strip()
        if arg_lower in ("on", "1", "start", "enable", "увімк", "увімкнути"):
            await alerts_on_command(update, context)
            return
        elif arg_lower in ("off", "0", "stop", "disable", "вимк", "вимкнути"):
            await alerts_off_command(update, context)
            return
        else:
            query = " ".join(args).strip()
            status_msg = await update.message.reply_text(f"🔍 Шукаю населений пункт «{query}»...")
            geo = await forward_geocode(query)
            if geo:
                await status_msg.delete()
                await prompt_location_confirmation(update, context, geo)
                return
            else:
                await status_msg.edit_text(
                    f"❌ Не вдалося знайти населений пункт «{query}» в Україні 🇺🇦\n\n"
                    "Спробуйте надіслати геопозицію кнопкою або оберіть область зі списку нижче:"
                )

    sub = await get_user_subscription(user_id)
    await set_awaiting_location(user_id, chat_id, True)

    is_enabled = sub and sub.get("enabled")
    status_str = "🟢 **Увімкнено**" if is_enabled else "⚪ **Вимкнено**"
    loc_str = "не встановлено"
    if sub and sub.get("oblast"):
        loc_parts = [p for p in [sub.get("locality"), sub.get("raion"), sub.get("oblast")] if p]
        loc_str = ", ".join(loc_parts) if loc_parts else sub.get("oblast", "")

    msg_text = (
        "🚨 **Налаштування сповіщень про повітряну тривогу**\n\n"
        f"• Статус сповіщень: {status_str}\n"
        f"• Ваша поточна локація: **{loc_str}**\n\n"
        "⚡ **Керування командами:**\n"
        "• `/alerts_on` — увімкнути сповіщення\n"
        "• `/alerts_off` — вимкнути сповіщення\n"
        "• `/map` — переглянути карту тривог\n\n"
        "📍 **Як встановити або оновити вашу локацію:**\n"
        "1️⃣ **Просто напишіть у цей чат назву вашого міста чи села** (наприклад: `Київ`, `Ірпінь`, `Полтава`)\n"
        "2️⃣ **Оберіть область зі списку** (кнопка «🗺️ Обрати область вручну» нижче)\n"
        "3️⃣ **Або надішліть GPS-координати** за допомогою кнопки «📍 Надіслати мою геопозицію» внизу екрана"
    )

    toggle_btn = (
        InlineKeyboardButton("🔕 Вимкнути сповіщення (/alerts_off)", callback_data="alerts_off")
        if is_enabled
        else InlineKeyboardButton("🔔 Увімкнути сповіщення (/alerts_on)", callback_data="alerts_on")
    )

    inline_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗺️ Обрати область вручну", callback_data="alerts_manual_page_0")],
        [InlineKeyboardButton("🗺️ Карта тривог України", callback_data="alerts_map_refresh")],
        [InlineKeyboardButton("🔍 Перевірити стан тривоги зараз", callback_data="alerts_check")],
        [toggle_btn],
        [InlineKeyboardButton("❌ Закрити меню", callback_data="close_menu")]
    ])

    await update.effective_message.reply_text(
        msg_text,
        reply_markup=inline_kb,
        parse_mode="Markdown"
    )

async def alerts_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    await delete_user_command_message(update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if await enable_user_alerts(user_id, chat_id):
        sub = await get_user_subscription(user_id)
        loc_parts = [p for p in [sub.get("locality"), sub.get("raion"), sub.get("oblast")] if p]
        loc_str = ", ".join(loc_parts) if loc_parts else sub.get("oblast", "")
        await update.message.reply_text(
            f"🔔 **Сповіщення про повітряну тривогу успішно УВІМКНЕНО!**\n\n"
            f"📍 **Регіон моніторингу:** {loc_str}\n\n"
            "Тепер ви будете оперативно отримувати сповіщення про загрози (🔴 червоний / 🟡 жовтий рівень) та відбої.\n"
            "Вимкнути можна будь-коли командою `/alerts_off`.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "📍 **У вас ще не встановлено регіон для сповіщень!**\n\n"
            "Будь ласка, надішліть вашу геопозицію або оберіть область зі списку:",
            parse_mode="Markdown"
        )
        await alerts_command(update, context)

async def alerts_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    await delete_user_command_message(update)
    user_id = update.effective_user.id
    await disable_user_alerts(user_id)
    _user_last_alert_state.pop(user_id, None)

    await update.message.reply_text(
        "🔕 **Сповіщення про повітряну тривогу ВИМКНЕНО.**\n\n"
        "Ви не отримуватимете автоматичних сповіщень. Увімкнути знову можна командою `/alerts_on` або `/alerts`.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )

async def map_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    await delete_user_command_message(update)
    user_id = update.effective_user.id
    now = time.time()
    last_refresh = _user_last_manual_refresh.get(user_id, 0)
    time_since = now - last_refresh
    if time_since < ALERT_REFRESH_COOLDOWN:
        remaining = int(ALERT_REFRESH_COOLDOWN - time_since) + 1
        await update.message.reply_text(f"⏳ Зачекайте ще {remaining} сек перед наступним оновленням карти.")
        return

    _user_last_manual_refresh[user_id] = now
    sub = await get_user_subscription(user_id)

    status_msg = await update.message.reply_text("⏳ Малюю актуальну карту повітряних тривог України...")
    try:
        alerts_data, threats_data = await fetch_neptun_data(force=True)
        map_path, caption = await generate_alerts_map_image(alerts_data, threats_data, sub)

        inline_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Оновити карту", callback_data="alerts_map_refresh")],
            [InlineKeyboardButton("🌐 Жива онлайн-карта (NEPTUN)", url="https://neptun.in.ua/")],
            [
                InlineKeyboardButton("🏠 В меню", callback_data="open_alerts"),
                InlineKeyboardButton("❌ Закрити карту", callback_data="close_map")
            ]
        ])

        with open(map_path, "rb") as photo_file:
            await update.message.reply_photo(
                photo=photo_file,
                caption=caption,
                reply_markup=inline_kb,
                parse_mode="Markdown"
            )

        if os.path.exists(map_path):
            os.remove(map_path)
        await status_msg.delete()

    except Exception as e:
        logger.error(f"Error generating map: {e}")
        await status_msg.edit_text(f"❌ Не вдалося створити карту: `{e}`", parse_mode="Markdown")

async def handle_manual_alert_refresh(query, user_id: int) -> None:
    """Обробляє ручне оновлення інформації про тривогу користувачем з кулдауном."""
    now = time.time()
    last_refresh = _user_last_manual_refresh.get(user_id, 0)
    time_since = now - last_refresh

    if time_since < ALERT_REFRESH_COOLDOWN:
        remaining = int(ALERT_REFRESH_COOLDOWN - time_since) + 1
        await query.answer(f"⏳ Зачекайте {remaining} сек перед наступним оновленням!", show_alert=False)
        return

    sub = await get_user_subscription(user_id)
    if not sub or not sub.get("oblast"):
        await query.answer("📍 Спочатку вкажіть вашу локацію через /alerts", show_alert=True)
        return

    _user_last_manual_refresh[user_id] = now
    current_msg_id = query.message.message_id if query.message else None
    last_stored_msg_id = sub.get("last_alert_msg_id")

    try:
        alerts_data, threats_data = await fetch_neptun_data(force=True)
        prev_threat_ids = _user_last_threat_ids.get(user_id)
        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data, prev_threat_ids=prev_threat_ids)
        _user_last_threat_ids[user_id] = set(current_state.get("current_threat_ids", []))

        prev_sig = _user_last_threat_signature.get(user_id)
        current_sig = make_threat_signature(current_state)

        _user_last_alert_state[user_id] = current_state["level"]
        _user_last_threat_signature[user_id] = current_sig

        sound_enabled = sub.get("sound_enabled", True) if sub else True
        msg_text = format_alert_message(sub, current_state, is_manual_check=True)
        alert_kb = get_alert_inline_keyboard(sound_enabled)

        is_same_as_last = (current_msg_id == last_stored_msg_id)
        edited_inplace = False

        if is_same_as_last and current_msg_id:
            try:
                await query.edit_message_text(
                    text=msg_text,
                    reply_markup=alert_kb,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
                edited_inplace = True
            except Exception as edit_err:
                logger.debug(f"In-place edit on refresh failed ({edit_err}), falling back to repost...")
                edited_inplace = False

        if not edited_inplace:
            await repost_alert_message_to_bottom(
                bot=query.get_bot(),
                chat_id=query.message.chat_id,
                user_id=user_id,
                sub=sub,
                current_state=current_state,
                is_manual_check=True,
                extra_msg_id_to_delete=current_msg_id
            )

        now_tz = datetime.now(pytz.timezone("Europe/Kiev")).strftime("%H:%M:%S")
        if current_sig != prev_sig:
            await query.answer(f"🚨 Є оновлення у небі! ({now_tz})", show_alert=False)
        else:
            await query.answer(f"✅ Оновлено ({now_tz})", show_alert=False)

    except Exception as e:
        logger.error(f"Error in manual alert refresh: {e}", exc_info=True)
        try:
            await query.answer(f"❌ Не вдалося оновити статус: {e}", show_alert=True)
        except Exception:
            pass

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /status або /check для ручної перевірки ситуації з кулдауном."""
    if not await check_access(update, context):
        return

    await delete_user_command_message(update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    now = time.time()
    last_refresh = _user_last_manual_refresh.get(user_id, 0)
    time_since = now - last_refresh

    if time_since < ALERT_REFRESH_COOLDOWN:
        remaining = int(ALERT_REFRESH_COOLDOWN - time_since) + 1
        await update.message.reply_text(f"⏳ Зачекайте ще {remaining} сек перед наступним оновленням статусу.")
        return

    sub = await get_user_subscription(user_id)
    if not sub or not sub.get("oblast"):
        await update.message.reply_text(
            "📍 У вас ще не встановлено локацію для моніторингу!\n"
            "Скористайтеся командою /alerts, щоб вказати ваше місто.",
            parse_mode="Markdown"
        )
        return

    _user_last_manual_refresh[user_id] = now
    status_msg = await update.message.reply_text("🔍 Отримуємо актуальні дані про тривоги...")

    try:
        alerts_data, threats_data = await fetch_neptun_data(force=True)
        prev_threat_ids = _user_last_threat_ids.get(user_id)
        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data, prev_threat_ids=prev_threat_ids)
        _user_last_threat_ids[user_id] = set(current_state.get("current_threat_ids", []))
        _user_last_alert_state[user_id] = current_state["level"]
        _user_last_threat_signature[user_id] = make_threat_signature(current_state)

        try:
            await status_msg.delete()
        except Exception:
            pass

        await repost_alert_message_to_bottom(
            bot=context.bot,
            chat_id=chat_id,
            user_id=user_id,
            sub=sub,
            current_state=current_state,
            is_manual_check=True
        )
    except Exception as e:
        logger.error(f"Error in status_command: {e}")
        await status_msg.edit_text(f"❌ Не вдалося отримати статус: `{e}`", parse_mode="Markdown")
