import os
import sys
import logging
import re
import threading
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram import (
    Update,
    BotCommand,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)

from downloader import download_video, download_audio
from tiktok_api import fetch_tiktok_data
from spotify_api import get_spotify_track_info
from database import (
    get_cached_file_id,
    set_cached_file_id,
    is_admin,
    is_user_allowed,
    create_single_use_invite,
    redeem_invite,
    clear_unused_invites,
    get_allowed_users_list,
    set_awaiting_location,
    save_user_location,
    disable_user_alerts,
    enable_user_alerts,
    get_user_subscription,
    get_all_enabled_subscriptions,
    set_user_last_alert_msg_id
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
import pytz
from datetime import datetime
import subprocess
import time
import aiohttp
from PIL import Image
import io

# Load environment variables
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

if not TOKEN:
    raise ValueError("No BOT_TOKEN provided. Please set it in .env file.")

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Пам'ять та кулдауни для моніторингу та оновлення тривог
ALERT_REFRESH_COOLDOWN = 15  # Секунди кулдауну для ручного оновлення
_user_last_alert_state = {}         # user_id -> рівень ("green", "yellow", "red")
_user_last_threat_signature = {}    # user_id -> кортеж-сигнатура загроз
_user_last_auto_edit_time = {}      # user_id -> час останнього редагування ботом
_user_last_manual_refresh = {}      # user_id -> час останнього запиту користувача

def get_alert_inline_keyboard() -> InlineKeyboardMarkup:
    """Повертає інлайн-кнопки швидкого ручного оновлення та карти під повідомленням тривоги."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Оновити статус", callback_data="refresh_alert_status"),
            InlineKeyboardButton("🗺️ Карта тривог", callback_data="alerts_map_refresh")
        ]
    ])

async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Перевіряє, чи має користувач доступ до бота (приватний режим)."""
    if not update.effective_user:
        return False
    user_id = update.effective_user.id
    if is_user_allowed(user_id, ADMIN_ID):
        return True

    if update.effective_message:
        await update.effective_message.reply_text(
            "⛔ **Цей бот є приватним.**\n\n"
            "Використовувати його можуть лише адміністратор та користувачі за персональним посиланням-запрошенням.\n\n"
            "Зверніться до власника бота, щоб отримати доступ! 🎟️",
            parse_mode="Markdown"
        )
    return False

# --- КОМАНДИ ДОСТУПУ ТА АДМІНІСТРУВАННЯ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name or ""

    welcome_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚨 Налаштувати тривоги", callback_data="open_alerts")],
        [InlineKeyboardButton("🗺️ Карта тривог України", callback_data="alerts_map_refresh")],
        [InlineKeyboardButton("❓ Докладна інструкція", callback_data="open_help")]
    ])

    # Перевірка наявності інвайт-коду у /start invite_CODE
    args = context.args
    if args and len(args) > 0 and args[0].startswith("invite_"):
        invite_code = args[0].replace("invite_", "").strip()

        # Якщо користувач (наприклад, адмін) уже авторизований — не спалюємо інвайт!
        if is_user_allowed(user_id, ADMIN_ID):
            await update.message.reply_text(
                "✅ **Ви вже маєте повний доступ до цього бота!**\n\n"
                "💡 *Це одноразове посилання-запрошення залишилося активним для вашого друга.*",
                reply_markup=welcome_keyboard,
                parse_mode="Markdown"
            )
            return

        ok, status = redeem_invite(invite_code, user_id, username)
        if ok:
            await update.message.reply_text(
                "🎉 **Ласкаво просимо! Доступ активовано!**\n\n"
                "Ваше персональне запрошення успішно активовано. Тепер ви маєте повний доступ до всіх можливостей бота:\n\n"
                "🎥 **Завантаження медіа:** просто надішліть сюди посилання на TikTok, YouTube, Instagram, Twitter(X), Spotify або SoundCloud.\n"
                "🚨 **Повітряні тривоги:** налаштуйте сповіщення про небезпеку (🔴/🟡 рівні) для вашого міста чи району.\n\n"
                "👇 **З чого почати? Оберіть дію нижче:**",
                reply_markup=welcome_keyboard,
                parse_mode="Markdown"
            )
            return
        elif status == "expired":
            await update.message.reply_text(
                "⏳ **Термін дії цього запрошення минув (30 хвилин).**\n\n"
                "Посилання більше не дійсне. Будь ласка, попросіть власника бота надіслати вам нове посилання-запрошення.",
                parse_mode="Markdown"
            )
            return
        elif status == "already_used":
            await update.message.reply_text(
                "❌ **Помилка запрошення:** це одноразове посилання вже було використане іншою людиною."
            )
            return
        else:
            await update.message.reply_text(
                "❌ **Помилка запрошення:** це посилання недійсне або було скасоване."
            )
            return

    # Якщо користувач вже має доступ
    if is_user_allowed(user_id, ADMIN_ID):
        await update.message.reply_text(
            f"Привіт, {user.first_name}! 👋\n\n"
            "Я ваш персональний бот для завантаження медіа та оперативного моніторингу повітряних тривог 🚀\n\n"
            "📥 **Швидкий старт:**\n"
            "• Надішліть посилання на будь-яке відео чи трек для швидкого завантаження.\n"
            "• Натисніть **«🚨 Налаштувати тривоги»**, щоб увімкнути сповіщення для вашого району.",
            reply_markup=welcome_keyboard,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "⛔ **Цей бот є приватним.**\n\n"
            "Щоб отримати доступ, відкрийте персональне посилання-запрошення від адміністратора."
        )

async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return

    args = context.args
    if args and len(args) > 0 and args[0].lower() in ("clear", "clean", "reset", "очистити", "видалити"):
        deleted = clear_unused_invites()
        await update.message.reply_text(f"🗑️ Очищено **{deleted}** невикористаних посилань-запрошень з бази даних.", parse_mode="Markdown")
        return

    code, expires_iso = create_single_use_invite(int(user_id), duration_minutes=30)
    bot_info = await context.bot.get_me()
    invite_url = f"https://t.me/{bot_info.username}?start=invite_{code}"

    # Форматуємо час закінчення дії за київським часом
    try:
        tz = pytz.timezone("Europe/Kiev")
        exp_dt = datetime.fromisoformat(expires_iso).replace(tzinfo=pytz.utc).astimezone(tz)
        exp_str = exp_dt.strftime("%H:%M")
    except Exception:
        exp_str = "через 30 хв"

    share_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Відкрити та активувати бота", url=invite_url)],
        [InlineKeyboardButton("📤 Поділитися з другом", url=f"https://t.me/share/url?url={invite_url}")]
    ])

    await update.message.reply_text(
        "🎟️ <b>Персональне посилання-запрошення:</b>\n\n"
        f"👉 <a href=\"{invite_url}\">{invite_url}</a>\n\n"
        f"⏳ <i>Посилання дійсне протягом <b>30 хвилин</b> (до {exp_str}) та розраховане лише на <b>1 людину</b>.</i>\n\n"
        "💡 <i>Ви можете просто переслати це повідомлення другу в інший чат — йому буде достатньо лише клікнути на посилання чи кнопку нижче!</i>",
        reply_markup=share_kb,
        parse_mode="HTML",
        disable_web_page_preview=True
    )

async def clear_invites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return
    deleted = clear_unused_invites()
    await update.message.reply_text(f"🗑️ Видалено **{deleted}** невикористаних запрошень з бази даних.", parse_mode="Markdown")

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return

    users = get_allowed_users_list()
    if not users:
        await update.message.reply_text("Список користувачів порожній (окрім адміна).")
        return

    lines = ["👥 **Авторизовані користувачі бота:**\n"]
    for u_id, uname, joined in users:
        u_display = f"@{uname}" if uname else f"ID: `{u_id}`"
        lines.append(f"• {u_display} (додано: {joined[:10]})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(f"Ваш Telegram ID: `{user.id}`", parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    help_text = (
        "📖 **Повна інструкція з користування ботом**\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📥 **1. Як завантажувати відео та музику**\n"
        "Просто надішліть боту посилання на потрібне медіа:\n"
        "• **TikTok**: скачує відео без водяного знаку + окремо надсилає MP3 аудіо. Для фото-слайдів відправляє всі фото альбомом.\n"
        "• **YouTube**: запропонує зручні кнопки (🎬 Відео / 🎵 MP3 аудіо / 🎬+🎵 Обидва разом).\n"
        "• **Instagram / Twitter (X)**: завантажує Reels та відеопости.\n"
        "• **Spotify, SoundCloud, YT Music**: миттєво знаходить і відправляє готовий трек у форматі MP3 з обкладинкою.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚨 **2. Як налаштувати повітряні тривоги**\n"
        "1. Напишіть команду `/alerts` (або одразу `/alerts НазваМіста`, наприклад `/alerts Ірпінь`).\n"
        "2. Просто напишіть у чат назву вашого міста/села або натисніть велику кнопку **«📍 Надіслати мою геопозицію»** (також можна обрати область зі списку).\n"
        "3. Бот автоматично визначить координати, район та увімкне сповіщення!\n\n"
        "**Що означають сповіщення:**\n"
        "• 🔴 **Червоний рівень** — повітряна тривога! Загроза ракет, балістики, шахедів або КАБ (негайно в укриття!).\n"
        "• 🟡 **Жовтий рівень** — підвищена небезпека (проліт дронів на підльоті, загроза артобстрілу).\n"
        "• 🛸 **Цілі поруч** — бот вказує, що саме летить, курс та приблизну відстань у км до вас.\n"
        "• 🟢 **Відбій** — приходить тихо (без звуку), щоб не турбувати вночі.\n"
        "• 🧹 **Одне оновлюване повідомлення** — бот тримає чат чистим, автоматично редагуючи поточне повідомлення при зміні цілей/рівня небезпеки або відбої.\n"
        "• 🔄 **Ручне оновлення** — ви можете будь-коли натиснути кнопку **«🔄 Оновити статус»** прямо під повідомленням (діє захисний кулдаун 15 секунд, щоб не перевантажувати сервери).\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🗺 **3. Карта тривог та швидкі команди**\n"
        "• `/status` (або `/check`) — перевірити актуальну ситуацію у вашому районі.\n"
        "• `/map` — актуальна графічна HD-карта України з тривогами та цілями в небі.\n"
        "• `/alerts [місто]` — налаштувати локацію або переглянути меню.\n"
        "• `/alerts_on` — увімкнути сповіщення (якщо локація вже була задана).\n"
        "• `/alerts_off` — вимкнути сповіщення.\n"
        "• `/help` — переглянути цю інструкцію."
    )
    quick_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚨 Налаштувати тривоги", callback_data="open_alerts")],
        [InlineKeyboardButton("🗺️ Відкрити карту тривог", callback_data="alerts_map_refresh")]
    ])
    await update.message.reply_text(help_text, reply_markup=quick_kb, parse_mode="Markdown")

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return

    await update.message.reply_text("⏳ Завантажую оновлення з GitHub і перезавантажуюсь... 🔄")
    await asyncio.sleep(1)

    try:
        subprocess.run(["git", "pull"], check=True)
    except Exception as e:
        logger.error(f"Git pull failed during restart: {e}")

    os.execv(sys.executable, ['python'] + sys.argv)

# --- ПОВІТРЯНІ ТРИВОГИ ТА ГЕОЛОКАЦІЯ ---

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
        [InlineKeyboardButton("🔙 Назад до меню тривог", callback_data="open_alerts")]
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

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Підтримка команд /alerts on, /alerts off та /alerts <місто>
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
            # Текстовий пошук міста прямо з команди: /alerts Київ, /alerts Ірпінь тощо
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
        "2️⃣ Або натисніть кнопку **«📍 Надіслати мою геопозицію»** нижче\n"
        "3️⃣ Або оберіть область зі списку кнопок нижче:"
    )

    # Кнопка відправки геопозиції у Reply-клавіатурі
    reply_kb = ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Надіслати мою геопозицію", request_location=True)],
            [KeyboardButton("❌ Скасувати")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    # Динамічна кнопка Увімкнути / Вимкнути
    toggle_btn = (
        InlineKeyboardButton("🔕 Вимкнути сповіщення (/alerts_off)", callback_data="alerts_off")
        if is_enabled
        else InlineKeyboardButton("🔔 Увімкнути сповіщення (/alerts_on)", callback_data="alerts_on")
    )

    inline_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗺️ Карта тривог України", callback_data="alerts_map_refresh")],
        [InlineKeyboardButton("🔍 Перевірити стан тривоги зараз", callback_data="alerts_check")],
        [InlineKeyboardButton("🗺️ Обрати область вручну", callback_data="alerts_manual_page_0")],
        [toggle_btn],
        [InlineKeyboardButton("❌ Закрити меню", callback_data="close_menu")]
    ])

    await update.effective_message.reply_text(msg_text, reply_markup=reply_kb, parse_mode="Markdown")
    await update.effective_message.reply_text("Швидкі дії:", reply_markup=inline_kb)

async def alerts_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

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
            [InlineKeyboardButton("🌐 Жива онлайн-карта (NEPTUN)", url="https://neptun.in.ua/")]
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

# --- ОБРОБНИК МЕДІА ТА КЕШУ ---

async def send_typing_action(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.constants import ChatAction
    while True:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(4)

async def clean_old_downloads():
    downloads_dir = "downloads"
    if not os.path.exists(downloads_dir):
        os.makedirs(downloads_dir, exist_ok=True)
        return

    current_time = time.time()
    for filename in os.listdir(downloads_dir):
        file_path = os.path.join(downloads_dir, filename)
        try:
            if os.path.isfile(file_path) and current_time - os.path.getmtime(file_path) > 3600:
                os.remove(file_path)
            elif os.path.isdir(file_path) and current_time - os.path.getmtime(file_path) > 3600:
                import shutil
                shutil.rmtree(file_path)
        except Exception as e:
            logger.warning(f"Could not delete old file {file_path}: {e}")

async def process_audio(target_message, context, url):
    cached_id = get_cached_file_id(url, "audio")
    if cached_id:
        try:
            await target_message.reply_audio(audio=cached_id)
            logger.info(f"Served audio from cache for {url}")
            return
        except Exception as e:
            logger.warning(f"Failed to use cached audio for {url}, redownloading: {e}")

    try:
        if "spotify" in url:
            track_info = await get_spotify_track_info(url)
            search_query = f"ytsearch1:{track_info['search_query']} Topic"
            audio_result = await download_audio(search_query)
        else:
            audio_result = await download_audio(url)
            track_info = {
                "title": audio_result["title"],
                "performer": audio_result["performer"],
                "thumbnail_url": audio_result["thumbnail_url"]
            }

        file_path = audio_result["file_path"]

        thumb_path = None
        if track_info.get("thumbnail_url"):
            thumb_path = f"downloads/thumb_{int(time.time())}.jpg"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(track_info["thumbnail_url"]) as resp:
                        if resp.status == 200:
                            img_bytes = await resp.read()
                            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                            img.thumbnail((320, 320), Image.Resampling.LANCZOS)
                            img.save(thumb_path, "JPEG", quality=90)
            except Exception as e:
                logger.warning(f"Failed to download thumbnail: {e}")
                thumb_path = None

        with open(file_path, 'rb') as audio_file:
            thumb_file = open(thumb_path, 'rb') if thumb_path and os.path.exists(thumb_path) else None
            try:
                msg = await target_message.reply_audio(
                    audio=audio_file,
                    title=track_info["title"],
                    performer=track_info["performer"],
                    thumbnail=thumb_file,
                    write_timeout=60,
                    read_timeout=60,
                    pool_timeout=60
                )
                if msg and msg.audio:
                    set_cached_file_id(url, "audio", msg.audio.file_id)
            finally:
                if thumb_file:
                    thumb_file.close()

        if os.path.exists(file_path):
            os.remove(file_path)
        if thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)

    except Exception as e:
        logger.error(f"Error in process_audio: {e}")
        await target_message.reply_text(f"❌ Сталася помилка при завантаженні аудіо.\n\nДеталі помилки (перешліть розробнику):\n`{str(e)}`", parse_mode="Markdown")

async def process_video(target_message, context, url):
    cached_id = get_cached_file_id(url, "video")
    if cached_id:
        try:
            await target_message.reply_video(video=cached_id)
            logger.info(f"Served video from cache for {url}")
            return
        except Exception as e:
            logger.warning(f"Failed to use cached video for {url}, redownloading: {e}")

    try:
        file_path = await download_video(url)
        with open(file_path, 'rb') as video_file:
            try:
                msg = await target_message.reply_video(
                    video=video_file,
                    write_timeout=60,
                    read_timeout=60,
                    pool_timeout=60
                )
                if msg and msg.video:
                    set_cached_file_id(url, "video", msg.video.file_id)
            except Exception as vid_e:
                logger.warning(f"Failed to send as video: {vid_e}. Trying as document...")
                video_file.seek(0)
                msg = await target_message.reply_document(
                    document=video_file,
                    write_timeout=60,
                    read_timeout=60,
                    pool_timeout=60
                )
                if msg and msg.document:
                    set_cached_file_id(url, "video", msg.document.file_id)
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logger.error(f"Error in process_video: {e}")
        await target_message.reply_text(f"❌ Сталася помилка при завантаженні відео.\n\nДеталі помилки (перешліть розробнику):\n`{str(e)}`", parse_mode="Markdown")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    url = update.message.text
    await clean_old_downloads()

    try:
        supported_domains = [
            "tiktok.com",
            "youtube.com", "youtu.be",
            "instagram.com",
            "twitter.com", "x.com",
            "spotify.com", "spotify.link", "spotify:",
            "music.youtube.com",
            "soundcloud.com"
        ]

        if not any(domain in url.lower() for domain in supported_domains):
            await update.message.reply_text("Вибачте, але я поки що не підтримую завантаження з цього сайту 😔\n\nЯ вмію працювати з: **TikTok, YouTube, Instagram, Twitter(X), Spotify та SoundCloud**.", parse_mode="Markdown")
            return

        if any(x in url.lower() for x in ["/album/", "/playlist/", "/artist/", "/podcast/", "/show/", "/episode/", "/user/"]) and any(domain in url.lower() for domain in ["spotify", "music.youtube", "soundcloud"]):
            await update.message.reply_text("Будь ласка, надішліть посилання на **окремий трек** (пісню), і я з радістю його завантажу! 🎵", parse_mode="Markdown")
            return

        if any(domain in url.lower() for domain in ["youtube.com", "youtu.be"]):
            keyboard = [
                [
                    InlineKeyboardButton("🎬 Відео", callback_data="yt_video"),
                    InlineKeyboardButton("🎵 Аудіо", callback_data="yt_audio"),
                ],
                [
                    InlineKeyboardButton("🎬+🎵 Відео та Аудіо", callback_data="yt_both")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Оберіть, що саме ви хочете завантажити з YouTube:", reply_markup=reply_markup, reply_to_message_id=update.message.message_id)
            return

        status_msg = await update.message.reply_text("⏳ Завантажую, зачекайте...")
        typing_task = asyncio.create_task(send_typing_action(update.message.chat_id, context))

        try:
            if "tiktok.com" in url:
                try:
                    data = await fetch_tiktok_data(url)

                    if data["type"] == "slideshow":
                        media_group = []
                        opened_files = []
                        for i, photo_path in enumerate(data["media"]):
                            f = open(photo_path, 'rb')
                            opened_files.append(f)
                            media_group.append(InputMediaPhoto(f))

                        try:
                            await update.message.reply_media_group(media=media_group, write_timeout=60, read_timeout=60, pool_timeout=60)
                        finally:
                            for f in opened_files:
                                f.close()
                            for photo_path in data["media"]:
                                if os.path.exists(photo_path):
                                    os.remove(photo_path)

                        if data.get("audio"):
                            with open(data["audio"], 'rb') as audio_file:
                                await update.message.reply_audio(audio=audio_file, write_timeout=60, read_timeout=60, pool_timeout=60)
                            if os.path.exists(data["audio"]):
                                os.remove(data["audio"])
                    else:
                        video_path = data["media"][0]
                        with open(video_path, 'rb') as video_file:
                            try:
                                await update.message.reply_video(video=video_file, write_timeout=60, read_timeout=60, pool_timeout=60)
                            except Exception as vid_e:
                                logger.warning(f"Failed to send as video: {vid_e}. Trying as document...")
                                video_file.seek(0)
                                await update.message.reply_document(document=video_file, write_timeout=60, read_timeout=60, pool_timeout=60)
                        if os.path.exists(video_path):
                            os.remove(video_path)

                        if data.get("audio"):
                            with open(data["audio"], 'rb') as audio_file:
                                await update.message.reply_audio(
                                    audio=audio_file,
                                    title=data.get("title", "TikTok Audio"),
                                    performer="TikTok",
                                    write_timeout=60, read_timeout=60, pool_timeout=60
                                )
                            if os.path.exists(data["audio"]):
                                os.remove(data["audio"])
                except Exception as tk_err:
                    logger.warning(f"TikTok API failed ({tk_err}). Falling back to yt-dlp...")
                    await process_video(update.message, context, url)

            elif any(domain in url for domain in ["spotify.com", "spotify.link", "spotify:", "music.youtube.com", "soundcloud.com"]):
                await process_audio(update.message, context, url)
            else:
                await process_video(update.message, context, url)
        finally:
            typing_task.cancel()
            try:
                await status_msg.delete()
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Error handling URL: {e}")
        await update.message.reply_text(f"❌ Сталася помилка при завантаженні відео.\n\nДеталі помилки (перешліть розробнику):\n`{str(e)}`", parse_mode="Markdown")

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
    await query.answer("🔄 Оновлюю статус...")

    try:
        alerts_data, threats_data = await fetch_neptun_data(force=True)
        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
        _user_last_alert_state[user_id] = current_state["level"]
        _user_last_threat_signature[user_id] = make_threat_signature(current_state)

        report = format_alert_message(sub, current_state, is_manual_check=True)
        await query.edit_message_text(
            report,
            reply_markup=get_alert_inline_keyboard(),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        if "Message is not modified" in str(e):
            logger.info(f"Manual alert refresh: message unchanged for user {user_id}")
        else:
            logger.warning(f"Error in manual alert refresh: {e}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /status або /check для ручної перевірки ситуації з кулдауном."""
    if not await check_access(update, context):
        return

    user_id = update.effective_user.id
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

        report = format_alert_message(sub, current_state, is_manual_check=True)
        await status_msg.edit_text(
            report,
            reply_markup=get_alert_inline_keyboard(),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        set_user_last_alert_msg_id(user_id, status_msg.message_id)
    except Exception as e:
        logger.error(f"Error in status_command: {e}")
        await status_msg.edit_text(f"❌ Не вдалося отримати статус: `{e}`", parse_mode="Markdown")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if not await check_access(update, context):
        await query.answer()
        return

    choice = query.data

    # 1. Керування тривогами
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
        await context.bot.send_message(
            chat_id=chat_id,
            text="Меню закрито 👍",
            reply_markup=ReplyKeyboardRemove()
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
                [InlineKeyboardButton("🔙 Назад до /alerts", callback_data="open_alerts")]
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

            # Якщо оновлювали з попереднього фото карти — видаляємо старе повідомлення, щоб тримати чат чистим
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
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад до /alerts", callback_data="open_alerts")]])
            await query.edit_message_text(f"🔔 **Сповіщення про повітряну тривогу успішно УВІМКНЕНО!**\n\n📍 Регіон: {loc_str}", reply_markup=kb, parse_mode="Markdown")
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад до /alerts", callback_data="open_alerts")]])
            await query.edit_message_text("📍 Спочатку вкажіть вашу область (надішліть локацію або оберіть зі списку через команду /alerts).", reply_markup=kb)
        return

    elif choice == "alerts_off":
        await query.answer()
        user_id = query.from_user.id
        disable_user_alerts(user_id)
        _user_last_alert_state.pop(user_id, None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад до /alerts", callback_data="open_alerts")]])
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
            InlineKeyboardButton("🔙 Назад до /alerts", callback_data="open_alerts"),
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

    # 2. Вибір формату YouTube
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

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    sub = get_user_subscription(user_id)

    # Якщо користувач у режимі очікування локації (після /alerts або натискання Налаштувати)
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
                "• Або натиснути кнопку **«📍 Надіслати мою геопозицію»**\n"
                "• Або оберіть область вручну у /alerts",
                parse_mode="Markdown"
            )
            return

    await update.message.reply_text(
        "Будь ласка, надішліть посилання на медіа (TikTok, YouTube, Instagram тощо) або скористайтеся командою /alerts, щоб налаштувати локацію для сповіщень про тривоги 🚨"
    )

# --- ФОНОВИЙ МОНІТОРИНГ ТРИВОГ (NEPTUN API) ---

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

                        # Первинна ініціалізація без зайвого спаму при запуску бота
                        if prev_level is None:
                            _user_last_alert_state[user_id] = curr_level
                            _user_last_threat_signature[user_id] = current_sig
                            continue

                        # Якщо рівень змінився (наприклад, спокійно -> тривога, або зміна червоний/жовтий, або відбій)
                        if curr_level != prev_level:
                            _user_last_alert_state[user_id] = curr_level
                            _user_last_threat_signature[user_id] = current_sig
                            _user_last_auto_edit_time[user_id] = now
                            msg_text = format_alert_message(sub, current_state, is_manual_check=False, prev_level=prev_level)

                            # Випадок 1: Нова тривога (зелений -> червоний/жовтий)
                            if prev_level == "green" and curr_level != "green":
                                # Видаляємо попереднє повідомлення відбою, щоб чат був чистим
                                if last_msg_id:
                                    try:
                                        await application.bot.delete_message(chat_id=chat_id, message_id=last_msg_id)
                                    except Exception as del_err:
                                        logger.info(f"Could not delete previous alert message for {user_id}: {del_err}")

                                # Відправляємо нове тривожне повідомлення (зі звуком)
                                try:
                                    sent_msg = await application.bot.send_message(
                                        chat_id=chat_id,
                                        text=msg_text,
                                        reply_markup=alert_kb,
                                        parse_mode="Markdown",
                                        disable_web_page_preview=True
                                    )
                                    set_user_last_alert_msg_id(user_id, sent_msg.message_id)
                                except Exception as send_err:
                                    logger.warning(f"Could not send new alert to {chat_id}: {send_err}")

                            # Випадок 2: Зміна рівня загрози під час тривоги (жовтий <-> червоний)
                            elif prev_level != "green" and curr_level != "green":
                                edited = False
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
                                        edited = True
                                    except Exception as edit_err:
                                        logger.warning(f"Could not edit active alert message for {user_id}: {edit_err}")

                                if not edited:
                                    try:
                                        sent_msg = await application.bot.send_message(
                                            chat_id=chat_id,
                                            text=msg_text,
                                            reply_markup=alert_kb,
                                            parse_mode="Markdown",
                                            disable_web_page_preview=True
                                        )
                                        set_user_last_alert_msg_id(user_id, sent_msg.message_id)
                                    except Exception as send_err:
                                        logger.warning(f"Could not send fallback alert to {chat_id}: {send_err}")

                            # Випадок 3: Відбій тривоги (червоний/жовтий -> зелений)
                            elif curr_level == "green":
                                edited = False
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
                                        edited = True
                                    except Exception as edit_err:
                                        logger.warning(f"Could not edit all-clear message for {user_id}: {edit_err}")

                                if not edited:
                                    try:
                                        sent_msg = await application.bot.send_message(
                                            chat_id=chat_id,
                                            text=msg_text,
                                            reply_markup=alert_kb,
                                            parse_mode="Markdown",
                                            disable_notification=True,
                                            disable_web_page_preview=True
                                        )
                                        set_user_last_alert_msg_id(user_id, sent_msg.message_id)
                                    except Exception as send_err:
                                        logger.warning(f"Could not send fallback all-clear to {chat_id}: {send_err}")

                        # Випадок 4: Рівень не змінився, але змінився склад або стан цілей в області (АВТОМАТИЧНЕ ОНОВЛЕННЯ ЛИШЕ ПРИ ЗМІНІ ЗАГРОЗ)
                        elif curr_level != "green" and last_msg_id:
                            needs_update = False
                            if (current_sig != prev_sig) and (now - last_auto_edit >= 15):
                                needs_update = True

                            if needs_update:
                                msg_text = format_alert_message(sub, current_state, is_manual_check=False, prev_level=prev_level)
                                try:
                                    await application.bot.edit_message_text(
                                        chat_id=chat_id,
                                        message_id=last_msg_id,
                                        text=msg_text,
                                        reply_markup=alert_kb,
                                        parse_mode="Markdown",
                                        disable_web_page_preview=True
                                    )
                                    _user_last_threat_signature[user_id] = current_sig
                                    _user_last_auto_edit_time[user_id] = now
                                except Exception as auto_edit_err:
                                    if "Message is not modified" not in str(auto_edit_err):
                                        logger.info(f"Auto-update alert message skip for {user_id}: {auto_edit_err}")

        except Exception as e:
            logger.error(f"Error in alerts monitoring loop: {e}")

        # Опитування кожні 10 секунд (відповідно до документації NEPTUN)
        await asyncio.sleep(10)

# --- ВЕБ-СЕРВЕР ДЛЯ ХОСТИНГУ ---

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running with NEPTUN alerts and private access!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    print(f"Dummy web server started on port {port}")
    server.serve_forever()

async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Запустити бота"),
        BotCommand("alerts", "Налаштування тривог та локація"),
        BotCommand("status", "Поточний стан тривог у вашому районі"),
        BotCommand("alerts_on", "Увімкнути сповіщення"),
        BotCommand("alerts_off", "Вимкнути сповіщення"),
        BotCommand("map", "Карта тривог України зараз"),
        BotCommand("help", "Як користуватися та можливості"),
        BotCommand("invite", "Створити запрошення (для адміна)"),
        BotCommand("clear_invites", "Видалити невикористані інвайти (адмін)"),
        BotCommand("users", "Список користувачів (для адміна)"),
        BotCommand("restart", "Перезапустити бота (для адміна)"),
    ]
    await application.bot.set_my_commands(commands)

    if ADMIN_ID:
        try:
            await application.bot.send_message(chat_id=int(ADMIN_ID), text="Бот успішно запущений і готовий до роботи! ✅\nПідключено систему повітряних тривог NEPTUN та приватний доступ.")
        except Exception as e:
            logger.warning(f"Could not send startup message to admin: {e}")

    # Запуск фонового моніторингу тривог та автооновлення
    asyncio.create_task(alerts_monitoring_loop(application))
    asyncio.create_task(auto_update(application))

def main() -> None:
    print("Starting bot...")

    downloads_dir = "downloads"
    if os.path.exists(downloads_dir):
        import shutil
        try:
            shutil.rmtree(downloads_dir)
        except Exception as e:
            logger.warning(f"Failed to clean up downloads directory on startup: {e}")
    os.makedirs(downloads_dir, exist_ok=True)

    threading.Thread(target=run_dummy_server, daemon=True).start()

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("check", status_command))
    application.add_handler(CommandHandler("alerts_on", alerts_on_command))
    application.add_handler(CommandHandler("alerts_off", alerts_off_command))
    application.add_handler(CommandHandler("map", map_command))
    application.add_handler(CommandHandler("invite", invite_command))
    application.add_handler(CommandHandler("clear_invites", clear_invites_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("restart", restart_command))

    # Обробник геолокації та скасування
    application.add_handler(MessageHandler(filters.LOCATION, location_handler))
    application.add_handler(MessageHandler(filters.Regex(r"^❌ Скасувати$"), cancel_handler))

    # Обробник посилань та callback кнопок
    url_pattern = re.compile(r'https?://[^\s]+')
    application.add_handler(MessageHandler(filters.Regex(url_pattern), handle_url))
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

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
            os.execv(sys.executable, ['python'] + sys.argv)

if __name__ == "__main__":
    main()
