import os
import sys
import time
import logging
import asyncio
import subprocess
from datetime import datetime
import pytz

from database import set_user_last_alert_msg_id, get_all_enabled_subscriptions
from neptun_alerts import (
    fetch_neptun_data,
    evaluate_user_alert_state,
    format_alert_message,
    make_threat_signature,
)
from keyboards import get_alert_inline_keyboard
from handlers.alerts.state import (
    _user_last_alert_state,
    _user_last_threat_signature,
    _user_last_auto_edit_time,
    _user_last_threat_ids,
)

logger = logging.getLogger(__name__)
ADMIN_ID = os.getenv("ADMIN_ID")

async def repost_alert_message_to_bottom(bot, chat_id: int, user_id: int, sub: dict, current_state: dict, is_manual_check: bool = False, prev_level: str = None, extra_msg_id_to_delete: int = None) -> None:
    """Видаляє попереднє повідомлення тривоги та надсилає НОВЕ насамкінець чату."""
    last_msg_id = sub.get("last_alert_msg_id")

    msg_ids_to_delete = set()
    if last_msg_id:
        msg_ids_to_delete.add(last_msg_id)
    if extra_msg_id_to_delete:
        msg_ids_to_delete.add(extra_msg_id_to_delete)

    for m_id in msg_ids_to_delete:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=m_id)
        except Exception as del_err:
            logger.debug(f"Could not delete old alert message {m_id} for user {user_id}: {del_err}")

    sound_enabled = sub.get("sound_enabled", True) if sub else True
    msg_text = format_alert_message(sub, current_state, is_manual_check=is_manual_check, prev_level=prev_level)
    alert_kb = get_alert_inline_keyboard(sound_enabled)
    is_silent = (current_state["level"] == "green") or (not sound_enabled)

    try:
        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=msg_text,
            reply_markup=alert_kb,
            parse_mode="Markdown",
            disable_notification=is_silent,
            disable_web_page_preview=True
        )
        await set_user_last_alert_msg_id(user_id, sent_msg.message_id)
    except Exception as send_err:
        logger.warning(f"Could not repost alert message to bottom for {chat_id}: {send_err}")

async def alerts_monitoring_loop(application) -> None:
    """Фоновий процес опитування NEPTUN API та розсилки сповіщень підписникам."""
    logger.info("Starting NEPTUN alert monitoring loop...")
    while True:
        try:
            subs = await get_all_enabled_subscriptions()
            if subs:
                alerts_data, threats_data = await fetch_neptun_data()

                if alerts_data or threats_data:
                    now = time.time()
                    for sub in subs:
                        user_id = sub["user_id"]
                        chat_id = sub["chat_id"]
                        last_msg_id = sub.get("last_alert_msg_id")

                        prev_threat_ids = _user_last_threat_ids.get(user_id)
                        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data, prev_threat_ids=prev_threat_ids)
                        _user_last_threat_ids[user_id] = set(current_state.get("current_threat_ids", []))
                        curr_level = current_state["level"]
                        prev_level = _user_last_alert_state.get(user_id)
                        current_sig = make_threat_signature(current_state)
                        prev_sig = _user_last_threat_signature.get(user_id)
                        last_auto_edit = _user_last_auto_edit_time.get(user_id, 0)
                        sound_enabled = sub.get("sound_enabled", True)
                        alert_kb = get_alert_inline_keyboard(sound_enabled)

                        if prev_level is None:
                            _user_last_alert_state[user_id] = curr_level
                            _user_last_threat_signature[user_id] = current_sig
                            await repost_alert_message_to_bottom(
                                bot=application.bot,
                                chat_id=chat_id,
                                user_id=user_id,
                                sub=sub,
                                current_state=current_state,
                                is_manual_check=True
                            )
                            continue

                        if curr_level != prev_level:
                            _user_last_alert_state[user_id] = curr_level
                            _user_last_threat_signature[user_id] = current_sig
                            _user_last_auto_edit_time[user_id] = now

                            await repost_alert_message_to_bottom(
                                bot=application.bot,
                                chat_id=chat_id,
                                user_id=user_id,
                                sub=sub,
                                current_state=current_state,
                                is_manual_check=False,
                                prev_level=prev_level
                            )
                        elif curr_level != "green":
                            if (current_sig != prev_sig) and (now - last_auto_edit >= 60):
                                _user_last_threat_signature[user_id] = current_sig
                                _user_last_auto_edit_time[user_id] = now

                                msg_text = format_alert_message(sub, current_state, is_manual_check=False, prev_level=prev_level)
                                if last_msg_id:
                                    try:
                                        await application.bot.edit_message_text(
                                            chat_id=chat_id,
                                            message_id=last_msg_id,
                                            text=msg_text,
                                            reply_markup=alert_kb,
                                            parse_mode="Markdown",
                                            disable_web_page_preview=True
                                        )
                                    except Exception as edit_err:
                                        logger.debug(f"In-place edit failed ({edit_err}), reposting...")
                                        await repost_alert_message_to_bottom(
                                            bot=application.bot,
                                            chat_id=chat_id,
                                            user_id=user_id,
                                            sub=sub,
                                            current_state=current_state,
                                            is_manual_check=False,
                                            prev_level=prev_level
                                        )

        except Exception as e:
            logger.error(f"Error in alerts monitoring loop: {e}")

        await asyncio.sleep(10)

async def auto_update(application) -> None:
    tz = pytz.timezone('Europe/Kiev')
    while True:
        await asyncio.sleep(60)
        now = datetime.now(tz)
        if now.hour == 3 and now.minute == 0:
            if ADMIN_ID:
                try:
                    await application.bot.send_message(
                        chat_id=int(ADMIN_ID),
                        text="🕒 3:00 ночі. Завантажую оновлення з GitHub і перезапускаюсь... 🔄",
                        disable_notification=True
                    )
                except Exception:
                    pass

            try:
                subprocess.run(["git", "pull"], check=True)
            except Exception as e:
                logger.error(f"Git pull failed: {e}")

            await asyncio.sleep(2)
            os.execv(sys.executable, [sys.executable] + sys.argv)
