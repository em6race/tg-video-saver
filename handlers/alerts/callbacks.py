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

    # 2. Обробка кнопок плагіна тривог
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
            not is_media_plugin_enabled() and len(text) <= 60 and not text.startswith("http")
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

    # 3. Підказка за замовчуванням
    hint = (
        "Будь ласка, надішліть посилання на медіа (TikTok, YouTube, Instagram, Spotify) або скористайтеся меню кнопок нижче 🚨"
        if is_media_plugin_enabled()
        else "Напишіть назву вашого міста для налаштування тривог або скористайтеся кнопками меню внизу екрана 🚨"
    )

    await update.message.reply_text(hint, reply_markup=get_main_reply_keyboard())
