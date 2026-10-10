"""Callback query handlers for Yasno Outages plugin."""

import time
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes

from database import (
    get_outage_subscription,
    save_outage_group,
    save_outage_city,
    set_outage_awaiting,
    set_outage_last_msg_id,
    set_outage_menu_msg_id,
    clear_outage_menu_msg_id,
    toggle_outage_notifications,
    toggle_outage_advance_notification,
    save_outage_coords,
)
from yasno_outages import (
    yasno_client,
    format_outage_card,
    format_outage_caption,
    generate_outage_card_image,
    nudge_coords,
    resolve_group_from_coords,
    DEFAULT_REGIONS,
)
from keyboards import (
    get_outage_inline_keyboard,
    get_outage_menu_keyboard,
    get_coordinate_nudge_keyboard,
    POWER_REFRESH_COOLDOWN,
)
from handlers.common import check_access
from handlers.outages.state import _user_last_power_refresh, _user_last_view_day
from handlers.outages.commands import (
    repost_outage_card_to_bottom,
    cleanup_previous_outage_message,
)

logger = logging.getLogger(__name__)


async def handle_outages_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Main callback query dispatcher for power outages.
    Returns True if callback was handled, False otherwise.
    """
    query = update.callback_query
    choice = query.data or ""

    valid_prefixes = (
        "power_",
        "switch_power_day:",
        "nudge_coords:",
        "confirm_coords:",
    )
    valid_exact = (
        "refresh_power_status",
        "toggle_power_notif",
        "toggle_power_advance",
        "open_power_menu",
        "close_power_card",
        "outage_setup_start",
        "outage_change_region",
    )

    if not (any(choice.startswith(p) for p in valid_prefixes) or choice in valid_exact):
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

        # Якщо користувач натиснув "🔙 До графіка" всередині меню (текстове повідомлення)
        if not query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await clear_outage_menu_msg_id(user_id)

            if not sub or not sub.get("group_name") or sub.get("group_name") == "None":
                await query.answer("📍 Спочатку оберіть вашу чергу (групу)!", show_alert=True)
                return True

            await repost_outage_card_to_bottom(context, chat_id, user_id)
            await query.answer("✅ Повернуто до графіка", show_alert=False)
            return True

        # Користувач натиснув "🔄 Оновити" на самій картці (фото)
        if not sub or not sub.get("group_name") or sub.get("group_name") == "None":
            await query.answer("📍 Спочатку оберіть вашу чергу (групу)!", show_alert=True)
            return True

        # Видаляємо будь-яке старе меню, якщо раптом було відкрите
        await cleanup_previous_outage_message(context, chat_id, user_id, delete_card=False, delete_menu=True)

        view_day = _user_last_view_day.get(user_id, "today")
        region_id = sub.get("region_id", 25)
        dso_id = sub.get("dso_id", 902)
        group = sub.get("group_name")
        region_name = sub.get("city_name") or sub.get("region_name") or "Київ"
        dso_name = sub.get("dso_name", "ДТЕК")
        address = sub.get("address")
        notif_enabled = sub.get("notifications_enabled", True)

        try:
            schedule = await yasno_client.get_group_schedule(region_id, dso_id, group, force=True)
            if not schedule:
                await query.answer("❌ Графік для вашої групи наразі відсутній.", show_alert=True)
                return True

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

            try:
                await query.edit_message_media(
                    media=InputMediaPhoto(media=img_buf.getvalue(), caption=caption, parse_mode="Markdown"),
                    reply_markup=kb,
                )
            except Exception as edit_err:
                if "Message is not modified" not in str(edit_err):
                    logger.debug(f"edit_message_media failed: {edit_err}")

            now_str = time.strftime("%H:%M:%S")
            await query.answer(f"✅ Графік актуальний ({now_str})", show_alert=False)

        except Exception as e:
            logger.error(f"Error on refresh_power_status: {e}", exc_info=True)
            await query.answer(f"❌ Помилка оновлення: {e}", show_alert=True)
        return True

    # 2. Перемикання дня (Сьогодні / Завтра)
    elif choice.startswith("switch_power_day:"):
        target_day = choice.split(":")[-1]
        _user_last_view_day[user_id] = target_day

        sub = await get_outage_subscription(user_id)
        notif_enabled = sub.get("notifications_enabled", True) if sub else True
        adv_enabled = sub.get("notify_advance_30m", True) if sub else True
        toast_day = "сьогодні" if target_day == "today" else "завтра"

        # Якщо ми знаходимося в текстовому меню (не фото)
        if query.message.text and "Меню керування графіком" in query.message.text:
            menu_kb = get_outage_menu_keyboard(
                notifications_enabled=notif_enabled,
                notify_advance_30m=adv_enabled,
                view_day=target_day,
            )
            try:
                await query.edit_message_reply_markup(reply_markup=menu_kb)
            except Exception:
                pass
            await query.answer(f"📅 Розклад перемкнуто на {toast_day}")
            return True

        if not sub or not sub.get("group_name") or sub.get("group_name") == "None":
            await query.answer(f"📅 Розклад перемкнуто на {toast_day}")
            return True

        region_id = sub.get("region_id", 25)
        dso_id = sub.get("dso_id", 902)
        group = sub.get("group_name")
        region_name = sub.get("region_name", "Київ")
        dso_name = sub.get("dso_name", "ДТЕК")
        address = sub.get("address")

        schedule = await yasno_client.get_group_schedule(region_id, dso_id, group)
        if schedule:
            img_buf = generate_outage_card_image(
                group_schedule=schedule,
                region_name=region_name,
                dso_name=dso_name,
                address=address,
                view_day=target_day,
            )
            caption = format_outage_caption(
                group_schedule=schedule,
                view_day=target_day,
                notifications_enabled=notif_enabled,
            )

            # If user is currently in menu photo view, update menu buttons, otherwise update card
            if query.message.caption and "Меню керування графіком" in query.message.caption:
                menu_kb = get_outage_menu_keyboard(
                    notifications_enabled=notif_enabled,
                    notify_advance_30m=adv_enabled,
                    view_day=target_day,
                )
                try:
                    await query.edit_message_media(
                        media=InputMediaPhoto(media=img_buf.getvalue(), caption=query.message.caption, parse_mode="Markdown"),
                        reply_markup=menu_kb,
                    )
                except Exception:
                    pass
            elif query.message.photo:
                try:
                    await query.edit_message_media(
                        media=InputMediaPhoto(media=img_buf.getvalue(), caption=caption, parse_mode="Markdown"),
                        reply_markup=get_outage_inline_keyboard(view_day=target_day),
                    )
                except Exception:
                    pass

        await query.answer(f"📅 Розклад перемкнуто на {toast_day}")
        return True

    # 3. Перемикання основних сповіщень
    elif choice == "toggle_power_notif":
        new_val = await toggle_outage_notifications(user_id)
        sub = await get_outage_subscription(user_id)
        adv_enabled = sub.get("notify_advance_30m", True) if sub else True
        view_day = _user_last_view_day.get(user_id, "today")
        menu_kb = get_outage_menu_keyboard(
            notifications_enabled=new_val,
            notify_advance_30m=adv_enabled,
            view_day=view_day,
        )
        try:
            await query.edit_message_reply_markup(reply_markup=menu_kb)
        except Exception:
            pass

        toast = "🔔 Сповіщення про відключення УВІМКНЕНО!" if new_val else "🔕 Сповіщення ВИМКНЕНО!"
        await query.answer(toast, show_alert=False)
        return True

    # 3b. Перемикання завчасних сповіщень (за 30 хв)
    elif choice == "toggle_power_advance":
        new_val = await toggle_outage_advance_notification(user_id)
        sub = await get_outage_subscription(user_id)
        notif_enabled = sub.get("notifications_enabled", True) if sub else True
        view_day = _user_last_view_day.get(user_id, "today")
        menu_kb = get_outage_menu_keyboard(
            notifications_enabled=notif_enabled,
            notify_advance_30m=new_val,
            view_day=view_day,
        )
        try:
            await query.edit_message_reply_markup(reply_markup=menu_kb)
        except Exception:
            pass

        toast = "⏱ Попередження за 30 хв УВІМКНЕНО!" if new_val else "⏱ Попередження за 30 хв ВИМКНЕНО!"
        await query.answer(toast, show_alert=False)
        return True

    # 4. Меню налаштувань групи, сповіщень та адреси
    elif choice == "open_power_menu":
        await query.answer()
        sub = await get_outage_subscription(user_id)
        curr_grp = sub.get("group_name") if sub and sub.get("group_name") and sub.get("group_name") != "None" else "не обрано"
        curr_reg = (sub.get("city_name") or sub.get("region_name")) if sub and (sub.get("city_name") or sub.get("region_name")) else "не обрано"
        curr_dso = f" ({sub.get('dso_name')})" if sub and sub.get("dso_name") and (sub.get("city_name") or sub.get("region_name")) else ""
        curr_addr = sub.get("address") if sub and sub.get("address") else "не вказано"
        notif_enabled = sub.get("notifications_enabled", True) if sub else True
        adv_enabled = sub.get("notify_advance_30m", True) if sub else True
        view_day = _user_last_view_day.get(user_id, "today")

        menu_text = (
            "⚙️ **Меню керування графіком відключень**\n\n"
            f"• Регіон / місто: **{curr_reg}**{curr_dso}\n"
            f"• Черга (група): **{curr_grp}**\n"
            f"• Адреса: `{curr_addr}`\n\n"
            "Оберіть дію або налаштування нижче:"
        )
        menu_kb = get_outage_menu_keyboard(
            notifications_enabled=notif_enabled,
            notify_advance_30m=adv_enabled,
            view_day=view_day,
        )

        if query.message.photo:
            await set_outage_last_msg_id(user_id, query.message.message_id)
            if sub and sub.get("last_menu_msg_id"):
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=sub["last_menu_msg_id"])
                except Exception:
                    pass
            menu_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=menu_text,
                reply_markup=menu_kb,
                parse_mode="Markdown",
            )
            await set_outage_menu_msg_id(user_id, menu_msg.message_id)
        else:
            try:
                await query.edit_message_text(menu_text, reply_markup=menu_kb, parse_mode="Markdown")
            except Exception:
                pass
            await set_outage_menu_msg_id(user_id, query.message.message_id)
        return True

    # 5. Вибір черги / адреси з меню
    elif choice == "outage_setup_start":
        await query.answer()
        sub = await get_outage_subscription(user_id)
        curr_reg = (sub.get("city_name") or sub.get("region_name")) if sub and (sub.get("city_name") or sub.get("region_name")) else None

        if not curr_reg:
            await set_outage_awaiting(user_id, chat_id, "search_city")
            setup_text = (
                "📍 **Крок 1: Вкажіть ваш населений пункт або область**\n\n"
                "Бот підтримує розклади відключень для **всіх областей України** 🇺🇦.\n\n"
                "Напишіть у чат назву вашого міста чи села (наприклад: `Львів`, `Київ`, `Одеса`, `Полтава`, `Харків`, `Черкаси` тощо):\n\n"
                "💡 *Або оберіть зі швидких кнопок нижче:*"
            )
            buttons = [
                [
                    InlineKeyboardButton("🗺️ Оберіть область зі списку (Вся Україна)", callback_data="power_regions_list"),
                ],
                [
                    InlineKeyboardButton("🏙️ Київ", callback_data="power_set_city:25:902:м. Київ:ДТЕК Київські електромережі:0:Київ"),
                    InlineKeyboardButton("🏙️ Львів", callback_data="power_sel_reg:46:Львівська область"),
                ],
                [
                    InlineKeyboardButton("🏙️ Одеса", callback_data="power_sel_reg:51:Одеська область"),
                    InlineKeyboardButton("🏙️ Дніпро", callback_data="power_set_city:3:301:Дніпропетровська область:ДТЕК Дніпровські електромережі:6:м. Дніпро"),
                ],
                [
                    InlineKeyboardButton("🏙️ Харків", callback_data="power_sel_reg:63:Харківська область"),
                    InlineKeyboardButton("🏙️ Полтава", callback_data="power_sel_reg:53:Полтавська область"),
                ],
                [
                    InlineKeyboardButton("🔙 До меню", callback_data="open_power_menu"),
                ]
            ]
        else:
            setup_text = (
                f"📍 **Налаштування черги ({curr_reg})**\n\n"
                "Оберіть вашу чергу (групу) зі списку нижче або змініть область:"
            )
            buttons = [
                [
                    InlineKeyboardButton("1️⃣ Черга 1.1", callback_data="power_quick_group:1.1"),
                    InlineKeyboardButton("1️⃣ Черга 1.2", callback_data="power_quick_group:1.2"),
                ],
                [
                    InlineKeyboardButton("2️⃣ Черга 2.1", callback_data="power_quick_group:2.1"),
                    InlineKeyboardButton("2️⃣ Черга 2.2", callback_data="power_quick_group:2.2"),
                ],
                [
                    InlineKeyboardButton("3️⃣ Черга 3.1", callback_data="power_quick_group:3.1"),
                    InlineKeyboardButton("3️⃣ Черга 3.2", callback_data="power_quick_group:3.2"),
                ],
                [
                    InlineKeyboardButton("4️⃣ Черга 4.1", callback_data="power_quick_group:4.1"),
                    InlineKeyboardButton("4️⃣ Черга 4.2", callback_data="power_quick_group:4.2"),
                ],
                [
                    InlineKeyboardButton("5️⃣ Черга 5.1", callback_data="power_quick_group:5.1"),
                    InlineKeyboardButton("5️⃣ Черга 5.2", callback_data="power_quick_group:5.2"),
                ],
                [
                    InlineKeyboardButton("6️⃣ Черга 6.1", callback_data="power_quick_group:6.1"),
                    InlineKeyboardButton("6️⃣ Черга 6.2", callback_data="power_quick_group:6.2"),
                ],
                [
                    InlineKeyboardButton("🗺️ Змінити область / місто", callback_data="power_regions_list"),
                ],
            ]
            r_id = sub.get("region_id") if sub else None
            if r_id in (25, 3):
                buttons.append([InlineKeyboardButton("🔍 Пошук черги за адресою", callback_data="power_search_address")])
            buttons.append([InlineKeyboardButton("🔙 Назад до меню", callback_data="open_power_menu")])
        if query.message.photo:
            try:
                await query.edit_message_caption(caption=setup_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
            except Exception:
                pass
        else:
            await query.edit_message_text(setup_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return True

    # 6. Швидкий вибір групи
    elif choice.startswith("power_quick_group:"):
        await query.answer()
        selected_grp = choice.split(":")[-1]
        sub = await get_outage_subscription(user_id)
        if not sub or not sub.get("region_id"):
            await query.answer("📍 Спочатку вкажіть ваш населений пункт!", show_alert=True)
            await set_outage_awaiting(user_id, chat_id, "search_city")
            return True
        region_id = sub.get("region_id")
        region_name = sub.get("region_name")
        dso_id = sub.get("dso_id")
        dso_name = sub.get("dso_name")

        await save_outage_group(
            user_id=user_id,
            chat_id=chat_id,
            region_id=region_id,
            region_name=region_name,
            dso_id=dso_id,
            dso_name=dso_name,
            group_name=selected_grp,
        )

        # Видаляємо текстове повідомлення меню
        if not query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await clear_outage_menu_msg_id(user_id)

        # Оновлюємо або надсилаємо свіжу картку внизу чату
        await repost_outage_card_to_bottom(context, chat_id, user_id)
        await query.answer(f"✅ Встановлено чергу: {selected_grp}!", show_alert=False)
        return True

    # 7. Список регіонів
    elif choice in ("power_regions_list", "outage_change_region"):
        await query.answer()
        regions = await yasno_client.get_regions()
        buttons = []
        row = []
        for r in regions:
            r_name = r.get("short_name") or r.get("name")
            full_name = r.get("name") or r_name
            r_id = r.get("id")
            row.append(InlineKeyboardButton(f"📍 {r_name}", callback_data=f"power_sel_reg:{r_id}:{full_name}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="open_power_menu")])

        reg_text = "🗺️ **Оберіть вашу область / регіон України:**"
        if query.message.photo:
            try:
                await query.edit_message_caption(caption=reg_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
            except Exception:
                pass
        else:
            await query.edit_message_text(reg_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return True

    # 8. Вибір регіону
    elif choice.startswith("power_sel_reg:"):
        await query.answer()
        parts = choice.split(":")
        r_id = int(parts[1])
        r_name = parts[2]

        regions = await yasno_client.get_regions()
        selected_r = next((r for r in regions if r.get("id") == r_id), None)
        dsos = selected_r.get("dsos", []) if selected_r else []

        if len(dsos) <= 1:
            dso = dsos[0] if dsos else {"id": 902, "name": "ДТЕК"}
            d_id = dso.get("id")
            d_name = dso.get("name")
            sub = await get_outage_subscription(user_id)
            curr_grp = sub.get("group_name", "1.1") if sub else "1.1"
            await save_outage_group(
                user_id=user_id,
                chat_id=chat_id,
                region_id=r_id,
                region_name=r_name,
                dso_id=d_id,
                dso_name=d_name,
                group_name=curr_grp,
            )
            buttons = [
                [
                    InlineKeyboardButton("1️⃣ Черга 1.1", callback_data="power_quick_group:1.1"),
                    InlineKeyboardButton("1️⃣ Черга 1.2", callback_data="power_quick_group:1.2"),
                ],
                [
                    InlineKeyboardButton("2️⃣ Черга 2.1", callback_data="power_quick_group:2.1"),
                    InlineKeyboardButton("2️⃣ Черга 2.2", callback_data="power_quick_group:2.2"),
                ],
                [
                    InlineKeyboardButton("3️⃣ Черга 3.1", callback_data="power_quick_group:3.1"),
                    InlineKeyboardButton("3️⃣ Черга 3.2", callback_data="power_quick_group:3.2"),
                ],
                [
                    InlineKeyboardButton("4️⃣ Черга 4.1", callback_data="power_quick_group:4.1"),
                    InlineKeyboardButton("4️⃣ Черга 4.2", callback_data="power_quick_group:4.2"),
                ],
                [
                    InlineKeyboardButton("5️⃣ Черга 5.1", callback_data="power_quick_group:5.1"),
                    InlineKeyboardButton("5️⃣ Черга 5.2", callback_data="power_quick_group:5.2"),
                ],
                [
                    InlineKeyboardButton("6️⃣ Черга 6.1", callback_data="power_quick_group:6.1"),
                    InlineKeyboardButton("6️⃣ Черга 6.2", callback_data="power_quick_group:6.2"),
                ],
            ]
            if r_id in (25, 3):
                buttons.append([InlineKeyboardButton("🔍 Пошук черги за адресою", callback_data="power_search_address")])
            buttons.append([InlineKeyboardButton("⬅️ До списку областей", callback_data="power_regions_list")])
            buttons.append([InlineKeyboardButton("🏠 В меню", callback_data="open_power_menu")])
            txt = f"✅ Обрано регіон **{r_name}** ({d_name}).\nТепер оберіть вашу чергу (групу):"
            if query.message.photo:
                try:
                    await query.edit_message_caption(caption=txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
                except Exception:
                    pass
            else:
                await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        else:
            buttons = []
            for d in dsos:
                buttons.append([InlineKeyboardButton(d.get("name"), callback_data=f"power_sel_dso:{r_id}:{r_name}:{d.get('id')}:{d.get('name')}")])
            buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="power_regions_list")])
            txt = f"⚡ Оберіть оператора розподілу (ОСР) для **{r_name}**:"
            if query.message.photo:
                try:
                    await query.edit_message_caption(caption=txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
                except Exception:
                    pass
            else:
                await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return True

    # 8b. Вибір оператора DSO
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
            user_id=user_id,
            chat_id=chat_id,
            region_id=r_id,
            region_name=r_name,
            dso_id=d_id,
            dso_name=d_name,
            group_name=curr_grp,
        )
        buttons = [
            [
                InlineKeyboardButton("1️⃣ Черга 1.1", callback_data="power_quick_group:1.1"),
                InlineKeyboardButton("1️⃣ Черга 1.2", callback_data="power_quick_group:1.2"),
            ],
            [
                InlineKeyboardButton("2️⃣ Черга 2.1", callback_data="power_quick_group:2.1"),
                InlineKeyboardButton("2️⃣ Черга 2.2", callback_data="power_quick_group:2.2"),
            ],
            [
                InlineKeyboardButton("3️⃣ Черга 3.1", callback_data="power_quick_group:3.1"),
                InlineKeyboardButton("3️⃣ Черга 3.2", callback_data="power_quick_group:3.2"),
            ],
            [
                InlineKeyboardButton("4️⃣ Черга 4.1", callback_data="power_quick_group:4.1"),
                InlineKeyboardButton("4️⃣ Черга 4.2", callback_data="power_quick_group:4.2"),
            ],
            [
                InlineKeyboardButton("5️⃣ Черга 5.1", callback_data="power_quick_group:5.1"),
                InlineKeyboardButton("5️⃣ Черга 5.2", callback_data="power_quick_group:5.2"),
            ],
            [
                InlineKeyboardButton("6️⃣ Черга 6.1", callback_data="power_quick_group:6.1"),
                InlineKeyboardButton("6️⃣ Черга 6.2", callback_data="power_quick_group:6.2"),
            ],
        ]
        if r_id in (25, 3):
            buttons.append([InlineKeyboardButton("🔍 Пошук черги за адресою", callback_data="power_search_address")])
        buttons.append([InlineKeyboardButton("⬅️ До списку областей", callback_data="power_regions_list")])
        buttons.append([InlineKeyboardButton("🏠 В меню", callback_data="open_power_menu")])
        txt = f"✅ Обрано **{r_name}** ({d_name}).\nТепер оберіть вашу чергу (групу):"
        if query.message.photo:
            try:
                await query.edit_message_caption(caption=txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
            except Exception:
                pass
        else:
            await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return True

    # 9. Пошук за адресою (Крок 1: Населений пункт)
    elif choice == "power_search_address":
        await query.answer()
        await set_outage_awaiting(user_id, chat_id, "search_city")
        msg_text = (
            "📍 **Крок 1: Вкажіть ваш населений пункт або область**\n\n"
            "Бот підтримує розклади відключень для **всіх областей України** 🇺🇦.\n\n"
            "Напишіть у чат назву вашого міста чи села (наприклад: `Львів`, `Київ`, `Одеса`, `Полтава`, `Черкаси` тощо):\n\n"
            "💡 *Або оберіть область зі списку чи швидких кнопок нижче:*"
        )
        buttons = [
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
                InlineKeyboardButton("⬅️ Назад в меню", callback_data="open_power_menu"),
            ]
        ]
        if query.message.photo:
            try:
                await query.edit_message_caption(caption=msg_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
            except Exception:
                pass
        else:
            await query.edit_message_text(msg_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return True

    # 9b. Збереження обраного міста та перехід до Кроку 2 (Вулиця)
    elif choice.startswith("power_set_city:"):
        await query.answer()
        parts = choice.split(":")
        r_id = int(parts[1])
        d_id = int(parts[2])
        r_name = parts[3]
        d_name = parts[4]
        c_id = int(parts[5]) if len(parts) > 5 and parts[5] != "0" else None
        c_name = parts[6] if len(parts) > 6 else r_name

        await save_outage_city(
            user_id=user_id,
            chat_id=chat_id,
            region_id=r_id,
            region_name=r_name,
            dso_id=d_id,
            dso_name=d_name,
            city_id=c_id,
            city_name=c_name,
        )
        context.user_data["outage_region_id"] = r_id
        context.user_data["outage_dso_id"] = d_id
        context.user_data["outage_city_id"] = c_id
        context.user_data["outage_city_name"] = c_name

        await set_outage_awaiting(user_id, chat_id, "search_street")
        msg_text = (
            f"🔍 **Крок 2 з 3: Вулиця ({c_name})**\n\n"
            "Напишіть у чат назву вашої вулиці (наприклад: `Центральна`, `Шевченка`, `Хрещатик`):"
        )
        buttons = [
            [InlineKeyboardButton("⬅️ Змінити місто", callback_data="power_search_address")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="open_power_menu")],
        ]
        if query.message.photo:
            try:
                await query.edit_message_caption(caption=msg_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
            except Exception:
                pass
        else:
            await query.edit_message_text(msg_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return True

    # 10. Вибір вулиці
    elif choice.startswith("power_sel_st:"):
        await query.answer()
        parts = choice.split(":", 2)
        st_id = int(parts[1])
        st_name = parts[2] if len(parts) > 2 else ""

        context.user_data["outage_street_id"] = st_id
        context.user_data["outage_street_name"] = st_name
        await set_outage_awaiting(user_id, chat_id, "search_house")

        sub = await get_outage_subscription(user_id)
        city_display = (sub.get("city_name") if sub else None) or (context.user_data.get("outage_city_name")) or "обраного міста"

        msg_text = (
            f"🏠 **Крок 3 з 3: Номер будинку**\n\n"
            f"• Вулиця: **{st_name}** ({city_display})\n\n"
            "Тепер напишіть у чат **номер вашого будинку** (наприклад: `1`, `15/2`, `44-А`):"
        )
        buttons = [
            [InlineKeyboardButton("⬅️ Змінити вулицю", callback_data="power_search_address")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="open_power_menu")],
        ]
        if query.message.photo:
            try:
                await query.edit_message_caption(caption=msg_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
            except Exception:
                pass
        else:
            await query.edit_message_text(msg_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return True

    # 11. Коригування координат (джойстик ±50м)
    elif choice.startswith("nudge_coords:"):
        await query.answer()
        parts = choice.split(":")
        direction = parts[1]
        lat = float(parts[2])
        lon = float(parts[3])

        new_lat, new_lon = nudge_coords(lat, lon, direction, 50.0)
        sub = await get_outage_subscription(user_id)
        region_id = sub.get("region_id", 25) if sub else 25
        dso_id = sub.get("dso_id", 902) if sub else 902

        group, addr, err = await resolve_group_from_coords(new_lat, new_lon, region_id, dso_id)

        nudge_text = (
            "📍 **Коригування позиції (±50м)**\n\n"
            f"• Координати: `{new_lat:.5f}, {new_lon:.5f}`\n"
            f"• Адреса: **{addr or 'Визначається...'}**\n"
            f"• Черга: **{group or 'Не визначено'}**\n\n"
            "Використовуйте кнопки для зміщення або натисніть Підтвердити:"
        )
        kb = get_coordinate_nudge_keyboard(new_lat, new_lon)

        if query.message.photo:
            try:
                await query.edit_message_caption(caption=nudge_text, reply_markup=kb, parse_mode="Markdown")
            except Exception:
                pass
        else:
            await query.edit_message_text(nudge_text, reply_markup=kb, parse_mode="Markdown")
        return True

    # 12. Підтвердження координат
    elif choice.startswith("confirm_coords:"):
        await query.answer()
        parts = choice.split(":")
        lat = float(parts[1])
        lon = float(parts[2])

        sub = await get_outage_subscription(user_id)
        region_id = sub.get("region_id", 25) if sub else 25
        dso_id = sub.get("dso_id", 902) if sub else 902
        region_name = sub.get("region_name", "Київ") if sub else "Київ"
        dso_name = sub.get("dso_name", "ДТЕК") if sub else "ДТЕК"

        group, addr, err = await resolve_group_from_coords(lat, lon, region_id, dso_id)
        if not group:
            await query.answer("❌ Будинок або групу не вдалося визначити. Оберіть вручну.", show_alert=True)
            return True

        await save_outage_coords(user_id, lat, lon, addr)
        await save_outage_group(
            user_id=user_id,
            chat_id=chat_id,
            region_id=region_id,
            region_name=region_name,
            dso_id=dso_id,
            dso_name=dso_name,
            group_name=group,
            address=addr,
            lat=lat,
            lon=lon,
        )

        # Видаляємо повідомлення налаштування / джойстика
        if not query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await clear_outage_menu_msg_id(user_id)

        # Оновлюємо або надсилаємо свіжу картку внизу чату
        await repost_outage_card_to_bottom(context, chat_id, user_id)
        await query.answer(f"✅ Встановлено чергу: {group}!", show_alert=False)
        return True

    # 13. Закрити картку / меню
    elif choice == "close_power_card":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        if query.message.photo:
            await cleanup_previous_outage_message(context, chat_id, user_id, delete_card=True, delete_menu=True)
        else:
            await clear_outage_menu_msg_id(user_id)
        return True

    return False
