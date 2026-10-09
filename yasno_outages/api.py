"""Asynchronous API client for Yasno Outages with in-memory caching."""

import time
import logging
from datetime import datetime, date
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
)
from yasno_outages.models import (
    OutageSlot,
    DaySchedule,
    GroupSchedule,
    YasnoApiError,
    YasnoNotFoundError,
)

logger = logging.getLogger(__name__)

# In-memory cache structures: key -> (timestamp, data)
_CACHE: Dict[str, Tuple[float, Any]] = {}
PLANNED_CACHE_TTL = 180       # 3 minutes for planned schedules
REGIONS_CACHE_TTL = 3600      # 1 hour for regions
STREETS_CACHE_TTL = 600       # 10 minutes for street queries

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TelegramBot/2.0; +https://t.me/)"
}

class YasnoClient:
    """Async client interacting with Yasno public outages API."""

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
                    return await resp.json()
        except YasnoApiError:
            raise
        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
            raise YasnoApiError(f"Network error querying Yasno: {e}") from e

    async def get_regions(self, force: bool = False) -> List[Dict[str, Any]]:
        """Fetch available regions and their DSOs."""
        cache_key = "regions"
        now = time.time()
        if not force and cache_key in _CACHE:
            ts, data = _CACHE[cache_key]
            if now - ts < REGIONS_CACHE_TTL:
                return data

        try:
            raw_data = await self._fetch_json(REGIONS_ENDPOINT)
            if isinstance(raw_data, list) and len(raw_data) > 0:
                _CACHE[cache_key] = (now, raw_data)
                return raw_data
        except Exception as e:
            logger.warning(f"Failed to fetch regions from Yasno API ({e}), using default regions fallback.")
            return DEFAULT_REGIONS

        return DEFAULT_REGIONS

    async def get_raw_planned_outages(self, region_id: int, dso_id: int, force: bool = False) -> Dict[str, Any]:
        """Fetch all planned outages for a region and DSO."""
        cache_key = f"planned_{region_id}_{dso_id}"
        now = time.time()
        if not force and cache_key in _CACHE:
            ts, data = _CACHE[cache_key]
            if now - ts < PLANNED_CACHE_TTL:
                return data

        url = PLANNED_OUTAGES_ENDPOINT.format(region_id=region_id, dso_id=dso_id)
        data = await self._fetch_json(url)
        if isinstance(data, dict):
            _CACHE[cache_key] = (now, data)
            return data
        raise YasnoApiError("Invalid response format for planned outages")

    def _parse_day_data(self, day_data: Optional[Dict[str, Any]]) -> DaySchedule:
        """Parse today/tomorrow dictionary from Yasno response."""
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
        """Get parsed today and tomorrow schedule for a specific group."""
        raw_data = await self.get_raw_planned_outages(region_id, dso_id, force=force)
        if not raw_data or group not in raw_data:
            return None

        group_info = raw_data[group]
        today_sched = self._parse_day_data(group_info.get("today"))
        tomorrow_sched = self._parse_day_data(group_info.get("tomorrow"))

        updated_on = None
        if "updatedOn" in group_info and group_info["updatedOn"]:
            try:
                updated_on = datetime.fromisoformat(group_info["updatedOn"])
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
        """Get list of all group identifiers available for this DSO."""
        raw_data = await self.get_raw_planned_outages(region_id, dso_id)
        keys = list(raw_data.keys())
        # Sort naturally (e.g. 1.1, 1.2, 2.1, ...)
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
