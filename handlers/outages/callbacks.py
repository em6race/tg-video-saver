"""Callback query handlers for Yasno Outages plugin."""

import time
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
from yasno_outages import yasno_client, format_outage_card, DEFAULT_REGIONS
from keyboards import get_outage_inline_keyboard, POWER_REFRESH_COOLDOWN
from handlers.common import check_access
from handlers.outages.state import _user_last_power_refresh, _user_last_view_day

logger = logging.getLogger(__name__)

async def handle_outages_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Main callback query dispatcher for power outages.
    Returns True if callback was handled, False otherwise.
    """
    query = update.callback_query
    choice = query.data or ""

    if not (choice.startswith("power_") or choice.startswith("switch_power_day:") or choice in (
        "refresh_power_status", "toggle_power_notif", "open_power_menu", "close_power_card"
    )):
        return False

    if not await check_access(update, context):
        await query.answer()
        return True

    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # 1. Ручне оновлення графіка (In-place silent refresh)
    if choice == "refresh_power_status":
        now = time.time()
        last_refresh = _user_last_power_refresh.get(user_id, 0)
        time_since = now - last_refresh

        if time_since < POWER_REFRESH_COOLDOWN:
            rem = int(POWER_REFRESH_COOLDOWN - time_since) + 1
            await query.answer(f"⏳ Зачекайте {rem} сек перед наступним оновленням!", show_alert=False)
            return True

        _user_last_power_refresh[user_id] = now
        sub = await get_outage_subscription(user_id)
        if not sub or not sub.get("group_name"):
            await query.answer("📍 Спочатку оберіть вашу групу!", show_alert=True)
            return True

        view_day = _user_last_view_day.get(user_id, "today")
        region_id = sub.get("region_id", 25)
        dso_id = sub.get("dso_id", 902)
        group = sub.get("group_name")
        region_name = sub.get("region_name", "Київ")
        dso_name = sub.get("dso_name", "ДТЕК")
        address = sub.get("address")
        notif_enabled = sub.get("notifications_enabled", True)

        try:
            schedule = await yasno_client.get_group_schedule(region_id, dso_id, group, force=True)
            if not schedule:
                await query.answer("❌ Графік для вашої групи не знайдено.", show_alert=True)
                return True

            card_text = format_outage_card(
                group_schedule=schedule,
                region_name=region_name,
                dso_name=dso_name,
                address=address,
                view_day=view_day,
                notifications_enabled=notif_enabled
            )
            kb = get_outage_inline_keyboard(notifications_enabled=notif_enabled, view_day=view_day)

            try:
                await query.edit_message_text(card_text, reply_markup=kb, parse_mode="Markdown")
            except Exception as edit_err:
                # If content is identical, Telegram raises 'Message is not modified'
                if "Message is not modified" not in str(edit_err):
                    logger.debug(f"In-place edit failed: {edit_err}")

            now_str = time.strftime("%H:%M:%S")
            await query.answer(f"✅ Графік актуальний ({now_str})", show_alert=False)

        except Exception as e:
            logger.error(f"Error on refresh_power_status: {e}")
            await query.answer(f"❌ Помилка оновлення: {e}", show_alert=True)
        return True

    # 2. Перемикання дня (Сьогодні / Завтра)
    elif choice.startswith("switch_power_day:"):
        target_day = choice.split(":")[-1]
        _user_last_view_day[user_id] = target_day

        sub = await get_outage_subscription(user_id)
        if not sub or not sub.get("group_name"):
            await query.answer()
            return True

        region_id = sub.get("region_id", 25)
        dso_id = sub.get("dso_id", 902)
        group = sub.get("group_name")
        region_name = sub.get("region_name", "Київ")
        dso_name = sub.get("dso_name", "ДТЕК")
        address = sub.get("address")
        notif_enabled = sub.get("notifications_enabled", True)

        schedule = await yasno_client.get_group_schedule(region_id, dso_id, group)
        if schedule:
            card_text = format_outage_card(
                group_schedule=schedule,
                region_name=region_name,
                dso_name=dso_name,
                address=address,
                view_day=target_day,
                notifications_enabled=notif_enabled
            )
            kb = get_outage_inline_keyboard(notifications_enabled=notif_enabled, view_day=target_day)
            try:
                await query.edit_message_text(card_text, reply_markup=kb, parse_mode="Markdown")
            except Exception:
                pass
        await query.answer()
        return True

    # 3. Перемикання сповіщень
    elif choice == "toggle_power_notif":
        new_val = await toggle_outage_notifications(user_id)
        view_day = _user_last_view_day.get(user_id, "today")
        kb = get_outage_inline_keyboard(notifications_enabled=new_val, view_day=view_day)
        try:
            await query.edit_message_reply_markup(reply_markup=kb)
        except Exception:
            pass

        toast = "🔔 Сповіщення про відключення УВІМКНЕНО!" if new_val else "🔕 Сповіщення ВИМКНЕНО!"
        await query.answer(toast, show_alert=False)
        return True

    # 4. Меню налаштувань групи та адреси
    elif choice == "open_power_menu":
        await query.answer()
        sub = await get_outage_subscription(user_id)
        curr_grp = sub.get("group_name", "не обрано") if sub else "не обрано"
        curr_reg = sub.get("region_name", "Київ") if sub else "Київ"

        menu_text = (
            "⚙️ **Керування графіком відключень**\n\n"
            f"• Поточний регіон: **{curr_reg}**\n"
            f"• Поточна черга (група): **{curr_grp}**\n\n"
            "Оберіть дію:"
        )

        buttons = [
            [
                InlineKeyboardButton("1️⃣ Група 1", callback_data="power_quick_group:1.1"),
                InlineKeyboardButton("2️⃣ Група 2", callback_data="power_quick_group:2.1"),
                InlineKeyboardButton("3️⃣ Група 3", callback_data="power_quick_group:3.1"),
            ],
            [
                InlineKeyboardButton("4️⃣ Група 4", callback_data="power_quick_group:4.1"),
                InlineKeyboardButton("5️⃣ Група 5", callback_data="power_quick_group:5.1"),
                InlineKeyboardButton("6️⃣ Група 6", callback_data="power_quick_group:6.1"),
            ],
            [
                InlineKeyboardButton("🔍 Пошук за адресою", callback_data="power_search_address"),
                InlineKeyboardButton("🗺️ Змінити регіон", callback_data="power_regions_list"),
            ],
            [
                InlineKeyboardButton("🏠 Назад до графіка", callback_data="power_show_card"),
                InlineKeyboardButton("❌ Закрити", callback_data="close_power_card")
            ]
        ]
        await query.edit_message_text(menu_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return True

    # 5. Швидкий вибір групи
    elif choice.startswith("power_quick_group:"):
        await query.answer()
        selected_grp = choice.split(":")[-1]
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
            group_name=selected_grp
        )

        # Render fresh card
        schedule = await yasno_client.get_group_schedule(region_id, dso_id, selected_grp)
        if schedule:
            card_text = format_outage_card(
                group_schedule=schedule,
                region_name=region_name,
                dso_name=dso_name,
                view_day="today",
                notifications_enabled=True
            )
            kb = get_outage_inline_keyboard(notifications_enabled=True, view_day="today")
            await query.edit_message_text(card_text, reply_markup=kb, parse_mode="Markdown")
            await set_outage_last_msg_id(user_id, query.message.message_id)
        else:
            await query.edit_message_text(
                f"✅ Групу **{selected_grp}** збережено, але розклад наразі відсутній.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="open_power_menu")]]),
                parse_mode="Markdown"
            )
        return True

    # 6. Список регіонів
    elif choice == "power_regions_list":
        await query.answer()
        regions = await yasno_client.get_regions()
        buttons = []
        for r in regions:
            r_name = r.get("value") or r.get("name")
            r_id = r.get("id")
            buttons.append([InlineKeyboardButton(f"📍 {r_name}", callback_data=f"power_sel_reg:{r_id}:{r_name}")])
        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="open_power_menu")])

        await query.edit_message_text(
            "🗺️ **Оберіть ваш регіон / місто:**",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
        return True

    # 7. Вибір регіону
    elif choice.startswith("power_sel_reg:"):
        await query.answer()
        parts = choice.split(":")
        r_id = int(parts[1])
        r_name = parts[2]

        regions = await yasno_client.get_regions()
        selected_r = next((r for r in regions if r.get("id") == r_id), None)
        dsos = selected_r.get("dsos", []) if selected_r else []

        if len(dsos) == 1:
            # Auto-select single DSO
            dso = dsos[0]
            d_id = dso.get("id")
            d_name = dso.get("name")
            sub = await get_outage_subscription(user_id)
            curr_grp = sub.get("group_name", "1.1") if sub else "1.1"
            await save_outage_group(
                user_id=user_id, chat_id=chat_id,
                region_id=r_id, region_name=r_name,
                dso_id=d_id, dso_name=d_name,
                group_name=curr_grp
            )
            # Show group picker
            buttons = [
                [
                    InlineKeyboardButton("1️⃣ Група 1", callback_data="power_quick_group:1.1"),
                    InlineKeyboardButton("2️⃣ Група 2", callback_data="power_quick_group:2.1"),
                    InlineKeyboardButton("3️⃣ Група 3", callback_data="power_quick_group:3.1"),
                ],
                [
                    InlineKeyboardButton("4️⃣ Група 4", callback_data="power_quick_group:4.1"),
                    InlineKeyboardButton("5️⃣ Група 5", callback_data="power_quick_group:5.1"),
                    InlineKeyboardButton("6️⃣ Група 6", callback_data="power_quick_group:6.1"),
                ],
                [InlineKeyboardButton("🔍 Пошук за адресою", callback_data="power_search_address")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="open_power_menu")]
            ]
            await query.edit_message_text(
                f"✅ Обрано регіон **{r_name}** ({d_name}).\nТепер оберіть вашу чергу (групу):",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown"
            )
        else:
            # Multiple DSOs (e.g. Dnipro DnEM vs CEK)
            buttons = []
            for d in dsos:
                buttons.append([InlineKeyboardButton(d.get("name"), callback_data=f"power_sel_dso:{r_id}:{r_name}:{d.get('id')}:{d.get('name')}")])
            buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="power_regions_list")])
            await query.edit_message_text(
                f"⚡ Оберіть оператора системи розподілу (ОСР) для **{r_name}**:",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown"
            )
        return True

    # 8. Вибір конкретного DSO
    elif choice.startswith("power_sel_dso:"):
        await query.answer()
        parts = choice.split(":")
        r_id = int(parts[1])
        r_name = parts[2]
        d_id = int(parts[3])
        d_name = parts[4]

        sub = await get_outage_subscription(user_id)
        curr_grp = sub.get("group_name", "1.1") if sub else "1.1"
        await save_outage_group(
            user_id=user_id, chat_id=chat_id,
            region_id=r_id, region_name=r_name,
            dso_id=d_id, dso_name=d_name,
            group_name=curr_grp
        )
        buttons = [
            [
                InlineKeyboardButton("1️⃣ Група 1.1", callback_data="power_quick_group:1.1"),
                InlineKeyboardButton("1️⃣ Група 1.2", callback_data="power_quick_group:1.2"),
            ],
            [
                InlineKeyboardButton("2️⃣ Група 2.1", callback_data="power_quick_group:2.1"),
                InlineKeyboardButton("2️⃣ Група 2.2", callback_data="power_quick_group:2.2"),
            ],
            [
                InlineKeyboardButton("3️⃣ Група 3.1", callback_data="power_quick_group:3.1"),
                InlineKeyboardButton("3️⃣ Група 3.2", callback_data="power_quick_group:3.2"),
            ],
            [
                InlineKeyboardButton("4️⃣ Група 4.1", callback_data="power_quick_group:4.1"),
                InlineKeyboardButton("4️⃣ Група 4.2", callback_data="power_quick_group:4.2"),
            ],
            [
                InlineKeyboardButton("5️⃣ Група 5.1", callback_data="power_quick_group:5.1"),
                InlineKeyboardButton("5️⃣ Група 5.2", callback_data="power_quick_group:5.2"),
            ],
            [
                InlineKeyboardButton("6️⃣ Група 6.1", callback_data="power_quick_group:6.1"),
                InlineKeyboardButton("6️⃣ Група 6.2", callback_data="power_quick_group:6.2"),
            ],
            [InlineKeyboardButton("🔍 Пошук за адресою", callback_data="power_search_address")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="open_power_menu")]
        ]
        await query.edit_message_text(
            f"✅ Обрано **{r_name}** ({d_name}).\nОберіть вашу підгрупу:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
        return True

    # 9. Пошук за адресою
    elif choice == "power_search_address":
        await query.answer()
        await set_outage_awaiting(user_id, chat_id, "search_street")
        await query.edit_message_text(
            "🔍 **Пошук черги за адресою**\n\n"
            "Напишіть у чат назву вашої вулиці (наприклад: `Хрещатик`, `Шевченка`, `Перемоги`):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Скасувати", callback_data="open_power_menu")]
            ]),
            parse_mode="Markdown"
        )
        return True

    # 10. Вибір вулиці
    elif choice.startswith("power_sel_st:"):
        await query.answer()
        st_id = int(choice.split(":")[-1])
        context.user_data["outage_street_id"] = st_id
        await set_outage_awaiting(user_id, chat_id, "search_house")

        await query.edit_message_text(
            "🏠 **Вулицю обрано!**\n\n"
            "Тепер напишіть у чат **номер вашого будинку** (наприклад: `1`, `15/2`, `44-А`):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Скасувати", callback_data="open_power_menu")]
            ]),
            parse_mode="Markdown"
        )
        return True

    # 11. Повернення на екран картки
    elif choice == "power_show_card":
        await query.answer()
        sub = await get_outage_subscription(user_id)
        if sub and sub.get("group_name"):
            region_id = sub.get("region_id", 25)
            dso_id = sub.get("dso_id", 902)
            group = sub.get("group_name")
            region_name = sub.get("region_name", "Київ")
            dso_name = sub.get("dso_name", "ДТЕК")
            address = sub.get("address")
            notif_enabled = sub.get("notifications_enabled", True)

            schedule = await yasno_client.get_group_schedule(region_id, dso_id, group)
            if schedule:
                card_text = format_outage_card(
                    group_schedule=schedule,
                    region_name=region_name,
                    dso_name=dso_name,
                    address=address,
                    view_day="today",
                    notifications_enabled=notif_enabled
                )
                kb = get_outage_inline_keyboard(notifications_enabled=notif_enabled, view_day="today")
                await query.edit_message_text(card_text, reply_markup=kb, parse_mode="Markdown")
                return True
        await prompt_group_setup(update, context)
        return True

    # 12. Закрити картку
    elif choice == "close_power_card":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return True

    return False
