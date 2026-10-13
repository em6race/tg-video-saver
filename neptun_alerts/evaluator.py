import re
import logging
from typing import Optional

from .models import ThreatItem, AlertStateData
from .districts_map import get_oblast_for_district
from .geocoding import (
    haversine_km,
    calculate_bearing,
    _heading_to_direction,
    _normalize_apostrophe,
)

logger = logging.getLogger(__name__)

def _clean_threat_explanation(text: str) -> str:
    if not text:
        return "БпЛА"
    cleaned = re.sub(r'\.?\s*Підтверджень:\s*\d+\.?', '', text, flags=re.IGNORECASE).strip()
    cleaned = cleaned.rstrip(".")
    return cleaned if cleaned else "БпЛА"

def _clean_reason(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r'\s*\((?:жовтий|червоний|зелений)\s+рівень\)', '', text, flags=re.IGNORECASE)
    return cleaned.strip()

def _escape_markdown(text: str) -> str:
    if not text:
        return ""
    return str(text).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`").replace("[", "\\[")

def _format_confidence(conf_raw: str) -> Optional[str]:
    if not conf_raw:
        return None
    c = str(conf_raw).lower().strip()
    if c in ("high", "висока"):
        return "Висока"
    elif c in ("medium", "середня"):
        return "Середня"
    elif c in ("low", "низька"):
        return "Низька"
    elif c in ("unconfirmed", "непідтверджена"):
        return "Непідтверджена"
    return str(conf_raw).capitalize()

def _match_raion(user_raion: str, api_raion_name: str, api_raion_key: str = "") -> bool:
    if not user_raion:
        return False
    u_norm = _normalize_apostrophe(user_raion).lower().replace(" район", "").replace(" р-н", "").strip()
    n_norm = _normalize_apostrophe(api_raion_name).lower().replace(" район", "").replace(" р-н", "").strip()
    k_norm = _normalize_apostrophe(api_raion_key).lower().strip() if api_raion_key else ""
    return u_norm == n_norm or (k_norm and u_norm == k_norm) or (u_norm and (u_norm in n_norm or n_norm in u_norm))

def _match_oblast(user_oblast: str, api_oblast: str) -> bool:
    if not user_oblast or not api_oblast:
        return False
    u_norm = _normalize_apostrophe(user_oblast).lower().replace("область", "").replace("м.", "").replace("місто", "").strip()
    a_norm = _normalize_apostrophe(api_oblast).lower().replace("область", "").replace("м.", "").replace("місто", "").strip()
    return u_norm == a_norm or (u_norm and (u_norm in a_norm or a_norm in u_norm))

def _match_community(user_text: str, api_comm_name: str) -> bool:
    if not user_text or not api_comm_name:
        return False
    u = _normalize_apostrophe(user_text).lower().replace("територіальна громада", "").replace("громада", "").replace(" район", "").replace(" р-н", "").replace("м.", "").replace("місто", "").strip()
    c = _normalize_apostrophe(api_comm_name).lower().replace("територіальна громада", "").replace("громада", "").replace(" район", "").replace(" р-н", "").replace("м.", "").replace("місто", "").strip()
    if not u or not c:
        return False
    if u == c or u in c or c in u:
        return True
    u_stem = u[:max(4, len(u)-2)]
    c_stem = c[:max(4, len(c)-2)]
    return u_stem in c or c_stem in u

def evaluate_user_alert_state(sub: dict, alerts_data: dict, threats_data: dict, ukraine_alarm_data: Optional[list] = None) -> dict:
    """
    Аналізує офіційні дані UkraineAlarm та радар NEPTUN для конкретного користувача:
    - Чи є тривога (active: bool)
    - Рівень: 'red', 'yellow' або 'green'
    - Причини (reasons: list[str])
    - Повітряні цілі поруч або в області (nearby_threats: list[dict])
    """
    user_oblast = sub.get("oblast") or ""
    user_raion = sub.get("raion") or ""
    user_locality = sub.get("locality") or ""
    user_lat = sub.get("lat")
    user_lon = sub.get("lon")

    matched_exact_raions = []
    matched_oblast_wide = []
    matched_other_raions_in_oblast = []

    if ukraine_alarm_data:
        for item in ukraine_alarm_data:
            r_type = item.get("regionType")
            r_name = item.get("regionName", "")
            active_alerts = item.get("activeAlerts", [])
            if not active_alerts:
                continue

            item_level = "yellow"
            item_reasons = []
            item_alert_levels = []
            item_alert_type = "AIR"
            item_since = item.get("lastUpdate")

            for alert in active_alerts:
                a_type = (alert.get("type") or "AIR").upper()
                if a_type in ("ARTILLERY", "URBAN_FIGHTS", "CHEMICAL", "NUCLEAR"):
                    item_alert_type = a_type
                for lvl in alert.get("activeAlertLevels", []):
                    item_alert_levels.append(lvl)
                    lvl_val = (lvl.get("alertLevel") or "").lower()
                    if lvl_val == "red":
                        item_level = "red"
                    r_text = _clean_reason(lvl.get("reason", ""))
                    if r_text and r_text not in item_reasons:
                        item_reasons.append(r_text)
                    if not item_since and lvl.get("createdAt"):
                        item_since = lvl.get("createdAt")

            alert_entry = {
                "name": r_name,
                "type": r_type,
                "level": item_level,
                "alert_type": item_alert_type,
                "alert_levels": item_alert_levels,
                "reasons": item_reasons,
                "since": item_since,
            }

            if r_type == "State":
                if _match_oblast(user_oblast, r_name):
                    matched_oblast_wide.append(alert_entry)
            elif r_type == "District":
                if user_raion and _match_raion(user_raion, r_name, ""):
                    matched_exact_raions.append(alert_entry)
                else:
                    dist_ob = get_oblast_for_district(r_name)
                    if dist_ob and _match_oblast(user_oblast, dist_ob):
                        matched_other_raions_in_oblast.append(alert_entry)
            elif r_type == "Community":
                is_exact_comm = False
                if user_locality and _match_community(user_locality, r_name):
                    is_exact_comm = True
                elif user_raion and _match_community(user_raion, r_name):
                    is_exact_comm = True

                if is_exact_comm:
                    matched_exact_raions.append(alert_entry)

    else:
        # Резервний аналіз за даними NEPTUN alerts_data
        raions_list = alerts_data.get("raions", []) if alerts_data else []
        oblasts_list = alerts_data.get("oblasts", []) if alerts_data else []

        for ob in oblasts_list:
            ob_name = ob.get("name", "") or ob.get("oblast", "") or ob.get("key", "")
            if _match_oblast(user_oblast, ob_name):
                matched_oblast_wide.append(ob)

        is_kyiv_city = (user_oblast == "м. Київ" or "київ" in _normalize_apostrophe(user_oblast).lower())

        for r in raions_list:
            r_ob = r.get("oblast", "")
            r_name = r.get("name", "")
            r_key = r.get("key", "")

            if is_kyiv_city:
                if "київ" in r_key.lower() or "київ" in r_name.lower() or _match_oblast("м. Київ", r_ob):
                    matched_exact_raions.append(r)
                continue

            if _match_oblast(user_oblast, r_ob):
                if user_raion and _match_raion(user_raion, r_name, r_key):
                    matched_exact_raions.append(r)
                else:
                    matched_other_raions_in_oblast.append(r)

    level = "green"
    reasons = []
    since_time = None
    matched_names = []
    alert_type = "AIR"
    alert_levels = []
    official_reasons = []

    # А. Пріоритет: точний збіг по району або пряма тривога по області
    primary_alerts = matched_exact_raions + matched_oblast_wide
    for alert in primary_alerts:
        a_level = alert.get("level", "red")
        if a_level == "red":
            level = "red"
        elif a_level == "yellow" and level != "red":
            level = "yellow"

        a_type = alert.get("alert_type")
        if a_type in ("ARTILLERY", "URBAN_FIGHTS", "CHEMICAL", "NUCLEAR"):
            alert_type = a_type

        for lvl in alert.get("alert_levels", []):
            if lvl not in alert_levels:
                alert_levels.append(lvl)

        for reason in alert.get("reasons", []):
            if reason not in reasons:
                reasons.append(reason)
            if reason not in official_reasons:
                official_reasons.append(reason)

        if not since_time and alert.get("since"):
            since_time = alert.get("since")

        r_name = alert.get("name")
        if r_name and r_name not in matched_names:
            matched_names.append(r_name)

    # Б. Інші райони області:
    # Якщо користувач підписаний на ВСЮ область (без конкретного району) — реагуємо на тривоги в будь-якому районі.
    # Якщо ж користувач обрав КОНКРЕТНИЙ район (наприклад, Коломийський) — не турбуємо його тривогами з чужих районів,
    # окрім випадків, коли радар бачить загрозу поруч або коли оголошено тривогу по всій області.
    if not user_raion and matched_other_raions_in_oblast:
        for r in matched_other_raions_in_oblast:
            a_type = r.get("alert_type")
            if a_type in ("ARTILLERY", "URBAN_FIGHTS", "CHEMICAL", "NUCLEAR"):
                alert_type = a_type
            for lvl in r.get("alert_levels", []):
                if lvl not in alert_levels:
                    alert_levels.append(lvl)
            for reas in r.get("reasons", []):
                if reas not in official_reasons:
                    official_reasons.append(reas)
        other_red = [r for r in matched_other_raions_in_oblast if r.get("level") == "red"]
        other_yellow = [r for r in matched_other_raions_in_oblast if r.get("level") == "yellow"]

        if other_red:
            level = "red"
            other_names = [r.get("name", "").replace(" район", " р-н") for r in other_red if r.get("name")]
            unique_other_reasons = []
            for r in other_red:
                for reas in r.get("reasons", []):
                    cleaned_r = _clean_reason(reas)
                    if cleaned_r and cleaned_r not in unique_other_reasons:
                        unique_other_reasons.append(cleaned_r)

            reasons_summary = ", ".join(unique_other_reasons) if unique_other_reasons else "Ракетна або дронова загроза"
            if len(other_names) <= 2:
                formatted_reason = f"Тривога в області ({', '.join(other_names)}: {reasons_summary})"
            else:
                formatted_reason = f"Тривога в області ({len(other_names)} р-н: {reasons_summary})"

            if formatted_reason not in reasons:
                reasons.append(formatted_reason)

            for r in other_red:
                if not since_time and r.get("since"):
                    since_time = r.get("since")
                if r.get("name") and r.get("name") not in matched_names:
                    matched_names.append(r.get("name"))

        elif other_yellow and level != "red":
            level = "yellow"
            other_names = [r.get("name", "").replace(" район", " р-н") for r in other_yellow if r.get("name")]
            unique_other_reasons = []
            for r in other_yellow:
                for reas in r.get("reasons", []):
                    cleaned_r = _clean_reason(reas)
                    if cleaned_r and cleaned_r not in unique_other_reasons:
                        unique_other_reasons.append(cleaned_r)

            reasons_summary = ", ".join(unique_other_reasons) if unique_other_reasons else "Дронова небезпека"
            if len(other_names) <= 2:
                formatted_reason = f"Підвищена небезпека в області ({', '.join(other_names)}: {reasons_summary})"
            else:
                formatted_reason = f"Підвищена небезпека в області ({len(other_names)} р-н: {reasons_summary})"

            if formatted_reason not in reasons:
                reasons.append(formatted_reason)

            for r in other_yellow:
                if not since_time and r.get("since"):
                    since_time = r.get("since")
                if r.get("name") and r.get("name") not in matched_names:
                    matched_names.append(r.get("name"))

    nearby_threats = []
    for t in threats_data.get("threats", []):
        if t.get("status") != "active":
            continue

        t_region = t.get("region", "")
        t_lat = t.get("lat")
        t_lon = t.get("lon")
        area_only = bool(t.get("areaOnly", False))

        t_type = (t.get("type") or "").lower().strip()
        raw_expl_orig = t.get("explanationShort") or t.get("title", "")
        raw_expl_lower = raw_expl_orig.lower()

        fast_types = {
            "missile", "ballistic", "kab", "mig31k", "jet_uav", "kinzhal",
            "hypersonic", "cruise_missile", "ballistic_missile", "guided_bomb",
            "aviation", "tactical_aviation", "artillery", "mlrs"
        }
        fast_keywords = [
            "ракета", "баліст", "кинжал", "кинджал", "каб", "фаб", "авіаційн",
            "реактивн", "іскандер", "калібр", "х-101", "х-47", "х-59", "град", "рсзв"
        ]

        is_fast_threat = t_type in fast_types or any(w in raw_expl_lower for w in fast_keywords)

        towards_dist_limit = 80 if is_fast_threat else 30
        close_dist_limit = 150 if is_fast_threat else 80

        dist_km = None
        if user_lat is not None and user_lon is not None and t_lat is not None and t_lon is not None and not area_only:
            dist_km = round(haversine_km(user_lat, user_lon, t_lat, t_lon))

        heading_raw = t.get("heading")
        heading_deg = None
        direction_name = None
        direction_arrow = None
        is_towards_user = False
        relative_dir_name = None
        relative_dir_arrow = None

        if user_lat is not None and user_lon is not None and t_lat is not None and t_lon is not None:
            try:
                bearing_from_user = calculate_bearing(user_lat, user_lon, t_lat, t_lon)
                relative_dir_name, relative_dir_arrow = _heading_to_direction(bearing_from_user)
            except Exception:
                pass

        if heading_raw is not None:
            try:
                heading_deg = round(float(heading_raw)) % 360
                direction_name, direction_arrow = _heading_to_direction(heading_deg)

                if user_lat is not None and user_lon is not None and t_lat is not None and t_lon is not None:
                    bearing_to_user = calculate_bearing(t_lat, t_lon, user_lat, user_lon)
                    angle_diff = abs((heading_deg - bearing_to_user + 180.0) % 360.0 - 180.0)
                    if angle_diff <= 45.0 and (dist_km is not None and dist_km <= towards_dist_limit):
                        is_towards_user = True
            except Exception as ge_err:
                logger.debug(f"Heading calc error: {ge_err}")

        in_same_oblast = _match_oblast(user_oblast, t_region)
        is_close = dist_km is not None and dist_km <= close_dist_limit

        if in_same_oblast or is_close:
            raw_expl = t.get("explanationShort") or t.get("title", "Зафіксовано ціль")
            cleaned_expl = _clean_threat_explanation(raw_expl)
            
            locality = t.get("locality")
            region = t.get("region")
            district = t.get("district")

            if locality and locality.lower() not in cleaned_expl.lower():
                loc_str = locality
                if district and district.lower() not in cleaned_expl.lower():
                    loc_str += f" ({district})"
                if region and region.lower() not in cleaned_expl.lower():
                    loc_str += f", {region}"
                cleaned_expl = f"{cleaned_expl} — {loc_str}"

            conf_raw = t.get("displayConfidence") or t.get("confidenceLevel") or t.get("confidence")
            conf_display = _format_confidence(conf_raw)

            threat_item = ThreatItem(
                id=t.get("id"),
                type=t.get("type", "unknown"),
                title=t.get("title", "Повітряна ціль"),
                explanation=cleaned_expl,
                distance_km=dist_km,
                advisory=bool(t.get("advisory", False)),
                heading=heading_deg,
                direction_name=direction_name,
                direction_arrow=direction_arrow,
                relative_dir_name=relative_dir_name,
                relative_dir_arrow=relative_dir_arrow,
                is_towards_user=is_towards_user,
                confidence=conf_display
            )
            nearby_threats.append(threat_item)

    nearby_threats.sort(key=lambda x: (not x.is_towards_user, x.distance_km if x.distance_km is not None else 9999))

    # Г. Автопідвищення рівня при виявленні небезпечних цілей поруч або курсом на користувача
    if level == "green" and nearby_threats:
        towards_threats = [t for t in nearby_threats if t.is_towards_user]
        close_threats = [t for t in nearby_threats if t.distance_km is not None and t.distance_km <= 50]

        if towards_threats:
            top_t = towards_threats[0]
            dist_str = f" (~{top_t.distance_km} км)" if top_t.distance_km is not None else ""
            if top_t.type in ("ballistic", "kinzhal", "mig31k", "missile", "jet_uav"):
                level = "red"
                reasons.append(f"Швидкісна ціль курсом у ваш бік!{dist_str}")
            else:
                level = "yellow"
                reasons.append(f"Ціль рухається у ваш бік!{dist_str}")
        elif close_threats:
            top_t = close_threats[0]
            dist_str = f" (~{top_t.distance_km} км)" if top_t.distance_km is not None else ""
            level = "yellow"
            reasons.append(f"Повітряна ціль поруч{dist_str}")

    # Д. Перевірка на дорозвідку
    is_dorozvidka = False
    for r in reasons:
        if "дорозвід" in str(r).lower():
            is_dorozvidka = True
            break

    if not is_dorozvidka:
        all_matched = primary_alerts + matched_other_raions_in_oblast
        for a in all_matched:
            for r in a.get("reasons", []):
                if "дорозвід" in str(r).lower():
                    is_dorozvidka = True
                    break
            if is_dorozvidka:
                break

    if not is_dorozvidka:
        for t in threats_data.get("threats", []):
            if t.get("status") != "active":
                continue
            t_region = t.get("region", "")
            t_lat = t.get("lat")
            t_lon = t.get("lon")
            in_same_oblast = _match_oblast(user_oblast, t_region)
            dist_km = None
            if user_lat is not None and user_lon is not None and t_lat is not None and t_lon is not None:
                dist_km = round(haversine_km(user_lat, user_lon, t_lat, t_lon))
            if in_same_oblast or (dist_km is not None and dist_km <= 150):
                t_text = f"{t.get('explanationShort', '')} {t.get('title', '')}".lower()
                if "дорозвід" in t_text:
                    is_dorozvidka = True
                    break

    if level != "green" and not reasons:
        if level == "red":
            reasons.append("Ракетна або дронова загроза")
        else:
            reasons.append("Підвищена небезпека")

    state_obj = AlertStateData(
        active=level in ("red", "yellow"),
        level=level,
        reasons=reasons,
        since=since_time,
        matched_names=matched_names,
        nearby_threats=nearby_threats[:5],
        is_dorozvidka=is_dorozvidka,
        alert_type=alert_type,
        alert_levels=alert_levels,
        official_reasons=official_reasons
    )
    return state_obj.to_dict()
