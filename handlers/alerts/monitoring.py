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
from handlers.common import is_media_plugin_enabled
from handlers.alerts.state import (
    _user_last_alert_state,
    _user_last_threat_signature,
    _user_last_auto_edit_time,
    _user_last_putin_date,
    _user_active_alert_meta,
    _user_active_msg_ids,
    _user_failed_send_anchor,
    _user_last_send_attempt,
)

logger = logging.getLogger(__name__)
ADMIN_ID = os.getenv("ADMIN_ID")


def is_message_missing_error(err: Exception) -> bool:
    """Повертає True лише якщо помилка Telegram означає, що повідомлення видалене або не підлягає редагуванню."""
    msg = str(err).lower()
    missing_markers = (
        "message to edit not found",
        "message can't be edited",
        "message to delete not found",
        "message can't be deleted",
        "message_id_invalid",
        "there is no text in the message to edit",
        "message not found",
    )
    return any(marker in msg for marker in missing_markers)


def is_permanent_delete_error(err: Exception) -> bool:
    """Повертає True, якщо повідомлення вже видалене або не може бути видалене (>48 год / невірний ID)."""
    msg = str(err).lower()
    permanent_markers = (
        "message to delete not found",
        "message can't be deleted",
        "message_id_invalid",
        "chat not found",
        "message not found",
    )
    return any(marker in msg for marker in permanent_markers)


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


async def _collect_known_alert_msg_ids(user_id: int, sub: dict = None, extra_msg_id: int = None) -> set[int]:
    """Збирає всі відомі ID повідомлень тривоги користувача з підписки, БД та оперативної пам'яті."""
    known_ids = set()
    if sub and sub.get("last_alert_msg_id"):
        known_ids.add(int(sub["last_alert_msg_id"]))
    if extra_msg_id:
        known_ids.add(int(extra_msg_id))

    try:
        fresh_sub = await get_user_subscription(user_id)
        if fresh_sub and fresh_sub.get("last_alert_msg_id"):
            known_ids.add(int(fresh_sub["last_alert_msg_id"]))
    except Exception:
        pass

    try:
        db_ids = await get_user_alert_message_ids(user_id)
        for d_id in db_ids:
            if d_id:
                known_ids.add(int(d_id))
    except Exception as db_err:
        logger.debug(f"Could not load alert messages from DB for user {user_id}: {db_err}")

    if user_id in _user_active_msg_ids:
        known_ids.update(int(x) for x in _user_active_msg_ids[user_id] if x)

    return known_ids


async def cleanup_stale_alert_messages(bot, chat_id: int, user_id: int, keep_msg_id: int) -> None:
    """
    Видаляє залишкові повідомлення тривоги, які не вдалося видалити раніше через мережеві збої (502 / Timed out).
    Видаляє записи з БД та пам'яті лише після підтвердженого видалення в Telegram.
    """
    if user_id not in _user_active_msg_ids:
        try:
            db_ids = await get_user_alert_message_ids(user_id)
            _user_active_msg_ids[user_id] = set(int(x) for x in db_ids if x)
        except Exception:
            _user_active_msg_ids[user_id] = set()

    if keep_msg_id:
        _user_active_msg_ids[user_id].add(int(keep_msg_id))

    stale_ids = [m_id for m_id in list(_user_active_msg_ids[user_id]) if m_id != keep_msg_id]
    if not stale_ids:
        return

    confirmed_deleted = []
    for m_id in sorted(stale_ids):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=m_id)
            confirmed_deleted.append(m_id)
            _user_active_msg_ids[user_id].discard(m_id)
        except Exception as del_err:
            if is_permanent_delete_error(del_err):
                confirmed_deleted.append(m_id)
                _user_active_msg_ids[user_id].discard(m_id)
            else:
                logger.debug(f"Transient error deleting stale alert message {m_id} for {user_id}: {del_err}")

    if confirmed_deleted:
        try:
            await delete_alert_message_records(user_id, confirmed_deleted)
        except Exception:
            pass


async def replace_active_alert_message(
    bot,
    chat_id: int,
    user_id: int,
    sub: dict,
    new_msg_id: int,
    extra_msg_id_to_delete: int = None,
) -> None:
    """
    Реєструє нове активне повідомлення бота (картку статусу, меню, карту або історію)
    і видаляє всі попередні (включно з можливими фантомними повідомленнями після таймаутів).
    """
    msg_ids_to_delete = await _collect_known_alert_msg_ids(user_id, sub=sub, extra_msg_id=extra_msg_id_to_delete)

    # Якщо перед цим був таймаут send_message, Telegram міг доставити фантомні повідомлення у проміжку (anchor..new_msg_id)
    anchor_id = _user_failed_send_anchor.pop(user_id, None)
    if anchor_id and new_msg_id > anchor_id + 1 and not is_media_plugin_enabled():
        sweep_start = max(int(anchor_id) + 1, int(new_msg_id) - 60)
        for phantom_id in range(sweep_start, int(new_msg_id)):
            msg_ids_to_delete.add(phantom_id)

    msg_ids_to_delete.discard(int(new_msg_id))

    # Зберігаємо нове повідомлення як актуальне
    if sub is not None:
        sub["last_alert_msg_id"] = int(new_msg_id)
    try:
        await set_user_last_alert_msg_id(user_id, int(new_msg_id))
        await add_alert_message_record(user_id, chat_id, int(new_msg_id))
    except Exception as db_err:
        logger.debug(f"Could not save new alert message ID {new_msg_id} for {user_id}: {db_err}")

    active_set = _user_active_msg_ids.setdefault(user_id, set())
    active_set.add(int(new_msg_id))

    confirmed_deleted = []
    for m_id in sorted(msg_ids_to_delete):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=m_id)
            confirmed_deleted.append(m_id)
            active_set.discard(m_id)
        except Exception as del_err:
            if is_permanent_delete_error(del_err):
                confirmed_deleted.append(m_id)
                active_set.discard(m_id)
            else:
                # Тимчасовий збій мережі (502 / Timed out) — зберігаємо ID для повторного видалення
                logger.warning(f"Transient error deleting old alert message {m_id} for user {user_id}: {del_err}")
                active_set.add(m_id)
                try:
                    await add_alert_message_record(user_id, chat_id, m_id)
                except Exception:
                    pass

    if confirmed_deleted:
        try:
            await delete_alert_message_records(user_id, confirmed_deleted)
        except Exception:
            pass


async def repost_alert_message_to_bottom(
    bot,
    chat_id: int,
    user_id: int,
    sub: dict,
    current_state: dict,
    is_manual_check: bool = False,
    prev_level: str = None,
    extra_msg_id_to_delete: int = None,
) -> bool:
    """
    Надсилає НОВЕ повідомлення тривоги насамкінець чату, після чого безпечно видаляє попередні.
    Повертає True у разі успіху або False при тимчасовому збої Telegram API.
    """
    known_ids = await _collect_known_alert_msg_ids(user_id, sub=sub, extra_msg_id=extra_msg_id_to_delete)

    # Якщо попередня відправка завершилась таймаутом/502, спочатку перевіряємо доступність API легким GET-запитом,
    # щоб не ставити в чергу Telegram нові фантомні повідомлення під час глобального збою.
    if user_id in _user_failed_send_anchor:
        try:
            await bot.get_me()
        except Exception as ping_err:
            _user_last_send_attempt[user_id] = time.time()
            logger.debug(f"Telegram API still unreachable for {user_id} (get_me failed: {ping_err}), skipping send_message")
            return False

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
            disable_web_page_preview=True,
        )
        _user_last_send_attempt.pop(user_id, None)
        await replace_active_alert_message(
            bot=bot,
            chat_id=chat_id,
            user_id=user_id,
            sub=sub,
            new_msg_id=sent_msg.message_id,
            extra_msg_id_to_delete=extra_msg_id_to_delete,
        )
        return True
    except Exception as send_err:
        _user_last_send_attempt[user_id] = time.time()
        if known_ids and user_id not in _user_failed_send_anchor:
            _user_failed_send_anchor[user_id] = max(known_ids)
        logger.warning(f"Could not repost alert message to bottom for {chat_id}: {send_err}")
        return False


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

                        # Дочищаємо старі повідомлення, якщо попередній delete_message не пройшов через мережевий збій
                        if last_msg_id:
                            await cleanup_stale_alert_messages(application.bot, chat_id, user_id, last_msg_id)

                        current_state = evaluate_user_alert_state(sub, alerts_data, threats_data, ua_data)
                        curr_level = current_state["level"]
                        prev_level = _user_last_alert_state.get(user_id)
                        current_sig = make_threat_signature(current_state)
                        prev_sig = _user_last_threat_signature.get(user_id)
                        last_auto_edit = _user_last_auto_edit_time.get(user_id, 0)
                        sound_enabled = sub.get("sound_enabled", True)
                        putin_enabled = sub.get("putin_check_enabled", True)
                        alert_kb = get_alert_inline_keyboard(sound_enabled, putin_enabled)

                        # Якщо попередня відправка send_message впала з таймаутом, після відновлення зв'язку
                        # перевідправляємо картку вниз і підмітаємо можливі фантомні повідомлення
                        if user_id in _user_failed_send_anchor and prev_level is not None and curr_level == prev_level:
                            if now - _user_last_send_attempt.get(user_id, 0) >= 30:
                                recovered = await repost_alert_message_to_bottom(
                                    bot=application.bot,
                                    chat_id=chat_id,
                                    user_id=user_id,
                                    sub=sub,
                                    current_state=current_state,
                                    is_manual_check=(curr_level == "green"),
                                    prev_level=prev_level,
                                )
                                if recovered:
                                    _user_last_alert_state[user_id] = curr_level
                                    _user_last_threat_signature[user_id] = current_sig
                                    _user_last_auto_edit_time[user_id] = now
                                continue

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
                                    if "message is not modified" not in str(date_edit_err).lower():
                                        _user_last_putin_date[user_id] = last_putin_date
                                    logger.debug(f"Daily putin date edit failed for {user_id}: {date_edit_err}")

                        if prev_level is None:
                            if user_id in _user_failed_send_anchor and (now - _user_last_send_attempt.get(user_id, 0) < 30):
                                continue

                            if curr_level in ("red", "yellow") and user_id not in _user_active_alert_meta:
                                start_fmt, start_ts = _parse_alert_start_time(current_state.get("since"), fallback_ts=now)
                                _user_active_alert_meta[user_id] = {
                                    "started_at": start_fmt,
                                    "alert_type": current_state.get("alert_type") or "AIR",
                                    "max_level": curr_level,
                                    "reasons": list(current_state.get("reasons", [])),
                                    "start_timestamp": start_ts
                                }

                            # Надсилаємо нове повідомлення насамкінець чату та видаляємо старе
                            sent_ok = await repost_alert_message_to_bottom(
                                bot=application.bot,
                                chat_id=chat_id,
                                user_id=user_id,
                                sub=sub,
                                current_state=current_state,
                                is_manual_check=True
                            )
                            if sent_ok:
                                _user_last_alert_state[user_id] = curr_level
                                _user_last_threat_signature[user_id] = current_sig
                                _user_last_auto_edit_time[user_id] = now
                            continue

                        if curr_level != prev_level:
                            if user_id in _user_failed_send_anchor and (now - _user_last_send_attempt.get(user_id, 0) < 30):
                                continue

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

                            sent_ok = await repost_alert_message_to_bottom(
                                bot=application.bot,
                                chat_id=chat_id,
                                user_id=user_id,
                                sub=sub,
                                current_state=current_state,
                                is_manual_check=False,
                                prev_level=prev_level
                            )
                            if sent_ok:
                                _user_last_alert_state[user_id] = curr_level
                                _user_last_threat_signature[user_id] = current_sig
                                _user_last_auto_edit_time[user_id] = now
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
                                        err_lower = str(edit_err).lower()
                                        if "message is not modified" in err_lower:
                                            pass
                                        elif is_message_missing_error(edit_err):
                                            logger.debug(f"In-place edit target missing ({edit_err}), reposting...")
                                            sent_ok = await repost_alert_message_to_bottom(
                                                bot=application.bot,
                                                chat_id=chat_id,
                                                user_id=user_id,
                                                sub=sub,
                                                current_state=current_state,
                                                is_manual_check=False,
                                                prev_level=prev_level
                                            )
                                            if not sent_ok:
                                                _user_last_threat_signature[user_id] = prev_sig
                                                _user_last_auto_edit_time[user_id] = last_auto_edit
                                        else:
                                            # Мережева помилка (502 / Timed out) — НЕ створюємо дублікат, повторимо edit на наступній ітерації
                                            logger.warning(f"Transient error editing alert message {last_msg_id} for {user_id}: {edit_err}")
                                            _user_last_threat_signature[user_id] = prev_sig
                                            _user_last_auto_edit_time[user_id] = last_auto_edit

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
