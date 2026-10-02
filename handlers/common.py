import os
import time
import logging
import traceback
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import Conflict, NetworkError, TimedOut
from dotenv import load_dotenv

from database import is_user_allowed
from keyboards import get_main_reply_keyboard

load_dotenv()
ADMIN_ID = os.getenv("ADMIN_ID")
logger = logging.getLogger(__name__)

def is_media_plugin_enabled() -> bool:
    """Перевіряє, чи увімкнено плагін завантаження медіа (TikTok/YouTube/Spotify) у .env."""
    return os.getenv("ENABLE_MEDIA_PLUGIN", "true").strip().lower() in ("1", "true", "yes", "on")


async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Перевіряє, чи має користувач доступ до бота (приватний режим)."""
    if not update.effective_user:
        return False
    user_id = update.effective_user.id
    if await is_user_allowed(user_id, ADMIN_ID):
        return True

    if update.effective_message:
        await update.effective_message.reply_text(
            "⛔ **Цей бот є приватним.**\n\n"
            "Використовувати його можуть лише адміністратор та користувачі за персональним посиланням-запрошенням.\n\n"
            "Зверніться до власника бота, щоб отримати доступ! 🎟️",
            parse_mode="Markdown"
        )
    return False

async def delete_user_command_message(update: Update) -> None:
    """Видаляє вхідне повідомлення-команду від користувача, щоб чат залишався чистим."""
    if update and update.message:
        try:
            await update.message.delete()
        except Exception as e:
            logger.debug(f"Could not delete user command message: {e}")

async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(f"Ваш Telegram ID: `{user.id}`", parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    await delete_user_command_message(update)
    media_section = ""
    if is_media_plugin_enabled():
        media_section = (
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📥 **Як завантажувати відео та музику (Плагін увімкнено)**\n"
            "Просто надішліть боту посилання на потрібне медіа:\n"
            "• **TikTok**: скачує відео без водяного знаку + окремо MP3 аудіо або фото-альбом.\n"
            "• **YouTube**: запропонує кнопки (🎬 Відео / 🎵 MP3 аудіо / 🎬+🎵 Обидва).\n"
            "• **Instagram / Twitter (X)**: завантажує Reels та відеопости.\n"
            "• **Spotify, SoundCloud, YT Music**: знаходить і відправляє MP3 з обкладинкою.\n\n"
        )

    help_text = (
        "📖 **Повна інструкція з користування ботом**\n\n"
        f"{media_section}"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚨 **1. Як налаштувати повітряні тривоги**\n"
        "1. Напишіть команду `/alerts` (або одразу `/alerts НазваМіста`, наприклад `/alerts Ірпінь`).\n"
        "2. Ви можете обрати область зі списку, вписати назву свого міста/села або натиснути кнопку **«📍 Надіслати мою геопозицію»** внизу екрана.\n"
        "3. Бот автоматично визначить координати, район та увімкне сповіщення!\n\n"
        "**Що означають сповіщення:**\n"
        "• 🔴 **Червоний рівень** — повітряна тривога! Загроза ракет, балістики, шахедів або КАБ (негайно в укриття!).\n"
        "• 🟡 **Жовтий рівень** — підвищена небезпека (проліт дронів на підльоті, загроза артобстрілу).\n"
        "• 🛸 **Цілі поруч** — якщо ви вказали місто або геопозицію, бот вказує курс та точну відстань у км до вас.\n"
        "• 🟢 **Відбій** — приходить тихо (без звуку), щоб не турбувати вночі.\n"
        "• 🧹 **Одне оновлюване повідомлення** — бот тримає чат чистим, автоматично редагуючи поточне повідомлення при зміні цілей/рівня небезпеки або відбої.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🗺 **2. Карта тривог та швидкі команди**\n"
        "• `/status` (або `/check`) — перевірити актуальну ситуацію у вашому районі.\n"
        "• `/map` — актуальна графічна HD-карта України з тривогами та цілями в небі.\n"
        "• `/alerts [місто]` — налаштувати локацію або переглянути меню.\n"
        "• `/alerts_on` / `/alerts_off` — увімкнути або вимкнути сповіщення.\n"
        "• `/invite` — конструктор запрошень (на 1..N людей та різний час, для адміна).\n"
        "• `/invites` — список активних запрошень та їх анулювання (для адміна).\n"
        "• `/help` — переглянути цю інструкцію."
    )
    await update.effective_message.reply_text(help_text, reply_markup=get_main_reply_keyboard(), parse_mode="Markdown")

_last_conflict_notify_time = 0.0

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логує помилку та надсилає сповіщення адміну при виникненні винятку."""
    global _last_conflict_notify_time

    err = context.error
    if isinstance(err, Conflict):
        logger.warning(f"Telegram API Conflict detected: {err}")
        now = time.time()
        if ADMIN_ID and (now - _last_conflict_notify_time > 600):
            _last_conflict_notify_time = now
            try:
                await context.bot.send_message(
                    chat_id=int(ADMIN_ID),
                    text=(
                        "⚠️ **Виявлено конфлікт запуску (Telegram Conflict)!**\n\n"
                        "З тим самим `BOT_TOKEN` одночасно запущено **два екземпляри бота**.\n"
                        "*Зупиніть зайвий екземпляр, щоб уникнути дублювання запитів.*"
                    ),
                    parse_mode="Markdown",
                    disable_notification=True
                )
            except Exception:
                pass
        return

    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(f"Transient network error: {err}")
        return

    logger.error("Exception while handling an update:", exc_info=err)

    tb_list = traceback.format_exception(None, err, err.__traceback__)
    tb_string = "".join(tb_list)

    if ADMIN_ID:
        try:
            message = (
                f"🚨 **Увага! У боті виникла необроблена помилка:**\n\n"
                f"```python\n{tb_string[-1500:]}\n```"
            )
            await context.bot.send_message(chat_id=int(ADMIN_ID), text=message, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Could not send error notification to admin: {e}")
