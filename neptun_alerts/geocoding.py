import math
import aiohttp
from typing import Dict, Tuple, Optional

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

_NOMINATIM_REVERSE_CACHE: Dict[Tuple[float, float], dict] = {}
_NOMINATIM_FORWARD_CACHE: Dict[str, dict] = {}

def _normalize_apostrophe(text: str) -> str:
    if not text:
        return ""
    return text.replace("’", "'").replace("`", "'").replace("ʼ", "'").strip()

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
        (337.5, 360.0, "Північ", "⬆️"),
        (0.0, 22.5, "Північ", "⬆️"),
        (22.5, 67.5, "Північний схід", "↗️"),
        (67.5, 112.5, "Схід", "➡️"),
        (112.5, 157.5, "Південний схід", "↘️"),
        (157.5, 202.5, "Південь", "⬇️"),
        (202.5, 247.5, "Південний захід", "↙️"),
        (247.5, 292.5, "Захід", "⬅️"),
        (292.5, 337.5, "Північний захід", "↖️"),
    ]
    for low, high, name, arrow in dirs:
        if low <= h < high or (low == 337.5 and h >= 337.5):
            return name, arrow
    return "Північ", "⬆️"

async def reverse_geocode(lat: float, lon: float) -> dict:
    """
    Визначає область, район та населений пункт України за координатами GPS.
    Використовує кеш у пам'яті для мінімізації запитів до Nominatim.
    """
    cache_key = (round(lat, 4), round(lon, 4))
    if cache_key in _NOMINATIM_REVERSE_CACHE:
        return _NOMINATIM_REVERSE_CACHE[cache_key]

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

    result = {
        "oblast": oblast or "Невідома область",
        "raion": county or "",
        "locality": locality or "",
        "is_ukraine": is_ukraine,
        "country_name": country_name,
        "lat": lat,
        "lon": lon
    }
    _NOMINATIM_REVERSE_CACHE[cache_key] = result
    return result

async def forward_geocode(query: str) -> dict:
    """
    Знаходить координати, область, район та населений пункт за текстовою назвою (місто/село/громада).
    Використовує кеш у пам'яті для повторних пошуків.
    """
    clean_query = query.strip()
    if not clean_query:
        return None

    cache_key = clean_query.lower()
    if cache_key in _NOMINATIM_FORWARD_CACHE:
        return _NOMINATIM_FORWARD_CACHE[cache_key]

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

        result = {
            "oblast": oblast or "Невідома область",
            "raion": county or "",
            "locality": locality or clean_query,
            "is_ukraine": is_ukraine,
            "country_name": country_name,
            "lat": lat,
            "lon": lon
        }
        _NOMINATIM_FORWARD_CACHE[cache_key] = result
        return result
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to forward geocode '{clean_query}': {e}")
        return None
