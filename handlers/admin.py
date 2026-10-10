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
from handlers.common import (
    delete_user_command_message,
    is_media_plugin_enabled,
    is_alerts_plugin_enabled,
)
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

import html

async def build_users_list_view() -> tuple[str, InlineKeyboardMarkup]:
    users = await get_allowed_users_list()
    if not users:
        text = "👥 <b>Авторизовані користувачі бота:</b>\n\nСписок користувачів порожній (окрім власника)."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Створити запрошення", callback_data="inv_open_builder")],
            [InlineKeyboardButton("❌ Закрити", callback_data="close_menu")]
        ])
        return text, kb

    lines = [f"👥 <b>Авторизовані користувачі бота ({len(users)}):</b>\n"]
    buttons = []
    for idx, row in enumerate(users, start=1):
        u_id = row[0]
        uname = row[1] or ""
        joined = (row[2] or "")[:10]
        oblast = row[3] or ""
        locality = row[4] or ""
        enabled = bool(row[5]) if row[5] is not None else False

        safe_uname = html.escape(uname)
        if safe_uname:
            u_display = f"@{safe_uname}" if not safe_uname.startswith("@") else safe_uname
        else:
            u_display = f"ID: <code>{u_id}</code>"

        loc_parts = [p for p in [locality, oblast] if p]
        loc_str = html.escape(", ".join(loc_parts)) if loc_parts else "локацію не обрано"
        bell = "🟢" if enabled and oblast else "⚪"

        lines.append(
            f"{idx}. <b>{u_display}</b> (<code>{u_id}</code>)\n"
            f"   └ {bell} 📍 {loc_str} | 📅 {joined}"
        )
        btn_label = f"@{uname}" if uname else f"ID {u_id}"
        buttons.append([
            InlineKeyboardButton(f"❌ Забрати доступ у {btn_label[:20]}", callback_data=f"usr_remove:{u_id}")
        ])

    buttons.append([
        InlineKeyboardButton("➕ Створити запрошення", callback_data="inv_open_builder"),
        InlineKeyboardButton("❌ Закрити", callback_data="close_menu")
    ])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not await is_admin(user_id, ADMIN_ID):
        await update.message.reply_text("У вас немає прав для цієї команди 🚫")
        return

    await delete_user_command_message(update)
    text, kb = await build_users_list_view()
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")

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
