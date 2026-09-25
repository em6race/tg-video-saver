import os
import time
import math
import aiohttp
import logging
from datetime import datetime
import pytz
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

NEPTUN_ALERTS_URL = "https://neptun.in.ua/api/v1/alerts"
NEPTUN_THREATS_URL = "https://neptun.in.ua/api/v1/threats"
NEPTUN_OBLASTS_GEOJSON_URL = "https://neptun.in.ua/oblasts.geojson"
NEPTUN_RAIONS_GEOJSON_URL = "https://neptun.in.ua/raions.geojson"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

# Кеш GeoJSON у пам'яті, щоб не завантажувати щоразу
_GEOJSON_CACHE = {
    "oblasts": None,
    "raions": None
}

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

def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Обчислює початковий азимут (в градусах 0-360) від точки 1 до точки 2."""
    r_lat1 = math.radians(lat1)
    r_lat2 = math.radians(lat2)
    d_lon = math.radians(lon2 - lon1)

    y = math.sin(d_lon) * math.cos(r_lat2)
    x = math.cos(r_lat1) * math.sin(r_lat2) - math.sin(r_lat1) * math.cos(r_lat2) * math.cos(d_lon)
    initial_bearing = math.atan2(y, x)
    initial_bearing = math.degrees(initial_bearing)
    compass_bearing = (initial_bearing + 360.0) % 360.0
    return compass_bearing

def _heading_to_direction(heading: float) -> tuple[str, str]:
    """Повертає текстовий напрямок та стрілку за градусом (0-360°)."""
    h = float(heading) % 360.0
    dirs = [
        (337.5, 360.0, "Північ", "↑"),
        (0.0, 22.5, "Північ", "↑"),
        (22.5, 67.5, "Північний схід", "↗"),
        (67.5, 112.5, "Схід", "→"),
        (112.5, 157.5, "Південний схід", "↘"),
        (157.5, 202.5, "Південь", "↓"),
        (202.5, 247.5, "Південний захід", "↙"),
        (247.5, 292.5, "Захід", "←"),
        (292.5, 337.5, "Північний захід", "↖"),
    ]
    for low, high, name, arrow in dirs:
        if low <= h < high or (low == 337.5 and h >= 337.5):
            return name, arrow
    return "Північ", "↑"

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
    country_code = (addr.get("country_code") or "").lower().strip()
    country_name = addr.get("country") or ""
    is_ukraine = (country_code == "ua") or ("укра" in country_name.lower())

    state = _normalize_apostrophe(addr.get("state", ""))
    county = _normalize_apostrophe(addr.get("county", "") or addr.get("state_district", "") or addr.get("district", ""))
    locality = _normalize_apostrophe(
        addr.get("city", "") or addr.get("town", "") or addr.get("village", "") or addr.get("municipality", "")
    )

    if locality == "Київ" or state in ("Київ", "місто Київ", "м. Київ"):
        oblast = "м. Київ"
        if not county:
            county = "Київ"
    else:
        oblast = state

    return {
        "oblast": oblast or "Невідома область",
        "raion": county or "",
        "locality": locality or "",
        "is_ukraine": is_ukraine,
        "country_name": country_name,
        "lat": lat,
        "lon": lon
    }

async def forward_geocode(query: str) -> dict:
    """
    Знаходить координати, область, район та населений пункт за текстовою назвою (місто/село/громада).
    """
    clean_query = query.strip()
    if not clean_query:
        return None

    headers = {
        "User-Agent": "TG-VideoSaver-AlertBot/1.0 (https://neptun.in.ua)",
        "Accept-Language": "uk"
    }
    params = {
        "q": clean_query,
        "countrycodes": "ua",
        "format": "json",
        "addressdetails": "1",
        "accept-language": "uk",
        "limit": "1"
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get("https://nominatim.openstreetmap.org/search", params=params, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not data:
                    return None
                item = data[0]

        addr = item.get("address", {})
        country_code = (addr.get("country_code") or "").lower().strip()
        country_name = addr.get("country") or ""
        is_ukraine = (country_code == "ua") or ("укра" in country_name.lower())

        state = _normalize_apostrophe(addr.get("state", ""))
        county = _normalize_apostrophe(addr.get("county", "") or addr.get("district", "") or addr.get("state_district", ""))
        locality = _normalize_apostrophe(
            addr.get("city", "") or addr.get("town", "") or addr.get("village", "") or addr.get("municipality", "") or clean_query
        )

        if locality == "Київ" or state in ("Київ", "місто Київ", "м. Київ"):
            oblast = "м. Київ"
            if not county:
                county = "Київ"
        else:
            oblast = state

        lat = float(item["lat"]) if "lat" in item else None
        lon = float(item["lon"]) if "lon" in item else None

        return {
            "oblast": oblast or "Невідома область",
            "raion": county or "",
            "locality": locality or clean_query,
            "is_ukraine": is_ukraine,
            "country_name": country_name,
            "lat": lat,
            "lon": lon
        }
    except Exception as e:
        logger.warning(f"Failed to forward geocode '{clean_query}': {e}")
        return None


_NEPTUN_DATA_CACHE = {
    "timestamp": 0.0,
    "alerts": {},
    "threats": {}
}

async def fetch_neptun_data(force: bool = False) -> tuple[dict, dict]:
    """
    Отримує актуальні тривоги (/api/v1/alerts) та загрози (/api/v1/threats) з NEPTUN API.
    Містить короткий кеш (3 секунди), щоб захистити API від перевантаження при одночасних запитах.
    """
    global _NEPTUN_DATA_CACHE
    now = time.time()
    if not force and (now - _NEPTUN_DATA_CACHE["timestamp"] < 3) and _NEPTUN_DATA_CACHE["alerts"]:
        return _NEPTUN_DATA_CACHE["alerts"], _NEPTUN_DATA_CACHE["threats"]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TGAlertBot/1.0; +https://neptun.in.ua)"
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(NEPTUN_ALERTS_URL, timeout=10) as a_resp:
                alerts_data = await a_resp.json() if a_resp.status == 200 else {}
            async with session.get(NEPTUN_THREATS_URL, timeout=10) as t_resp:
                threats_data = await t_resp.json() if t_resp.status == 200 else {}

        if alerts_data or threats_data:
            _NEPTUN_DATA_CACHE["timestamp"] = now
            _NEPTUN_DATA_CACHE["alerts"] = alerts_data
            _NEPTUN_DATA_CACHE["threats"] = threats_data

        return alerts_data, threats_data
    except Exception as e:
        logger.warning(f"Error fetching NEPTUN data: {e}")
        if _NEPTUN_DATA_CACHE["alerts"]:
            return _NEPTUN_DATA_CACHE["alerts"], _NEPTUN_DATA_CACHE["threats"]
        return {}, {}

async def _ensure_geojson_loaded():
    """Завантажує контури областей та районів України з NEPTUN при першому виклику."""
    if _GEOJSON_CACHE["oblasts"] and _GEOJSON_CACHE["raions"]:
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TGAlertBot/1.0; +https://neptun.in.ua)"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        if not _GEOJSON_CACHE["oblasts"]:
            async with session.get(NEPTUN_OBLASTS_GEOJSON_URL, timeout=15) as r1:
                if r1.status == 200:
                    _GEOJSON_CACHE["oblasts"] = await r1.json()
        if not _GEOJSON_CACHE["raions"]:
            async with session.get(NEPTUN_RAIONS_GEOJSON_URL, timeout=15) as r2:
                if r2.status == 200:
                    _GEOJSON_CACHE["raions"] = await r2.json()

async def generate_alerts_map_image(alerts_data: dict, threats_data: dict, user_sub: dict = None) -> tuple[str, str]:
    """
    Генерує актуальне зображення карти повітряних тривог та цілей України у стилі NEPTUN.
    Повертає (шлях_до_файлу_png, текстовий_опис_карти).
    """
    await _ensure_geojson_loaded()
    oblasts_geo = _GEOJSON_CACHE["oblasts"] or {}
    raions_geo = _GEOJSON_CACHE["raions"] or {}

    width, height = 1440, 960
    pad_x, pad_y = 55, 65
    min_lon, max_lon = 22.05, 40.30
    min_lat, max_lat = 44.25, 52.45

    def project(lon: float, lat: float) -> tuple[float, float]:
        x = pad_x + (lon - min_lon) / (max_lon - min_lon) * (width - 2 * pad_x)
        y = height - pad_y - (lat - min_lat) / (max_lat - min_lat) * (height - 2 * pad_y)
        return (x, y)

    img = Image.new("RGB", (width, height), color=(14, 19, 27))
    draw = ImageDraw.Draw(img)

    raions_list = alerts_data.get("raions", [])
    oblasts_list = alerts_data.get("oblasts", [])

    red_raions = set(_normalize_apostrophe(r.get("key", "")).lower() for r in raions_list if r.get("level") == "red")
    yellow_raions = set(_normalize_apostrophe(r.get("key", "")).lower() for r in raions_list if r.get("level") == "yellow")

    red_oblasts = set(_normalize_apostrophe(o.get("key", "")).lower() for o in oblasts_list if o.get("level") == "red")
    yellow_oblasts = set(_normalize_apostrophe(o.get("key", "")).lower() for o in oblasts_list if o.get("level") == "yellow")

    # 1. Спочатку замальовуємо всі області базовим кольором (або червоним/жовтим, якщо вся область у тривозі)
    for feat in oblasts_geo.get("features", []):
        props = feat.get("properties", {})
        ob_key = _normalize_apostrophe(props.get("key", "")).lower()
        fill_col = (26, 35, 48)
        out_col = (42, 55, 76)
        if ob_key in red_oblasts:
            fill_col = (180, 36, 42)
            out_col = (245, 75, 75)
        elif ob_key in yellow_oblasts:
            fill_col = (205, 145, 20)
            out_col = (245, 185, 45)

        geom = feat.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        polys = coords if gtype == "MultiPolygon" else [coords] if gtype == "Polygon" else []
        for poly in polys:
            if poly and poly[0]:
                pts = [project(pt[0], pt[1]) for pt in poly[0]]
                if len(pts) >= 3:
                    draw.polygon(pts, fill=fill_col, outline=out_col)

    # 2. Малюємо всі райони (raions.geojson) з їхніми актуальними рівнями (red / yellow)
    for feat in raions_geo.get("features", []):
        props = feat.get("properties", {})
        r_key = _normalize_apostrophe(props.get("key", "")).lower()

        if r_key in red_raions:
            fill_col = (188, 38, 45)
            out_col = (255, 85, 85)
        elif r_key in yellow_raions:
            fill_col = (214, 152, 22)
            out_col = (248, 190, 50)
        else:
            # Якщо район спокійний, малюємо лише тонку сітку району
            fill_col = None
            out_col = (38, 50, 68)

        geom = feat.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        polys = coords if gtype == "MultiPolygon" else [coords] if gtype == "Polygon" else []
        for poly in polys:
            if poly and poly[0]:
                pts = [project(pt[0], pt[1]) for pt in poly[0]]
                if len(pts) >= 3:
                    if fill_col:
                        draw.polygon(pts, fill=fill_col, outline=out_col)
                    else:
                        draw.line(pts, fill=out_col, width=1)

    # 3. Поверх районів малюємо чіткі межі областей
    for feat in oblasts_geo.get("features", []):
        geom = feat.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        polys = coords if gtype == "MultiPolygon" else [coords] if gtype == "Polygon" else []
        for poly in polys:
            if poly and poly[0]:
                pts = [project(pt[0], pt[1]) for pt in poly[0]]
                if len(pts) >= 3:
                    draw.line(pts, fill=(88, 112, 148), width=2)

    # 4. Малюємо активні повітряні цілі (threats) та вектори їх руху
    active_threats_count = 0
    for t in threats_data.get("threats", []):
        if t.get("status") != "active":
            continue
        active_threats_count += 1
        t_lat = t.get("lat")
        t_lon = t.get("lon")
        if t_lat is None or t_lon is None or t.get("areaOnly"):
            continue

        tx, ty = project(t_lon, t_lat)
        t_type = t.get("type", "uav")

        # Колір маркера цілі: ракети/балістика — яскраво-пурпуровий/білий, БпЛА — помаранчево-жовтий
        if t_type in ("missile", "ballistic", "kab", "mig31k"):
            marker_col = (255, 50, 220)
        elif t_type == "recon":
            marker_col = (80, 200, 255)
        else:
            marker_col = (255, 210, 40)

        # Зовнішній ореол та точка
        r_outer = 9
        r_inner = 5
        draw.ellipse((tx - r_outer, ty - r_outer, tx + r_outer, ty + r_outer), outline=marker_col, width=2)
        draw.ellipse((tx - r_inner, ty - r_inner, tx + r_inner, ty + r_inner), fill=marker_col)

        # Напрямок польоту (стрілка за курсом heading)
        heading = t.get("heading")
        if heading is not None:
            rad = math.radians(float(heading))
            arrow_len = 24
            ex = tx + arrow_len * math.sin(rad)
            ey = ty - arrow_len * math.cos(rad)
            draw.line([(tx, ty), (ex, ey)], fill=marker_col, width=3)

    # 5. Якщо у користувача збережені власні координати — малюємо його точку (блакитно-зелений пін)
    if user_sub and user_sub.get("lat") is not None and user_sub.get("lon") is not None:
        ux, uy = project(user_sub["lon"], user_sub["lat"])
        draw.ellipse((ux - 11, uy - 11, ux + 11, uy + 11), fill=(0, 220, 130), outline=(255, 255, 255), width=3)
        draw.ellipse((ux - 4, uy - 4, ux + 4, uy + 4), fill=(255, 255, 255))

    # 6. Кольорові індикатори легенди внизу зліва
    legend_items = [
        ((188, 38, 45), (255, 85, 85)),   # Червоний
        ((214, 152, 22), (248, 190, 50)), # Жовтий
        ((26, 35, 48), (88, 112, 148)),   # Спокійно
        ((255, 210, 40), (255, 210, 40)), # Цілі
    ]
    lx = 40
    ly = height - 45
    for idx, (f_c, o_c) in enumerate(legend_items):
        bx = lx + idx * 42
        draw.rectangle((bx, ly, bx + 26, ly + 18), fill=f_c, outline=o_c, width=2)

    os.makedirs("downloads", exist_ok=True)
    map_path = f"downloads/neptun_map_{int(time.time())}.png"
    img.save(map_path, "PNG")

    # Підготовка текстового опису до карти
    tz = pytz.timezone("Europe/Kiev")
    now_str = datetime.now(tz).strftime("%H:%M:%S (%d.%m.%Y)")
    red_count = len(red_raions) + len(red_oblasts)
    yellow_count = len(yellow_raions) + len(yellow_oblasts)

    caption_lines = [
        f"🗺 **Карта повітряних тривог України**",
        f"🕒 Станом на: **{now_str}**\n",
        f"🔴 Червоний рівень (тривога): **{red_count}** р-н/обл.",
        f"🟡 Жовтий рівень (підвищена небезпека): **{yellow_count}** р-н/обл.",
        f"🛸 Повітряних цілей у небі: **{active_threats_count}**"
    ]
    if user_sub and user_sub.get("lat") is not None:
        caption_lines.append("🟢 *Ваша геопозиція позначена зелено-білою точкою на карті*")

    caption_lines.append("\n🔗 [Дані: Карта повітряних тривог — NEPTUN](https://neptun.in.ua/)")
    return map_path, "\n".join(caption_lines)

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

        dist_km = None
        if user_lat is not None and user_lon is not None and t_lat is not None and t_lon is not None and not area_only:
            dist_km = round(haversine_km(user_lat, user_lon, t_lat, t_lon))

        heading_raw = t.get("heading")
        heading_deg = None
        direction_name = None
        direction_arrow = None
        is_towards_user = False

        if heading_raw is not None:
            try:
                heading_deg = round(float(heading_raw)) % 360
                direction_name, direction_arrow = _heading_to_direction(heading_deg)

                if user_lat is not None and user_lon is not None and t_lat is not None and t_lon is not None:
                    bearing_to_user = calculate_bearing(t_lat, t_lon, user_lat, user_lon)
                    angle_diff = abs((heading_deg - bearing_to_user + 180.0) % 360.0 - 180.0)
                    if angle_diff <= 45.0 and (dist_km is not None and dist_km <= 150):
                        is_towards_user = True
            except Exception as ge_err:
                logger.debug(f"Heading calc error: {ge_err}")

        in_same_oblast = _match_oblast(user_oblast, t_region)
        is_close = dist_km is not None and dist_km <= 80

        if in_same_oblast or is_close:
            nearby_threats.append({
                "id": t.get("id"),
                "type": t.get("type", "unknown"),
                "title": t.get("title", "Повітряна ціль"),
                "explanation": t.get("explanationShort") or t.get("title", "Зафіксовано ціль"),
                "distance_km": dist_km,
                "advisory": bool(t.get("advisory", False)),
                "heading": heading_deg,
                "direction_name": direction_name,
                "direction_arrow": direction_arrow,
                "is_towards_user": is_towards_user
            })

    nearby_threats.sort(key=lambda x: (not x["is_towards_user"], x["distance_km"] if x["distance_km"] is not None else 9999))

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
        "nearby_threats": nearby_threats[:5]
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

def make_threat_signature(state: dict) -> tuple:
    """
    Повертає кортеж-сигнатуру поточної загрози для відстеження змін у фоновому моніторингу:
    рівень, список причин та повітряні цілі (id, тип, відстань з кроком 5 км, статус "до користувача").
    """
    level = state.get("level", "green")
    reasons = tuple(sorted(state.get("reasons", [])))
    threats = tuple(
        (
            t.get("id"),
            t.get("type"),
            round((t.get("distance_km") or 0) / 5) * 5 if t.get("distance_km") is not None else None,
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
            heading_info = ""
            if t.get("heading") is not None and t.get("direction_name"):
                heading_info = f" | курс {t['direction_arrow']} {t['direction_name']} ({t['heading']}°)"

            towards_warn = ""
            if t.get("is_towards_user"):
                towards_warn = "\n  ⚠️ **МОЖЛИВА НЕБЕЗПЕКА: Вектор руху цілі спрямований у ваш бік!**"

            lines.append(f"• {t['explanation']}{dist_str}{heading_info}{towards_warn}")

    if level == "red":
        lines.append("\n🙏 *Будь ласка, перебувайте в укритті або дотримуйтесь правила двох стін!*")

    now_tz = datetime.now(pytz.timezone("Europe/Kiev")).strftime("%H:%M:%S")
    lines.append(f"\n⏱ **Оновлено:** {now_tz}")
    lines.append("🗺 [Дані: Карта повітряних тривог — NEPTUN](https://neptun.in.ua/)")
    return "\n".join(lines)
