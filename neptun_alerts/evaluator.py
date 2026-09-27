import re
import logging
from typing import Optional

from .models import ThreatItem, AlertStateData
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

def _match_raion(user_raion: str, api_raion_name: str, api_raion_key: str) -> bool:
    if not user_raion:
        return False
    u_norm = _normalize_apostrophe(user_raion).lower().replace(" район", "").strip()
    n_norm = _normalize_apostrophe(api_raion_name).lower().replace(" район", "").strip()
    k_norm = _normalize_apostrophe(api_raion_key).lower().strip()
    return u_norm == n_norm or u_norm == k_norm or (u_norm and (u_norm in n_norm or n_norm in u_norm))

def _match_oblast(user_oblast: str, api_oblast: str) -> bool:
    if not user_oblast or not api_oblast:
        return False
    u_norm = _normalize_apostrophe(user_oblast).lower().replace("область", "").replace("м.", "").replace("місто", "").strip()
    a_norm = _normalize_apostrophe(api_oblast).lower().replace("область", "").replace("м.", "").replace("місто", "").strip()
    return u_norm == a_norm or (u_norm and (u_norm in a_norm or a_norm in u_norm))

def evaluate_user_alert_state(sub: dict, alerts_data: dict, threats_data: dict) -> dict:
    """
    Аналізує дані NEPTUN для конкретного користувача та визначає:
    - Чи є тривога (active: bool)
    - Рівень: 'red', 'yellow' або 'green'
    - Причини (reasons: list[str])
    - Повітряні цілі поруч або в області (nearby_threats: list[dict])
    """
    user_oblast = sub.get("oblast") or ""
    user_raion = sub.get("raion") or ""
    user_lat = sub.get("lat")
    user_lon = sub.get("lon")

    raions_list = alerts_data.get("raions", [])
    oblasts_list = alerts_data.get("oblasts", [])

    matched_alerts = []

    for ob in oblasts_list:
        ob_name = ob.get("name", "") or ob.get("oblast", "")
        if _match_oblast(user_oblast, ob_name):
            matched_alerts.append(ob)

    if user_raion and user_oblast != "м. Київ":
        for r in raions_list:
            if _match_oblast(user_oblast, r.get("oblast", "")) and _match_raion(user_raion, r.get("name", ""), r.get("key", "")):
                matched_alerts.append(r)
    else:
        for r in raions_list:
            if _match_oblast(user_oblast, r.get("oblast", "")) or (user_oblast == "м. Київ" and "київ" in r.get("key", "").lower()):
                matched_alerts.append(r)

    level = "green"
    reasons = []
    since_time = None
    matched_names = []

    for alert in matched_alerts:
        a_level = alert.get("level", "red")
        if a_level == "red":
            level = "red"
        elif a_level == "yellow" and level != "red":
            level = "yellow"

        for reason in alert.get("reasons", []):
            if reason not in reasons:
                reasons.append(reason)

        if not since_time and alert.get("since"):
            since_time = alert.get("since")

        r_name = alert.get("name")
        if r_name and r_name not in matched_names:
            matched_names.append(r_name)

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

        is_fast_threat = t_type in ("missile", "ballistic", "kab", "mig31k", "jet_uav") or any(
            w in raw_expl_lower for w in ["ракета", "баліст", "кинжал", "кинджал", "каб", "авіаційн", "реактивн"]
        )

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
        nearby_threats=nearby_threats[:5]
    )
    return state_obj.to_dict()
