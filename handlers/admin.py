import os
import sys
import re
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
    set_invite_message_info,
    redeem_invite,
    get_active_invites,
    revoke_invite,
    clear_unused_invites,
    get_allowed_users_list,
)
from handlers.common import delete_user_command_message, is_media_plugin_enabled
from keyboards import get_main_reply_keyboard

load_dotenv()
ADMIN_ID = os.getenv("ADMIN_ID")
logger = logging.getLogger(__name__)

# Варіанти для інтерактивного конструктора інвайтів
INVITE_USES_OPTIONS = [
    (1, "1 👤"),
    (2, "2 👥"),
    (3, "3 👥"),
    (5, "5 👥"),
    (10, "10 👥"),
    (0, "♾️ Безліміт"),
]

INVITE_DURATION_OPTIONS = [
    (30, "30 хв"),
    (60, "1 год"),
    (360, "6 год"),
    (1440, "24 год"),
    (10080, "7 днів"),
    (0, "♾️ Безстроково"),
]

def _format_expiry_kyiv(expires_iso: str | None) -> str:
    if not expires_iso:
        return "♾️ Безстроково"
    try:
        tz = pytz.timezone("Europe/Kiev")
        exp_dt = datetime.fromisoformat(expires_iso).replace(tzinfo=pytz.utc).astimezone(tz)
        now_dt = datetime.now(tz)
        if exp_dt.date() == now_dt.date():
            return f"до {exp_dt.strftime('%H:%M')}"
        return f"до {exp_dt.strftime('%d.%m %H:%M')}"
    except Exception:
        return "обмежений час"

def _format_uses_label(used_count: int, max_uses: int) -> str:
    if max_uses <= 0:
        return f"{used_count} / ♾️"
    return f"{used_count} з {max_uses}"

def build_invite_card_html(invite_url: str, max_uses: int, used_count: int, expires_iso: str | None, code: str) -> tuple[str, InlineKeyboardMarkup]:
    exp_str = _format_expiry_kyiv(expires_iso)
    uses_desc = "безлімітна кількість людей (♾️)" if max_uses <= 0 else f"<b>{max_uses}</b> люд. (активовано: <b>{used_count}/{max_uses}</b>)"
    time_desc = "<b>безстроково (♾️)</b>" if not expires_iso else f"<b>{exp_str}</b>"

    text = (
        "🎟️ <b>Персональне посилання-запрошення:</b>\n\n"
        f"👉 <a href=\"{invite_url}\">{invite_url}</a>\n\n"
        f"👥 Ліміт місць: {uses_desc}\n"
        f"⏳ Термін дії: {time_desc}\n\n"
        "💡 <i>Перешліть це повідомлення другу або натисніть «Поділитися». Повідомлення автоматично видалиться з вашого чату, коли всі місця будуть використані!</i>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Відкрити та активувати бота", url=invite_url)],
        [InlineKeyboardButton("📤 Поділитися запрошенням", url=f"https://t.me/share/url?url={invite_url}")],
        [InlineKeyboardButton("❌ Анулювати це посилання", callback_data=f"inv_revoke:{code}")]
    ])
    return text, kb

def get_invite_builder_keyboard(selected_uses: int = 1, selected_dur: int = 30) -> tuple[str, InlineKeyboardMarkup]:
    uses_label = "♾️ Безліміт" if selected_uses <= 0 else f"{selected_uses} люд."
    dur_map = dict(INVITE_DURATION_OPTIONS)
    dur_label = dur_map.get(selected_dur, f"{selected_dur} хв")

    text = (
        "🎟️ **Конструктор посилань-запрошень**\n\n"
        f"• 👥 **Кількість людей (активацій):** `{uses_label}`\n"
        f"• ⏳ **Термін дії посилання:** `{dur_label}`\n\n"
        "Оберіть параметри кнопками нижче та натисніть **«✨ Створити запрошення»**\n"
        "*(або пишіть швидко: `/invite 3 2h`, `/invite 1 30m`, `/invite 5 7d`, `/invite 2 inf`)*:"
    )

    uses_row_1 = []
    uses_row_2 = []
    for i, (u_val, u_text) in enumerate(INVITE_USES_OPTIONS):
        prefix = "✅ " if u_val == selected_uses else ""
        btn = InlineKeyboardButton(f"{prefix}{u_text}", callback_data=f"inv_cfg_u:{u_val}:{selected_dur}")
        if i < 3:
            uses_row_1.append(btn)
        else:
            uses_row_2.append(btn)

    dur_row_1 = []
    dur_row_2 = []
    for i, (d_val, d_text) in enumerate(INVITE_DURATION_OPTIONS):
        prefix = "✅ " if d_val == selected_dur else ""
        btn = InlineKeyboardButton(f"{prefix}{d_text}", callback_data=f"inv_cfg_d:{selected_uses}:{d_val}")
        if i < 3:
            dur_row_1.append(btn)
        else:
            dur_row_2.append(btn)

    kb = InlineKeyboardMarkup([
        uses_row_1,
        uses_row_2,
        dur_row_1,
        dur_row_2,
        [InlineKeyboardButton("✨ Створити запрошення", callback_data=f"inv_create:{selected_uses}:{selected_dur}")],
        [
            InlineKeyboardButton("📋 Список активних", callback_data="inv_list"),
            InlineKeyboardButton("🗑️ Очистити всі", callback_data="inv_clear_all")
        ],
        [InlineKeyboardButton("❌ Закрити", callback_data="close_menu")]
    ])
    return text, kb

def _parse_duration_arg(arg: str) -> int | None:
    """Парсить рядок часу типу '30m', '2h', '7d', 'inf', '0' у хвилини (0 = безстроково)."""
    s = arg.strip().lower()
    if s in ("0", "inf", "infinite", "forever", "безліміт", "безстроково", "unlim"):
        return 0
    m = re.match(r"^(\d+)\s*([mhdхвгді]*)$", s)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2)
    if unit in ("", "m", "min", "хв"):
        return val
    if unit in ("h", "hr", "год", "г"):
        return val * 60
    if unit in ("d", "day", "д", "дн", "дні", "днів"):
        return val * 1440
    return val

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
            await update.message.reply_text(
                "🎉 **Ласкаво просимо! Доступ активовано!**\n\n"
                "Ваше запрошення успішно активовано. Тепер ви маєте повний доступ до бота:\n\n"
                f"{media_line}"
                "🚨 **Повітряні тривоги:** налаштуйте сповіщення про небезпеку (🔴/🟡 рівні) для вашого міста чи району.\n"
                "🗺️ **Карта тривог:** переглядайте актуальну HD-карту тривог та цілей у небі.\n\n"
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
        media_hint = (
            "• Надішліть посилання на відео чи трек для завантаження.\n"
            if is_media_plugin_enabled() else ""
        )
        await update.message.reply_text(
            f"Привіт, {user.first_name}! 👋\n\n"
            "Я ваш персональний бот для оперативного моніторингу повітряних тривог та загроз 🚨\n\n"
            "📌 **Швидкий старт:**\n"
            f"{media_hint}"
            "• Натисніть **«🚨 Налаштувати тривоги»** або надішліть свою геопозицію.\n"
            "• Скористайтесь кнопками меню внизу екрана для перевірки статусу тривог, карти та налаштувань.",
            reply_markup=get_main_reply_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "⛔ **Цей бот є приватним.**\n\n"
            "Щоб отримати доступ, відкрийте персональне посилання-запрошення від адміністратора."
        )

async def send_created_invite_message(bot, chat_id: int, user_id: int, max_uses: int = 1, duration_minutes: int = 30) -> None:
    dur_arg = None if duration_minutes <= 0 else duration_minutes
    code, expires_iso = await create_single_use_invite(int(user_id), duration_minutes=dur_arg, max_uses=max_uses)
    bot_info = await bot.get_me()
    invite_url = f"https://t.me/{bot_info.username}?start=invite_{code}"

    text_html, share_kb = build_invite_card_html(invite_url, max_uses, 0, expires_iso, code)
    sent_msg = await bot.send_message(
        chat_id=chat_id,
        text=text_html,
        reply_markup=share_kb,
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    await set_invite_message_info(code, sent_msg.chat_id, sent_msg.message_id)

async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not await is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return

    await delete_user_command_message(update)

    args = context.args
    if args and len(args) > 0:
        first_arg = args[0].lower().strip()
        if first_arg in ("clear", "clean", "reset", "очистити", "видалити"):
            deleted, msgs_to_delete = await clear_unused_invites()
            for c_id, m_id in msgs_to_delete:
                try:
                    await context.bot.delete_message(chat_id=c_id, message_id=m_id)
                except Exception:
                    pass
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🗑️ Анульовано та видалено **{deleted}** активних посилань-запрошень.",
                parse_mode="Markdown"
            )
            return
        elif first_arg in ("list", "active", "список", "активні"):
            await invites_list_command(update, context)
            return

        # Швидке створення через аргументи: /invite [кількість_людей] [час]
        try:
            max_uses = 0 if first_arg in ("0", "inf", "unlim", "безліміт") else int(first_arg)
            duration_minutes = 30
            if len(args) >= 2:
                parsed_dur = _parse_duration_arg(args[1])
                if parsed_dur is not None:
                    duration_minutes = parsed_dur
            await send_created_invite_message(context.bot, chat_id, user_id, max_uses=max_uses, duration_minutes=duration_minutes)
            return
        except ValueError:
            pass

    # Якщо аргументів немає — відкриваємо зручний інтерактивний конструктор
    text, kb = get_invite_builder_keyboard(selected_uses=1, selected_dur=30)
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="Markdown")

async def build_invites_list_view(bot) -> tuple[str, InlineKeyboardMarkup]:
    invites = await get_active_invites()
    bot_info = await bot.get_me()
    if not invites:
        text = (
            "📋 **Активні посилання-запрошення**\n\n"
            "Зараз немає жодного активного запрошення (усі використані або очищені)."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Створити нове запрошення", callback_data="inv_open_builder")],
            [InlineKeyboardButton("❌ Закрити", callback_data="close_menu")]
        ])
        return text, kb

    lines = [f"📋 **Активні посилання-запрошення ({len(invites)}):**\n"]
    buttons = []
    for idx, inv in enumerate(invites, start=1):
        code = inv["code"]
        max_u = inv["max_uses"]
        used_c = inv["used_count"]
        exp_str = _format_expiry_kyiv(inv["expires_at"])
        uses_str = _format_uses_label(used_c, max_u)
        inv_url = f"https://t.me/{bot_info.username}?start=invite_{code}"
        lines.append(f"{idx}. [`{code}`]({inv_url}) — 👥 **{uses_str}** | ⏳ {exp_str}")
        buttons.append([
            InlineKeyboardButton(f"❌ Анулювати #{code[:6]} ({uses_str})", callback_data=f"inv_revoke_list:{code}")
        ])

    buttons.append([InlineKeyboardButton("🗑️ Анулювати ВСІ невикористані", callback_data="inv_clear_all")])
    buttons.append([
        InlineKeyboardButton("➕ Створити нове", callback_data="inv_open_builder"),
        InlineKeyboardButton("❌ Закрити", callback_data="close_menu")
    ])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)

async def invites_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not await is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return
    await delete_user_command_message(update)
    text, kb = await build_invites_list_view(context.bot)
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="Markdown", disable_web_page_preview=True)

async def clear_invites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not await is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return
    await delete_user_command_message(update)
    deleted, msgs_to_delete = await clear_unused_invites()
    for c_id, m_id in msgs_to_delete:
        try:
            await context.bot.delete_message(chat_id=c_id, message_id=m_id)
        except Exception:
            pass
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🗑️ Анульовано та видалено **{deleted}** активних запрошень (усі старі посилання тепер недійсні).",
        parse_mode="Markdown"
    )

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

    status_msg = await update.message.reply_text("⏳ Завантажую оновлення з GitHub (гілка `main`) і перезавантажуюсь... 🔄", parse_mode="Markdown", disable_notification=True)
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

    try:
        if context.application.updater and context.application.updater.running:
            await context.application.updater.stop()
    except Exception:
        pass

    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)
