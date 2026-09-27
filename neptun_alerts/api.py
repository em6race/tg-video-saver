import time
import aiohttp
import logging

logger = logging.getLogger(__name__)

NEPTUN_ALERTS_URL = "https://neptun.in.ua/api/v1/alerts"
NEPTUN_THREATS_URL = "https://neptun.in.ua/api/v1/threats"

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
