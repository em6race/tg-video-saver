"""Background monitoring loop for schedule updates and upcoming power cuts."""

import asyncio
import hashlib
import logging
from datetime import datetime
import pytz
from telegram.ext import Application

from database import (
    get_all_enabled_outage_subscriptions,
    update_outage_schedule_hash,
    set_outage_last_msg_id,
)
from yasno_outages import yasno_client, format_outage_card, get_current_kyiv_time
from keyboards import get_outage_inline_keyboard
from handlers.outages.state import _user_last_upcoming_notified

logger = logging.getLogger(__name__)

KYIV_TZ = pytz.timezone("Europe/Kiev")

def compute_schedule_hash(slots: list) -> str:
    """Computes a short hash representing the day's outage slots."""
    sig = "|".join(f"{s.start}-{s.end}-{s.slot_type}" for s in slots)
    return hashlib.md5(sig.encode("utf-8")).hexdigest()[:10]

async def outages_monitoring_loop(application: Application) -> None:
    """
    Periodic background task checking Yasno API for schedule changes
    and alerting users ~30 minutes before upcoming blackouts.
    """
    logger.info("Starting Yasno outages background monitoring loop...")
    await asyncio.sleep(15)  # Wait for bot startup

    while True:
        try:
            subs = await get_all_enabled_outage_subscriptions()
            if not subs:
                await asyncio.sleep(120)
                continue

            now = get_current_kyiv_time()
            now_minute = now.hour * 60 + now.minute

            # Cache raw data per (region_id, dso_id) within this cycle to avoid duplicate requests
            cycle_cache = {}

            for sub in subs:
                user_id = sub["user_id"]
                chat_id = sub["chat_id"] or user_id
                region_id = sub["region_id"] or 25
                dso_id = sub["dso_id"] or 902
                group = sub["group_name"]
                region_name = sub.get("region_name", "Київ")
                dso_name = sub.get("dso_name", "ДТЕК")
                address = sub.get("address")
                last_hash = sub.get("last_schedule_hash")

                cache_key = (region_id, dso_id)
                if cache_key not in cycle_cache:
                    try:
                        cycle_cache[cache_key] = await yasno_client.get_raw_planned_outages(region_id, dso_id, force=True)
                    except Exception as api_err:
                        logger.warning(f"Failed to fetch outages for region {region_id}, dso {dso_id}: {api_err}")
                        cycle_cache[cache_key] = None

                raw_data = cycle_cache.get(cache_key)
                if not raw_data or group not in raw_data:
                    continue

                group_info = raw_data[group]
                today_sched = yasno_client._parse_day_data(group_info.get("today"))
                tomorrow_sched = yasno_client._parse_day_data(group_info.get("tomorrow"))
                current_hash = compute_schedule_hash(today_sched.slots)

                # 1. Перевірка на зміну графіка
                if last_hash and last_hash != current_hash:
                    logger.info(f"Schedule changed for user {user_id} (group {group})!")
                    await update_outage_schedule_hash(user_id, current_hash)

                    from yasno_outages.models import GroupSchedule
                    from yasno_outages.image_generator import generate_outage_card_image
                    from yasno_outages.formatter import format_outage_caption

                    group_sched = GroupSchedule(
                        group=group, region_id=region_id, dso_id=dso_id,
                        today=today_sched, tomorrow=tomorrow_sched
                    )
                    img_buf = generate_outage_card_image(
                        group_schedule=group_sched,
                        region_name=region_name,
                        dso_name=dso_name,
                        address=address,
                        view_day="today",
                    )
                    caption = (
                        "⚠️ **Увага! Графік відключень оновлено!**\n\n"
                        + format_outage_caption(group_sched, view_day="today", notifications_enabled=True)
                    )
                    kb = get_outage_inline_keyboard(view_day="today")
                    try:
                        old_msg_id = sub.get("last_msg_id")
                        if old_msg_id:
                            try:
                                await application.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
                            except Exception:
                                pass
                        sent = await application.bot.send_photo(
                            chat_id=chat_id,
                            photo=img_buf.getvalue(),
                            caption=caption,
                            reply_markup=kb,
                            parse_mode="Markdown"
                        )
                        await set_outage_last_msg_id(user_id, sent.message_id)
                    except Exception as send_err:
                        logger.debug(f"Could not send schedule update to {chat_id}: {send_err}")

                elif not last_hash:
                    # First cycle initialization
                    await update_outage_schedule_hash(user_id, current_hash)

                # 2. Попередження за 20-35 хвилин до відключення
                adv_enabled = sub.get("notify_advance_30m", True)
                if adv_enabled and today_sched.slots:
                    for slot in today_sched.slots:
                        if slot.slot_type == "Definite":
                            mins_until = slot.start - now_minute
                            if 20 <= mins_until <= 35:
                                last_notified_slot = _user_last_upcoming_notified.get(user_id)
                                if last_notified_slot != slot.start:
                                    _user_last_upcoming_notified[user_id] = slot.start
                                    warn_text = (
                                        f"⏳ **Попередження про відключення світла!**\n\n"
                                        f"Через **{mins_until} хв** (орієнтовно о **{slot.start_time_str}**) "
                                        f"для вашої групи (**{group}**) заплановано відключення.\n\n"
                                        f"🔋 *Підзарядіть павербанки та важливі пристрої!*"
                                    )
                                    try:
                                        await application.bot.send_message(
                                            chat_id=chat_id,
                                            text=warn_text,
                                            parse_mode="Markdown"
                                        )
                                    except Exception as warn_err:
                                        logger.debug(f"Could not send upcoming warning to {chat_id}: {warn_err}")

        except Exception as e:
            logger.error(f"Error in outages monitoring loop: {e}", exc_info=True)

        await asyncio.sleep(240)  # Check every 4 minutes
