"""Command handlers for Yasno Outages plugin."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import (
    get_outage_subscription,
    save_outage_group,
    set_outage_awaiting,
    set_outage_last_msg_id,
    set_outage_menu_msg_id,
    clear_outage_menu_msg_id,
    toggle_outage_notifications,
)
from yasno_outages import (
    yasno_client,
    format_outage_card,
    format_outage_caption,
    generate_outage_card_image,
    DEFAULT_REGIONS,
)
from keyboards import get_outage_inline_keyboard, get_main_reply_keyboard
from handlers.common import check_access, delete_user_command_message
from handlers.outages.state import _user_last_view_day

logger = logging.getLogger(__name__)

async def cleanup_previous_outage_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    delete_card: bool = True,
    delete_menu: bool = True,
    extra_msg_id: int = None,
) -> None:
    """Видаляє попередні повідомлення (картку та/або меню), щоб чат не засмічувався."""
    try:
        sub = await get_outage_subscription(user_id)
        to_delete = set()
        if extra_msg_id:
            to_delete.add(extra_msg_id)
        if sub:
            if delete_card and sub.get("last_msg_id"):
                to_delete.add(sub["last_msg_id"])
            if delete_menu and sub.get("last_menu_msg_id"):
                to_delete.add(sub["last_menu_msg_id"])

        for mid in to_delete:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

        if delete_menu:
            await clear_outage_menu_msg_id(user_id)
    except Exception as e:
        logger.debug(f"cleanup_previous_outage_message error: {e}")

async def repost_outage_card_to_bottom(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    view_day: str = None,
    extra_msg_id: int = None,
) -> None:
    """
    Видаляє старе меню та попередню картку, і надсилає актуальний графік
    насамкінець чату (як головний екран). Залишає рівно 1 повідомлення в чаті!
    """
    sub = await get_outage_subscription(user_id)
    if not sub or not sub.get("group_name") or sub.get("group_name") == "None":
        return

    # Видаляємо старе меню та попередню картку
    await cleanup_previous_outage_message(
        context, chat_id, user_id, delete_card=True, delete_menu=True, extra_msg_id=extra_msg_id
    )

    if view_day is None:
        view_day = _user_last_view_day.get(user_id, "today")
    else:
        _user_last_view_day[user_id] = view_day

    region_id = sub.get("region_id") or 25
    dso_id = sub.get("dso_id") or 902
    group = sub.get("group_name")
    region_name = sub.get("city_name") or sub.get("region_name") or "Київ"
    dso_name = sub.get("dso_name") or "ДТЕК"
    address = sub.get("address")
    notif_enabled = sub.get("notifications_enabled", True)

    try:
        schedule = await yasno_client.get_group_schedule(region_id, dso_id, group)
        if not schedule:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Не вдалося знайти групу **{group}** для **{region_name}** ({dso_name}).\n\n"
                     "Спробуйте обрати чергу зі списку або знайти за адресою:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ Налаштувати групу", callback_data="open_power_menu")]
                ]),
                parse_mode="Markdown"
            )
            await set_outage_menu_msg_id(user_id, sent_msg.message_id)
            return

        img_buf = generate_outage_card_image(
            group_schedule=schedule,
            region_name=region_name,
            dso_name=dso_name,
            address=address,
            view_day=view_day,
        )
        caption = format_outage_caption(
            group_schedule=schedule,
            view_day=view_day,
            notifications_enabled=notif_enabled,
        )
        kb = get_outage_inline_keyboard(view_day=view_day)

        sent_msg = await context.bot.send_photo(
            chat_id=chat_id,
            photo=img_buf.getvalue(),
            caption=caption,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        await set_outage_last_msg_id(user_id, sent_msg.message_id)
    except Exception as e:
        logger.error(f"Error fetching outages for user {user_id}: {e}", exc_info=True)
        sent_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Не вдалося завантажити графік: `{e}`\n"
                 "Спробуйте оновити пізніше або змінити групу.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Спробувати знову", callback_data="refresh_power_status")],
                [InlineKeyboardButton("⚙️ Змінити групу", callback_data="open_power_menu")]
            ]),
            parse_mode="Markdown"
        )
        await set_outage_menu_msg_id(user_id, sent_msg.message_id)

async def prompt_group_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_msg: str = "") -> None:
    """Prompts the user to select their settlement, region, or search by address."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await delete_user_command_message(update)
    # Очищуємо старе меню (але картку не чіпаємо, якщо вона є)
    await cleanup_previous_outage_message(context, chat_id, user_id, delete_card=False, delete_menu=True)
    await set_outage_awaiting(user_id, chat_id, "search_city")

    msg_text = (
        f"{prefix_msg}"
        "⚡ **Налаштування графіка відключень світла**\n\n"
        "Бот підтримує розклади відключень для **всіх областей України** 🇺🇦.\n\n"
        "📍 **Крок 1: Населений пункт або область**\n"
        "Напишіть у чат назву вашого міста чи села (наприклад: `Львів`, `Київ`, `Одеса`, `Полтава`, `Черкаси` тощо)\n\n"
        "💡 *Або оберіть вашу область зі списку нижче:*"
    )

    keyboard = [
        [
            InlineKeyboardButton("🗺️ Список всіх 24 областей України", callback_data="power_regions_list"),
        ],
        [
            InlineKeyboardButton("🏙️ Київ", callback_data="power_set_city:25:902:м. Київ:ДТЕК Київські електромережі:0:Київ"),
            InlineKeyboardButton("🏙️ Львів", callback_data="power_sel_reg:46:Львівська область"),
        ],
        [
            InlineKeyboardButton("🏙️ Одеса", callback_data="power_sel_reg:51:Одеська область"),
            InlineKeyboardButton("🏙️ Дніпро", callback_data="power_sel_reg:3:Дніпропетровська область"),
        ],
        [
            InlineKeyboardButton("🏙️ Харків", callback_data="power_sel_reg:63:Харківська область"),
            InlineKeyboardButton("🏙️ Полтава", callback_data="power_sel_reg:53:Полтавська область"),
        ],
        [
            InlineKeyboardButton("❌ Закрити", callback_data="close_power_card")
        ]
    ]

    sent_msg = await update.effective_message.reply_text(
        msg_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    await set_outage_menu_msg_id(user_id, sent_msg.message_id)

async def power_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Головна команда /power або /svitlo: відображає інтерактивний екран стану світла."""
    if not await check_access(update, context):
        return

    await delete_user_command_message(update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Check if arguments were passed (e.g. /power on/off)
    args = context.args
    if args and len(args) > 0:
        arg_lower = args[0].lower().strip()
        if arg_lower in ("on", "1", "start", "enable", "увімк", "увімкнути"):
            await power_on_command(update, context)
            return
        elif arg_lower in ("off", "0", "stop", "disable", "вимк", "вимкнути"):
            await power_off_command(update, context)
            return

    sub = await get_outage_subscription(user_id)
    if not sub or not sub.get("group_name") or not sub.get("region_id") or sub.get("group_name") == "None":
        await prompt_group_setup(update, context)
        return

    await repost_outage_card_to_bottom(context, chat_id, user_id)

async def power_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Увімкнення сповіщень про відключення світла."""
    if not await check_access(update, context):
        return
    await delete_user_command_message(update)
    user_id = update.effective_user.id
    sub = await get_outage_subscription(user_id)
    if not sub or not sub.get("notifications_enabled"):
        await toggle_outage_notifications(user_id)

    await update.effective_message.reply_text(
        "🔔 **Сповіщення про відключення світла УВІМКНЕНО!**\n\n"
        "Бот заздалегідь (за ~30 хв) попередить вас перед вимкненням, "
        "а також надішле оновлення при зміні графіків Укренерго.\n"
        "Вимкнути можна будь-коли командою `/power_off`.",
        reply_markup=get_main_reply_keyboard(),
        parse_mode="Markdown"
    )

async def power_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вимкнення сповіщень про відключення світла."""
    if not await check_access(update, context):
        return
    await delete_user_command_message(update)
    user_id = update.effective_user.id
    sub = await get_outage_subscription(user_id)
    if sub and sub.get("notifications_enabled"):
        await toggle_outage_notifications(user_id)

    await update.effective_message.reply_text(
        "🔕 **Сповіщення про відключення світла ВИМКНЕНО.**\n\n"
        "Ви не будете отримувати завчасні попередження. "
        "Увімкнути знову можна командою `/power_on`.",
        reply_markup=get_main_reply_keyboard(),
        parse_mode="Markdown"
    )

async def group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /group для швидкого встановлення або перегляду черги."""
    if not await check_access(update, context):
        return
    await delete_user_command_message(update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    args = context.args
    if args and len(args) > 0:
        group_candidate = args[0].strip()
        if not "." in group_candidate and group_candidate.isdigit():
            group_candidate = f"{group_candidate}.1"

        sub = await get_outage_subscription(user_id)
        region_id = sub.get("region_id", 25) if sub else 25
        region_name = sub.get("region_name", "Київ") if sub else "Київ"
        dso_id = sub.get("dso_id", 902) if sub else 902
        dso_name = sub.get("dso_name", "ДТЕК Київські електромережі") if sub else "ДТЕК Київські електромережі"

        await save_outage_group(
            user_id=user_id,
            chat_id=chat_id,
            region_id=region_id,
            region_name=region_name,
            dso_id=dso_id,
            dso_name=dso_name,
            group_name=group_candidate
        )
        await repost_outage_card_to_bottom(context, chat_id, user_id)
    else:
        await prompt_group_setup(update, context)

async def address_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /address для пошуку групи за адресою."""
    if not await check_access(update, context):
        return
    await delete_user_command_message(update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await cleanup_previous_outage_message(context, chat_id, user_id, delete_card=False, delete_menu=True)

    args = context.args
    if args and len(args) > 0:
        query = " ".join(args).strip()
        status_msg = await update.effective_message.reply_text(f"🔍 Шукаю вулицю «{query}»...")
        await set_outage_menu_msg_id(user_id, status_msg.message_id)
        streets = await yasno_client.search_streets(25, 902, query)
        if streets:
            buttons = []
            for s in streets[:6]:
                buttons.append([InlineKeyboardButton(s["value"], callback_data=f"power_sel_st:{s['id']}")])
            buttons.append([InlineKeyboardButton("❌ Скасувати", callback_data="close_power_card")])
            await status_msg.edit_text(
                f"📍 Оберіть вашу вулицю серед знайдених за запитом «{query}»:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        else:
            await status_msg.edit_text(
                f"❌ Вулицю «{query}» не знайдено.\n"
                "Спробуйте ввести лише назву без слова «вулиця» (наприклад: `Хрещатик` або `Перемоги`):"
            )
            await set_outage_awaiting(user_id, chat_id, "search_street")
    else:
        await set_outage_awaiting(user_id, chat_id, "search_street")
        sent_msg = await update.effective_message.reply_text(
            "🔍 **Пошук групи за адресою**\n\n"
            "Напишіть у чат назву вашої вулиці (наприклад: `Хрещатик`, `Шевченка`, `Героїв полку Азов`):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Скасувати", callback_data="close_power_card")]
            ]),
            parse_mode="Markdown"
        )
        await set_outage_menu_msg_id(user_id, sent_msg.message_id)
