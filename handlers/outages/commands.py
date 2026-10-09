"""Command handlers for Yasno Outages plugin."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import (
    get_outage_subscription,
    save_outage_group,
    set_outage_awaiting,
    set_outage_last_msg_id,
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

async def prompt_group_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_msg: str = "") -> None:
    """Prompts the user to select their settlement, region, or search by address."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await set_outage_awaiting(user_id, chat_id, "search_city")

    msg_text = (
        f"{prefix_msg}"
        "⚡ **Налаштування графіка відключень світла**\n\n"
        "Вкажіть вашу адресу, щоб бот визначив чергу (групу) та показував актуальний розклад:\n\n"
        "📍 **Крок 1 з 3: Населений пункт**\n"
        "Напишіть у чат назву вашого міста чи села (наприклад: `Київ`, `Дніпро`, `Кам'янське`, `Нікополь`, `Павлоград` тощо)\n\n"
        "💡 *Або оберіть зі швидких кнопок нижче:*"
    )

    keyboard = [
        [
            InlineKeyboardButton("🏙️ Київ", callback_data="power_set_city:25:902:Київ:ДТЕК Київські електромережі:0:Київ"),
        ],
        [
            InlineKeyboardButton("🏙️ Дніпро (ДТЕК)", callback_data="power_set_city:3:301:Дніпро:ДТЕК Дніпровські електромережі:6:м. Дніпро"),
            InlineKeyboardButton("🏙️ Дніпро (ЦЕК)", callback_data="power_set_city:3:303:Дніпро:ЦЕК:6:Дніпро"),
        ],
        [
            InlineKeyboardButton("🗺️ Список всіх областей", callback_data="power_regions_list"),
            InlineKeyboardButton("❌ Закрити", callback_data="close_power_card")
        ]
    ]

    await update.effective_message.reply_text(
        msg_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

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
    if not sub or not sub.get("group_name") or not sub.get("region_id"):
        await prompt_group_setup(update, context)
        return

    view_day = _user_last_view_day.get(user_id, "today")
    region_id = sub.get("region_id")
    dso_id = sub.get("dso_id")
    group = sub.get("group_name")
    region_name = sub.get("city_name") or sub.get("region_name") or "Регіон"
    dso_name = sub.get("dso_name") or "ДТЕК"
    address = sub.get("address")
    notif_enabled = sub.get("notifications_enabled", True)

    try:
        schedule = await yasno_client.get_group_schedule(region_id, dso_id, group)
        if not schedule:
            await update.effective_message.reply_text(
                f"❌ Не вдалося знайти групу **{group}** для **{region_name}** ({dso_name}).\n\n"
                "Спробуйте обрати чергу зі списку або знайти за адресою:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ Налаштувати групу", callback_data="open_power_menu")]
                ]),
                parse_mode="Markdown"
            )
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

        sent_msg = await update.effective_message.reply_photo(
            photo=img_buf.getvalue(),
            caption=caption,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        await set_outage_last_msg_id(user_id, sent_msg.message_id)

    except Exception as e:
        logger.error(f"Error fetching outages for user {user_id}: {e}", exc_info=True)
        await update.effective_message.reply_text(
            f"❌ Не вдалося завантажити графік: `{e}`\n"
            "Спробуйте оновити пізніше або змінити групу.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Спробувати знову", callback_data="refresh_power_status")],
                [InlineKeyboardButton("⚙️ Змінити групу", callback_data="open_power_menu")]
            ]),
            parse_mode="Markdown"
        )

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
        await update.effective_message.reply_text(
            f"✅ Встановлено чергу (групу): **{group_candidate}** ({region_name})!",
            parse_mode="Markdown"
        )
        await power_command(update, context)
    else:
        await prompt_group_setup(update, context)

async def address_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /address для пошуку групи за адресою."""
    if not await check_access(update, context):
        return
    await delete_user_command_message(update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    args = context.args
    if args and len(args) > 0:
        query = " ".join(args).strip()
        status_msg = await update.effective_message.reply_text(f"🔍 Шукаю вулицю «{query}»...")
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
        await update.effective_message.reply_text(
            "🔍 **Пошук групи за адресою**\n\n"
            "Напишіть у чат назву вашої вулиці (наприклад: `Хрещатик`, `Шевченка`, `Героїв полку Азов`):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Скасувати", callback_data="close_power_card")]
            ]),
            parse_mode="Markdown"
        )
