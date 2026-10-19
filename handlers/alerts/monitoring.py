import os
import sys
import time
import json
import tempfile
import logging
import asyncio
import subprocess
from datetime import datetime
import pytz

from database import (
    set_user_last_alert_msg_id,
    get_all_enabled_subscriptions,
    get_user_subscription,
    record_alert_history,
    add_alert_message_record,
    get_user_alert_message_ids,
    delete_alert_message_records,
)
from neptun_alerts import (
    fetch_neptun_data,
    fetch_ukraine_alarm_data,
    evaluate_user_alert_state,
    format_alert_message,
    make_threat_signature,
)
from keyboards import get_alert_inline_keyboard
from handlers.alerts.state import (
    _user_last_alert_state,
    _user_last_threat_signature,
    _user_last_auto_edit_time,
    _user_last_putin_date,
    _user_active_alert_meta,
    _user_active_msg_ids,
)

logger = logging.getLogger(__name__)
ADMIN_ID = os.getenv("ADMIN_ID")

def _parse_alert_start_time(since_val: str = None, fallback_ts: float = None) -> tuple[str, float]:
    """
    Повертає (formatted_str, unix_timestamp) за часом Києва.
    formatted_str має вигляд 'HH:MM (DD.MM)'
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Kyiv")
    except Exception:
        tz = pytz.timezone("Europe/Kiev")

    if fallback_ts is None:
        fallback_ts = time.time()

    if since_val:
        try:
            dt = datetime.fromisoformat(since_val.replace("Z", "+00:00")).astimezone(tz)
            return dt.strftime("%H:%M (%d.%m)"), dt.timestamp()
        except Exception:
            pass
        if "(" in since_val and ")" in since_val:
            return since_val, fallback_ts

    dt = datetime.fromtimestamp(fallback_ts, tz)
    return dt.strftime("%H:%M (%d.%m)"), fallback_ts

async def repost_alert_message_to_bottom(bot, chat_id: int, user_id: int, sub: dict, current_state: dict, is_manual_check: bool = False, prev_level: str = None, extra_msg_id_to_delete: int = None) -> None:
    """Видаляє всі відомі попередні повідомлення тривоги та надсилає НОВЕ насамкінець чату."""
    last_msg_id = sub.get("last_alert_msg_id") if sub else None

    msg_ids_to_delete = set()
    if last_msg_id:
        msg_ids_to_delete.add(last_msg_id)
    if extra_msg_id_to_delete:
        msg_ids_to_delete.add(extra_msg_id_to_delete)

    try:
        fresh_sub = await get_user_subscription(user_id)
        if fresh_sub and fresh_sub.get("last_alert_msg_id"):
            msg_ids_to_delete.add(fresh_sub["last_alert_msg_id"])
    except Exception:
        pass

    # Отримуємо всі зареєстровані повідомлення з БД
    try:
        db_ids = await get_user_alert_message_ids(user_id)
        for d_id in db_ids:
            msg_ids_to_delete.add(d_id)
    except Exception as db_err:
        logger.debug(f"Could not load alert messages from DB for user {user_id}: {db_err}")

    # Отримуємо повідомлення з оперативної пам'яті
    if user_id in _user_active_msg_ids:
        msg_ids_to_delete.update(_user_active_msg_ids[user_id])

    for m_id in list(msg_ids_to_delete):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=m_id)
        except Exception as del_err:
            logger.debug(f"Could not delete old alert message {m_id} for user {user_id}: {del_err}")

    # Очищаємо застарілі ID у БД та пам'яті
    try:
        await delete_alert_message_records(user_id)
    except Exception:
        pass
    _user_active_msg_ids[user_id] = set()

    sound_enabled = sub.get("sound_enabled", True) if sub else True
    putin_enabled = sub.get("putin_check_enabled", True) if sub else True
    msg_text = format_alert_message(sub, current_state, is_manual_check=is_manual_check, prev_level=prev_level)
    alert_kb = get_alert_inline_keyboard(sound_enabled, putin_enabled)
    is_silent = (current_state["level"] == "green") or (not sound_enabled)
    _user_last_putin_date[user_id] = datetime.now(pytz.timezone("Europe/Kiev")).strftime("%d.%m.%Y")

    try:
        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=msg_text,
            reply_markup=alert_kb,
            parse_mode="HTML",
            disable_notification=is_silent,
            disable_web_page_preview=True
        )
        if sub is not None:
            sub["last_alert_msg_id"] = sent_msg.message_id
        await set_user_last_alert_msg_id(user_id, sent_msg.message_id)
        await add_alert_message_record(user_id, chat_id, sent_msg.message_id)
        _user_active_msg_ids[user_id] = {sent_msg.message_id}
    except Exception as send_err:
        logger.warning(f"Could not repost alert message to bottom for {chat_id}: {send_err}")

async def alerts_monitoring_loop(application) -> None:
    """Фоновий процес опитування NEPTUN API та розсилки сповіщень підписникам."""
    logger.info("Starting NEPTUN alert monitoring loop...")

    # Перевірка чи щойно відбувся рестарт через /restart
    try:
        restart_file = os.path.join(tempfile.gettempdir(), "tg_alerts_restart.json")
        if os.path.exists(restart_file):
            try:
                with open(restart_file, "r", encoding="utf-8") as f:
                    r_data = json.load(f)
                os.remove(restart_file)
                c_id = r_data.get("chat_id")
                m_id = r_data.get("message_id")
                g_out = r_data.get("git_output", "")
                p_ok = r_data.get("pull_success", True)
                if c_id and m_id:
                    if p_ok:
                        new_text = f"✅ **Оновлено з GitHub:**\n`{g_out[:300]}`\n\nБот успішно перезапущено! 🚀"
                    else:
                        new_text = f"⚠️ **Не вдалося оновити код:**\n`{g_out[:300]}`\n\nБот перезапущено на поточній версії. ⚠️"
                    await application.bot.edit_message_text(
                        chat_id=c_id,
                        message_id=m_id,
                        text=new_text,
                        parse_mode="Markdown"
                    )
            except Exception as e:
                logger.debug(f"Failed to finalize restart notice: {e}")
                if os.path.exists(restart_file):
                    try:
                        os.remove(restart_file)
                    except Exception:
                        pass
    except Exception as init_err:
        logger.debug(f"Restart file check error: {init_err}")

    while True:
        try:
            subs = await get_all_enabled_subscriptions()
            if subs:
                alerts_data, threats_data = await fetch_neptun_data()
                ua_data = await fetch_ukraine_alarm_data()

                if alerts_data or threats_data or ua_data:
                    now = time.time()
                    for sub in subs:
                        user_id = sub["user_id"]
                        chat_id = sub["chat_id"]
                        last_msg_id = sub.get("last_alert_msg_id")

                        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data, ua_data)
                        curr_level = current_state["level"]
                        prev_level = _user_last_alert_state.get(user_id)
                        current_sig = make_threat_signature(current_state)
                        prev_sig = _user_last_threat_signature.get(user_id)
                        last_auto_edit = _user_last_auto_edit_time.get(user_id, 0)
                        sound_enabled = sub.get("sound_enabled", True)
                        putin_enabled = sub.get("putin_check_enabled", True)
                        alert_kb = get_alert_inline_keyboard(sound_enabled, putin_enabled)

                        # Щоденне оновлення рядка «Чи здох путін?» при зміні календарної дати
                        today_kyiv = datetime.now(pytz.timezone("Europe/Kiev")).strftime("%d.%m.%Y")
                        last_putin_date = _user_last_putin_date.get(user_id)
                        if last_putin_date is None:
                            _user_last_putin_date[user_id] = today_kyiv
                        elif last_putin_date != today_kyiv:
                            _user_last_putin_date[user_id] = today_kyiv
                            if putin_enabled and last_msg_id:
                                msg_text = format_alert_message(sub, current_state, is_manual_check=(curr_level == "green"), prev_level=prev_level)
                                try:
                                    await application.bot.edit_message_text(
                                        chat_id=chat_id,
                                        message_id=last_msg_id,
                                        text=msg_text,
                                        reply_markup=alert_kb,
                                        parse_mode="HTML",
                                        disable_web_page_preview=True
                                    )
                                    continue
                                except Exception as date_edit_err:
                                    logger.debug(f"Daily putin date edit failed for {user_id}: {date_edit_err}")

                        if prev_level is None:
                            _user_last_alert_state[user_id] = curr_level
                            _user_last_threat_signature[user_id] = current_sig
                            _user_last_auto_edit_time[user_id] = now

                            if curr_level in ("red", "yellow") and user_id not in _user_active_alert_meta:
                                start_fmt, start_ts = _parse_alert_start_time(current_state.get("since"), fallback_ts=now)
                                _user_active_alert_meta[user_id] = {
                                    "started_at": start_fmt,
                                    "alert_type": current_state.get("alert_type") or "AIR",
                                    "max_level": curr_level,
                                    "reasons": list(current_state.get("reasons", [])),
                                    "start_timestamp": start_ts
                                }

                            # Видаляємо старе повідомлення тривоги до перезапуску та надсилаємо нове насамкінець чату
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

                            if curr_level in ("red", "yellow"):
                                if user_id not in _user_active_alert_meta:
                                    start_fmt, start_ts = _parse_alert_start_time(current_state.get("since"), fallback_ts=now)
                                    _user_active_alert_meta[user_id] = {
                                        "started_at": start_fmt,
                                        "alert_type": current_state.get("alert_type") or "AIR",
                                        "max_level": curr_level,
                                        "reasons": list(current_state.get("reasons", [])),
                                        "start_timestamp": start_ts
                                    }
                                else:
                                    meta = _user_active_alert_meta[user_id]
                                    if curr_level == "red":
                                        meta["max_level"] = "red"
                                    if current_state.get("alert_type") in ("ARTILLERY", "CHEMICAL", "NUCLEAR"):
                                        meta["alert_type"] = current_state.get("alert_type")
                                    for r in current_state.get("reasons", []):
                                        if r not in meta["reasons"]:
                                            meta["reasons"].append(r)
                            elif curr_level == "green" and prev_level in ("red", "yellow"):
                                meta = _user_active_alert_meta.pop(user_id, None)
                                if meta and meta.get("started_at"):
                                    dur_sec = max(1, int(now - meta["start_timestamp"]))
                                    s_at = meta["started_at"]
                                    e_at = datetime.now(pytz.timezone("Europe/Kiev")).strftime("%H:%M (%d.%m)")
                                    a_type = meta.get("alert_type", "AIR")
                                    m_lvl = meta.get("max_level", prev_level)
                                    reas_str = ", ".join(meta.get("reasons", [])) if meta.get("reasons") else ", ".join(current_state.get("reasons", []))
                                    try:
                                        await record_alert_history(
                                            user_id=user_id,
                                            chat_id=chat_id,
                                            oblast=sub.get("oblast", ""),
                                            raion=sub.get("raion", ""),
                                            locality=sub.get("locality", ""),
                                            alert_type=a_type,
                                            max_level=m_lvl,
                                            reasons=reas_str,
                                            started_at=s_at,
                                            ended_at=e_at,
                                            duration_seconds=dur_sec
                                        )
                                    except Exception as hist_err:
                                        logger.error(f"Failed to record alert history for {user_id}: {hist_err}")

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
                            if user_id in _user_active_alert_meta:
                                meta = _user_active_alert_meta[user_id]
                                if current_state.get("alert_type") in ("ARTILLERY", "CHEMICAL", "NUCLEAR"):
                                    meta["alert_type"] = current_state.get("alert_type")
                                for r in current_state.get("reasons", []):
                                    if r not in meta["reasons"]:
                                        meta["reasons"].append(r)
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
                                            parse_mode="HTML",
                                            disable_web_page_preview=True
                                        )
                                    except Exception as edit_err:
                                        if "message is not modified" not in str(edit_err).lower():
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
            notice_msg = None
            if ADMIN_ID:
                try:
                    notice_msg = await application.bot.send_message(
                        chat_id=int(ADMIN_ID),
                        text="🕒 3:00 ночі. Завантажую оновлення з GitHub (гілка main) і перезапускаюсь... 🔄",
                        disable_notification=True
                    )
                except Exception:
                    pass

            try:
                subprocess.run(["git", "pull", "origin", "main"], check=True)
            except Exception as e:
                logger.error(f"Git pull failed: {e}")

            if notice_msg:
                try:
                    await notice_msg.delete()
                except Exception:
                    pass

            try:
                if application.updater and application.updater.running:
                    await application.updater.stop()
            except Exception:
                pass

            await asyncio.sleep(2)
            os.execv(sys.executable, [sys.executable] + sys.argv)
