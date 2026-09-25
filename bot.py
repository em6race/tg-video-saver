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
    get_allowed_users_list,
    set_awaiting_location,
    save_user_location,
    disable_user_alerts,
    enable_user_alerts,
    get_user_subscription,
    get_all_enabled_subscriptions
)
from neptun_alerts import (
    reverse_geocode,
    fetch_neptun_data,
    evaluate_user_alert_state,
    format_alert_message,
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

# Пам'ять останнього рівня тривоги для кожного підписника
_user_last_alert_state = {}

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

    # Перевірка наявності інвайт-коду у /start invite_CODE
    args = context.args
    if args and len(args) > 0 and args[0].startswith("invite_"):
        invite_code = args[0].replace("invite_", "").strip()
        if redeem_invite(invite_code, user_id, username):
            await update.message.reply_text(
                "🎉 **Ласкаво просимо! Доступ активовано!**\n\n"
                "Ваше запрошення успішно використано. Тепер ви маєте повний доступ до всіх можливостей бота:\n"
                "🎥 **Завантаження медіа:** просто надішліть посилання на TikTok, YouTube, Instagram, Twitter, Spotify або SoundCloud.\n"
                "🚨 **Повітряні тривоги:** команда /alerts дозволить увімкнути сповіщення про загрози (🔴 червоний / 🟡 жовтий рівень) за вашою геопозицією.",
                parse_mode="Markdown"
            )
            return
        else:
            await update.message.reply_text(
                "❌ **Помилка запрошення:** це посилання-запрошення недійсне або вже було використане іншою людиною."
            )
            return

    # Якщо користувач вже має доступ
    if is_user_allowed(user_id, ADMIN_ID):
        await update.message.reply_text(
            f"Привіт, {user.first_name}! 👋\n\n"
            "Я ваш персональний бот для завантаження медіа та моніторингу повітряних тривог 🚀\n\n"
            "📥 **Як завантажити відео/музику:**\n"
            "Просто надішліть сюди посилання на TikTok, Instagram, YouTube, Twitter(X), Spotify або SoundCloud.\n\n"
            "🚨 **Сповіщення про тривоги:**\n"
            "Напишіть команду /alerts, щоб прив'язати сповіщення до вашої локації.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "⛔ **Цей бот є приватним.**\n\n"
            "Щоб отримати доступ, відкрийте посилання-запрошення, надане адміністратором бота."
        )

async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return

    code = create_single_use_invite(int(user_id))
    bot_info = await context.bot.get_me()
    invite_url = f"https://t.me/{bot_info.username}?start=invite_{code}"

    await update.message.reply_text(
        "🎟️ **Одноразове посилання-запрошення створено:**\n\n"
        f"`{invite_url}`\n\n"
        "⚠️ *Це посилання може використати лише 1 людина. Після першого натискання Start воно стане недійсним.*",
        parse_mode="Markdown"
    )

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
        "👋 *Довідка по боту:*\n\n"
        "🎥 *Завантаження відео та аудіо*\n"
        "• *TikTok*: відео без водяного знаку або слайдшоу + музика окремо.\n"
        "• *YouTube*: інтерактивний вибір (відео, аудіо MP3, або обидва).\n"
        "• *Instagram & Twitter (X)*: завантаження reels/відео.\n"
        "• *Spotify, SoundCloud, YouTube Music*: швидке завантаження треків у MP3.\n\n"
        "🚨 *Повітряні тривоги (NEPTUN API)*\n"
        "• `/alerts` — увімкнути моніторинг тривог та надіслати геолокацію.\n"
        "• `/alerts_off` — вимкнути сповіщення про тривоги.\n\n"
        "⚙️ *Команди адміна*\n"
        "• `/invite` — створити одноразове посилання для друга.\n"
        "• `/users` — список дозволених користувачів.\n"
        "• `/restart` — перезавантажити бота та стягнути оновлення з GitHub."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

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

async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    # Підтримка команд /alerts on та /alerts off
    args = context.args
    if args and len(args) > 0:
        arg_lower = args[0].lower().strip()
        if arg_lower in ("on", "1", "start", "enable", "увімк", "увімкнути"):
            await alerts_on_command(update, context)
            return
        elif arg_lower in ("off", "0", "stop", "disable", "вимк", "вимкнути"):
            await alerts_off_command(update, context)
            return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

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
        "📍 **Щоб оновити/прив'язати локацію:**\n"
        "Натисніть кнопку **'📍 Надіслати мою геопозицію'** на клавіатурі або оберіть область зі списку нижче:"
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
        [toggle_btn]
    ])

    await update.message.reply_text(msg_text, reply_markup=reply_kb, parse_mode="Markdown")
    await update.message.reply_text("Швидкі дії:", reply_markup=inline_kb)

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
    sub = get_user_subscription(user_id)

    status_msg = await update.message.reply_text("⏳ Малюю актуальну карту повітряних тривог України...")
    try:
        alerts_data, threats_data = await fetch_neptun_data()
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
        oblast = geo["oblast"]
        raion = geo["raion"]
        locality = geo["locality"]

        save_user_location(user_id, chat_id, oblast, raion, locality, lat, lon)

        loc_parts = [p for p in [locality, raion, oblast] if p]
        unique_loc = []
        for p in loc_parts:
            if p not in unique_loc:
                unique_loc.append(p)
        loc_display = ", ".join(unique_loc)

        await status_msg.delete()
        await update.message.reply_text(
            f"✅ **Локацію успішно збережено!**\n\n"
            f"📍 **Ваш регіон:** {loc_display}\n\n"
            "Тепер бот цілодобово моніторитиме повітряний простір для вашої локації і надсилатиме сповіщення про:\n"
            "🔴 **Червоний рівень** — ракетна/дронова тривога (пройдіть в укриття!)\n"
            "🟡 **Жовтий рівень** — загроза артобстрілу / підвищена небезпека\n"
            "🛸 **Повітряні цілі поруч** — тип (БпЛА/ракета), курс та відстань у км\n"
            "🟢 **Відбій тривоги**",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )

        # Одразу перевіряємо поточний стан у цьому районі
        alerts_data, threats_data = await fetch_neptun_data()
        sub = get_user_subscription(user_id)
        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
        _user_last_alert_state[user_id] = current_state["level"]

        report = format_alert_message(sub, current_state, is_manual_check=True)
        await update.message.reply_text(f"📊 **Поточна ситуація на цей момент:**\n\n{report}", parse_mode="Markdown", disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error handling location: {e}")
        await status_msg.edit_text(
            f"❌ Не вдалося точно визначити область за GPS: `{e}`\n\n"
            "Будь ласка, оберіть вашу область вручну за допомогою /alerts.",
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

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not await check_access(update, context):
        return

    choice = query.data

    # 1. Керування тривогами
    if choice == "alerts_map_refresh":
        user_id = query.from_user.id
        sub = get_user_subscription(user_id)
        await query.answer("Оновлюю карту...")
        try:
            alerts_data, threats_data = await fetch_neptun_data()
            map_path, caption = await generate_alerts_map_image(alerts_data, threats_data, sub)

            inline_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Оновити карту", callback_data="alerts_map_refresh")],
                [InlineKeyboardButton("🌐 Жива онлайн-карта (NEPTUN)", url="https://neptun.in.ua/")]
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
        except Exception as e:
            logger.error(f"Error refreshing map: {e}")
            await query.message.reply_text(f"❌ Не вдалося оновити карту: `{e}`")
        return

    elif choice == "alerts_check":
        user_id = query.from_user.id
        sub = get_user_subscription(user_id)
        if not sub or not sub.get("enabled"):
            await query.edit_message_text("⚪ У вас наразі вимкнено сповіщення про тривоги. Увімкніть командою /alerts.")
            return

        try:
            alerts_data, threats_data = await fetch_neptun_data()
            state = evaluate_user_alert_state(sub, alerts_data, threats_data)
            _user_last_alert_state[user_id] = state["level"]
            report = format_alert_message(sub, state, is_manual_check=True)
            await query.edit_message_text(report, parse_mode="Markdown", disable_web_page_preview=True)
        except Exception as e:
            await query.edit_message_text(f"❌ Не вдалося отримати дані з NEPTUN API: `{e}`", parse_mode="Markdown")
        return

    elif choice == "alerts_on":
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        if enable_user_alerts(user_id, chat_id):
            sub = get_user_subscription(user_id)
            loc_parts = [p for p in [sub.get("locality"), sub.get("raion"), sub.get("oblast")] if p]
            loc_str = ", ".join(loc_parts) if loc_parts else sub.get("oblast", "")
            await query.edit_message_text(f"🔔 **Сповіщення про повітряну тривогу успішно УВІМКНЕНО!**\n\n📍 Регіон: {loc_str}", parse_mode="Markdown")
        else:
            await query.edit_message_text("📍 Спочатку вкажіть вашу область (надішліть локацію або оберіть зі списку через команду /alerts).")
        return

    elif choice == "alerts_off":
        user_id = query.from_user.id
        disable_user_alerts(user_id)
        _user_last_alert_state.pop(user_id, None)
        await query.edit_message_text("🔕 **Сповіщення про повітряну тривогу ВИМКНЕНО.**\n\nУвімкнути знову можна командою /alerts_on.", parse_mode="Markdown")
        return

    elif choice.startswith("alerts_manual_page_"):
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

        await query.edit_message_text("Оберіть вашу область зі списку:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    elif choice.startswith("set_oblast:"):
        oblast_name = choice.split("set_oblast:")[1]
        user_id = query.from_user.id
        chat_id = query.message.chat_id

        save_user_location(user_id, chat_id, oblast_name, "", "", None, None)
        await query.edit_message_text(
            f"✅ **Область збережено:** {oblast_name}!\n\n"
            "Тепер ви будете оперативно отримувати сповіщення про зміну рівнів тривог (🔴/🟡) та відбої.",
            parse_mode="Markdown"
        )
        return

    # 2. Вибір формату YouTube
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
    await update.message.reply_text("Будь ласка, відправте мені дійсне посилання на медіа або скористайтеся командою /alerts.")

# --- ФОНОВИЙ МОНІТОРИНГ ТРИВОГ (NEPTUN API) ---

async def alerts_monitoring_loop(application: Application) -> None:
    """Фоновий процес опитування NEPTUN API та розсилки сповіщень підписникам."""
    logger.info("Starting NEPTUN alert monitoring loop...")
    while True:
        try:
            subs = get_all_enabled_subscriptions()
            if subs:
                alerts_data, threats_data = await fetch_neptun_data()

                if alerts_data or threats_data:
                    for sub in subs:
                        user_id = sub["user_id"]
                        chat_id = sub["chat_id"]

                        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
                        curr_level = current_state["level"]
                        prev_level = _user_last_alert_state.get(user_id)

                        # Первинна ініціалізація без зайвого спаму при запуску бота
                        if prev_level is None:
                            _user_last_alert_state[user_id] = curr_level
                            continue

                        # Якщо рівень змінився (наприклад, спокійно -> тривога, або зміна червоний/жовтий, або відбій)
                        if curr_level != prev_level:
                            _user_last_alert_state[user_id] = curr_level
                            msg_text = format_alert_message(sub, current_state, is_manual_check=False, prev_level=prev_level)
                            # Відбій тривоги надсилаємо тихо (без звуку), щоб не будити вночі
                            is_silent = (curr_level == "green")
                            try:
                                await application.bot.send_message(
                                    chat_id=chat_id,
                                    text=msg_text,
                                    parse_mode="Markdown",
                                    disable_notification=is_silent,
                                    disable_web_page_preview=True
                                )
                            except Exception as send_err:
                                logger.warning(f"Could not send alert to chat_id {chat_id}: {send_err}")

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
        BotCommand("alerts_on", "Увімкнути сповіщення"),
        BotCommand("alerts_off", "Вимкнути сповіщення"),
        BotCommand("map", "Карта тривог України зараз"),
        BotCommand("help", "Як користуватися та можливості"),
        BotCommand("invite", "Створити запрошення (для адміна)"),
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
    application.add_handler(CommandHandler("alerts_on", alerts_on_command))
    application.add_handler(CommandHandler("alerts_off", alerts_off_command))
    application.add_handler(CommandHandler("map", map_command))
    application.add_handler(CommandHandler("invite", invite_command))
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
