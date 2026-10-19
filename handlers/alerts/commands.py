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
    get_user_alert_history,
    get_user_alert_stats,
    set_user_last_alert_msg_id,
    add_alert_message_record,
    delete_alert_message_records,
)
from neptun_alerts import (
    forward_geocode,
    fetch_neptun_data,
    fetch_ukraine_alarm_data,
    evaluate_user_alert_state,
    format_alert_message,
    format_alerts_history_message,
    make_threat_signature,
    generate_alerts_map_image,
    get_daily_putin_phrase,
)
from keyboards import (
    get_alert_inline_keyboard,
    get_main_reply_keyboard,
    ALERT_REFRESH_COOLDOWN,
)
from handlers.common import check_access, delete_user_command_message
from handlers.alerts.state import (
    _user_last_alert_state,
    _user_last_threat_signature,
    _user_last_manual_refresh,
    _user_active_msg_ids,
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
        "📍 **Як встановити або змінити місто / локацію:**\n"
        "1️⃣ **Просто напишіть у цей чат назву вашого міста чи села** (наприклад: `Київ`, `Ірпінь`, `Полтава`)\n"
        "2️⃣ **Натисніть кнопку «📍 Надіслати мою геопозицію»** внизу екрана\n"
        "3️⃣ **Або оберіть область зі списку** нижче (після вибору області також можна уточнити місто або GPS)"
    )

    toggle_btn = (
        InlineKeyboardButton("🔕 Вимкнути сповіщення (/alerts_off)", callback_data="alerts_off")
        if is_enabled
        else InlineKeyboardButton("🔔 Увімкнути сповіщення (/alerts_on)", callback_data="alerts_on")
    )

    inline_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏙️ Вписати місто / село", callback_data="prompt_city"),
            InlineKeyboardButton("📍 Надіслати GPS", callback_data="prompt_gps")
        ],
        [InlineKeyboardButton("🗺️ Обрати область зі списку", callback_data="alerts_manual_page_0")],
        [InlineKeyboardButton("🗺️ Карта тривог України", callback_data="alerts_map_refresh")],
        [InlineKeyboardButton("🔍 Перевірити стан тривоги зараз", callback_data="alerts_check")],
        [toggle_btn],
        [InlineKeyboardButton("❌ Закрити меню", callback_data="close_menu")]
    ])

    sent_menu = await context.bot.send_message(
        chat_id=chat_id,
        text=msg_text,
        reply_markup=inline_kb,
        parse_mode="Markdown"
    )
    if sub and sub.get("last_alert_msg_id"):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=sub["last_alert_msg_id"])
        except Exception:
            pass
    if sub is not None:
        sub["last_alert_msg_id"] = sent_menu.message_id
    await set_user_last_alert_msg_id(user_id, sent_menu.message_id)
    await add_alert_message_record(user_id, chat_id, sent_menu.message_id)
    _user_active_msg_ids[user_id] = {sent_menu.message_id}

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
            reply_markup=get_main_reply_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "📍 **У вас ще не встановлено регіон для сповіщень!**\n\n"
            "Будь ласка, надішліть вашу геопозицію або оберіть область зі списку:",
            reply_markup=get_main_reply_keyboard(),
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
        reply_markup=get_main_reply_keyboard(),
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

        chat_id = update.effective_chat.id
        sent_photo = None
        with open(map_path, "rb") as photo_file:
            sent_photo = await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_file,
                caption=caption,
                reply_markup=inline_kb,
                parse_mode="Markdown"
            )

        if os.path.exists(map_path):
            os.remove(map_path)
        try:
            await status_msg.delete()
        except Exception:
            pass

        if sub and sub.get("last_alert_msg_id"):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=sub["last_alert_msg_id"])
            except Exception:
                pass
        if sent_photo:
            if sub is not None:
                sub["last_alert_msg_id"] = sent_photo.message_id
            await set_user_last_alert_msg_id(user_id, sent_photo.message_id)
            await add_alert_message_record(user_id, chat_id, sent_photo.message_id)
            _user_active_msg_ids[user_id] = {sent_photo.message_id}

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
        ua_data = await fetch_ukraine_alarm_data()
        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data, ua_data)

        prev_sig = _user_last_threat_signature.get(user_id)
        current_sig = make_threat_signature(current_state)

        _user_last_alert_state[user_id] = current_state["level"]
        _user_last_threat_signature[user_id] = current_sig

        sound_enabled = sub.get("sound_enabled", True) if sub else True
        putin_enabled = sub.get("putin_check_enabled", True) if sub else True
        msg_text = format_alert_message(sub, current_state, is_manual_check=True)
        alert_kb = get_alert_inline_keyboard(sound_enabled, putin_enabled)

        is_same_as_last = (current_msg_id == last_stored_msg_id)
        edited_inplace = False

        if is_same_as_last and current_msg_id:
            try:
                await query.edit_message_text(
                    text=msg_text,
                    reply_markup=alert_kb,
                    parse_mode="HTML",
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
        ua_data = await fetch_ukraine_alarm_data()
        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data, ua_data)
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

async def putin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник команди /putin — показує актуальний статус чи здох путін."""
    if not await check_access(update, context):
        return
    await delete_user_command_message(update)
    user_id = update.effective_user.id
    sub = await get_user_subscription(user_id)
    putin_enabled = sub.get("putin_check_enabled", True) if sub else True

    tz = pytz.timezone("Europe/Kiev")
    kyiv_now = datetime.now(tz)
    kyiv_today = kyiv_now.strftime("%d.%m.%Y")
    daily_phrase = get_daily_putin_phrase(kyiv_now)

    status_text = (
        f"☠️ <b>{kyiv_today}. Чи здох путін?</b>\n"
        f"<b>{daily_phrase}</b>\n\n"
        "• ЗСУ та сили оборони працюють над цим 24/7 🇺🇦\n"
        "• Бавовна неминуча 🔥\n\n"
        "💡 <i>Цей статус також відображається прямо у вашій картці тривог (/status). "
        "Його можна увімкнути або вимкнути кнопкою «☠️ Путін» під карткою.</i>"
    )
    toggle_btn = (
        InlineKeyboardButton("☠️ Путін у картці: ВИМК", callback_data="toggle_putin")
        if putin_enabled
        else InlineKeyboardButton("☠️ Путін у картці: УВІМК", callback_data="toggle_putin")
    )
    kb = InlineKeyboardMarkup([[toggle_btn]])
    await update.effective_message.reply_text(
        status_text,
        reply_markup=kb,
        parse_mode="HTML"
    )

async def alerts_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник команди /history — показує історію тривог та статистику для району користувача."""
    if not await check_access(update, context):
        return
    await delete_user_command_message(update)
    user_id = update.effective_user.id
    sub = await get_user_subscription(user_id)
    if not sub or not sub.get("oblast"):
        await update.effective_message.reply_text(
            "📍 Спочатку вкажіть ваше місто або область для перегляду історії:\n/alerts",
            parse_mode="HTML"
        )
        return

    records = await get_user_alert_history(user_id, limit=5)
    stats = await get_user_alert_stats(user_id, days=7)
    msg_text = format_alerts_history_message(sub, records, stats)

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Оновити", callback_data="alerts_history"),
            InlineKeyboardButton("📊 Статус", callback_data="refresh_alert_status")
        ],
        [
            InlineKeyboardButton("⚙️ Меню", callback_data="alerts_settings_menu")
        ]
    ])
    chat_id = update.effective_chat.id
    sent_history = await context.bot.send_message(
        chat_id=chat_id,
        text=msg_text,
        reply_markup=kb,
        parse_mode="HTML"
    )
    if sub and sub.get("last_alert_msg_id"):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=sub["last_alert_msg_id"])
        except Exception:
            pass
    if sub is not None:
        sub["last_alert_msg_id"] = sent_history.message_id
    await set_user_last_alert_msg_id(user_id, sent_history.message_id)
    await add_alert_message_record(user_id, chat_id, sent_history.message_id)
    _user_active_msg_ids[user_id] = {sent_history.message_id}
