"""Geocoding, reverse resolution and coordinate nudging for Yasno Outages."""

import math
import logging
from typing import Optional, Dict, Any, Tuple
import aiohttp

from yasno_outages.api import yasno_client

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
