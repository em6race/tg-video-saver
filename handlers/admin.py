import os
import sys
import logging
import asyncio
import subprocess
from datetime import datetime
import pytz
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from dotenv import load_dotenv

from database import (
    is_admin,
    is_user_allowed,
    create_single_use_invite,
    redeem_invite,
    clear_unused_invites,
    get_allowed_users_list
)
from handlers.common import delete_user_command_message
from keyboards import get_main_reply_keyboard

load_dotenv()
ADMIN_ID = os.getenv("ADMIN_ID")
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name or ""

    welcome_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚨 Налаштувати тривоги", callback_data="open_alerts")],
        [InlineKeyboardButton("🗺️ Карта тривог України", callback_data="alerts_map_refresh")],
        [InlineKeyboardButton("❓ Докладна інструкція", callback_data="open_help")]
    ])

    args = context.args
    if args and len(args) > 0 and args[0].startswith("invite_"):
        invite_code = args[0].replace("invite_", "").strip()

        if await is_user_allowed(user_id, ADMIN_ID):
            await update.message.reply_text(
                "✅ **Ви вже маєте повний доступ до цього бота!**\n\n"
                "💡 *Це одноразове посилання-запрошення залишилося активним для вашого друга.*",
                reply_markup=welcome_keyboard,
                parse_mode="Markdown"
            )
            return

        ok, status = await redeem_invite(invite_code, user_id, username)
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

    if await is_user_allowed(user_id, ADMIN_ID):
        await update.message.reply_text(
            f"Привіт, {user.first_name}! 👋\n\n"
            "Я ваш персональний бот для завантаження медіа та оперативного моніторингу повітряних тривог 🚀\n\n"
            "📥 **Швидкий старт:**\n"
            "• Надішліть посилання на будь-яке відео чи трек для завантаження.\n"
            "• Скористайтесь кнопками меню внизу екрана для перевірки статусу тривог, карти та налаштувань.",
            reply_markup=get_main_reply_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "⛔ **Цей бот є приватним.**\n\n"
            "Щоб отримати доступ, відкрийте персональне посилання-запрошення від адміністратора."
        )

async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return

    args = context.args
    if args and len(args) > 0 and args[0].lower() in ("clear", "clean", "reset", "очистити", "видалити"):
        deleted = await clear_unused_invites()
        await update.message.reply_text(f"🗑️ Очищено **{deleted}** невикористаних посилань-запрошень з бази даних.", parse_mode="Markdown")
        return

    code, expires_iso = await create_single_use_invite(int(user_id), duration_minutes=30)
    bot_info = await context.bot.get_me()
    invite_url = f"https://t.me/{bot_info.username}?start=invite_{code}"

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
    if not await is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return
    deleted = await clear_unused_invites()
    await update.message.reply_text(f"🗑️ Видалено **{deleted}** невикористаних запрошень з бази даних.", parse_mode="Markdown")

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return

    users = await get_allowed_users_list()
    if not users:
        await update.message.reply_text("Список користувачів порожній (окрім адміна).")
        return

    lines = ["👥 **Авторизовані користувачі бота:**\n"]
    for u_id, uname, joined in users:
        u_display = f"@{uname}" if uname else f"ID: `{u_id}`"
        lines.append(f"• {u_display} (додано: {joined[:10]})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not await is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return

    status_msg = await update.message.reply_text("⏳ Завантажую оновлення з GitHub і перезавантажуюсь... 🔄", disable_notification=True)
    await asyncio.sleep(0.5)

    git_output = ""
    pull_success = False

    try:
        res = subprocess.run(["git", "pull", "origin", "main"], capture_output=True, text=True, check=True)
        git_output = res.stdout.strip() or "Оновлено успішно."
        pull_success = True
    except Exception as e:
        err_detail = getattr(e, 'stderr', str(e)) or str(e)
        logger.warning(f"Git pull failed during restart: {err_detail}. Attempting git fetch & reset...")
        try:
            subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, text=True, check=True)
            res_reset = subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, text=True, check=True)
            git_output = f"Скинуто до origin/main: {res_reset.stdout.strip()}"
            pull_success = True
        except Exception as reset_err:
            git_output = f"Помилка pull: {err_detail}\nПомилка reset: {reset_err}"

    if pull_success:
        try:
            await status_msg.edit_text(f"✅ **Оновлено з GitHub:**\n`{git_output[:300]}`\n\nПерезапуск бота... 🔄", parse_mode="Markdown")
        except Exception:
            pass
    else:
        try:
            await status_msg.edit_text(f"⚠️ **Не вдалося оновити код:**\n`{git_output[:300]}`\n\nПерезапуск на поточній версії... 🔄", parse_mode="Markdown")
        except Exception:
            pass

    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)
