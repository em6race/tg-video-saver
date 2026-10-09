"""Callback query handlers for Yasno Outages plugin."""

import time
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes

from database import (
    get_outage_subscription,
    save_outage_group,
    set_outage_awaiting,
    set_outage_last_msg_id,
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
        if not sub or not sub.get("group_name"):
            await query.answer("📍 Спочатку оберіть вашу чергу (групу)!", show_alert=True)
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
            kb = get_outage_inline_keyboard()

            if query.message.photo:
                try:
                    await query.edit_message_media(
                        media=InputMediaPhoto(media=img_buf.getvalue(), caption=caption, parse_mode="Markdown"),
                        reply_markup=kb,
                    )
                except Exception as edit_err:
                    if "Message is not modified" not in str(edit_err):
                        logger.debug(f"edit_message_media failed: {edit_err}")
            else:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                sent_msg = await query.message.reply_photo(
                    photo=img_buf.getvalue(),
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=kb,
                )
                await set_outage_last_msg_id(user_id, sent_msg.message_id)

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
        adv_enabled = sub.get("notify_advance_30m", True)

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

            # If user is currently in menu view, update menu buttons, otherwise update card
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
                        reply_markup=get_outage_inline_keyboard(),
                    )
                except Exception:
                    pass

        toast_day = "сьогодні" if target_day == "today" else "завтра"
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
        curr_grp = sub.get("group_name", "не обрано") if sub else "не обрано"
        curr_reg = sub.get("region_name", "Київ") if sub else "Київ"
        curr_dso = sub.get("dso_name", "ДТЕК") if sub else "ДТЕК"
        curr_addr = sub.get("address") or "не вказано"
        notif_enabled = sub.get("notifications_enabled", True) if sub else True
        adv_enabled = sub.get("notify_advance_30m", True) if sub else True
        view_day = _user_last_view_day.get(user_id, "today")

        menu_text = (
            "⚙️ **Меню керування графіком відключень**\n\n"
            f"• Регіон: **{curr_reg}** ({curr_dso})\n"
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
            try:
                await query.edit_message_caption(caption=menu_text, reply_markup=menu_kb, parse_mode="Markdown")
            except Exception:
                pass
        else:
            await query.edit_message_text(menu_text, reply_markup=menu_kb, parse_mode="Markdown")
        return True

    # 5. Вибір черги / адреси з меню
    elif choice == "outage_setup_start":
        await query.answer()
        sub = await get_outage_subscription(user_id)
        curr_reg = sub.get("region_name", "Київ") if sub else "Київ"

        setup_text = (
            f"📍 **Налаштування черги ({curr_reg})**\n\n"
            "Оберіть вашу чергу (групу) зі швидких кнопок нижче, "
            "знайдіть за адресою або скористайтеся координатами:"
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
                InlineKeyboardButton("🔙 Назад до меню", callback_data="open_power_menu")
            ]
        ]
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
            group_name=selected_grp,
        )

        # Re-render fresh card
        schedule = await yasno_client.get_group_schedule(region_id, dso_id, selected_grp)
        if schedule:
            view_day = _user_last_view_day.get(user_id, "today")
            img_buf = generate_outage_card_image(
                group_schedule=schedule,
                region_name=region_name,
                dso_name=dso_name,
                view_day=view_day,
            )
            caption = format_outage_caption(
                group_schedule=schedule,
                view_day=view_day,
                notifications_enabled=True,
            )
            kb = get_outage_inline_keyboard()

            if query.message.photo:
                await query.edit_message_media(
                    media=InputMediaPhoto(media=img_buf.getvalue(), caption=caption, parse_mode="Markdown"),
                    reply_markup=kb,
                )
            else:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                sent_msg = await query.message.reply_photo(
                    photo=img_buf.getvalue(),
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=kb,
                )
                await set_outage_last_msg_id(user_id, sent_msg.message_id)
        else:
            await query.edit_message_text(
                f"✅ Групу **{selected_grp}** збережено, але розклад наразі відсутній.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="open_power_menu")]]),
                parse_mode="Markdown",
            )
        return True

    # 7. Список регіонів
    elif choice in ("power_regions_list", "outage_change_region"):
        await query.answer()
        regions = await yasno_client.get_regions()
        buttons = []
        for r in regions:
            r_name = r.get("value") or r.get("name")
            r_id = r.get("id")
            buttons.append([InlineKeyboardButton(f"📍 {r_name}", callback_data=f"power_sel_reg:{r_id}:{r_name}")])
        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="open_power_menu")])

        reg_text = "🗺️ **Оберіть ваш регіон / область:**"
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
                [InlineKeyboardButton("⬅️ В меню", callback_data="open_power_menu")],
            ]
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

    # 9. Пошук за адресою
    elif choice == "power_search_address":
        await query.answer()
        await set_outage_awaiting(user_id, chat_id, "search_street")
        msg_text = (
            "🔍 **Пошук черги за адресою**\n\n"
            "Напишіть у чат назву вашої вулиці (наприклад: `Хрещатик`, `Шевченка`, `Перемоги`):"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="open_power_menu")]])
        if query.message.photo:
            try:
                await query.edit_message_caption(caption=msg_text, reply_markup=kb, parse_mode="Markdown")
            except Exception:
                pass
        else:
            await query.edit_message_text(msg_text, reply_markup=kb, parse_mode="Markdown")
        return True

    # 10. Вибір вулиці
    elif choice.startswith("power_sel_st:"):
        await query.answer()
        st_id = int(choice.split(":")[-1])
        context.user_data["outage_street_id"] = st_id
        await set_outage_awaiting(user_id, chat_id, "search_house")
        msg_text = (
            "🏠 **Вулицю обрано!**\n\n"
            "Тепер напишіть у чат **номер вашого будинку** (наприклад: `1`, `15/2`, `44-А`):"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Скасувати", callback_data="open_power_menu")]])
        if query.message.photo:
            try:
                await query.edit_message_caption(caption=msg_text, reply_markup=kb, parse_mode="Markdown")
            except Exception:
                pass
        else:
            await query.edit_message_text(msg_text, reply_markup=kb, parse_mode="Markdown")
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

        schedule = await yasno_client.get_group_schedule(region_id, dso_id, group)
        if schedule:
            view_day = _user_last_view_day.get(user_id, "today")
            img_buf = generate_outage_card_image(
                group_schedule=schedule,
                region_name=region_name,
                dso_name=dso_name,
                address=addr,
                view_day=view_day,
            )
            caption = format_outage_caption(
                group_schedule=schedule,
                view_day=view_day,
                notifications_enabled=True,
            )
            kb = get_outage_inline_keyboard()

            if query.message.photo:
                await query.edit_message_media(
                    media=InputMediaPhoto(media=img_buf.getvalue(), caption=caption, parse_mode="Markdown"),
                    reply_markup=kb,
                )
            else:
                sent_msg = await query.message.reply_photo(
                    photo=img_buf.getvalue(),
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=kb,
                )
                await set_outage_last_msg_id(user_id, sent_msg.message_id)
        return True

    # 13. Закрити картку
    elif choice == "close_power_card":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return True

    return False
