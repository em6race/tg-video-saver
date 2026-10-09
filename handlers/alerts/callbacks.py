import os
import time
import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import (
    is_admin,
    set_awaiting_location,
    save_user_location,
    disable_user_alerts,
    enable_user_alerts,
    get_user_subscription,
    set_user_last_alert_msg_id,
    toggle_user_sound,
    toggle_user_putin,
    revoke_invite,
    clear_unused_invites,
)
from neptun_alerts import (
    forward_geocode,
    fetch_neptun_data,
    evaluate_user_alert_state,
    format_alert_message,
    make_threat_signature,
    generate_alerts_map_image,
    UKRAINE_OBLASTS,
)
from keyboards import (
    get_alert_inline_keyboard,
    get_main_reply_keyboard,
    ALERT_REFRESH_COOLDOWN,
)
from handlers.common import (
    check_access,
    help_command,
    is_media_plugin_enabled,
    is_alerts_plugin_enabled,
    is_outages_plugin_enabled,
    ADMIN_ID,
)
from handlers.admin import (
    get_invite_builder_keyboard,
    send_created_invite_message,
    build_invites_list_view,
)
from handlers.alerts.state import (
    _user_last_alert_state,
    _user_last_threat_signature,
    _user_last_manual_refresh,
)
from handlers.alerts.location import (
    prompt_location_confirmation,
    save_and_confirm_location,
)
from handlers.alerts.commands import (
    alerts_command,
    status_command,
    map_command,
    handle_manual_alert_refresh,
)
from handlers.alerts.monitoring import repost_alert_message_to_bottom

logger = logging.getLogger(__name__)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if not await check_access(update, context):
        await query.answer()
        return

    choice = query.data or ""

    # --- ОБРОБКА ПЛАГІНА ВІДКЛЮЧЕНЬ СВІТЛА (YASNO / ДТЕК) ---
    if is_outages_plugin_enabled():
        from handlers.outages.callbacks import handle_outages_callback_query
        if await handle_outages_callback_query(update, context):
            return

    # --- ОБРОБКА КОНСТРУКТОРА ТА СПИСКУ ІНВАЙТІВ (ДЛЯ АДМІНА) ---
    if choice.startswith("inv_"):
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        if not await is_admin(user_id, ADMIN_ID):
            await query.answer("У вас немає прав для цієї дії 🚫", show_alert=True)
            return

        if choice.startswith("inv_cfg_u:"):
            parts = choice.split(":")
            sel_uses = int(parts[1])
            sel_dur = int(parts[2])
            await query.answer()
            text, kb = get_invite_builder_keyboard(selected_uses=sel_uses, selected_dur=sel_dur)
            await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
            return

        elif choice.startswith("inv_cfg_d:"):
            parts = choice.split(":")
            sel_uses = int(parts[1])
            sel_dur = int(parts[2])
            await query.answer()
            text, kb = get_invite_builder_keyboard(selected_uses=sel_uses, selected_dur=sel_dur)
            await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
            return

        elif choice.startswith("inv_create:"):
            parts = choice.split(":")
            sel_uses = int(parts[1])
            sel_dur = int(parts[2])
            await query.answer("🎟️ Створюю запрошення...")
            try:
                await query.message.delete()
            except Exception:
                pass
            await send_created_invite_message(context.bot, chat_id, user_id, max_uses=sel_uses, duration_minutes=sel_dur)
            return

        elif choice == "inv_open_builder":
            await query.answer()
            text, kb = get_invite_builder_keyboard(selected_uses=1, selected_dur=30)
            await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
            return

        elif choice == "inv_list":
            await query.answer()
            text, kb = await build_invites_list_view(context.bot)
            await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown", disable_web_page_preview=True)
            return

        elif choice.startswith("inv_revoke:"):
            code = choice.split(":", 1)[1]
            ok, _ = await revoke_invite(code)
            await query.answer("🗑️ Посилання-запрошення анульовано!", show_alert=False)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        elif choice.startswith("inv_revoke_list:"):
            code = choice.split(":", 1)[1]
            ok, msg_pair = await revoke_invite(code)
            if msg_pair:
                c_id, m_id = msg_pair
                try:
                    await context.bot.delete_message(chat_id=c_id, message_id=m_id)
                except Exception:
                    pass
            await query.answer("🗑️ Запрошення анульовано!", show_alert=False)
            text, kb = await build_invites_list_view(context.bot)
            await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown", disable_web_page_preview=True)
            return

        elif choice == "inv_clear_all":
            deleted, msgs_to_delete = await clear_unused_invites()
            for c_id, m_id in msgs_to_delete:
                try:
                    await context.bot.delete_message(chat_id=c_id, message_id=m_id)
                except Exception:
                    pass
            await query.answer(f"🗑️ Анульовано запрошень: {deleted}", show_alert=True)
            text, kb = await build_invites_list_view(context.bot)
            try:
                await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown", disable_web_page_preview=True)
            except Exception:
                pass
            return

    if choice.startswith("usr_remove:"):
        user_id = query.from_user.id
        if not await is_admin(user_id, ADMIN_ID):
            await query.answer("У вас немає прав для цієї дії 🚫", show_alert=True)
            return
        target_uid = int(choice.split(":", 1)[1])
        from database import remove_allowed_user
        from handlers.admin import build_users_list_view
        await remove_allowed_user(target_uid)
        _user_last_alert_state.pop(target_uid, None)
        await query.answer("🗑️ Користувача видалено зі списку доступу!", show_alert=False)
        text, kb = await build_users_list_view()
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="HTML")
        return

    # --- ОБРОБКА ТРИВОГ ТА ЛОКАЦІЇ ---
    if choice == "open_alerts":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        await set_awaiting_location(user_id, chat_id, True)
        try:
            if query.message:
                await query.message.delete()
        except Exception:
            pass
        await alerts_command(update, context)
        return

    elif choice == "close_menu":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        await set_awaiting_location(user_id, chat_id, False)
        context.user_data.pop("pending_geo", None)
        try:
            await query.message.delete()
        except Exception:
            pass

        sub = await get_user_subscription(user_id)
        if sub and sub.get("oblast"):
            try:
                alerts_data, threats_data = await fetch_neptun_data()
                current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
                _user_last_alert_state[user_id] = current_state["level"]
                _user_last_threat_signature[user_id] = make_threat_signature(current_state)

                await repost_alert_message_to_bottom(
                    bot=context.bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    sub=sub,
                    current_state=current_state,
                    is_manual_check=True
                )
            except Exception as e:
                logger.warning(f"Failed to repost status card on close_menu: {e}")
        else:
            close_msg = await context.bot.send_message(
                chat_id=chat_id,
                text="Меню закрито 👍",
                reply_markup=get_main_reply_keyboard()
            )

            async def _auto_delete_notice(msg):
                await asyncio.sleep(2)
                try:
                    await msg.delete()
                except Exception:
                    pass

            asyncio.create_task(_auto_delete_notice(close_msg))
        return

    elif choice == "close_map":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        try:
            await query.message.delete()
        except Exception:
            pass

        sub = await get_user_subscription(user_id)
        if sub and sub.get("oblast"):
            try:
                alerts_data, threats_data = await fetch_neptun_data()
                current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
                _user_last_alert_state[user_id] = current_state["level"]
                _user_last_threat_signature[user_id] = make_threat_signature(current_state)

                await repost_alert_message_to_bottom(
                    bot=context.bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    sub=sub,
                    current_state=current_state,
                    is_manual_check=True
                )
            except Exception as e:
                logger.warning(f"Failed to repost status card on close_map: {e}")
        return

    elif choice == "prompt_city":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        await set_awaiting_location(user_id, chat_id, True)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🏙️ **Напишіть у цей чат назву вашого міста, селища або села в Україні**\n"
                "*(наприклад: `Київ`, `Ірпінь`, `Бровари`, `с. Софіївська Борщагівка`)*\n\n"
                "📍 Або натисніть кнопку **«📍 Надіслати мою геопозицію»** внизу екрана:"
            ),
            reply_markup=get_main_reply_keyboard(),
            parse_mode="Markdown"
        )
        return

    elif choice == "prompt_gps":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        await set_awaiting_location(user_id, chat_id, True)
        await context.bot.send_message(
            chat_id=chat_id,
            text="📍 **Натисніть кнопку «📍 Надіслати мою геопозицію» внизу екрана** (або просто напишіть назву вашого міста у чат):",
            reply_markup=get_main_reply_keyboard(),
            parse_mode="Markdown"
        )
        return

    elif choice == "open_help":
        await query.answer()
        await help_command(update, context)
        return

    elif choice == "toggle_sound":
        user_id = query.from_user.id
        new_sound = await toggle_user_sound(user_id)
        status_text = "🔔 Сповіщення зі звуком УВІМКНЕНО!" if new_sound else "🔕 Сповіщення без звуку (тихий режим) УВІМКНЕНО!"
        await query.answer(status_text, show_alert=False)

        sub = await get_user_subscription(user_id)
        if sub:
            try:
                putin_enabled = sub.get("putin_check_enabled", True)
                await query.edit_message_reply_markup(reply_markup=get_alert_inline_keyboard(new_sound, putin_enabled))
            except Exception as e:
                logger.debug(f"Could not update reply_markup on toggle_sound: {e}")
        return

    elif choice == "toggle_putin":
        user_id = query.from_user.id
        new_putin = await toggle_user_putin(user_id)
        status_text = "☠️ Статус путіна УВІМКНЕНО в картці!" if new_putin else "☠️ Статус путіна ВИМКНЕНО з картки!"
        await query.answer(status_text, show_alert=False)

        sub = await get_user_subscription(user_id)
        if sub:
            sound_enabled = sub.get("sound_enabled", True)
            alerts_data, threats_data = await fetch_neptun_data()
            current_state = evaluate_user_alert_state(sub, alerts_data, threats_data)
            msg_text = format_alert_message(sub, current_state, is_manual_check=True)
            kb = get_alert_inline_keyboard(sound_enabled, new_putin)
            try:
                await query.edit_message_text(
                    text=msg_text,
                    reply_markup=kb,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.debug(f"Could not update message on toggle_putin: {e}")
        return

    elif choice in ("alerts_check", "refresh_alert_status"):
        user_id = query.from_user.id
        await handle_manual_alert_refresh(query, user_id)
        return

    elif choice == "alerts_map_refresh":
        user_id = query.from_user.id
        now = time.time()
        last_refresh = _user_last_manual_refresh.get(user_id, 0)
        time_since = now - last_refresh
        if time_since < ALERT_REFRESH_COOLDOWN:
            remaining = int(ALERT_REFRESH_COOLDOWN - time_since) + 1
            await query.answer(f"⏳ Зачекайте {remaining} сек перед оновленням карти!", show_alert=False)
            return

        _user_last_manual_refresh[user_id] = now
        sub = await get_user_subscription(user_id)
        await query.answer("Оновлюю карту...")
        try:
            alerts_data, threats_data = await fetch_neptun_data(force=True)
            map_path, caption = await generate_alerts_map_image(alerts_data, threats_data, sub)

            inline_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Оновити карту", callback_data="alerts_map_refresh")],
                [InlineKeyboardButton("🌐 Жива онлайн-карта (NEPTUN)", url="https://neptun.in.ua/")],
                [
                    InlineKeyboardButton("🏠 В меню", callback_data="open_alerts"),
                    InlineKeyboardButton("❌ Закрити карту", callback_data="close_map")
                ]
            ])

            with open(map_path, "rb") as photo_file:
                await query.message.reply_photo(
                    photo=photo_file,
                    caption=caption,
                    reply_markup=inline_kb,
                    parse_mode="Markdown"
                )
            if os.path.exists(map_path):
                os.remove(map_path)

            if query.message and query.message.photo:
                try:
                    await query.message.delete()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error refreshing map: {e}")
            await query.message.reply_text(f"❌ Не вдалося оновити карту: `{e}`")
        return

    elif choice == "alerts_on":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        if await enable_user_alerts(user_id, chat_id):
            sub = await get_user_subscription(user_id)
            loc_parts = [p for p in [sub.get("locality"), sub.get("raion"), sub.get("oblast")] if p]
            loc_str = ", ".join(loc_parts) if loc_parts else sub.get("oblast", "")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="open_alerts")]])
            await query.edit_message_text(f"🔔 **Сповіщення про повітряну тривогу успішно УВІМКНЕНО!**\n\n📍 Регіон: {loc_str}", reply_markup=kb, parse_mode="Markdown")
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="open_alerts")]])
            await query.edit_message_text("📍 Спочатку вкажіть вашу область (надішліть локацію або оберіть зі списку через команду /alerts).", reply_markup=kb)
        return

    elif choice == "alerts_off":
        await query.answer()
        user_id = query.from_user.id
        await disable_user_alerts(user_id)
        _user_last_alert_state.pop(user_id, None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="open_alerts")]])
        await query.edit_message_text("🔕 **Сповіщення про повітряну тривогу ВИМКНЕНО.**\n\nУвімкнути знову можна командою /alerts_on.", reply_markup=kb, parse_mode="Markdown")
        return

    elif choice.startswith("alerts_manual_page_"):
        await query.answer()
        page = int(choice.split("_")[-1])
        per_page = 8
        total = len(UKRAINE_OBLASTS)
        start_idx = page * per_page
        end_idx = min(start_idx + per_page, total)

        buttons = []
        for i in range(start_idx, end_idx, 2):
            row = [InlineKeyboardButton(UKRAINE_OBLASTS[i], callback_data=f"set_oblast:{UKRAINE_OBLASTS[i]}")]
            if i + 1 < end_idx:
                row.append(InlineKeyboardButton(UKRAINE_OBLASTS[i + 1], callback_data=f"set_oblast:{UKRAINE_OBLASTS[i + 1]}"))
            buttons.append(row)

        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"alerts_manual_page_{page - 1}"))
        if end_idx < total:
            nav_row.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"alerts_manual_page_{page + 1}"))
        if nav_row:
            buttons.append(nav_row)

        buttons.append([
            InlineKeyboardButton("🏙️ Вписати місто", callback_data="prompt_city"),
            InlineKeyboardButton("📍 Надіслати GPS", callback_data="prompt_gps")
        ])
        buttons.append([
            InlineKeyboardButton("🏠 В меню", callback_data="open_alerts"),
            InlineKeyboardButton("❌ Закрити", callback_data="close_menu")
        ])

        await query.edit_message_text(
            "🗺️ **Оберіть вашу область зі списку** (або одразу напишіть назву міста у чат):",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
        return

    elif choice == "confirm_pending_loc":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        pending_geo = context.user_data.get("pending_geo")
        await set_awaiting_location(user_id, chat_id, False)
        if pending_geo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await save_and_confirm_location(update, context, user_id, chat_id, pending_geo)
            context.user_data.pop("pending_geo", None)
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Термін дії запиту минув. Будь ласка, введіть назву міста ще раз через /alerts.",
                reply_markup=get_main_reply_keyboard()
            )
        return

    elif choice == "cancel_pending_loc":
        await query.answer()
        user_id = query.from_user.id
        chat_id = query.message.chat_id
        context.user_data.pop("pending_geo", None)
        await set_awaiting_location(user_id, chat_id, True)
        try:
            await query.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=chat_id,
            text="🏙️ Напишіть іншу назву міста/села у чат або натисніть кнопку **«📍 Надіслати мою геопозицію»** внизу екрана:",
            reply_markup=get_main_reply_keyboard(),
            parse_mode="Markdown"
        )
        return

    elif choice.startswith("set_oblast:"):
        await query.answer()
        oblast_name = choice.split("set_oblast:")[1]
        user_id = query.from_user.id
        chat_id = query.message.chat_id

        await save_user_location(user_id, chat_id, oblast_name, "", "", None, None)
        # Залишаємо режим очікування локації увімкненим, щоб користувач міг одразу вписати місто або дати GPS!
        await set_awaiting_location(user_id, chat_id, True)

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🏙️ Вписати місто / село", callback_data="prompt_city"),
                InlineKeyboardButton("📍 Надіслати GPS", callback_data="prompt_gps")
            ],
            [InlineKeyboardButton("✅ Залишити тільки область (до статусу)", callback_data="close_menu")]
        ])

        await query.edit_message_text(
            text=(
                f"✅ **Область збережено:** {oblast_name}!\n\n"
                "📍 **Бажаєте також вказати ваше місто/село або геопозицію?**\n"
                "*(Це дозволить боту показувати точну відстань у км та курс шахедів/ракет відносно вас)*\n\n"
                "• ✍️ **Просто напишіть у чат назву міста чи села** (наприклад: `Ірпінь`, `Бровари`)\n"
                "• 📍 **Або натисніть «📍 Надіслати мою геопозицію»** внизу екрана\n"
                "• ⏭️ Або натисніть **«✅ Залишити тільки область»**, якщо уточнення не потрібне:"
            ),
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return

    # Якщо увімкнено медіа-плагін — обробляємо кнопки вибору формату YouTube
    if is_media_plugin_enabled() and choice in ("yt_video", "yt_audio", "yt_both"):
        from handlers.media import process_video, process_audio, send_typing_action
        await query.answer()
        if not query.message.reply_to_message or not query.message.reply_to_message.text:
            await query.edit_message_text(text="❌ Помилка: не вдалося знайти оригінальне посилання. Відправте його ще раз.")
            return

        url = query.message.reply_to_message.text
        await query.edit_message_text(text="⏳ Завантажую, зачекайте...")
        typing_task = asyncio.create_task(send_typing_action(query.message.chat_id, context))
        try:
            if choice == "yt_video":
                await process_video(query.message.reply_to_message, context, url)
            elif choice == "yt_audio":
                await process_audio(query.message.reply_to_message, context, url)
            elif choice == "yt_both":
                await process_video(query.message.reply_to_message, context, url)
                await process_audio(query.message.reply_to_message, context, url)
        finally:
            typing_task.cancel()
            try:
                await query.message.delete()
            except Exception:
                pass
        return

    await query.answer()

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update, context):
        return

    text = update.message.text.strip() if update.message and update.message.text else ""
    if not text:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # 1. Загальні команди та кнопки меню
    if text in ("❓ Довідка", "❓ Довідка та допомога", "Довідка", "/help"):
        await help_command(update, context)
        return

    # 2. Обробка кнопок плагіна відключень світла
    if is_outages_plugin_enabled():
        from handlers.outages.commands import power_command, prompt_group_setup, group_command
        from database import get_outage_subscription, save_outage_group, set_outage_awaiting
        from yasno_outages import yasno_client
        import re

        if text in ("💡 Стан світла", "⚡ Графік відключень", "Стан світла", "Графік відключень", "/power", "/svitlo"):
            await power_command(update, context)
            return
        elif text in ("⚙️ Налаштувати групу/адресу", "Налаштувати групу/адресу", "Налаштувати групу", "/group"):
            await prompt_group_setup(update, context)
            return

        outage_sub = await get_outage_subscription(user_id)
        awaiting = outage_sub.get("awaiting_input") if outage_sub else None

        # Якщо користувач прямо ввів номер групи (наприклад: 1, 2, 2.1, 3.2 тощо)
        if re.fullmatch(r"^[1-6](\.[1-2])?$", text):
            grp = text if "." in text else f"{text}.1"
            if not outage_sub or not outage_sub.get("region_id"):
                context.user_data["pending_group"] = grp
                await update.message.reply_text(
                    f"⚡ Ви вказали чергу: **{grp}**.\n\n"
                    "Тепер оберіть вашу область (регіон), щоб застосувати розклад:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗺️ Список всіх 24 областей України", callback_data="power_regions_list")]
                    ]),
                    parse_mode="Markdown"
                )
                return
            reg_id = outage_sub.get("region_id")
            reg_name = outage_sub.get("region_name") or "Регіон"
            dso_id = outage_sub.get("dso_id") or 902
            dso_name = outage_sub.get("dso_name") or "Обленерго"
            await save_outage_group(user_id, chat_id, reg_id, reg_name, dso_id, dso_name, grp)
            await update.message.reply_text(f"✅ Встановлено чергу (групу): **{grp}** ({reg_name})!", parse_mode="Markdown")
            await power_command(update, context)
            return

        # Якщо користувач надіслав координати вручну (наприклад: 50.4501, 30.5234)
        coords_match = re.fullmatch(r"^(-?\d{1,2}\.\d+)[,\s]+(-?\d{1,3}\.\d+)$", text)
        if coords_match:
            lat = float(coords_match.group(1))
            lon = float(coords_match.group(2))
            from yasno_outages import resolve_group_from_coords
            from keyboards import get_coordinate_nudge_keyboard
            reg_id = outage_sub.get("region_id", 25) if outage_sub else 25
            dso_id = outage_sub.get("dso_id", 902) if outage_sub else 902
            status_msg = await update.message.reply_text("⏳ Визначаю чергу за координатами...")
            group, addr, err = await resolve_group_from_coords(lat, lon, reg_id, dso_id)
            nudge_text = (
                "📍 **Визначення адреси за координатами**\n\n"
                f"• Координати: `{lat:.5f}, {lon:.5f}`\n"
                f"• Адреса: **{addr or 'Не визначено'}**\n"
                f"• Знайдена черга: **{group or 'Не визначено'}**\n\n"
                "Використовуйте кнопки для коригування або натисніть Підтвердити:"
            )
            kb = get_coordinate_nudge_keyboard(lat, lon)
            await status_msg.edit_text(nudge_text, reply_markup=kb, parse_mode="Markdown")
            return

        # 1. Пошук населеного пункту (Крок 1)
        if awaiting == "search_city" or (
            (not outage_sub or not outage_sub.get("region_id"))
            and not text.startswith("/")
            and len(text) <= 60
            and not text.startswith("http")
            and not is_alerts_plugin_enabled()
        ):
            text_clean = text.strip()
            text_lower = text_clean.lower()

            # Якщо ввели Київ
            if text_lower in ("київ", "м. київ", "м.київ", "kyiv", "kiev"):
                from database import save_outage_city
                await save_outage_city(user_id, chat_id, 25, "м. Київ", 902, "ДТЕК Київські електромережі", None, "Київ")
                context.user_data["outage_region_id"] = 25
                context.user_data["outage_region_name"] = "м. Київ"
                context.user_data["outage_dso_id"] = 902
                context.user_data["outage_dso_name"] = "ДТЕК Київські електромережі"
                context.user_data["outage_city_id"] = None
                context.user_data["outage_city_name"] = "Київ"
                await update.message.reply_text(
                    "✅ Обрано населений пункт: **Київ**!\n\n"
                    "🔍 **Крок 2 з 3: Вулиця**\n"
                    "Напишіть у чат назву вашої вулиці (наприклад: `Хрещатик`, `Шевченка`, `Перемоги`):",
                    parse_mode="Markdown"
                )
                return

            # Якщо ввели Дніпро
            if text_lower in ("дніпро", "м. дніпро", "м.дніпро", "dnipro"):
                buttons = [
                    [
                        InlineKeyboardButton("🏙️ Дніпро (ДТЕК)", callback_data="power_set_city:3:301:Дніпропетровська область:ДТЕК Дніпровські електромережі:6:м. Дніпро"),
                        InlineKeyboardButton("🏙️ Дніпро (ЦЕК)", callback_data="power_set_city:3:303:Дніпропетровська область:ЦЕК:6:Дніпро"),
                    ],
                    [InlineKeyboardButton("❌ Скасувати", callback_data="open_power_menu")]
                ]
                await update.message.reply_text(
                    "⚡ Для **м. Дніпро** оберіть вашого оператора розподілу (ОСР):",
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode="Markdown"
                )
                return

            # Загальноукраїнський пошук (будь-яке місто, селище, село або область України)
            from yasno_outages import find_ukrainian_settlement
            status_msg = await update.message.reply_text(f"🔍 Шукаю населений пункт «{text_clean}»...")
            try:
                found = await find_ukrainian_settlement(text_clean)
                if found:
                    r_id = found["region_id"]
                    r_name = found["region_name"]
                    d_id = found["dso_id"]
                    d_name = found["dso_name"]
                    c_name = found.get("city_name") or text_clean

                    from database import save_outage_city
                    await save_outage_city(user_id, chat_id, r_id, r_name, d_id, d_name, None, c_name)
                    context.user_data["outage_region_id"] = r_id
                    context.user_data["outage_region_name"] = r_name
                    context.user_data["outage_dso_id"] = d_id
                    context.user_data["outage_dso_name"] = d_name
                    context.user_data["outage_city_id"] = None
                    context.user_data["outage_city_name"] = c_name

                    if found.get("is_yasno"):
                        await status_msg.edit_text(
                            f"✅ Обрано населений пункт: **{c_name}** ({r_name})!\n\n"
                            "🔍 **Крок 2 з 3: Вулиця**\n"
                            "Напишіть у чат назву вашої вулиці (наприклад: `Центральна`, `Шевченка`, `Перемоги`):",
                            parse_mode="Markdown"
                        )
                        return
                    else:
                        # Для всіх інших областей України пропонуємо вибір підчерги 1.1 - 6.2
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
                            [InlineKeyboardButton("🗺️ Список всіх областей", callback_data="power_regions_list")],
                            [InlineKeyboardButton("❌ Скасувати", callback_data="open_power_menu")],
                        ]
                        await status_msg.edit_text(
                            f"✅ Знайдено: **{c_name}** ({r_name})\n"
                            f"⚡ Оператор: **{d_name}**\n\n"
                            "Будь ласка, оберіть вашу чергу (підчергу) відключень:",
                            reply_markup=InlineKeyboardMarkup(buttons),
                            parse_mode="Markdown"
                        )
                        return

                # Пошук серед міст Yasno (Дніпропетровська обл.)
                cities = await yasno_client.get_cities(3, 301)
                matches = [c for c in cities if text_lower in c.get("value", "").lower()]
                if len(matches) == 1:
                    c = matches[0]
                    from database import save_outage_city
                    await save_outage_city(user_id, chat_id, 3, "Дніпропетровська область", 301, "ДТЕК Дніпровські електромережі", c["id"], c["value"])
                    context.user_data["outage_region_id"] = 3
                    context.user_data["outage_region_name"] = "Дніпропетровська область"
                    context.user_data["outage_dso_id"] = 301
                    context.user_data["outage_dso_name"] = "ДТЕК Дніпровські електромережі"
                    context.user_data["outage_city_id"] = c["id"]
                    context.user_data["outage_city_name"] = c["value"]
                    await status_msg.edit_text(
                        f"✅ Обрано населений пункт: **{c['value']}**!\n\n"
                        "🔍 **Крок 2 з 3: Вулиця**\n"
                        "Напишіть у чат назву вашої вулиці (наприклад: `Центральна`, `Шевченка`, `Перемоги`):",
                        parse_mode="Markdown"
                    )
                    return
                elif len(matches) > 1 and len(matches) <= 8:
                    buttons = []
                    for c in matches:
                        buttons.append([InlineKeyboardButton(c["value"], callback_data=f"power_set_city:3:301:Дніпропетровська область:ДТЕК Дніпровські електромережі:{c['id']}:{c['value'][:25]}")])
                    buttons.append([InlineKeyboardButton("❌ Скасувати", callback_data="open_power_menu")])
                    await status_msg.edit_text(
                        f"📍 Оберіть населений пункт серед знайдених за запитом «{text_clean}»:",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                    return
                else:
                    buttons = [
                        [InlineKeyboardButton("🗺️ Список всіх 24 областей України", callback_data="power_regions_list")],
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
                        [InlineKeyboardButton("❌ Скасувати", callback_data="open_power_menu")]
                    ]
                    await status_msg.edit_text(
                        f"❌ Населений пункт «{text_clean}» не знайдено.\n\n"
                        "Спробуйте уточнити назву або оберіть вашу область зі списку нижче:",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                    return
            except Exception as city_err:
                logger.error(f"Error searching cities: {city_err}")
                await status_msg.edit_text(f"❌ Помилка пошуку населеного пункту: {city_err}")
                return

        # 2. Пошук вулиці (Крок 2)
        elif awaiting == "search_street":
            reg_id = outage_sub.get("region_id") or context.user_data.get("outage_region_id") or 25
            dso_id = outage_sub.get("dso_id") or context.user_data.get("outage_dso_id") or 902
            city_id = outage_sub.get("city_id") or context.user_data.get("outage_city_id")
            city_name = outage_sub.get("city_name") or context.user_data.get("outage_city_name") or outage_sub.get("region_name") or "обраного міста"
            status_msg = await update.message.reply_text(f"🔍 Шукаю вулицю «{text}» у {city_name}...")
            try:
                streets = await yasno_client.search_streets(reg_id, dso_id, text, city_id=city_id)
                if streets:
                    buttons = []
                    for s in streets[:8]:
                        buttons.append([InlineKeyboardButton(s["value"], callback_data=f"power_sel_st:{s['id']}:{s['value'][:25]}")])
                    buttons.append([InlineKeyboardButton("⬅️ Змінити місто", callback_data="power_search_address")])
                    buttons.append([InlineKeyboardButton("❌ Скасувати", callback_data="open_power_menu")])
                    await status_msg.edit_text(
                        f"📍 Оберіть вашу вулицю у **{city_name}** серед знайдених за запитом «{text}»:",
                        reply_markup=InlineKeyboardMarkup(buttons),
                        parse_mode="Markdown"
                    )
                    return
                else:
                    await status_msg.edit_text(
                        f"❌ Вулицю «{text}» не знайдено у **{city_name}**.\n"
                        "Спробуйте ввести лише назву без слова «вулиця» (наприклад: `Хрещатик`, `Шевченка`, `Центральна`):",
                        parse_mode="Markdown"
                    )
                    return
            except Exception as search_err:
                logger.error(f"Error searching streets: {search_err}")
                await status_msg.edit_text(f"❌ Помилка пошуку вулиці: {search_err}")
                return

        # 3. Пошук будинку та збереження групи (Крок 3)
        elif awaiting == "search_house":
            street_id = context.user_data.get("outage_street_id")
            street_name = context.user_data.get("outage_street_name", "")
            if street_id:
                reg_id = outage_sub.get("region_id") or context.user_data.get("outage_region_id") or 25
                dso_id = outage_sub.get("dso_id") or context.user_data.get("outage_dso_id") or 902
                city_id = outage_sub.get("city_id") or context.user_data.get("outage_city_id")
                city_name = outage_sub.get("city_name") or context.user_data.get("outage_city_name") or outage_sub.get("region_name") or "обраного міста"
                status_msg = await update.message.reply_text(f"🔍 Шукаю будинок «{text}»...")
                try:
                    houses = await yasno_client.search_houses(reg_id, dso_id, street_id, text)
                    if houses:
                        target_house = next((h for h in houses if h.get("value", "").strip().lower() == text.lower()), houses[0])
                        house_id = target_house["id"]
                        group_res = await yasno_client.resolve_group_by_address(reg_id, dso_id, street_id, house_id, city_id=city_id)
                        if group_res:
                            full_addr = f"{city_name}, вул. {street_name}, {target_house.get('value')}"
                            reg_name = outage_sub.get("region_name") or "Київ"
                            dso_name = outage_sub.get("dso_name") or "ДТЕК"
                            await save_outage_group(
                                user_id=user_id,
                                chat_id=chat_id,
                                region_id=reg_id,
                                region_name=reg_name,
                                dso_id=dso_id,
                                dso_name=dso_name,
                                group_name=group_res,
                                address=full_addr,
                                city_id=city_id,
                                city_name=city_name,
                            )
                            await status_msg.edit_text(
                                f"🎉 Знайдено групу для будинку: **{group_res}**!\n"
                                f"• Адреса: `{full_addr}`\n\n"
                                "Завантажую ваш розклад...",
                                parse_mode="Markdown"
                            )
                            await power_command(update, context)
                            return
                        else:
                            await status_msg.edit_text("❌ Не вдалося визначити групу для цієї адреси у системі Yasno.")
                            return
                    else:
                        await status_msg.edit_text(f"❌ Будинок «{text}» не знайдено на цій вулиці. Перевірте номер або виберіть групу вручну.")
                        return
                except Exception as house_err:
                    logger.error(f"Error searching houses: {house_err}")
                    await status_msg.edit_text(f"❌ Помилка пошуку будинку: {house_err}")
                    return

    # 3. Обробка кнопок плагіна тривог
    if is_alerts_plugin_enabled():
        if text in ("📊 Статус тривоги", "Статус тривоги", "/status", "/check"):
            await status_command(update, context)
            return
        elif text in ("🗺️ Карта тривог", "Карта тривог", "/map"):
            await map_command(update, context)
            return
        elif text in ("🚨 Налаштувати тривоги", "Налаштувати тривоги", "/alerts"):
            await alerts_command(update, context)
            return

        sub = await get_user_subscription(user_id)
        should_try_geocode = (sub and sub.get("awaiting_location")) or (
            not is_media_plugin_enabled() and not is_outages_plugin_enabled() and len(text) <= 60 and not text.startswith("http")
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

    # 4. Підказка за замовчуванням
    if is_outages_plugin_enabled() and not is_alerts_plugin_enabled():
        hint = "⚡ Натисніть **«💡 Стан світла»** або оберіть дію з кнопок меню внизу чату:"
    elif is_alerts_plugin_enabled() and not is_outages_plugin_enabled():
        hint = (
            "Будь ласка, надішліть посилання на медіа (TikTok, YouTube, Instagram, Spotify) або скористайтеся меню кнопок нижче 🚨"
            if is_media_plugin_enabled()
            else "Напишіть назву вашого міста для налаштування тривог або скористайтеся кнопками меню внизу екрана 🚨"
        )
    else:
        hint = "Скористайтеся кнопками меню внизу екрана або введіть /help 💡"

    await update.message.reply_text(hint, reply_markup=get_main_reply_keyboard())
