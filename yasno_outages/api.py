"""Asynchronous API client for Yasno Outages with in-memory caching."""

import time
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Tuple
import aiohttp

from yasno_outages.const import (
    REGIONS_ENDPOINT,
    PLANNED_OUTAGES_ENDPOINT,
    PROBABLE_OUTAGES_ENDPOINT,
    CITIES_ENDPOINT,
    STREETS_ENDPOINT,
    HOUSES_ENDPOINT,
    GROUP_BY_ADDRESS_ENDPOINT,
    DEFAULT_REGIONS,
    REGION_FEEDS,
)
from yasno_outages.models import (
    OutageSlot,
    DaySchedule,
    GroupSchedule,
    YasnoApiError,
    YasnoNotFoundError,
)

TZ_UA = ZoneInfo("Europe/Kyiv")

logger = logging.getLogger(__name__)

# In-memory cache structures: key -> (timestamp, data)
_CACHE: Dict[str, Tuple[float, Any]] = {}
PLANNED_CACHE_TTL = 180       # 3 minutes for planned schedules
REGIONS_CACHE_TTL = 3600      # 1 hour for regions
STREETS_CACHE_TTL = 600       # 10 minutes for street queries

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TelegramBot/2.0; +https://t.me/)"
}

def hours_to_slots(hours_dict: Dict[str, str]) -> List[OutageSlot]:
    """Convert a 24-hour dictionary from Oblenergo format to OutageSlot list."""
    intervals = []
    for h in range(1, 25):
        val = str(hours_dict.get(str(h), "yes")).lower().strip()
        if val == "no":
            t1, t2 = "Definite", "Definite"
        elif val == "maybe":
            t1, t2 = "Probable", "Probable"
        elif val in ("first", "mfirst"):
            t1 = "Definite" if val == "first" else "Probable"
            t2 = "NotPlanned"
        elif val in ("second", "msecond"):
            t1 = "NotPlanned"
            t2 = "Definite" if val == "second" else "Probable"
        else:
            t1, t2 = "NotPlanned", "NotPlanned"
        intervals.extend([t1, t2])

    slots = []
    curr_type = intervals[0]
    start_min = 0
    for i in range(1, 48):
        if intervals[i] != curr_type:
            slots.append(OutageSlot(start=start_min, end=i * 30, slot_type=curr_type))
            curr_type = intervals[i]
            start_min = i * 30
    slots.append(OutageSlot(start=start_min, end=1440, slot_type=curr_type))
    return slots

def parse_feed_to_raw(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Oblenergo open data JSON feed into Yasno-compatible raw planned structure."""
    now_kyiv = datetime.now(TZ_UA)
    today_date = now_kyiv.date()
    tomorrow_date = today_date + timedelta(days=1)

    today_midnight = datetime.combine(today_date, datetime.min.time(), tzinfo=TZ_UA)
    tomorrow_midnight = datetime.combine(tomorrow_date, datetime.min.time(), tzinfo=TZ_UA)
    today_ts = str(int(today_midnight.timestamp()))
    tomorrow_ts = str(int(tomorrow_midnight.timestamp()))

    fact = data.get("fact", {})
    preset = data.get("preset", {})
    fact_data = fact.get("data") if isinstance(fact.get("data"), dict) else {}
    preset_data = preset.get("data") if isinstance(preset.get("data"), dict) else {}

    today_weekday = str(now_kyiv.isoweekday())
    tomorrow_weekday = str(tomorrow_date.isoweekday())

    all_groups = set()
    if today_ts in fact_data:
        all_groups.update(fact_data[today_ts].keys())
    if tomorrow_ts in fact_data:
        all_groups.update(fact_data[tomorrow_ts].keys())
    all_groups.update(preset_data.keys())

    if not all_groups:
        for g in range(1, 7):
            all_groups.add(f"GPV{g}.1")
            all_groups.add(f"GPV{g}.2")

    result = {}
    for grp in all_groups:
        # Today
        today_hours = None
        if today_ts in fact_data and grp in fact_data[today_ts]:
            today_hours = fact_data[today_ts][grp]
        elif grp in preset_data and today_weekday in preset_data[grp]:
            today_hours = preset_data[grp][today_weekday]

        if today_hours:
            slots = hours_to_slots(today_hours)
            has_outages = any(s.slot_type in ("Definite", "Probable") for s in slots)
            raw_slots = [{"start": s.start, "end": s.end, "type": s.slot_type} for s in slots]
            today_obj = {
                "status": "ScheduleApplies" if has_outages else "NoOutages",
                "date": today_date.isoformat(),
                "slots": raw_slots,
            }
        else:
            today_obj = {
                "status": "WaitingForSchedule",
                "date": today_date.isoformat(),
                "slots": [],
            }

        # Tomorrow
        tomorrow_hours = None
        if tomorrow_ts in fact_data and grp in fact_data[tomorrow_ts]:
            tomorrow_hours = fact_data[tomorrow_ts][grp]
        elif grp in preset_data and tomorrow_weekday in preset_data[grp]:
            tomorrow_hours = preset_data[grp][tomorrow_weekday]

        if tomorrow_hours:
            slots_tom = hours_to_slots(tomorrow_hours)
            has_outages_tom = any(s.slot_type in ("Definite", "Probable") for s in slots_tom)
            raw_slots_tom = [{"start": s.start, "end": s.end, "type": s.slot_type} for s in slots_tom]
            tomorrow_obj = {
                "status": "ScheduleApplies" if has_outages_tom else "NoOutages",
                "date": tomorrow_date.isoformat(),
                "slots": raw_slots_tom,
            }
        else:
            tomorrow_obj = {
                "status": "WaitingForSchedule",
                "date": tomorrow_date.isoformat(),
                "slots": [],
            }

        grp_data = {
            "today": today_obj,
            "tomorrow": tomorrow_obj,
            "updatedOn": fact.get("update"),
        }
        result[grp] = grp_data
        if grp.startswith("GPV"):
            short_key = grp[3:]
            result[short_key] = grp_data

    return result

def generate_empty_schedules() -> Dict[str, Any]:
    now_kyiv = datetime.now(TZ_UA)
    today_date = now_kyiv.date()
    tomorrow_date = today_date + timedelta(days=1)
    res = {}
    for g in range(1, 7):
        for sub in (1, 2):
            key = f"{g}.{sub}"
            gpv = f"GPV{key}"
            entry = {
                "today": {
                    "status": "WaitingForSchedule",
                    "date": today_date.isoformat(),
                    "slots": [],
                },
                "tomorrow": {
                    "status": "WaitingForSchedule",
                    "date": tomorrow_date.isoformat(),
                    "slots": [],
                },
                "updatedOn": None,
            }
            res[key] = entry
            res[gpv] = entry
    return res

class YasnoClient:
    """Async client interacting with Yasno public outages API and All-Ukraine Oblenergo feeds."""

    def __init__(self, timeout_seconds: int = 12):
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def _fetch_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Internal helper to make GET requests and parse JSON."""
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, headers=HEADERS) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status == 404:
                        raise YasnoNotFoundError(f"Resource not found: {url}")
                    if resp.status != 200:
                        body = await resp.text()
                        raise YasnoApiError(f"HTTP {resp.status} from {url}: {body[:200]}")
                    return await resp.json(content_type=None)
        except YasnoApiError:
            raise
        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
            raise YasnoApiError(f"Network error querying Yasno: {e}") from e

    async def get_regions(self, force: bool = False) -> List[Dict[str, Any]]:
        """Fetch available regions across Ukraine."""
        return DEFAULT_REGIONS

    async def get_raw_planned_outages(self, region_id: int, dso_id: int, force: bool = False) -> Dict[str, Any]:
        """Fetch all planned outages for a region and DSO across Ukraine."""
        cache_key = f"planned_{region_id}_{dso_id}"
        now = time.time()
        if not force and cache_key in _CACHE:
            ts, data = _CACHE[cache_key]
            if now - ts < PLANNED_CACHE_TTL:
                return data

        # 1. Для Києва (25) та Дніпропетровщини (3) пробуємо напряму Yasno API
        if region_id in (25, 3):
            try:
                url = PLANNED_OUTAGES_ENDPOINT.format(region_id=region_id, dso_id=dso_id)
                data = await self._fetch_json(url)
                if isinstance(data, dict) and len(data) > 0:
                    _CACHE[cache_key] = (now, data)
                    return data
            except Exception as e:
                logger.warning(f"Yasno API failed for region {region_id}/{dso_id} ({e}), falling back to open feeds.")

        # 2. Пробуємо відкриті JSON фіди обленерго (Baskerville42, yaroslav2901, olnet93)
        feed_url = REGION_FEEDS.get(region_id)
        if feed_url:
            try:
                feed_data = await self._fetch_json(feed_url)
                if isinstance(feed_data, dict):
                    parsed_data = parse_feed_to_raw(feed_data)
                    if parsed_data:
                        _CACHE[cache_key] = (now, parsed_data)
                        return parsed_data
            except Exception as e:
                logger.warning(f"Failed to fetch open data feed for region {region_id} ({feed_url}): {e}")

        # 3. Дефолтний розклад для регіонів без активних фідів (черги 1.1 - 6.2)
        fallback_data = generate_empty_schedules()
        _CACHE[cache_key] = (now, fallback_data)
        return fallback_data

    def _parse_day_data(self, day_data: Optional[Dict[str, Any]]) -> DaySchedule:
        """Parse today/tomorrow dictionary from response."""
        if not day_data or not isinstance(day_data, dict):
            return DaySchedule(day_date=None, status="WaitingForSchedule", slots=[])

        status = day_data.get("status", "WaitingForSchedule")
        raw_date_str = day_data.get("date", "")
        day_date = None
        if raw_date_str:
            try:
                day_date = datetime.fromisoformat(raw_date_str).date()
            except Exception:
                pass

        slots = []
        raw_slots = day_data.get("slots", [])
        if isinstance(raw_slots, list):
            for s in raw_slots:
                try:
                    slots.append(OutageSlot(
                        start=int(s["start"]),
                        end=int(s["end"]),
                        slot_type=str(s.get("type", "NotPlanned"))
                    ))
                except (KeyError, ValueError):
                    continue

        return DaySchedule(day_date=day_date, status=status, slots=slots, raw_date_str=raw_date_str)

    async def get_group_schedule(self, region_id: int, dso_id: int, group: str, force: bool = False) -> Optional[GroupSchedule]:
        """Get parsed today and tomorrow schedule for a specific group across Ukraine."""
        raw_data = await self.get_raw_planned_outages(region_id, dso_id, force=force)
        if not raw_data:
            return None

        # Look up group with aliases (e.g. 1.1, GPV1.1, 1)
        group_info = (
            raw_data.get(group)
            or raw_data.get(f"GPV{group}")
            or raw_data.get(group.replace("GPV", ""))
            or raw_data.get(f"{group}.1")
            or raw_data.get(f"GPV{group}.1")
        )
        if not group_info:
            for k, val in raw_data.items():
                if k.startswith(group) or group.startswith(k):
                    group_info = val
                    break

        if not group_info:
            return None

        today_sched = self._parse_day_data(group_info.get("today"))
        tomorrow_sched = self._parse_day_data(group_info.get("tomorrow"))

        updated_on = None
        if "updatedOn" in group_info and group_info["updatedOn"]:
            raw_up = str(group_info["updatedOn"])
            try:
                updated_on = datetime.fromisoformat(raw_up)
            except Exception:
                for fmt in ("%d.%m.%Y %H:%M", "%H:%M %d.%m.%Y"):
                    try:
                        updated_on = datetime.strptime(raw_up, fmt).replace(tzinfo=TZ_UA)
                        break
                    except Exception:
                        pass

        return GroupSchedule(
            group=group,
            region_id=region_id,
            dso_id=dso_id,
            today=today_sched,
            tomorrow=tomorrow_sched,
            updated_on=updated_on
        )

    async def get_available_groups(self, region_id: int, dso_id: int) -> List[str]:
        """Get list of clean group identifiers available for this DSO across Ukraine."""
        raw_data = await self.get_raw_planned_outages(region_id, dso_id)
        if not raw_data:
            return ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "5.1", "5.2", "6.1", "6.2"]

        keys = set()
        for k in raw_data.keys():
            if k.startswith("GPV"):
                clean = k[3:]
                keys.add(clean)
            else:
                keys.add(k)

        def sort_key(val: str):
            parts = val.split(".")
            res = []
            for p in parts:
                res.append(int(p) if p.isdigit() else p)
            return res

        try:
            return sorted(keys, key=sort_key)
        except Exception:
            return sorted(keys)

    async def get_cities(self, region_id: int, dso_id: int) -> List[Dict[str, Any]]:
        """Fetch all cities/settlements for a region and DSO."""
        cache_key = f"cities_{region_id}_{dso_id}"
        now = time.time()
        if cache_key in _CACHE:
            ts, data = _CACHE[cache_key]
            if now - ts < 3600:
                return data

        params = {"regionId": region_id, "dsoId": dso_id}
        try:
            data = await self._fetch_json(CITIES_ENDPOINT, params=params)
            if isinstance(data, list):
                _CACHE[cache_key] = (now, data)
                return data
        except Exception as e:
            logger.warning(f"Error fetching cities for {region_id}/{dso_id}: {e}")
        return []

    async def search_streets(
        self,
        region_id: int,
        dso_id: int,
        query: str,
        city_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Search streets in region and DSO by text query."""
        query = query.strip()
        if not query:
            return []

        cache_key = f"streets_{region_id}_{dso_id}_{city_id}_{query.lower()}"
        now = time.time()
        if cache_key in _CACHE:
            ts, data = _CACHE[cache_key]
            if now - ts < STREETS_CACHE_TTL:
                return data

        params: Dict[str, Any] = {
            "regionId": region_id,
            "dsoId": dso_id,
            "query": query,
        }
        if city_id is not None:
            params["cityId"] = city_id

        data = await self._fetch_json(STREETS_ENDPOINT, params=params)
        if isinstance(data, list):
            _CACHE[cache_key] = (now, data)
            return data
        return []

    async def search_houses(self, region_id: int, dso_id: int, street_id: int, query: str) -> List[Dict[str, Any]]:
        """Search houses in street by number/query."""
        params = {
            "regionId": region_id,
            "dsoId": dso_id,
            "streetId": street_id,
            "query": query.strip(),
        }
        data = await self._fetch_json(HOUSES_ENDPOINT, params=params)
        if isinstance(data, list):
            return data
        return []

    async def resolve_group_by_address(
        self,
        region_id: int,
        dso_id: int,
        street_id: int,
        house_id: int,
        city_id: Optional[int] = None,
    ) -> Optional[str]:
        """Resolve exact group string (e.g. '20.1') by address components."""
        params: Dict[str, Any] = {
            "regionId": region_id,
            "dsoId": dso_id,
            "streetId": street_id,
            "houseId": house_id,
        }
        if city_id is not None:
            params["cityId"] = city_id

        data = await self._fetch_json(GROUP_BY_ADDRESS_ENDPOINT, params=params)
        if isinstance(data, dict):
            grp = data.get("group")
            subgrp = data.get("subgroup")
            if grp is not None and subgrp is not None:
                return f"{grp}.{subgrp}"
            elif grp is not None:
                return str(grp)
        return None

# Singleton instance
yasno_client = YasnoClient()
