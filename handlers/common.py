import os
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from dotenv import load_dotenv

from database import is_user_allowed
from keyboards import get_main_reply_keyboard

load_dotenv()
ADMIN_ID = os.getenv("ADMIN_ID")
logger = logging.getLogger(__name__)

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
    await update.message.reply_text(help_text, reply_markup=get_main_reply_keyboard(), parse_mode="Markdown")
