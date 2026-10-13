import os
import time
import aiohttp
import logging

logger = logging.getLogger(__name__)

NEPTUN_ALERTS_URL = "https://neptun.in.ua/api/v1/alerts"
NEPTUN_THREATS_URL = "https://neptun.in.ua/api/v1/threats"

UKRAINE_ALARM_URL = "https://api.ukrainealarm.com/api/v3/alerts"
UKRAINE_ALARM_API_KEY = os.getenv("UKRAINE_ALARM_API_KEY", "4798af8d:aa2c7a815937b657cd07ea0f97234e5f")

_NEPTUN_DATA_CACHE = {
    "timestamp": 0.0,
    "alerts": {},
    "threats": {}
}

_UKRAINE_ALARM_CACHE = {
    "timestamp": 0.0,
    "data": []
}

UKRAINE_ALARM_CACHE_TTL = 20.0  # 20 секунд — захист від 401 рейт-ліміту API

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

async def fetch_ukraine_alarm_data(force: bool = False) -> list[dict]:
    """
    Отримує офіційні дані про повітряні тривоги з UkraineAlarm API (Stfalcon / Ajax).
    Кешує результат на 20 секунд згідно з вимогами рейт-ліміту API.
    """
    global _UKRAINE_ALARM_CACHE
    now = time.time()
    if not force and (now - _UKRAINE_ALARM_CACHE["timestamp"] < UKRAINE_ALARM_CACHE_TTL) and _UKRAINE_ALARM_CACHE["data"]:
        return _UKRAINE_ALARM_CACHE["data"]

    headers = {
        "Authorization": UKRAINE_ALARM_API_KEY,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(UKRAINE_ALARM_URL, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        _UKRAINE_ALARM_CACHE["timestamp"] = now
                        _UKRAINE_ALARM_CACHE["data"] = data
                        return data
                elif resp.status == 401:
                    logger.debug("UkraineAlarm API 401 (rate-limit window active), serving from cache")
                else:
                    logger.warning(f"UkraineAlarm API status: {resp.status}")
    except Exception as e:
        logger.warning(f"Error fetching UkraineAlarm data: {e}")

    return _UKRAINE_ALARM_CACHE["data"]

async def fetch_all_alerts_data(force: bool = False) -> tuple[dict, dict, list]:
    """
    Зручний метод, який паралельно отримує дані з NEPTUN (тривоги + радари)
    та офіційного UkraineAlarm API (офіційні сирени).
    """
    alerts_data, threats_data = await fetch_neptun_data(force=force)
    ua_data = await fetch_ukraine_alarm_data(force=force)
    return alerts_data, threats_data, ua_data
