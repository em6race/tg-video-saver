"""Card formatter and visualization for Yasno Outages."""

import datetime
from typing import Optional, Tuple
import pytz

from yasno_outages.models import GroupSchedule, DaySchedule, OutageSlot

KYIV_TZ = pytz.timezone("Europe/Kiev")

DAY_NAMES_UA = {
    0: "понеділок",
    1: "вівторок",
    2: "середа",
    3: "четвер",
    4: "п'ятниця",
    5: "субота",
    6: "неділя",
}

MONTH_NAMES_UA = {
    1: "січня",
    2: "лютого",
    3: "березня",
    4: "квітня",
    5: "травня",
    6: "червня",
    7: "липня",
    8: "серпня",
    9: "вересня",
    10: "жовтня",
    11: "листопада",
    12: "грудня",
}

def get_current_kyiv_time() -> datetime.datetime:
    """Returns current datetime in Kyiv timezone."""
    return datetime.datetime.now(KYIV_TZ)

def format_duration(minutes: int) -> str:
    """Formats minutes into human-readable Ukrainian string (e.g. '2 год 15 хв')."""
    h = minutes // 60
    m = minutes % 60
    parts = []
    if h > 0:
        parts.append(f"{h} год")
    if m > 0:
        parts.append(f"{m} хв")
    return " ".join(parts) if parts else "0 хв"

def format_date_ua(d: datetime.date) -> str:
    """Formats date into Ukrainian (e.g. '10 жовтня, субота')."""
    day_name = DAY_NAMES_UA.get(d.weekday(), "")
    month_name = MONTH_NAMES_UA.get(d.month, "")
    return f"{d.day} {month_name}, {day_name}"

def get_current_state_and_countdown(day_schedule: DaySchedule, current_time: datetime.datetime) -> Tuple[str, str, Optional[int]]:
    """
    Evaluates current state (ON, OFF, PROBABLE) and countdown to next change.
    Returns: (status_label, countdown_text, next_transition_minute)
    """
    if day_schedule.is_emergency:
        return ("EMERGENCY", "🚨 Діють екстрені аварійні відключення. Графіки тимчасово не діють.", None)

    if day_schedule.is_no_outages:
        return ("ON", "🟢 Відключень не заплановано. Світло є постійно.", None)

    if not day_schedule.is_schedule_available:
        return ("UNKNOWN", "⏳ Очікується публікація графіка від Укренерго.", None)

    current_minute = current_time.hour * 60 + current_time.minute

    # Find the current active slot
    current_slot: Optional[OutageSlot] = None
    next_slot: Optional[OutageSlot] = None

    for i, slot in enumerate(day_schedule.slots):
        if slot.start <= current_minute < slot.end:
            current_slot = slot
            if i + 1 < len(day_schedule.slots):
                next_slot = day_schedule.slots[i + 1]
            break

    if not current_slot:
        # End of day or not covered
        return ("ON", "🟢 До кінця доби відключень не планується.", None)

    slot_type = current_slot.slot_type
    time_left = max(0, current_slot.end - current_minute)
    time_left_str = format_duration(time_left)

    end_hour = current_slot.end // 60
    end_min = current_slot.end % 60
    end_str = "24:00" if (end_hour == 24 and end_min == 0) else f"{end_hour:02d}:{end_min:02d}"

    if slot_type == "Definite":
        countdown_text = f"🔌 **Світла немає**. До включення: **{time_left_str}** (орієнтовно о {end_str})"
        state_key = "OFF"
    elif slot_type == "Probable":
        countdown_text = f"⚠️ **Можливе відключення**. Закінчення сірої зони через: **{time_left_str}** (о {end_str})"
        state_key = "PROBABLE"
    else:
        # NotPlanned = power ON
        if next_slot and next_slot.slot_type in ("Definite", "Probable"):
            action = "вимкнення" if next_slot.slot_type == "Definite" else "можливого вимкнення"
            countdown_text = f"⚡ **Світло є**. До {action}: **{time_left_str}** (о {end_str})"
        else:
            countdown_text = "⚡ **Світло є**. До кінця доби відключень більше не заплановано."
        state_key = "ON"

    return (state_key, countdown_text, current_slot.end)

def generate_visual_timeline(day_schedule: DaySchedule, current_time: Optional[datetime.datetime] = None) -> str:
    """
    Generates a 24-character visual timeline bar with current hour marker.
    🟩 = NotPlanned (Світло є)
    🟥 = Definite (Відключення)
    🟨 = Probable (Можливе)
    ⬜ = Unknown
    """
    if not day_schedule.slots:
        if day_schedule.is_no_outages:
            bar = "🟩" * 24
        else:
            bar = "⬜" * 24
    else:
        # Build minute map
        minute_map = ["🟩"] * 1440
        for slot in day_schedule.slots:
            char = "🟥" if slot.slot_type == "Definite" else ("🟨" if slot.slot_type == "Probable" else "🟩")
            start = max(0, min(1440, slot.start))
            end = max(0, min(1440, slot.end))
            for m in range(start, end):
                minute_map[m] = char

        # Sample for each of the 24 hours (taking middle of the hour)
        blocks = []
        for h in range(24):
            mid_min = h * 60 + 30
            blocks.append(minute_map[mid_min])
        bar = "".join(blocks)

    time_labels = "`00   04   08   12   16   20   24`"

    # Arrow indicator for current time
    arrow_line = ""
    if current_time:
        cur_h = current_time.hour
        # In monospace / emoji layout, each block is 1 emoji.
        # Place pointer indicator
        spaces = "  " * cur_h
        arrow_line = f"\n`{spaces}▲ зараз ({current_time.strftime('%H:%M')})`"

    return f"`{bar}`\n{time_labels}{arrow_line}"

def format_outage_card(
    group_schedule: GroupSchedule,
    region_name: str = "Київ",
    dso_name: str = "ДТЕК",
    address: Optional[str] = None,
    view_day: str = "today",
    notifications_enabled: bool = True
) -> str:
    """
    Formats the complete interactive status card in Markdown.
    """
    now = get_current_kyiv_time()
    today_date = now.date()
    tomorrow_date = today_date + datetime.timedelta(days=1)

    is_viewing_today = (view_day == "today")
    target_schedule = group_schedule.today if is_viewing_today else group_schedule.tomorrow
    target_date = today_date if is_viewing_today else tomorrow_date

    # Determine state and countdown (only relevant for today)
    if is_viewing_today:
        state_key, countdown_text, _ = get_current_state_and_countdown(target_schedule, now)
    else:
        if target_schedule.is_emergency:
            countdown_text = "🚨 На завтра оголошено аварійні відключення."
        elif target_schedule.is_no_outages:
            countdown_text = "🟢 На завтра відключень не заплановано."
        elif target_schedule.is_schedule_available:
            countdown_text = "📋 Графік на завтра затверджено."
        else:
            countdown_text = "⏳ Графік на завтра ще формується."

    # Header styling
    if is_viewing_today:
        if target_schedule.is_emergency:
            header = "🚨 **АВАРІЙНІ ВІДКЛЮЧЕННЯ СВІТЛА** 🚨"
        elif state_key == "OFF":
            header = "🔌 **ВІДКЛЮЧЕННЯ СВІТЛА ЗА ГРАФІКОМ**"
        elif state_key == "PROBABLE":
            header = "⚠️ **СІРА ЗОНА (МОЖЛИВЕ ВІДКЛЮЧЕННЯ)**"
        else:
            header = "⚡ **СВІТЛО Є — ГРАФІК У НОРМІ**"
    else:
        header = f"📅 **ГРАФІК НА ЗАВТРА ({format_date_ua(target_date)})**"

    # Address / Group line
    loc_parts = [f"📍 **{region_name}** ({dso_name})", f"Група **{group_schedule.group}**"]
    loc_header = " • ".join(loc_parts)
    addr_line = f"🏠 *{address}*\n" if address else ""

    # Notification status
    notif_icon = "🔔" if notifications_enabled else "🔕"

    # Visual timeline
    timeline_str = generate_visual_timeline(target_schedule, now if is_viewing_today else None)

    # Detailed slots list
    slots_text = ""
    if target_schedule.slots:
        slot_lines = []
        for s in target_schedule.slots:
            dur_str = format_duration(s.duration_minutes)
            if s.slot_type == "Definite":
                slot_lines.append(f"• 🔴 `{s.start_time_str} – {s.end_time_str}` ({dur_str}) — Відключення")
            elif s.slot_type == "Probable":
                slot_lines.append(f"• 🟡 `{s.start_time_str} – {s.end_time_str}` ({dur_str}) — Можливе вимкнення")
            else:
                slot_lines.append(f"• 🟢 `{s.start_time_str} – {s.end_time_str}` ({dur_str}) — Світло є")
        slots_text = "\n".join(slot_lines)
    elif target_schedule.is_no_outages:
        slots_text = "• 🟢 `00:00 – 24:00` (24 год) — Відключень немає"
    elif target_schedule.is_emergency:
        slots_text = "• 🚨 Діють графіки аварійних відключень (ГАВ). Погодинні інтервали не діють."
    else:
        slots_text = "• ⏳ Графік ще не опубліковано. Зазвичай з'являється після 18:00–20:00 напередодні."

    # Totals summary
    outage_hours = round(target_schedule.total_outage_minutes / 60, 1)
    probable_hours = round(target_schedule.total_probable_minutes / 60, 1)

    totals_parts = []
    if outage_hours > 0:
        totals_parts.append(f"🔴 Без світла: ~**{outage_hours} год**")
    if probable_hours > 0:
        totals_parts.append(f"🟡 Сіра зона: ~**{probable_hours} год**")
    if not totals_parts and target_schedule.is_schedule_available:
        totals_parts.append("🟢 Без відключень (0 год)")

    totals_line = f"\n📊 {' • '.join(totals_parts)}" if totals_parts else ""

    day_label = "сьогодні" if is_viewing_today else "завтра"
    date_display = format_date_ua(target_date)

    updated_time_str = now.strftime("%H:%M:%S")

    msg = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{loc_header}\n"
        f"{addr_line}\n"
        f"📌 {countdown_text}\n\n"
        f"📊 **Шкала доби ({day_label}):**\n"
        f"{timeline_str}\n\n"
        f"📋 **Розклад на {date_display}:**\n"
        f"{slots_text}"
        f"{totals_line}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 *Легенда: 🟢 світло є | 🔴 відключення | 🟡 можливе*\n"
        f"🔄 Оновлено: `{updated_time_str}` • Сповіщення: {notif_icon}"
    )
    return msg
