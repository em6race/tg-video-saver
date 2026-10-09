"""Visual infographic card generator for Yasno/DTEK Outage schedules."""

import io
import os
import math
from datetime import datetime, timedelta
from typing import Optional, Tuple
import pytz
from PIL import Image, ImageDraw, ImageFont

from yasno_outages.models import GroupSchedule, DaySchedule

KYIV_TZ = pytz.timezone("Europe/Kiev")

MONTHS_UA = [
    "",
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
]


def _find_font(font_name: str) -> str:
    """Finds font file across standard paths."""
    candidates = [
        os.path.join("assets", "fonts", font_name),
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts", font_name),
        os.path.join(os.path.dirname(__file__), "..", "assets", "fonts", font_name),
        font_name,
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.abspath(c)
    return font_name


def _load_font(font_name: str, size: int) -> ImageFont.ImageFont:
    """Safely loads TrueType font with fallback."""
    path = _find_font(font_name)
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        try:
            return ImageFont.load_default()
        except Exception:
            return None


def draw_vector_bolt(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float = 18, color: Tuple[int, int, int] = (250, 204, 21)):
    pts = [
        (cx + size * 0.15, cy - size * 0.5),
        (cx - size * 0.35, cy + size * 0.05),
        (cx - size * 0.05, cy + size * 0.05),
        (cx - size * 0.2, cy + size * 0.5),
        (cx + size * 0.35, cy - size * 0.05),
        (cx + size * 0.05, cy - size * 0.05),
    ]
    draw.polygon(pts, fill=color)


def draw_vector_pin(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float = 16, color: Tuple[int, int, int] = (244, 63, 94)):
    r = size / 2.0
    draw.ellipse([cx - r, cy - r - 2, cx + r, cy + r - 2], fill=color)
    draw.polygon([(cx - r + 1, cy - 2), (cx + r - 1, cy - 2), (cx, cy + r + 2)], fill=color)
    draw.ellipse([cx - 2, cy - 4, cx + 2, cy], fill=(255, 255, 255))


def draw_vector_clock(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float = 8, color: Tuple[int, int, int] = (56, 189, 248)):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=2)
    draw.line([cx, cy, cx, cy - r + 3], fill=color, width=2)
    draw.line([cx, cy, cx + r - 3, cy], fill=color, width=2)


def draw_vector_calendar(draw: ImageDraw.ImageDraw, cx: float, cy: float, w: float = 15, h: float = 14, color: Tuple[int, int, int] = (168, 85, 247)):
    draw.rounded_rectangle([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], radius=3, outline=color, width=2)
    draw.rectangle([cx - w / 2 + 1, cy - h / 2, cx + w / 2 - 1, cy - h / 2 + 4], fill=color)
    draw.line([cx - 4, cy - h / 2 - 2, cx - 4, cy - h / 2], fill=(241, 245, 249), width=2)
    draw.line([cx + 4, cy - h / 2 - 2, cx + 4, cy - h / 2], fill=(241, 245, 249), width=2)


def generate_outage_card_image(
    group_schedule: GroupSchedule,
    region_name: str = "Київ",
    dso_name: str = "ДТЕК",
    address: Optional[str] = None,
    view_day: str = "today",
) -> io.BytesIO:
    """
    Renders an 800x330 visual infographic card with edge-to-edge styling,
    vector icons (no Unicode emoji boxes), and flush ticks.
    """
    now = datetime.now(KYIV_TZ)
    is_today = (view_day == "today")
    target_schedule: DaySchedule = group_schedule.today if is_today else group_schedule.tomorrow
    target_date = now.date() if is_today else (now.date() + timedelta(days=1))

    # Font definitions
    font_bold_title = _load_font("Roboto-Bold.ttf", 23)
    font_bold_sub = _load_font("Roboto-Bold.ttf", 19)
    font_reg_addr = _load_font("Roboto-Regular.ttf", 17)
    font_date_pill = _load_font("Roboto-Bold.ttf", 16)
    font_time_tag = _load_font("Roboto-Bold.ttf", 16)
    font_ticks = _load_font("Roboto-Regular.ttf", 16)
    font_stats = _load_font("Roboto-Bold.ttf", 17)

    # Canvas setup: 800x330 seamless edge-to-edge background
    W, H = 800, 330
    CARD_BG = (24, 27, 44)
    img = Image.new("RGB", (W, H), color=CARD_BG)
    draw = ImageDraw.Draw(img)

    # 1. State computation for header
    current_minute = now.hour * 60 + now.minute
    current_slot = None
    next_slot = None
    next_change_minute: Optional[int] = None

    if is_today and target_schedule.slots:
        for i, slot in enumerate(target_schedule.slots):
            if slot.start <= current_minute < slot.end:
                current_slot = slot
                if i + 1 < len(target_schedule.slots):
                    next_slot = target_schedule.slots[i + 1]
                break

    badge_cy = 40
    badge_h = 42

    if not is_today:
        badge_w = 175
        badge_bg = (49, 46, 129)
        badge_text = "НА ЗАВТРА"
        bolt_col = (165, 180, 252)
        sub_text = "Графік затверджено" if target_schedule.is_schedule_available else "Очікується графік від Укренерго"
    elif target_schedule.is_emergency:
        badge_w = 155
        badge_bg = (153, 27, 27)
        badge_text = "АВАРІЯ"
        bolt_col = (250, 204, 21)
        sub_text = "Діють екстрені аварійні відключення"
    elif current_slot and current_slot.slot_type == "Definite":
        badge_w = 205
        badge_bg = (153, 27, 27)
        badge_text = "СВІТЛА НЕМАЄ"
        bolt_col = (248, 113, 113)
        rem = max(0, current_slot.end - current_minute)
        rh, rm = rem // 60, rem % 60
        dur_str = f"{rh} год {rm:02d} хв" if rh > 0 else f"{rm} хв"
        eh, em = current_slot.end // 60, current_slot.end % 60
        end_time_str = "24:00" if eh == 24 else f"{eh:02d}:{em:02d}"
        sub_text = f"До увімкнення: {dur_str} (о {end_time_str})"
        next_change_minute = current_slot.end
    elif current_slot and current_slot.slot_type == "Probable":
        badge_w = 165
        badge_bg = (133, 77, 14)
        badge_text = "СІРА ЗОНА"
        bolt_col = (253, 224, 71)
        eh, em = current_slot.end // 60, current_slot.end % 60
        end_time_str = "24:00" if eh == 24 else f"{eh:02d}:{em:02d}"
        sub_text = f"Можливе вимкнення (до {end_time_str})"
        next_change_minute = current_slot.end
    else:
        badge_w = 165
        badge_bg = (22, 101, 52)
        badge_text = "СВІТЛО Є"
        bolt_col = (250, 204, 21)
        if current_slot and next_slot and next_slot.slot_type in ("Definite", "Probable"):
            rem = max(0, current_slot.end - current_minute)
            rh, rm = rem // 60, rem % 60
            dur_str = f"{rh} год {rm:02d} хв" if rh > 0 else f"{rm} хв"
            eh, em = current_slot.end // 60, current_slot.end % 60
            end_time_str = "24:00" if eh == 24 else f"{eh:02d}:{em:02d}"
            sub_text = f"До вимкнення: {dur_str} (о {end_time_str})"
            next_change_minute = current_slot.end
        elif target_schedule.is_no_outages:
            sub_text = "Відключень на добу не заплановано"
        else:
            sub_text = "Відключень більше не заплановано"

    # Draw header badge
    draw.rounded_rectangle([24, badge_cy - badge_h // 2, 24 + badge_w, badge_cy + badge_h // 2], radius=8, fill=badge_bg)
    draw_vector_bolt(draw, 45, badge_cy, size=19, color=bolt_col)
    draw.text((63, badge_cy), badge_text, font=font_bold_title, fill=(255, 255, 255), anchor="lm")
    draw.text((24 + badge_w + 16, badge_cy), sub_text, font=font_bold_sub, fill=(241, 245, 249), anchor="lm")

    # 2. Row 2: Address on Left, Date Pill on Right
    addr_cy = 84
    date_str = f"{target_date.day} {MONTHS_UA[target_date.month]}"
    d_box = font_date_pill.getbbox(date_str)
    d_w = d_box[2] - d_box[0]
    pill_w = d_w + 42
    pill_x2 = 776
    pill_x1 = pill_x2 - pill_w

    # Date badge pill (Right)
    draw.rounded_rectangle([pill_x1, addr_cy - 16, pill_x2, addr_cy + 16], radius=7, fill=(49, 46, 129), outline=(99, 102, 241), width=1)
    draw_vector_calendar(draw, pill_x1 + 16, addr_cy, w=15, h=14, color=(165, 180, 252))
    draw.text((pill_x1 + 32, addr_cy), date_str, font=font_date_pill, fill=(224, 231, 255), anchor="lm")

    # Address & Group (Left)
    draw_vector_pin(draw, 34, addr_cy, size=16, color=(244, 63, 94))
    addr_parts = [f"{region_name} ({dso_name})", f"Група {group_schedule.group}"]
    if address:
        addr_parts.append(address)
    full_addr = " • ".join(addr_parts)

    # Truncate address if needed to not collide with date pill
    max_addr_w = pill_x1 - 60
    while len(full_addr) > 10:
        box = font_reg_addr.getbbox(full_addr)
        if (box[2] - box[0]) <= max_addr_w:
            break
        full_addr = full_addr[:-4] + "..."

    draw.text((50, addr_cy), full_addr, font=font_reg_addr, fill=(148, 163, 184), anchor="lm")

    # 3. Timeline Bar
    bx, by, bw, bh = 24, 150, 752, 52
    bar_right = bx + bw  # 776

    slots = []
    if target_schedule.slots:
        slots = [(s.start, s.end, s.slot_type) for s in target_schedule.slots]
    elif target_schedule.is_no_outages:
        slots = [(0, 1440, "NotPlanned")]
    elif target_schedule.is_emergency:
        slots = [(0, 1440, "Definite")]
    else:
        slots = [(0, 1440, "Waiting")]

    for s_start, s_end, s_type in slots:
        sx = bx + int((s_start / 1440) * bw)
        ex = bx + int((s_end / 1440) * bw)
        if s_type == "Definite":
            c = (239, 68, 68)   # Red
        elif s_type == "Probable":
            c = (234, 179, 8)   # Yellow
        elif s_type == "NotPlanned":
            c = (34, 197, 94)   # Green
        else:
            c = (71, 85, 105)   # Slate
        draw.rectangle([sx, by, ex, by + bh], fill=c)

    # Timeline ticks & hour markers (00, 04, 08, 12, 16, 20, 24)
    tick_y = by + bh + 19
    for h in (0, 4, 8, 12, 16, 20, 24):
        tx = bx + int(h * (bw / 24))
        draw.line([tx, by + bh, tx, by + bh + 7], fill=(148, 163, 184), width=1)
        label = f"{h:02d}:00"
        if h == 0:
            draw.text((bx, tick_y), label, font=font_ticks, fill=(203, 213, 225), anchor="lm")
        elif h == 24:
            draw.text((bar_right + 1, tick_y), label, font=font_ticks, fill=(203, 213, 225), anchor="rm")
        else:
            draw.text((tx, tick_y), label, font=font_ticks, fill=(203, 213, 225), anchor="mm")

    # Current time needle (only for today)
    if is_today:
        nx = bx + int((current_minute / 1440) * bw)
        now_str = now.strftime("%H:%M")
        t_box = font_time_tag.getbbox(now_str)
        t_w = t_box[2] - t_box[0]
        t_badge_w = t_w + 18
        b_left = nx - (t_badge_w / 2)
        b_right = nx + (t_badge_w / 2)
        if b_left < bx:
            b_left = bx
            b_right = bx + t_badge_w
        elif b_right > bar_right:
            b_right = bar_right
            b_left = bar_right - t_badge_w

        tag_cy = by - 26
        draw.rounded_rectangle([b_left, tag_cy - 12, b_right, tag_cy + 12], radius=6, fill=(234, 179, 8))
        draw.text(((b_left + b_right) / 2, tag_cy), now_str, font=font_time_tag, fill=(15, 23, 42), anchor="mm")
        draw.polygon([(nx - 7, by - 12), (nx + 7, by - 12), (nx, by - 2)], fill=(234, 179, 8))
        draw.line([nx, by, nx, by + bh], fill=(234, 179, 8), width=3)

    # 4. Bottom Stats Summary
    sy = 262
    sh = 44
    pill_gap = 12
    p_w = (bw - pill_gap * 2) // 3  # ~242 px each

    outage_mins = target_schedule.total_outage_minutes
    prob_mins = target_schedule.total_probable_minutes
    light_mins = max(0, 1440 - outage_mins - prob_mins)

    light_h = round(light_mins / 60)
    outage_h = round(outage_mins / 60)

    # Stat 1: Green light
    p1_x = bx
    draw.rounded_rectangle([p1_x, sy, p1_x + p_w, sy + sh], radius=8, fill=(36, 41, 64))
    draw.ellipse([p1_x + 14, sy + 15, p1_x + 28, sy + 29], fill=(34, 197, 94))
    draw.text((p1_x + 36, sy + 22), f"Зі світлом: ~{light_h} год", font=font_stats, fill=(74, 222, 128), anchor="lm")

    # Stat 2: Red blackout
    p2_x = p1_x + p_w + pill_gap
    draw.rounded_rectangle([p2_x, sy, p2_x + p_w, sy + sh], radius=8, fill=(36, 41, 64))
    draw.ellipse([p2_x + 14, sy + 15, p2_x + 28, sy + 29], fill=(239, 68, 68))
    draw.text((p2_x + 36, sy + 22), f"Без світла: ~{outage_h} год", font=font_stats, fill=(248, 113, 113), anchor="lm")

    # Stat 3: Next change / status
    p3_x = p2_x + p_w + pill_gap
    draw.rounded_rectangle([p3_x, sy, bar_right, sy + sh], radius=8, fill=(36, 41, 64))
    draw_vector_clock(draw, p3_x + 20, sy + 22, r=8, color=(56, 189, 248))

    if is_today and next_change_minute is not None:
        ch_h, ch_m = next_change_minute // 60, next_change_minute % 60
        ch_str = "24:00" if ch_h == 24 else f"{ch_h:02d}:{ch_m:02d}"
        stat3_label = f"Зміна: о {ch_str}"
    elif not is_today:
        stat3_label = "Графік на добу"
    else:
        stat3_label = "Без змін"
    draw.text((p3_x + 36, sy + 22), stat3_label, font=font_stats, fill=(56, 189, 248), anchor="lm")

    # Export to memory buffer
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
