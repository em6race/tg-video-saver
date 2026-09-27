from datetime import datetime
import pytz

from .evaluator import _clean_reason, _escape_markdown

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
            dur_str = "< 1 хв"

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

def format_alert_message(sub: dict, state: dict, is_manual_check: bool = False, prev_level: str = None) -> str:
    """
    Формує повідомлення для користувача з кольоровим рівнем, причиною, векторами цілей та загрозою.
    """
    oblast = sub.get("oblast") or "Невідома область"
    raion = sub.get("raion") or ""
    locality = sub.get("locality") or ""

    loc_parts = [p for p in [locality, raion, oblast] if p]
    unique_loc = []
    for p in loc_parts:
        if p not in unique_loc:
            unique_loc.append(p)
    loc_display = ", ".join(unique_loc)

    level = state["level"]
    reasons = state["reasons"]
    threats = state["nearby_threats"]
    duration_str = _format_alert_time_and_duration(state.get("since"))

    cleaned_reasons = []
    for r in reasons:
        cr = _clean_reason(r)
        if cr and cr not in cleaned_reasons:
            cleaned_reasons.append(cr)

    reasons_str = ", ".join(cleaned_reasons) if cleaned_reasons else ("Ракетна або дронова загроза" if level == "red" else "Підвищена небезпека")

    lines = []

    if level == "red":
        if prev_level == "yellow" and not is_manual_check:
            lines.append("🔴 **УВАГА! РІВЕНЬ ЗАГРОЗИ ПІДВИЩЕНО ДО ЧЕРВОНОГО!** 🚨")
        else:
            lines.append("🔴 **ПОВІТРЯНА ТРИВОГА! (Червоний рівень)** 🚨")
        lines.append(f"📍 **Локація:** {loc_display}")
        lines.append(f"❓ **Чому тривога:** {reasons_str}")
        lines.append(f"🕒 **Тривога триває:** {duration_str}")
    elif level == "yellow":
        if prev_level == "red" and not is_manual_check:
            lines.append("🟡 **Рівень загрози знижено до ЖОВТОГО (Підвищена увага)** ⚠️")
        else:
            lines.append("🟡 **ПІДВИЩЕНА НЕБЕЗПЕКА! (Жовтий рівень)** ⚠️")
        lines.append(f"📍 **Локація:** {loc_display}")
        lines.append(f"❓ **Причина:** {reasons_str}")
        lines.append(f"🕒 **Загроза триває:** {duration_str}")
    else:
        if is_manual_check:
            lines.append("🟢 **Наразі у вашому районі СПОКІЙНО (Тривоги немає)** ✅")
            lines.append(f"📍 **Локація:** {loc_display}")
        else:
            lines.append("🟢 **ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ!** ✅")
            lines.append(f"📍 **Локація:** {loc_display}")
            lines.append(f"🕒 **Час відбою:** {_format_kyiv_time()}")

    if threats:
        lines.append("\n🛸 **Зафіксовані цілі поруч / в області:**")
        towards_count = 0
        for t in threats:
            dist_str = f" *(~{t['distance_km']} км від вас)*" if t.get("distance_km") is not None else ""

            relative_info = ""
            if t.get("relative_dir_name") and t.get("relative_dir_arrow"):
                relative_info = f"від вас: {t['relative_dir_arrow']} {t['relative_dir_name']}"

            heading_info = ""
            if t.get("direction_name") and t.get("direction_arrow"):
                heading_info = f"курс {t['direction_arrow']} {t['direction_name']}"

            conf_info = ""
            if t.get("confidence"):
                conf_info = f"достовірність: {t['confidence']}"

            if t.get("is_towards_user"):
                towards_count += 1

            clean_expl = _escape_markdown(t['explanation'])
            if t.get("advisory") or "ппо" in clean_expl.lower():
                lines.append(f"\n🛡️ **{clean_expl}**{dist_str}")
            else:
                lines.append(f"\n• **{clean_expl}**{dist_str}")
            details = [d for d in [relative_info, heading_info, conf_info] if d]
            if details:
                lines.append(f"  ↳ {' | '.join(details)}")

        if towards_count > 0:
            count_str = "1 цілі" if towards_count == 1 else f"{towards_count} цілей"
            lines.append(f"\n⚠️ **МОЖЛИВА НЕБЕЗПЕКА: Вектор руху {count_str} спрямований у ваш бік!**")

    has_ballistics = any(
        (t.get("type") in ("ballistic", "mig31k", "kinzhal")) or
        any(w in (t.get("explanation") or "").lower() for w in ["баліст", "кинжал", "кинджал"])
        for t in threats
    ) or any(w in reasons_str.lower() for w in ["баліст", "кинжал", "кинджал"])

    if has_ballistics and level == "red":
        lines.append("\n💥 🛡️ **УВАГА: При загрозі балістики/гіперзвуку можлива активна робота ППО!** Перебувайте в укриттях через високу швидкість та загрозу падіння уламків!")
    elif level == "red":
        lines.append("\n🙏 *Будь ласка, перебувайте в укритті або дотримуйтесь правила двох стін!*")

    now_tz = datetime.now(pytz.timezone("Europe/Kiev")).strftime("%H:%M:%S")
    lines.append(f"\n⏱ **Оновлено:** {now_tz}")
    lines.append("🗺 [Дані: Карта повітряних тривог — NEPTUN](https://neptun.in.ua/)")
    return "\n".join(lines)
