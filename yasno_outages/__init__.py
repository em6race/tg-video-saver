"""Yasno Outages package."""

from yasno_outages.const import (
    REGIONS_ENDPOINT,
    PLANNED_OUTAGES_ENDPOINT,
    PROBABLE_OUTAGES_ENDPOINT,
    DEFAULT_REGIONS,
)
from yasno_outages.models import (
    OutageSlot,
    DaySchedule,
    GroupSchedule,
    YasnoApiError,
    YasnoNotFoundError,
)
from yasno_outages.api import yasno_client, YasnoClient
from yasno_outages.formatter import (
    format_outage_card,
    get_current_kyiv_time,
    get_current_state_and_countdown,
    generate_visual_timeline,
)

__all__ = [
    "yasno_client",
    "YasnoClient",
    "format_outage_card",
    "get_current_kyiv_time",
    "get_current_state_and_countdown",
    "generate_visual_timeline",
    "OutageSlot",
    "DaySchedule",
    "GroupSchedule",
    "YasnoApiError",
    "YasnoNotFoundError",
    "DEFAULT_REGIONS",
]
