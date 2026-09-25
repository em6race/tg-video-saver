import os
import sys
import time
import logging
import asyncio
import subprocess
from datetime import datetime
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes, Application

from database import (
    set_awaiting_location,
    save_user_location,
    disable_user_alerts,
    enable_user_alerts,
    get_user_subscription,
    get_all_enabled_subscriptions,
    set_user_last_alert_msg_id,
    is_admin
)
from neptun_alerts import (
    reverse_geocode,
    forward_geocode,
    fetch_neptun_data,
    evaluate_user_alert_state,
    format_alert_message,
    make_threat_signature,
    generate_alerts_map_image,
    UKRAINE_OBLASTS
)
from keyboards import (
    get_alert_inline_keyboard,
    get_main_reply_keyboard,
    ALERT_REFRESH_COOLDOWN
)
from handlers.common import check_access, delete_user_command_message, help_command
from handlers.media import process_video, process_audio

logger = logging.getLogger(__name__)
ADMIN_ID = os.getenv("ADMIN_ID")

# Пам'ять та кулдауни для моніторингу та оновлення тривог
_user_last_alert_state = {}         # user_id -> рівень ("green", "yellow", "red")
_user_last_threat_signature = {}    # user_id -> кортеж-сигнатура загроз
_user_last_auto_edit_time = {}      # user_id -> час останнього редагування ботом
_user_last_manual_refresh = {}      # user_id -> час останнього запиту користувача

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

    save_user_location(user_id, chat_id, oblast, raion, locality, lat, lon)
    set_awaiting_location(user_id, chat_id, False)

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
        sub = get_user_subscription(user_id)
        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
        _user_last_alert_state[user_id] = current_state["level"]
        _user_last_threat_signature[user_id] = make_threat_signature(current_state)

        report = format_alert_message(sub, current_state, is_manual_check=True)
        sent_status_msg = await msg.reply_text(
            f"📊 **Поточна ситуація на цей момент:**\n\n{report}",
            reply_markup=get_alert_inline_keyboard(),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        set_user_last_alert_msg_id(user_id, sent_status_msg.message_id)
    except Exception as e:
        logger.warning(f"Failed to fetch initial alert state: {e}")

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

    sub = get_user_subscription(user_id)
    set_awaiting_location(user_id, chat_id, True)

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

    if enable_user_alerts(user_id, chat_id):
        sub = get_user_subscription(user_id)
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
    disable_user_alerts(user_id)
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
    sub = get_user_subscription(user_id)

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

async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    location = update.message.location
    if not location:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    sub = get_user_subscription(user_id)
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
    set_awaiting_location(user_id, chat_id, False)
    await update.message.reply_text("Дію скасовано 👍", reply_markup=ReplyKeyboardRemove())

async def repost_alert_message_to_bottom(bot, chat_id: int, user_id: int, sub: dict, current_state: dict, is_manual_check: bool = False, prev_level: str = None, extra_msg_id_to_delete: int = None) -> None:
    """Видаляє попереднє повідомлення тривоги та надсилає НОВЕ насамкінець чату."""
    last_msg_id = sub.get("last_alert_msg_id")

    msg_ids_to_delete = set()
    if last_msg_id:
        msg_ids_to_delete.add(last_msg_id)
    if extra_msg_id_to_delete:
        msg_ids_to_delete.add(extra_msg_id_to_delete)

    for m_id in msg_ids_to_delete:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=m_id)
        except Exception as del_err:
            logger.debug(f"Could not delete old alert message {m_id} for user {user_id}: {del_err}")

    msg_text = format_alert_message(sub, current_state, is_manual_check=is_manual_check, prev_level=prev_level)
    alert_kb = get_alert_inline_keyboard()
    is_silent = (current_state["level"] == "green")

    try:
        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=msg_text,
            reply_markup=alert_kb,
            parse_mode="Markdown",
            disable_notification=is_silent,
            disable_web_page_preview=True
        )
        set_user_last_alert_msg_id(user_id, sent_msg.message_id)
    except Exception as send_err:
        logger.warning(f"Could not repost alert message to bottom for {chat_id}: {send_err}")

async def handle_manual_alert_refresh(query, user_id: int) -> None:
    """Обробляє ручне оновлення інформації про тривогу користувачем з кулдауном."""
    now = time.time()
    last_refresh = _user_last_manual_refresh.get(user_id, 0)
    time_since = now - last_refresh

    if time_since < ALERT_REFRESH_COOLDOWN:
        remaining = int(ALERT_REFRESH_COOLDOWN - time_since) + 1
        await query.answer(f"⏳ Зачекайте {remaining} сек перед наступним оновленням!", show_alert=False)
        return

    sub = get_user_subscription(user_id)
    if not sub or not sub.get("oblast"):
        await query.answer("📍 Спочатку вкажіть вашу локацію через /alerts", show_alert=True)
        return

    _user_last_manual_refresh[user_id] = now
    current_msg_id = query.message.message_id if query.message else None
    last_stored_msg_id = sub.get("last_alert_msg_id")

    try:
        alerts_data, threats_data = await fetch_neptun_data(force=True)
        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)

        prev_sig = _user_last_threat_signature.get(user_id)
        current_sig = make_threat_signature(current_state)

        _user_last_alert_state[user_id] = current_state["level"]
        _user_last_threat_signature[user_id] = current_sig

        msg_text = format_alert_message(sub, current_state, is_manual_check=True)
        alert_kb = get_alert_inline_keyboard()

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

    sub = get_user_subscription(user_id)
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
        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
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

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if not await check_access(update, context):
        await query.answer()
        return

    choice = query.data

    if choice == "open_alerts":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        set_awaiting_location(user_id, chat_id, False)
        try:
            if query.message:
                await query.message.delete()
        except Exception:
            pass
        await alerts_command(update, context)
        return

    elif choice == "close_menu":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        set_awaiting_location(user_id, chat_id, False)
        context.user_data.pop("pending_geo", None)
        try:
            await query.message.delete()
        except Exception:
            pass
        close_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="Меню закрито 👍",
            reply_markup=ReplyKeyboardRemove()
        )

        async def _auto_delete_notice(msg):
            await asyncio.sleep(2)
            try:
                await msg.delete()
            except Exception:
                pass

        asyncio.create_task(_auto_delete_notice(close_msg))
        return

    elif choice == "close_map":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    elif choice == "prompt_gps":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        set_awaiting_location(user_id, chat_id, True)
        await context.bot.send_message(
            chat_id=chat_id,
            text="📍 **Скористайтеся кнопкою «📍 Надіслати мою геопозицію» внизу екрана:**",
            reply_markup=get_main_reply_keyboard(),
            parse_mode="Markdown"
        )
        return

    elif choice == "open_help":
        await query.answer()
        await help_command(update, context)
        return

    elif choice in ("alerts_check", "refresh_alert_status"):
        user_id = query.from_user.id
        await handle_manual_alert_refresh(query, user_id)
        return

    elif choice == "alerts_map_refresh":
        user_id = query.from_user.id
        now = time.time()
        last_refresh = _user_last_manual_refresh.get(user_id, 0)
        time_since = now - last_refresh
        if time_since < ALERT_REFRESH_COOLDOWN:
            remaining = int(ALERT_REFRESH_COOLDOWN - time_since) + 1
            await query.answer(f"⏳ Зачекайте {remaining} сек перед оновленням карти!", show_alert=False)
            return

        _user_last_manual_refresh[user_id] = now
        sub = get_user_subscription(user_id)
        await query.answer("Оновлюю карту...")
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
                await query.message.reply_photo(
                    photo=photo_file,
                    caption=caption,
                    reply_markup=inline_kb,
                    parse_mode="Markdown"
                )
            if os.path.exists(map_path):
                os.remove(map_path)

            if query.message and query.message.photo:
                try:
                    await query.message.delete()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error refreshing map: {e}")
            await query.message.reply_text(f"❌ Не вдалося оновити карту: `{e}`")
        return

    elif choice == "alerts_on":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        if enable_user_alerts(user_id, chat_id):
            sub = get_user_subscription(user_id)
            loc_parts = [p for p in [sub.get("locality"), sub.get("raion"), sub.get("oblast")] if p]
            loc_str = ", ".join(loc_parts) if loc_parts else sub.get("oblast", "")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="open_alerts")]])
            await query.edit_message_text(f"🔔 **Сповіщення про повітряну тривогу успішно УВІМКНЕНО!**\n\n📍 Регіон: {loc_str}", reply_markup=kb, parse_mode="Markdown")
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="open_alerts")]])
            await query.edit_message_text("📍 Спочатку вкажіть вашу область (надішліть локацію або оберіть зі списку через команду /alerts).", reply_markup=kb)
        return

    elif choice == "alerts_off":
        await query.answer()
        user_id = query.from_user.id
        disable_user_alerts(user_id)
        _user_last_alert_state.pop(user_id, None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="open_alerts")]])
        await query.edit_message_text("🔕 **Сповіщення про повітряну тривогу ВИМКНЕНО.**\n\nУвімкнути знову можна командою /alerts_on.", reply_markup=kb, parse_mode="Markdown")
        return

    elif choice.startswith("alerts_manual_page_"):
        await query.answer()
        page = int(choice.split("_")[-1])
        per_page = 8
        total = len(UKRAINE_OBLASTS)
        start_idx = page * per_page
        end_idx = min(start_idx + per_page, total)

        buttons = []
        for i in range(start_idx, end_idx, 2):
            row = [InlineKeyboardButton(UKRAINE_OBLASTS[i], callback_data=f"set_oblast:{UKRAINE_OBLASTS[i]}")]
            if i + 1 < end_idx:
                row.append(InlineKeyboardButton(UKRAINE_OBLASTS[i + 1], callback_data=f"set_oblast:{UKRAINE_OBLASTS[i + 1]}"))
            buttons.append(row)

        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"alerts_manual_page_{page - 1}"))
        if end_idx < total:
            nav_row.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"alerts_manual_page_{page + 1}"))
        if nav_row:
            buttons.append(nav_row)

        buttons.append([
            InlineKeyboardButton("🏠 В меню", callback_data="open_alerts"),
            InlineKeyboardButton("❌ Закрити", callback_data="close_menu")
        ])

        await query.edit_message_text("Оберіть вашу область зі списку:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    elif choice == "confirm_pending_loc":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        pending_geo = context.user_data.get("pending_geo")
        set_awaiting_location(user_id, chat_id, False)
        if pending_geo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await save_and_confirm_location(update, context, user_id, chat_id, pending_geo)
            context.user_data.pop("pending_geo", None)
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Термін дії запиту минув. Будь ласка, введіть назву міста ще раз через /alerts.",
                reply_markup=ReplyKeyboardRemove()
            )
        return

    elif choice == "cancel_pending_loc":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        context.user_data.pop("pending_geo", None)
        set_awaiting_location(user_id, chat_id, False)
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Встановлення локації скасовано.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    elif choice.startswith("set_oblast:"):
        await query.answer()
        oblast_name = choice.split("set_oblast:")[1]
        user_id = query.from_user.id
        chat_id = query.message.chat_id

        save_user_location(user_id, chat_id, oblast_name, "", "", None, None)
        set_awaiting_location(user_id, chat_id, False)

        try:
            await query.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ **Область збережено:** {oblast_name}!\n\n"
                 "Тепер ви будете оперативно отримувати сповіщення про зміну рівнів тривог (🔴/🟡) та відбої.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )

        try:
            alerts_data, threats_data = await fetch_neptun_data()
            sub = get_user_subscription(user_id)
            current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
            _user_last_alert_state[user_id] = current_state["level"]
            _user_last_threat_signature[user_id] = make_threat_signature(current_state)

            report = format_alert_message(sub, current_state, is_manual_check=True)
            sent_status_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"📊 **Поточна ситуація на цей момент:**\n\n{report}",
                reply_markup=get_alert_inline_keyboard(),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            set_user_last_alert_msg_id(user_id, sent_status_msg.message_id)
        except Exception as e:
            logger.warning(f"Failed to fetch initial alert state after set_oblast: {e}")
        return

    await query.answer()
    if not query.message.reply_to_message or not query.message.reply_to_message.text:
        await query.edit_message_text(text="❌ Помилка: не вдалося знайти оригінальне посилання. Відправте його ще раз.")
        return

    url = query.message.reply_to_message.text
    await query.edit_message_text(text="⏳ Завантажую, зачекайте...")

    typing_task = asyncio.create_task(send_typing_action(query.message.chat_id, context))

    try:
        if choice == "yt_video":
            await process_video(query.message.reply_to_message, context, url)
        elif choice == "yt_audio":
            await process_audio(query.message.reply_to_message, context, url)
        elif choice == "yt_both":
            await process_video(query.message.reply_to_message, context, url)
            await process_audio(query.message.reply_to_message, context, url)
    finally:
        typing_task.cancel()
        try:
            await query.message.delete()
        except Exception:
            pass

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    text = update.message.text.strip() if update.message and update.message.text else ""
    if not text:
        return

    if text in ("📊 Статус тривоги", "Статус тривоги", "/status", "/check"):
        await status_command(update, context)
        return
    elif text in ("🗺️ Карта тривог", "Карта тривог", "/map"):
        await map_command(update, context)
        return
    elif text in ("🚨 Налаштувати тривоги", "Налаштувати тривоги", "/alerts"):
        await alerts_command(update, context)
        return
    elif text in ("❓ Довідка", "❓ Довідка та допомога", "Довідка", "/help"):
        await help_command(update, context)
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sub = get_user_subscription(user_id)

    if sub and sub.get("awaiting_location"):
        status_msg = await update.message.reply_text(f"🔍 Шукаю населений пункт «{text}»...")
        geo = await forward_geocode(text)
        if geo:
            await status_msg.delete()
            await prompt_location_confirmation(update, context, geo)
            return
        else:
            await status_msg.edit_text(
                f"❌ Не вдалося знайти населений пункт «{text}» в Україні 🇺🇦\n\n"
                "Спробуйте:\n"
                "• Написати назву точніше (наприклад: *Бровари*, *м. Одеса*, *с. Софіївська Борщагівка*)\n"
                "• Або натиснути кнопку **«📍 Надіслати мою геопозицію»** внизу екрана\n"
                "• Або оберіть область вручну у /alerts",
                parse_mode="Markdown"
            )
            return

    await update.message.reply_text(
        "Будь ласка, надішліть посилання на медіа (TikTok, YouTube, Instagram тощо) або скористайтеся меню кнопок нижче 🚨",
        reply_markup=get_main_reply_keyboard()
    )

async def alerts_monitoring_loop(application: Application) -> None:
    """Фоновий процес опитування NEPTUN API та розсилки сповіщент підписникам."""
    logger.info("Starting NEPTUN alert monitoring loop...")
    while True:
        try:
            subs = get_all_enabled_subscriptions()
            if subs:
                alerts_data, threats_data = await fetch_neptun_data()

                if alerts_data or threats_data:
                    now = time.time()
                    for sub in subs:
                        user_id = sub["user_id"]
                        chat_id = sub["chat_id"]
                        last_msg_id = sub.get("last_alert_msg_id")

                        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
                        curr_level = current_state["level"]
                        prev_level = _user_last_alert_state.get(user_id)
                        current_sig = make_threat_signature(current_state)
                        prev_sig = _user_last_threat_signature.get(user_id)
                        last_auto_edit = _user_last_auto_edit_time.get(user_id, 0)
                        alert_kb = get_alert_inline_keyboard()

                        if prev_level is None:
                            _user_last_alert_state[user_id] = curr_level
                            _user_last_threat_signature[user_id] = current_sig
                            if curr_level != "green":
                                await repost_alert_message_to_bottom(
                                    bot=application.bot,
                                    chat_id=chat_id,
                                    user_id=user_id,
                                    sub=sub,
                                    current_state=current_state,
                                    is_manual_check=False
                                )
                            continue

                        if curr_level != prev_level:
                            _user_last_alert_state[user_id] = curr_level
                            _user_last_threat_signature[user_id] = current_sig
                            _user_last_auto_edit_time[user_id] = now

                            await repost_alert_message_to_bottom(
                                bot=application.bot,
                                chat_id=chat_id,
                                user_id=user_id,
                                sub=sub,
                                current_state=current_state,
                                is_manual_check=False,
                                prev_level=prev_level
                            )
                        elif curr_level != "green":
                            if (current_sig != prev_sig) and (now - last_auto_edit >= 60):
                                _user_last_threat_signature[user_id] = current_sig
                                _user_last_auto_edit_time[user_id] = now

                                msg_text = format_alert_message(sub, current_state, is_manual_check=False, prev_level=prev_level)
                                if last_msg_id:
                                    try:
                                        await application.bot.edit_message_text(
                                            chat_id=chat_id,
                                            message_id=last_msg_id,
                                            text=msg_text,
                                            reply_markup=alert_kb,
                                            parse_mode="Markdown",
                                            disable_web_page_preview=True
                                        )
                                    except Exception as edit_err:
                                        logger.debug(f"In-place edit failed ({edit_err}), reposting...")
                                        await repost_alert_message_to_bottom(
                                            bot=application.bot,
                                            chat_id=chat_id,
                                            user_id=user_id,
                                            sub=sub,
                                            current_state=current_state,
                                            is_manual_check=False,
                                            prev_level=prev_level
                                        )

        except Exception as e:
            logger.error(f"Error in alerts monitoring loop: {e}")

        await asyncio.sleep(10)

async def auto_update(application: Application) -> None:
    tz = pytz.timezone('Europe/Kiev')
    while True:
        await asyncio.sleep(60)
        now = datetime.now(tz)
        if now.hour == 3 and now.minute == 0:
            if ADMIN_ID:
                try:
                    await application.bot.send_message(chat_id=int(ADMIN_ID), text="🕒 3:00 ночі. Завантажую оновлення з GitHub і перезапускаюсь... 🔄")
                except Exception:
                    pass

            try:
                subprocess.run(["git", "pull"], check=True)
            except Exception as e:
                logger.error(f"Git pull failed: {e}")

            await asyncio.sleep(2)
            os.execv(sys.executable, [sys.executable] + sys.argv)
