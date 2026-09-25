import math
import aiohttp
import logging
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)

NEPTUN_ALERTS_URL = "https://neptun.in.ua/api/v1/alerts"
NEPTUN_THREATS_URL = "https://neptun.in.ua/api/v1/threats"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

UKRAINE_OBLASTS = [
    "м. Київ",
    "Київська область",
    "Вінницька область",
    "Волинська область",
    "Дніпропетровська область",
    "Донецька область",
    "Житомирська область",
    "Закарпатська область",
    "Запорізька область",
    "Івано-Франківська область",
    "Кіровоградська область",
    "Львівська область",
    "Миколаївська область",
    "Одеська область",
    "Полтавська область",
    "Рівненська область",
    "Сумська область",
    "Тернопільська область",
    "Харківська область",
    "Херсонська область",
    "Хмельницька область",
    "Черкаська область",
    "Чернівецька область",
    "Чернігівська область",
]

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Обчислює відстань у кілометрах між двома точками за формулою гаверсинуса."""
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c

def _normalize_apostrophe(text: str) -> str:
    if not text:
        return ""
    return text.replace("’", "'").replace("`", "'").replace("ʼ", "'").strip()

async def reverse_geocode(lat: float, lon: float) -> dict:
    """
    Визначає область, район та населений пункт України за координатами GPS.
    """
    headers = {
        "User-Agent": "TG-VideoSaver-AlertBot/1.0 (https://neptun.in.ua)",
        "Accept-Language": "uk"
    }
    params = {
        "lat": str(lat),
        "lon": str(lon),
        "format": "json",
        "addressdetails": "1",
        "accept-language": "uk"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(NOMINATIM_URL, params=params, timeout=10) as resp:
            if resp.status != 200:
                raise Exception(f"Nominatim status {resp.status}")
            data = await resp.json()

    addr = data.get("address", {})
    state = _normalize_apostrophe(addr.get("state", ""))
    county = _normalize_apostrophe(addr.get("county", "") or addr.get("state_district", "") or addr.get("district", ""))
    locality = _normalize_apostrophe(
        addr.get("city", "") or addr.get("town", "") or addr.get("village", "") or addr.get("municipality", "")
    )

    # Особливий випадок для м. Київ
    if locality == "Київ" or state in ("Київ", "місто Київ", "м. Київ"):
        oblast = "м. Київ"
        if not county:
            county = "Київ"
    else:
        oblast = state

    return {
        "oblast": oblast or "Невідома область",
        "raion": county or "",
        "locality": locality or ""
    }

async def fetch_neptun_data() -> tuple[dict, dict]:
    """
    Отримує актуальні тривоги (/api/v1/alerts) та загрози (/api/v1/threats) з NEPTUN API.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TGAlertBot/1.0; +https://neptun.in.ua)"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(NEPTUN_ALERTS_URL, timeout=10) as a_resp:
            alerts_data = await a_resp.json() if a_resp.status == 200 else {}
        async with session.get(NEPTUN_THREATS_URL, timeout=10) as t_resp:
            threats_data = await t_resp.json() if t_resp.status == 200 else {}

    return alerts_data, threats_data

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

    # 1. Перевірка цілих областей (наприклад м. Київ або повна область)
    for ob in oblasts_list:
        ob_name = ob.get("name", "") or ob.get("oblast", "")
        if _match_oblast(user_oblast, ob_name):
            matched_alerts.append(ob)

    # 2. Перевірка районів
    if user_raion and user_oblast != "м. Київ":
        # Якщо у користувача відомий конкретний район — шукаємо точний збіг району в його області
        for r in raions_list:
            if _match_oblast(user_oblast, r.get("oblast", "")) and _match_raion(user_raion, r.get("name", ""), r.get("key", "")):
                matched_alerts.append(r)
    else:
        # Якщо користувач обрав лише область (або м. Київ) — перевіряємо всі райони цієї області
        for r in raions_list:
            if _match_oblast(user_oblast, r.get("oblast", "")) or (user_oblast == "м. Київ" and "київ" in r.get("key", "").lower()):
                matched_alerts.append(r)

    # Визначаємо найвищий рівень небезпеки серед знайдених збігів
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

    # 3. Шукаємо повітряні цілі (threats) у цій області або в радіусі 80 км від координат користувача
    nearby_threats = []
    for t in threats_data.get("threats", []):
        if t.get("status") != "active":
            continue

        t_region = t.get("region", "")
        t_lat = t.get("lat")
        t_lon = t.get("lon")
        area_only = bool(t.get("areaOnly", False))

        dist_km = None
        if user_lat is not None and user_lon is not None and t_lat is not None and t_lon is not None and not area_only:
            dist_km = round(haversine_km(user_lat, user_lon, t_lat, t_lon))

        in_same_oblast = _match_oblast(user_oblast, t_region)
        is_close = dist_km is not None and dist_km <= 80

        if in_same_oblast or is_close:
            nearby_threats.append({
                "id": t.get("id"),
                "type": t.get("type", "unknown"),
                "title": t.get("title", "Повітряна ціль"),
                "explanation": t.get("explanationShort") or t.get("title", "Зафіксовано ціль"),
                "distance_km": dist_km,
                "advisory": bool(t.get("advisory", False))
            })

    # Сортуємо цілі за відстанню (спочатку найближчі)
    nearby_threats.sort(key=lambda x: x["distance_km"] if x["distance_km"] is not None else 9999)

    if level != "green" and not reasons:
        if level == "red":
            reasons.append("Ракетна або дронова загроза (червоний рівень)")
        else:
            reasons.append("Підвищена небезпека (жовтий рівень)")

    return {
        "active": level in ("red", "yellow"),
        "level": level,
        "reasons": reasons,
        "since": since_time,
        "matched_names": matched_names,
        "nearby_threats": nearby_threats[:5]  # до 5 найближчих цілей
    }

def _format_kyiv_time(iso_str: str = None) -> str:
    tz = pytz.timezone("Europe/Kiev")
    if iso_str:
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.astimezone(tz).strftime("%H:%M (%d.%m)")
        except Exception:
            pass
    return datetime.now(tz).strftime("%H:%M")

def format_alert_message(sub: dict, state: dict, is_manual_check: bool = False, prev_level: str = None) -> str:
    """
    Формує повідомлення для користувача з кольоровим рівнем, причиною та цілями.
    """
    oblast = sub.get("oblast") or "Невідома область"
    raion = sub.get("raion") or ""
    locality = sub.get("locality") or ""

    loc_parts = [p for p in [locality, raion, oblast] if p]
    # Уникаємо дублювання типу "м. Київ, Київ, м. Київ"
    unique_loc = []
    for p in loc_parts:
        if p not in unique_loc:
            unique_loc.append(p)
    loc_display = ", ".join(unique_loc)

    level = state["level"]
    reasons = state["reasons"]
    threats = state["nearby_threats"]
    time_str = _format_kyiv_time(state.get("since"))

    lines = []

    if level == "red":
        if prev_level == "yellow" and not is_manual_check:
            lines.append("🔴 **УВАГА! РІВЕНЬ ЗАГРОЗИ ПІДВИЩЕНО ДО ЧЕРВОНОГО!** 🚨")
        else:
            lines.append("🔴 **ПОВІТРЯНА ТРИВОГА! (Червоний рівень)** 🚨")
        lines.append(f"📍 **Локація:** {loc_display}")
        lines.append(f"❓ **Чому тривога:** {', '.join(reasons)}")
        lines.append(f"🕒 **Триває з:** {time_str}")
    elif level == "yellow":
        if prev_level == "red" and not is_manual_check:
            lines.append("🟡 **Рівень загрози знижено до ЖОВТОГО (Підвищена увага)** ⚠️")
        else:
            lines.append("🟡 **ПІДВИЩЕНА НЕБЕЗПЕКА! (Жовтий рівень)** ⚠️")
        lines.append(f"📍 **Локація:** {loc_display}")
        lines.append(f"❓ **Причина:** {', '.join(reasons)}")
        lines.append(f"🕒 **Початок:** {time_str}")
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
        for t in threats:
            dist_str = f" *(~{t['distance_km']} км від вас)*" if t.get("distance_km") is not None else ""
            lines.append(f"• {t['explanation']}{dist_str}")

    if level == "red":
        lines.append("\n🙏 *Будь ласка, перебувайте в укритті або дотримуйтесь правила двох стін!*")

    lines.append("\n🗺 [Дані: Карта повітряних тривог — NEPTUN](https://neptun.in.ua/)")
    return "\n".join(lines)
