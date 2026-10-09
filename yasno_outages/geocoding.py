"""Geocoding, reverse resolution and coordinate nudging for Yasno Outages."""

import math
import logging
from typing import Optional, Dict, Any, Tuple
import aiohttp

from yasno_outages.api import yasno_client
from yasno_outages.const import DEFAULT_REGIONS

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
GEOCODE_HEADERS = {
    "User-Agent": "PowerOutagesTelegramBot/2.0 (Ukraine; tg-bot)"
}


def nudge_coords(lat: float, lon: float, direction: str, meters: float = 50.0) -> Tuple[float, float]:
    """
    Shifts coordinates by specified meters along cardinal directions.
    'n' = North, 's' = South, 'e' = East, 'w' = West.
    """
    d_lat = meters / 111139.0
    lat_rad = math.radians(lat)
    cos_lat = math.cos(lat_rad)
    if abs(cos_lat) < 1e-6:
        cos_lat = 1e-6
    d_lon = meters / (111139.0 * cos_lat)

    dir_clean = direction.lower().strip()
    if dir_clean == "n":
        return round(lat + d_lat, 6), round(lon, 6)
    elif dir_clean == "s":
        return round(lat - d_lat, 6), round(lon, 6)
    elif dir_clean == "e":
        return round(lat, 6), round(lon + d_lon, 6)
    elif dir_clean == "w":
        return round(lat, 6), round(lon - d_lon, 6)
    return round(lat, 6), round(lon, 6)


async def reverse_coords_to_address(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """
    Reverse geocodes latitude/longitude into address components using OSM Nominatim.
    """
    params = {
        "format": "jsonv2",
        "lat": lat,
        "lon": lon,
        "addressdetails": 1,
        "accept-language": "uk,en",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, headers=GEOCODE_HEADERS) as session:
            async with session.get(NOMINATIM_URL, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"Nominatim returned HTTP {resp.status} for coords {lat}, {lon}")
                    return None
                data = await resp.json()
                if not isinstance(data, dict):
                    return None

                addr = data.get("address", {})
                city = (
                    addr.get("city")
                    or addr.get("town")
                    or addr.get("village")
                    or addr.get("municipality")
                    or addr.get("county")
                    or ""
                )
                road = addr.get("road") or addr.get("street") or addr.get("pedestrian") or ""
                house_num = addr.get("house_number") or ""

                return {
                    "city": city,
                    "street": road,
                    "house_number": house_num,
                    "display_name": data.get("display_name", ""),
                    "raw": data,
                }
    except Exception as e:
        logger.error(f"Error reverse geocoding {lat}, {lon}: {e}")
        return None


async def resolve_group_from_coords(
    lat: float,
    lon: float,
    region_id: int = 25,
    dso_id: int = 902,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Resolves outage group from GPS coordinates.
    Returns: (group_name, readable_address, error_description)
    """
    addr_info = await reverse_coords_to_address(lat, lon)
    if not addr_info:
        return None, None, "Не вдалося отримати адресу за вказаними координатами."

    street = addr_info.get("street", "").strip()
    house = addr_info.get("house_number", "").strip()
    city = addr_info.get("city", "").strip()

    if not street:
        return (
            None,
            f"{city} ({lat:.5f}, {lon:.5f})",
            "За цими координатами не знайдено назви вулиці. Скористайтеся пошуком за назвою.",
        )

    # Search street in Yasno
    streets = await yasno_client.search_streets(region_id, dso_id, street)
    if not streets:
        # Try stripping common prefix words
        clean_street = street.replace("вулиця", "").replace("проспект", "").replace("провулок", "").strip()
        if clean_street != street:
            streets = await yasno_client.search_streets(region_id, dso_id, clean_street)

    if not streets:
        return (
            None,
            f"{street}, {house}",
            f"Вулицю «{street}» не знайдено в базі Yasno для цього регіону.",
        )

    matched_street = streets[0]
    street_id = matched_street["id"]
    street_name = matched_street["value"]

    if house:
        houses = await yasno_client.search_houses(region_id, dso_id, street_id, house)
        if houses:
            matched_house = houses[0]
            house_id = matched_house["id"]
            group = await yasno_client.resolve_group_by_address(region_id, dso_id, street_id, house_id)
            if group:
                full_address = f"{street_name}, буд. {matched_house['value']}"
                return group, full_address, None

    # If house wasn't matched or wasn't provided, return street info so user can pick house
    return None, street_name, f"Вулицю знайдено: «{street_name}». Будь ласка, вкажіть або оберіть номер будинку."


# Мапа головних міст України: ключ_нижній_регістр -> (region_id, region_name, dso_id, dso_name)
MAJOR_CITIES: Dict[str, Tuple[int, str, int, str]] = {
    # Київ
    "київ": (25, "м. Київ", 902, "ДТЕК Київські електромережі"),
    "kyiv": (25, "м. Київ", 902, "ДТЕК Київські електромережі"),
    "kiev": (25, "м. Київ", 902, "ДТЕК Київські електромережі"),
    # Київська область
    "біла церква": (26, "Київська область", 901, "ДТЕК Київські регіональні електромережі"),
    "бровари": (26, "Київська область", 901, "ДТЕК Київські регіональні електромережі"),
    "бориспіль": (26, "Київська область", 901, "ДТЕК Київські регіональні електромережі"),
    "ірпінь": (26, "Київська область", 901, "ДТЕК Київські регіональні електромережі"),
    "буча": (26, "Київська область", 901, "ДТЕК Київські регіональні електромережі"),
    "фастів": (26, "Київська область", 901, "ДТЕК Київські регіональні електромережі"),
    "вишгород": (26, "Київська область", 901, "ДТЕК Київські регіональні електромережі"),
    "васильків": (26, "Київська область", 901, "ДТЕК Київські регіональні електромережі"),
    "обухів": (26, "Київська область", 901, "ДТЕК Київські регіональні електромережі"),
    # Дніпро та область
    "дніпро": (3, "Дніпропетровська область", 301, "ДТЕК Дніпровські електромережі"),
    "dnipro": (3, "Дніпропетровська область", 301, "ДТЕК Дніпровські електромережі"),
    "кривий ріг": (3, "Дніпропетровська область", 301, "ДТЕК Дніпровські електромережі"),
    "кам'янське": (3, "Дніпропетровська область", 301, "ДТЕК Дніпровські електромережі"),
    "нікополь": (3, "Дніпропетровська область", 301, "ДТЕК Дніпровські електромережі"),
    "павлоград": (3, "Дніпропетровська область", 301, "ДТЕК Дніпровські електромережі"),
    "новомосковськ": (3, "Дніпропетровська область", 301, "ДТЕК Дніпровські електромережі"),
    "жовті води": (3, "Дніпропетровська область", 301, "ДТЕК Дніпровські електромережі"),
    "марганець": (3, "Дніпропетровська область", 301, "ДТЕК Дніпровські електромережі"),
    "покров": (3, "Дніпропетровська область", 301, "ДТЕК Дніпровські електромережі"),
    # Одеса
    "одеса": (51, "Одеська область", 5101, "ДТЕК Одеські електромережі"),
    "odesa": (51, "Одеська область", 5101, "ДТЕК Одеські електромережі"),
    "чорноморськ": (51, "Одеська область", 5101, "ДТЕК Одеські електромережі"),
    "ізмаїл": (51, "Одеська область", 5101, "ДТЕК Одеські електромережі"),
    "білгород-дністровський": (51, "Одеська область", 5101, "ДТЕК Одеські електромережі"),
    "подільськ": (51, "Одеська область", 5101, "ДТЕК Одеські електромережі"),
    # Львів
    "львів": (46, "Львівська область", 4601, "ПрАТ Львівобленерго"),
    "lviv": (46, "Львівська область", 4601, "ПрАТ Львівобленерго"),
    "дрогобич": (46, "Львівська область", 4601, "ПрАТ Львівобленерго"),
    "стрий": (46, "Львівська область", 4601, "ПрАТ Львівобленерго"),
    "трускавець": (46, "Львівська область", 4601, "ПрАТ Львівобленерго"),
    "червоноград": (46, "Львівська область", 4601, "ПрАТ Львівобленерго"),
    "шептицький": (46, "Львівська область", 4601, "ПрАТ Львівобленерго"),
    "самбір": (46, "Львівська область", 4601, "ПрАТ Львівобленерго"),
    # Харків
    "харків": (63, "Харківська область", 6301, "АТ Харківобленерго"),
    "kharkiv": (63, "Харківська область", 6301, "АТ Харківобленерго"),
    "лозова": (63, "Харківська область", 6301, "АТ Харківобленерго"),
    "ізюм": (63, "Харківська область", 6301, "АТ Харківобленерго"),
    "чугуїв": (63, "Харківська область", 6301, "АТ Харківобленерго"),
    # Полтава
    "полтава": (53, "Полтавська область", 5301, "АТ Полтаваобленерго"),
    "poltava": (53, "Полтавська область", 5301, "АТ Полтаваобленерго"),
    "кременчук": (53, "Полтавська область", 5301, "АТ Полтаваобленерго"),
    "миргород": (53, "Полтавська область", 5301, "АТ Полтаваобленерго"),
    "лубни": (53, "Полтавська область", 5301, "АТ Полтаваобленерго"),
    "горішні плавні": (53, "Полтавська область", 5301, "АТ Полтаваобленерго"),
    # Черкаси
    "черкаси": (71, "Черкаська область", 7101, "ПАТ Черкасиобленерго"),
    "cherkasy": (71, "Черкаська область", 7101, "ПАТ Черкасиобленерго"),
    "умань": (71, "Черкаська область", 7101, "ПАТ Черкасиобленерго"),
    "сміла": (71, "Черкаська область", 7101, "ПАТ Черкасиобленерго"),
    "золотоноша": (71, "Черкаська область", 7101, "ПАТ Черкасиобленерго"),
    "канів": (71, "Черкаська область", 7101, "ПАТ Черкасиобленерго"),
    # Чернігів
    "чернігів": (74, "Чернігівська область", 7401, "АТ Чернігівобленерго"),
    "ніжин": (74, "Чернігівська область", 7401, "АТ Чернігівобленерго"),
    "прилуки": (74, "Чернігівська область", 7401, "АТ Чернігівобленерго"),
    # Вінниця
    "вінниця": (5, "Вінницька область", 501, "АТ Вінницяобленерго"),
    "жмеринка": (5, "Вінницька область", 501, "АТ Вінницяобленерго"),
    "могилів-подільський": (5, "Вінницька область", 501, "АТ Вінницяобленерго"),
    "хмільник": (5, "Вінницька область", 501, "АТ Вінницяобленерго"),
    # Житомир
    "житомир": (18, "Житомирська область", 1801, "АТ Житомиробленерго"),
    "бердичів": (18, "Житомирська область", 1801, "АТ Житомиробленерго"),
    "коростень": (18, "Житомирська область", 1801, "АТ Житомиробленерго"),
    "новоград-волинський": (18, "Житомирська область", 1801, "АТ Житомиробленерго"),
    "звягель": (18, "Житомирська область", 1801, "АТ Житомиробленерго"),
    "малин": (18, "Житомирська область", 1801, "АТ Житомиробленерго"),
    # Запоріжжя
    "запоріжжя": (23, "Запорізька область", 2301, "ПАТ Запоріжжяобленерго"),
    "мелітополь": (23, "Запорізька область", 2301, "ПАТ Запоріжжяобленерго"),
    "бердянськ": (23, "Запорізька область", 2301, "ПАТ Запоріжжяобленерго"),
    # Івано-Франківськ
    "івано-франківськ": (260, "Івано-Франківська область", 2601, "АТ Прикарпаттяобленерго"),
    "калуш": (260, "Івано-Франківська область", 2601, "АТ Прикарпаттяобленерго"),
    "коломия": (260, "Івано-Франківська область", 2601, "АТ Прикарпаттяобленерго"),
    "яремче": (260, "Івано-Франківська область", 2601, "АТ Прикарпаттяобленерго"),
    # Закарпаття
    "ужгород": (21, "Закарпатська область", 2101, "ПрАТ Закарпаттяобленерго"),
    "мукачево": (21, "Закарпатська область", 2101, "ПрАТ Закарпаттяобленерго"),
    "хуст": (21, "Закарпатська область", 2101, "ПрАТ Закарпаттяобленерго"),
    "берегове": (21, "Закарпатська область", 2101, "ПрАТ Закарпаттяобленерго"),
    # Рівне
    "рівне": (56, "Рівненська область", 5601, "ПрАТ Рівнеобленерго"),
    "дубно": (56, "Рівненська область", 5601, "ПрАТ Рівнеобленерго"),
    "вараш": (56, "Рівненська область", 5601, "ПрАТ Рівнеобленерго"),
    "сарни": (56, "Рівненська область", 5601, "ПрАТ Рівнеобленерго"),
    # Тернопіль
    "тернопіль": (61, "Тернопільська область", 6101, "ВАТ Тернопільобленерго"),
    "чортків": (61, "Тернопільська область", 6101, "ВАТ Тернопільобленерго"),
    "кременець": (61, "Тернопільська область", 6101, "ВАТ Тернопільобленерго"),
    # Хмельницький
    "хмельницький": (68, "Хмельницька область", 6801, "АТ Хмельницькобленерго"),
    "кам'янець-подільський": (68, "Хмельницька область", 6801, "АТ Хмельницькобленерго"),
    "шепетівка": (68, "Хмельницька область", 6801, "АТ Хмельницькобленерго"),
    "нетішин": (68, "Хмельницька область", 6801, "АТ Хмельницькобленерго"),
    # Волинь
    "луцьк": (7, "Волинська область", 701, "ПрАТ Волиньобленерго"),
    "ковель": (7, "Волинська область", 701, "ПрАТ Волиньобленерго"),
    "нововолинськ": (7, "Волинська область", 701, "ПрАТ Волиньобленерго"),
    "володимир": (7, "Волинська область", 701, "ПрАТ Волиньобленерго"),
    # Суми
    "суми": (59, "Сумська область", 5901, "АТ Сумиобленерго"),
    "конотоп": (59, "Сумська область", 5901, "АТ Сумиобленерго"),
    "шостка": (59, "Сумська область", 5901, "АТ Сумиобленерго"),
    "охтирка": (59, "Сумська область", 5901, "АТ Сумиобленерго"),
    "ромни": (59, "Сумська область", 5901, "АТ Сумиобленерго"),
    # Кіровоград
    "кропивницький": (35, "Кіровоградська область", 3501, "ПрАТ Кіровоградобленерго"),
    "олександрія": (35, "Кіровоградська область", 3501, "ПрАТ Кіровоградобленерго"),
    "знам'янка": (35, "Кіровоградська область", 3501, "ПрАТ Кіровоградобленерго"),
    "світловодськ": (35, "Кіровоградська область", 3501, "ПрАТ Кіровоградобленерго"),
    # Миколаїв
    "миколаїв": (48, "Миколаївська область", 4801, "АТ Миколаївобленерго"),
    "первомайськ": (48, "Миколаївська область", 4801, "АТ Миколаївобленерго"),
    "южноукраїнськ": (48, "Миколаївська область", 4801, "АТ Миколаївобленерго"),
    "вознесенськ": (48, "Миколаївська область", 4801, "АТ Миколаївобленерго"),
    # Чернівці
    "чернівці": (73, "Чернівецька область", 7301, "АТ Чернівціобленерго"),
    "сторожинець": (73, "Чернівецька область", 7301, "АТ Чернівціобленерго"),
    # Донецьк
    "краматорськ": (14, "Донецька область", 1401, "ДТЕК Донецькі електромережі"),
    "слов'янськ": (14, "Донецька область", 1401, "ДТЕК Донецькі електромережі"),
    "покровськ": (14, "Донецька область", 1401, "ДТЕК Донецькі електромережі"),
    "костянтинівка": (14, "Донецька область", 1401, "ДТЕК Донецькі електромережі"),
    # Херсон
    "херсон": (65, "Херсонська область", 6501, "АТ Херсонобленерго"),
    "нова каховка": (65, "Херсонська область", 6501, "АТ Херсонобленерго"),
}


async def find_ukrainian_settlement(query: str) -> Optional[Dict[str, Any]]:
    """Шукає населений пункт по всій території України (словник міст + Nominatim)."""
    q_clean = query.strip()
    q_lower = q_clean.lower()
    if not q_lower:
        return None

    # 1. Прямий збіг з назвою області (наприклад "Львівська", "Одеська", "Полтавщина")
    for r in DEFAULT_REGIONS:
        r_name_lower = r["name"].lower()
        short_lower = r.get("short_name", "").lower()
        if (
            q_lower == r_name_lower
            or q_lower == short_lower
            or q_lower in r_name_lower
            or (short_lower and q_lower in short_lower)
        ):
            dso = r["dsos"][0] if r.get("dsos") else {"id": 902, "name": "Обленерго"}
            return {
                "region_id": r["id"],
                "region_name": r["name"],
                "dso_id": dso["id"],
                "dso_name": dso["name"],
                "city_name": r.get("short_name", r["name"]),
                "is_yasno": r["id"] in (25, 3),
            }

    # 2. Перевірка за словником великих міст та райцентрів
    if q_lower in MAJOR_CITIES:
        r_id, r_name, d_id, d_name = MAJOR_CITIES[q_lower]
        return {
            "region_id": r_id,
            "region_name": r_name,
            "dso_id": d_id,
            "dso_name": d_name,
            "city_name": q_clean.capitalize(),
            "is_yasno": r_id in (25, 3),
        }

    # 3. Онлайн-пошук населеного пункту через OpenStreetMap Nominatim в Україні
    try:
        import urllib.parse
        search_url = f"https://nominatim.openstreetmap.org/search?format=jsonv2&countrycodes=ua&addressdetails=1&q={urllib.parse.quote(q_clean)}"
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout, headers=GEOCODE_HEADERS) as session:
            async with session.get(search_url) as resp:
                if resp.status == 200:
                    items = await resp.json(content_type=None)
                    if isinstance(items, list) and len(items) > 0:
                        addr = items[0].get("address", {})
                        state = addr.get("state", "").lower()
                        city = addr.get("city") or addr.get("town") or addr.get("village") or q_clean
                        for r in DEFAULT_REGIONS:
                            r_name_lower = r["name"].lower()
                            short_lower = r.get("short_name", "").lower()
                            if r_name_lower in state or (short_lower and short_lower in state) or state in r_name_lower:
                                dso = r["dsos"][0] if r.get("dsos") else {"id": 902, "name": "Обленерго"}
                                return {
                                    "region_id": r["id"],
                                    "region_name": r["name"],
                                    "dso_id": dso["id"],
                                    "dso_name": dso["name"],
                                    "city_name": str(city).capitalize(),
                                    "is_yasno": r["id"] in (25, 3),
                                }
    except Exception as e:
        logger.warning(f"Nominatim lookup failed for {q_clean}: {e}")

    return None

