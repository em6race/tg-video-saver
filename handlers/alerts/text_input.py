import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import get_user_subscription
from neptun_alerts import forward_geocode
from keyboards import get_main_reply_keyboard
from handlers.common import (
    check_access,
    help_command,
    is_media_plugin_enabled,
    is_alerts_plugin_enabled,
)
from handlers.alerts.location import prompt_location_confirmation
from handlers.alerts.commands import (
    alerts_command,
    status_command,
    map_command,
    alerts_history_command,
)

logger = logging.getLogger(__name__)

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробляє звичайні текстові повідомлення: команди меню та геокодинг міст для тривог."""
    if not await check_access(update, context):
        return

    text = update.message.text.strip() if update.message and update.message.text else ""
    if not text:
        return

    user_id = update.effective_user.id

    # 1. Загальні команди та кнопки меню
    if text in ("❓ Довідка", "❓ Довідка та допомога", "Довідка", "/help"):
        await help_command(update, context)
        return

    # 2. Обробка кнопок плагіна тривог
    if is_alerts_plugin_enabled():
        if text in ("📊 Статус тривоги", "Статус тривоги", "/status", "/check"):
            await status_command(update, context)
            return
        elif text in ("🗺️ Карта тривог", "Карта тривог", "/map"):
            await map_command(update, context)
            return
        elif text in ("📜 Історія тривог", "Історія тривог", "Історія", "/history"):
            await alerts_history_command(update, context)
            return
        elif text in ("🚨 Налаштувати тривоги", "Налаштувати тривоги", "/alerts"):
            await alerts_command(update, context)
            return

        sub = await get_user_subscription(user_id)
        should_try_geocode = (sub and sub.get("awaiting_location")) or (
            not is_media_plugin_enabled() and len(text) <= 60 and not text.startswith("http")
        )

        if should_try_geocode:
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

    # 3. Підказка за замовчуванням
    hint = (
        "Будь ласка, надішліть посилання на медіа (TikTok, YouTube, Instagram, Spotify) або скористайтеся меню кнопок нижче 🚨"
        if is_media_plugin_enabled()
        else "Напишіть назву вашого міста для налаштування тривог або скористайтеся кнопками меню внизу екрана 🚨"
    )

    await update.message.reply_text(hint, reply_markup=get_main_reply_keyboard())
