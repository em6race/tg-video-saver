import html
import random
from datetime import datetime
import pytz

from .evaluator import _clean_reason

# Карта кастомних емодзі з паків @monitorwarr та PS_WAR_NEW
# Якщо у бота є доступ до Premium — Telegram покаже стилізовану іконку.
# Якщо немає — Telegram автоматично покаже fallback Unicode емодзі.
CUSTOM_EMOJIS = {
    # Рівні тривоги
    "alert_red": ("5343853332509243087", "🚨"),      # Червоний трикутник тривоги з тризубом
    "alert_yellow": ("5344060457307094045", "⚠️"),   # Жовтий трикутник небезпеки
    "shield_green": ("5343624711400077475", "🟢"),   # Зелений щит спокою / відбою
    "siren": ("5237802734475820637", "📢"),          # Гучномовець / сирена
    
    # Інтерфейс та статус
    "pin": ("5240472408837601851", "📍"),            # Карта з міткою
    "radar": ("5442668495491837532", "📡"),          # Радар моніторингу
    "trident": ("5344036285231151054", "🔱"),        # Золотий Тризуб
    "flag_ua": ("5202137240150486571", "🇺🇦"),        # Прапор України
    "lightning": ("5217852946393346015", "⚡"),      # Блискавка / терміново
    "burst": ("5217800771130632659", "💥"),          # Вибух / розрив
    
    # Цілі
    "shahed": ("5201712313266119903", "🛵"),         # Shahed-136 (чорний нічний)
    "drone_recon": ("5237922680027496419", "🛸"),    # Розвід-БПЛА Supercam
    "missile_cruise": ("5393250541654530016", "🚀"), # Крилата ракета Х-101 / Калібр
    "missile_ballistic": ("5409320320706303601", "☄️"), # Балістика Іскандер-М
    "missile_kinzhal": ("5264948654363393239", "🚀"),  # Гіперзвуковий Кинджал
    "plane_mig31": ("5406623811388851062", "✈️"),    # МіГ-31К
    "plane_su34": ("5377781031527006892", "✈️"),     # Тактична авіація Су-34
    "plane_bomber": ("5373091455520750612", "✈️"),   # Стратегічний бомбардувальник Ту-95
    "bomb_kab": ("5471895550372634802", "💣"),       # Авіабомба КАБ
    "target_unknown": ("5264711486269317317", "⚠️"), # Невідома ціль
    
    # Захист / ППО
    "air_defense_patriot": ("5289486657223934256", "🛡️"), # Patriot ЗРК
    "air_defense_gepard": ("5287674730780835511", "⚡"),   # Gepard зенітка
}

# Варіанти відповідей на питання «Чи здох путін?», які щодня змінюються за календарем
PUTIN_STATUS_PHRASES = [
    "Поки дихає, але чорний пакет уже випрасувано. ⬛",
    "На жаль, ні. Пекло все ще розширює VIP-зону. 🔥",
    "Ще ні, але дні бункерного щура полічені. 🐀",
    "На жаль, ще ні. Але зворотний відлік невблаганно триває. ⌛",
    "Ще ні, але українська земля вже чекає на добриво. 🌱",
    "На жаль, ще ні. Але доля диктаторів завжди однакова. ⚡",
]

def get_daily_putin_phrase(target_date=None) -> str:
    """Повертає щоденну випадкову відповідь на питання 'Чи здох путін?' (рандомний порядок без повторень два дні поспіль)."""
    if target_date is None:
        target_date = datetime.now(pytz.timezone("Europe/Kiev")).date()
    elif isinstance(target_date, datetime):
        target_date = target_date.date()
    
    ord_val = target_date.toordinal()
    block_size = len(PUTIN_STATUS_PHRASES)
    if block_size == 0:
        return "На жаль, ще ні."
    if block_size == 1:
        return PUTIN_STATUS_PHRASES[0]

    block_id = ord_val // block_size
    offset = ord_val % block_size
    
    rng = random.Random(f"putin_rnd_block_{block_id}")
    shuffled = list(PUTIN_STATUS_PHRASES)
    rng.shuffle(shuffled)
    
    # Уникаємо повторення однакової фрази на стику двох блоків
    prev_rng = random.Random(f"putin_rnd_block_{block_id - 1}")
    prev_shuffled = list(PUTIN_STATUS_PHRASES)
    prev_rng.shuffle(prev_shuffled)
    if shuffled[0] == prev_shuffled[-1] and len(shuffled) > 1:
        shuffled[0], shuffled[1] = shuffled[1], shuffled[0]
        
    return shuffled[offset]

def e(key: str) -> str:
    """Повертає HTML-тег <tg-emoji> для кастомного емодзі з fallback-значенням."""
    item = CUSTOM_EMOJIS.get(key)
    if not item:
        return ""
    cid, fb = item
    return f'<tg-emoji emoji-id="{cid}">{fb}</tg-emoji>'

def _format_kyiv_time(iso_str: str = None) -> str:
    tz = pytz.timezone("Europe/Kiev")
    if iso_str:
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.astimezone(tz).strftime("%H:%M (%d.%m)")
        except Exception:
            pass
    return datetime.now(tz).strftime("%H:%M")

def _format_alert_time_and_duration(iso_str: str = None) -> str:
    tz = pytz.timezone("Europe/Kiev")
    now_dt = datetime.now(tz)
    if not iso_str:
        return now_dt.strftime("%H:%M")

    try:
        start_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(tz)
        diff_sec = max(0, int((now_dt - start_dt).total_seconds()))
        hours = diff_sec // 3600
        minutes = (diff_sec % 3600) // 60

        if hours > 0:
            dur_str = f"{hours} год {minutes} хв"
        elif minutes > 0:
            dur_str = f"{minutes} хв"
        else:
            dur_str = "&lt; 1 хв"

        start_time_str = start_dt.strftime("%H:%M")
        return f"{dur_str} (з {start_time_str})"
    except Exception:
        return now_dt.strftime("%H:%M")

def make_threat_signature(state: dict) -> tuple:
    """
    Повертає кортеж-сигнатуру поточної загрози для відстеження важливих змін у фоновому моніторингу:
    рівень, список причин та суттєвий склад цілей (id, тип, напрямок до користувача).
    """
    level = state.get("level", "green")
    reasons = tuple(sorted(state.get("reasons", [])))
    threats = tuple(
        (
            t.get("id"),
            t.get("type"),
            t.get("is_towards_user", False)
        )
        for t in state.get("nearby_threats", [])
    )
    return (level, reasons, threats)

def _get_threat_emoji(threat: dict) -> str:
    """Визначає найбільш точну іконку з паку для типу загрози."""
    t_type = (threat.get("type") or "").lower()
    expl = (threat.get("explanation") or "").lower()

    if any(w in expl for w in ["міг-31", "міг31", "миг-31"]):
        return e("plane_mig31")
    if any(w in expl for w in ["ту-95", "ту-22", "ту-160", "стратег"]):
        return e("plane_bomber")
    if any(w in expl for w in ["каб", "фаб", "бомб"]):
        return e("bomb_kab")
    if any(w in expl for w in ["кинжал", "кинджал"]):
        return e("missile_kinzhal")
    if t_type in ("ballistic", "kinzhal", "mig31k") or any(w in expl for w in ["баліст", "іскандер-м", "іскандер"]):
        return e("missile_ballistic")
    if t_type in ("shahed", "drone", "uav") or any(w in expl for w in ["шахед", "shahed", "мопед", "дрон", "бпла"]):
        if any(w in expl for w in ["розвід", "supercam", "zala", "орлан"]):
            return e("drone_recon")
        return e("shahed")
    if t_type in ("missile", "cruise") or any(w in expl for w in ["ракет", "калібр", "х-101", "х-59", "х-69"]):
        return e("missile_cruise")
    if t_type in ("aviation", "plane") or any(w in expl for w in ["су-34", "су-35", "авіац", "борт"]):
        return e("plane_su34")
    if threat.get("advisory") or "ппо" in expl:
        return e("air_defense_gepard")

def _get_threat_status_badge(threat: dict) -> tuple:
    """
    Визначає статус цілі (збито, падає/знижується, локаційно втрачено, вибух, робота ППО)
    та повертає кортеж (badge_html, override_icon).
    """
    expl = (threat.get("explanation") or "").lower()
    t_status = (threat.get("status") or "").lower()

    # 1. Збиття / перехоплення / ліквідація
    if any(w in expl for w in ["збит", "знищен", "перехоплен", "ліквідован"]) or t_status in ("down", "destroyed", "intercepted"):
        return ("🎯 <b>[ЗБИТО]</b> ", e("burst"))

    # 2. Падіння / зниження висоти
    if any(w in expl for w in ["падає", "падают", "падінн", "знижуєт", "втрачає висоту", "низька висота"]):
        return ("⚠️ ⬇️ <b>[ПАДАЄ / ЗНИЖУЄТЬСЯ]</b> ", e("lightning"))

    # 3. Локаційно втрачено / припинив існування / РЕБ
    if any(w in expl for w in ["локаційно втрачен", "втрачено з радар", "припинив існуван", "зник з радар"]):
        return ("💨 <b>[ЛОКАЦІЙНО ВТРАЧЕНО]</b> ", e("target_unknown"))

    # 4. Вибух / детонація
    if any(w in expl for w in ["детонац", "вибух"]):
        return ("💥 <b>[ВИБУХ]</b> ", e("burst"))

    # 5. Робота ППО
    if threat.get("advisory") or any(w in expl for w in ["працює ппо", "робота ппо", "вогонь ппо"]):
        return ("⚡ <b>[РОБОТА ППО]</b> ", e("air_defense_gepard"))

    return ("", "")

def _get_reason_emoji(reasons_str: str, level: str) -> str:
    """Підбирає іконку для рядка 'Причина'."""
    r_lower = reasons_str.lower()
    if any(w in r_lower for w in ["міг-31", "міг31"]):
        return e("plane_mig31")
    if any(w in r_lower for w in ["баліст", "іскандер"]):
        return e("missile_ballistic")
    if any(w in r_lower for w in ["кинжал", "кинджал"]):
        return e("missile_kinzhal")
    if any(w in r_lower for w in ["каб", "фаб", "бомб"]):
        return e("bomb_kab")
    if any(w in r_lower for w in ["шахед", "shahed", "бпла", "дрон"]):
        return e("shahed")
    if any(w in r_lower for w in ["ракет"]):
        return e("missile_cruise")
    if any(w in r_lower for w in ["авіац"]):
        return e("plane_su34")
    
    return e("alert_red") if level == "red" else e("alert_yellow")

def format_alert_message(sub: dict, state: dict, is_manual_check: bool = False, prev_level: str = None) -> str:
    """
    Формує повідомлення для користувача в HTML-форматі з кольоровим рівнем,
    причиною, векторами цілей та кастомними емодзі Telegram.
    """
    oblast = sub.get("oblast") or "Невідома область"
    raion = sub.get("raion") or ""
    locality = sub.get("locality") or ""

    loc_parts = [p for p in [locality, raion, oblast] if p]
    unique_loc = []
    for p in loc_parts:
        p_clean = p.replace(" область", " обл.").replace(" район", " р-н")
        if p_clean not in unique_loc:
            unique_loc.append(p_clean)
    loc_display = html.escape(", ".join(unique_loc))

    level = state.get("level", "green")
    reasons = state.get("reasons", [])
    threats = state.get("nearby_threats", [])
    duration_str = _format_alert_time_and_duration(state.get("since"))

    cleaned_reasons = []
    for r in reasons:
        cr = _clean_reason(r)
        if cr and cr not in cleaned_reasons:
            cleaned_reasons.append(cr)

    raw_reasons_str = ", ".join(cleaned_reasons) if cleaned_reasons else (
        "Ракетна або дронова загроза" if level == "red" else "Підвищена небезпека"
    )
    reasons_display = html.escape(raw_reasons_str)
    reason_icon = _get_reason_emoji(raw_reasons_str, level)

    lines = []

    # 1. Заголовок рівня загрози
    if level == "red":
        if prev_level == "yellow" and not is_manual_check:
            lines.append(f"{e('lightning')} {e('alert_red')} <b>УВАГА! РІВЕНЬ ЗАГРОЗИ ПІДВИЩЕНО ДО ЧЕРВОНОГО!</b>")
        else:
            lines.append(f"{e('alert_red')} <b>ПОВІТРЯНА ТРИВОГА! (Червоний рівень)</b>")
        lines.append(f"{e('pin')} <b>Локація:</b> {loc_display}")
        lines.append(f"{reason_icon} <b>Причина:</b> {reasons_display}")
        lines.append(f"🕒 <b>Тривога триває:</b> {duration_str}")
    elif level == "yellow":
        if prev_level == "red" and not is_manual_check:
            lines.append(f"{e('alert_yellow')} <b>Рівень загрози знижено до ЖОВТОГО (Підвищена увага)</b>")
        else:
            lines.append(f"{e('alert_yellow')} <b>ПІДВИЩЕНА НЕБЕЗПЕКА! (Жовтий рівень)</b>")
        lines.append(f"{e('pin')} <b>Локація:</b> {loc_display}")
        lines.append(f"{reason_icon} <b>Причина:</b> {reasons_display}")
        lines.append(f"🕒 <b>Загроза триває:</b> {duration_str}")
    else:
        if is_manual_check:
            lines.append(f"{e('shield_green')} <b>Наразі у вашому районі СПОКІЙНО (Тривоги немає)</b>")
            lines.append(f"{e('pin')} <b>Локація:</b> {loc_display}")
            lines.append(f"{e('radar')} <b>Радар:</b> Прямих загроз чи цілей у радіусі спостереження не виявлено.")
        else:
            lines.append(f"{e('shield_green')} <b>ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ!</b>")
            lines.append(f"{e('pin')} <b>Локація:</b> {loc_display}")
            lines.append(f"🕒 <b>Час відбою:</b> {_format_kyiv_time()}")
            lines.append(f"\n{e('trident')} {e('flag_ua')} <i>Небезпека минула. Дякуємо силам ППО!</i>")

    # 2. Блок цілей на підльоті
    if threats:
        lines.append(f"\n{e('radar')} <b>Зафіксовані цілі поруч / в області:</b>")
        for t in threats:
            dist_str = f" <i>(~{t['distance_km']} км)</i>" if t.get("distance_km") is not None else ""
            towards_tag = " ⚠️ <b>у ваш бік!</b>" if t.get("is_towards_user") else ""

            rel_arrow = t.get("relative_dir_arrow") or ""
            rel_name = t.get("relative_dir_name") or ""
            dir_arrow = t.get("direction_arrow") or ""
            dir_name = t.get("direction_name") or ""

            dir_str = ""
            if rel_name and dir_name:
                dir_str = f"  ↳ {rel_arrow} {rel_name} (курс {dir_arrow} {dir_name})"
            elif rel_name:
                dir_str = f"  ↳ {rel_arrow} {rel_name}"
            elif dir_name:
                dir_str = f"  ↳ курс {dir_arrow} {dir_name}"

            clean_expl = html.escape(t.get("explanation") or "Повітряна ціль")
            badge, override_icon = _get_threat_status_badge(t)
            t_icon = override_icon or _get_threat_emoji(t)

            lines.append(f"\n• {t_icon} {badge}<b>{clean_expl}</b>{dist_str}{towards_tag}")
            if dir_str:
                lines.append(f"  {html.escape(dir_str)}")
    elif level in ("red", "yellow"):
        if sub and sub.get("lat") is not None and sub.get("lon") is not None:
            lines.append(f"\n{e('radar')} <b>Радар:</b> <i>Прямих цілей у вашому радіусі (~80 км) наразі не виявлено (триває дорозвідка або загроза на підльоті).</i>")
        else:
            lines.append(f"\n{e('radar')} <b>Радар:</b> <i>Прямих цілей у вашій області наразі не виявлено (триває дорозвідка або загроза на підльоті).</i>")

    # 3. Попередження щодо безпеки та ППО
    has_ballistics = any(
        (t.get("type") in ("ballistic", "mig31k", "kinzhal")) or
        any(w in (t.get("explanation") or "").lower() for w in ["баліст", "кинжал", "кинджал", "саваслейка"])
        for t in threats
    ) or any(w in raw_reasons_str.lower() for w in ["баліст", "кинжал", "кинджал"])

    if level == "red":
        if has_ballistics:
            lines.append(
                f"\n{e('air_defense_patriot')} {e('burst')} <b>УВАГА: Загроза балістики/гіперзвуку! Можлива активна робота ППО!</b> "
                "Перебувайте в укриттях через критично високу швидкість цілей та загрозу падіння уламків! 🙏"
            )
        else:
            lines.append(
                f"\n{e('air_defense_gepard')} <b>УВАГА: Можлива робота ППО!</b> "
                "Будь ласка, перебувайте в укриттях або дотримуйтесь правила двох стін через загрозу вибухової хвилі та падіння уламків! 🙏"
            )
    elif level == "yellow":
        lines.append("\n⚠️ <i>Будь ласка, будьте уважні та стежте за оновленнями ситуації!</i>")

    putin_enabled = sub.get("putin_check_enabled", True) if sub else True
    if putin_enabled:
        kyiv_now = datetime.now(pytz.timezone("Europe/Kiev"))
        kyiv_today = kyiv_now.strftime("%d.%m.%Y")
        putin_phrase = get_daily_putin_phrase(kyiv_now)
        lines.append(f"\n☠️ <i>{kyiv_today}. Чи здох путін? — {putin_phrase}</i>")

    now_tz = datetime.now(pytz.timezone("Europe/Kiev")).strftime("%H:%M:%S")
    lines.append(f"\n⏱ <b>Оновлено:</b> {now_tz} | <a href=\"https://neptun.in.ua/\">NEPTUN Map</a>")
    return "\n".join(lines)
