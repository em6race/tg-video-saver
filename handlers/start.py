import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from database import is_user_allowed, redeem_invite
from keyboards import get_main_reply_keyboard
from handlers.common import (
    ADMIN_ID,
    is_media_plugin_enabled,
    is_alerts_plugin_enabled,
)
from handlers.admin import build_invite_card_html

logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробляє команду /start: активація посилань-запрошень та онбординг."""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name or ""

    welcome_buttons = []
    if is_alerts_plugin_enabled():
        welcome_buttons.append([InlineKeyboardButton("🚨 Налаштувати тривоги", callback_data="open_alerts")])
        welcome_buttons.append([InlineKeyboardButton("🗺️ Карта тривог України", callback_data="alerts_map_refresh")])
    welcome_buttons.append([InlineKeyboardButton("❓ Докладна інструкція", callback_data="open_help")])
    welcome_keyboard = InlineKeyboardMarkup(welcome_buttons)

    args = context.args
    if args and len(args) > 0 and args[0].startswith("invite_"):
        invite_code = args[0].replace("invite_", "").strip()

        if await is_user_allowed(user_id, ADMIN_ID):
            await update.message.reply_text(
                "✅ **Ви вже маєте повний доступ до цього бота!**\n\n"
                "💡 *Це посилання-запрошення залишилося активним для інших користувачів.*",
                reply_markup=welcome_keyboard,
                parse_mode="Markdown"
            )
            return

        ok, status, invite_meta = await redeem_invite(invite_code, user_id, username)
        if ok:
            if invite_meta and invite_meta.get("chat_id") and invite_meta.get("msg_id"):
                c_id = int(invite_meta["chat_id"])
                m_id = int(invite_meta["msg_id"])
                if invite_meta.get("fully_used"):
                    try:
                        await context.bot.delete_message(chat_id=c_id, message_id=m_id)
                    except Exception as del_err:
                        logger.debug(f"Could not delete invite message after full redemption: {del_err}")
                else:
                    try:
                        bot_info = await context.bot.get_me()
                        inv_url = f"https://t.me/{bot_info.username}?start=invite_{invite_code}"
                        new_html, new_kb = build_invite_card_html(
                            invite_url=inv_url,
                            max_uses=invite_meta.get("max_uses", 1),
                            used_count=invite_meta.get("used_count", 1),
                            expires_iso=invite_meta.get("expires_at"),
                            code=invite_code
                        )
                        await context.bot.edit_message_text(
                            chat_id=c_id,
                            message_id=m_id,
                            text=new_html,
                            reply_markup=new_kb,
                            parse_mode="HTML",
                            disable_web_page_preview=True
                        )
                    except Exception as edit_err:
                        logger.debug(f"Could not update multi-use invite message: {edit_err}")

            media_line = (
                "🎥 **Завантаження медіа:** надсилайте посилання на TikTok, YouTube, Instagram, Spotify.\n"
                if is_media_plugin_enabled() else ""
            )
            alerts_line = (
                "🚨 **Повітряні тривоги:** налаштуйте сповіщення про небезпеку (🔴/🟡 рівні) для вашого міста чи району.\n"
                "🗺️ **Карта тривог:** переглядайте актуальну HD-карту тривог та цілей у небі.\n"
                if is_alerts_plugin_enabled() else ""
            )
            await update.message.reply_text(
                "🎉 **Ласкаво просимо! Доступ активовано!**\n\n"
                "Ваше запрошення успішно активовано. Тепер ви маєте доступ до бота:\n\n"
                f"{media_line}"
                f"{alerts_line}\n"
                "👇 **З чого почати? Оберіть дію нижче:**",
                reply_markup=welcome_keyboard,
                parse_mode="Markdown"
            )
            return
        elif status == "expired":
            if invite_meta and invite_meta.get("chat_id") and invite_meta.get("msg_id"):
                try:
                    await context.bot.delete_message(
                        chat_id=int(invite_meta["chat_id"]),
                        message_id=int(invite_meta["msg_id"])
                    )
                except Exception:
                    pass
            await update.message.reply_text(
                "⏳ **Термін дії цього запрошення минув.**\n\n"
                "Посилання більше не дійсне. Будь ласка, попросіть власника бота надіслати вам нове запрошення.",
                parse_mode="Markdown"
            )
            return
        elif status == "already_used":
            await update.message.reply_text(
                "❌ **Помилка запрошення:** усі місця за цим посиланням вже використані."
            )
            return
        else:
            await update.message.reply_text(
                "❌ **Помилка запрошення:** це посилання недійсне або було анульоване власником."
            )
            return

    if await is_user_allowed(user_id, ADMIN_ID):
        features_desc = []
        if is_media_plugin_enabled():
            features_desc.append("завантаження музики та відео (TikTok, YouTube, Spotify)")
        if is_alerts_plugin_enabled():
            features_desc.append("оперативного моніторингу повітряних тривог 🚨")

        desc_str = " та ".join(features_desc) if features_desc else "ваших щоденних завдань"

        quick_start_lines = []
        if is_media_plugin_enabled():
            quick_start_lines.append("• Надішліть посилання на відео чи трек для завантаження 🎬🎵")
        if is_alerts_plugin_enabled():
            quick_start_lines.append("• Натисніть **«🚨 Налаштувати тривоги»** або надішліть свою геопозицію 📍")
            quick_start_lines.append("• Скористайтесь кнопками меню внизу екрана для перевірки статусу тривог та карти.")
        if not quick_start_lines:
            quick_start_lines.append("• Скористайтесь командою /help для перегляду можливостей бота.")

        await update.message.reply_text(
            f"Привіт, {user.first_name}! 👋\n\n"
            f"Я ваш персональний бот для {desc_str}.\n\n"
            "📌 **Швидкий старт:**\n"
            + "\n".join(quick_start_lines),
            reply_markup=get_main_reply_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "⛔ **Цей бот є приватним.**\n\n"
            "Щоб отримати доступ, відкрийте персональне посилання-запрошення від адміністратора."
        )
