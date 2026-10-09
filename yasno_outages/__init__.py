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
    format_outage_caption,
    get_current_kyiv_time,
    get_current_state_and_countdown,
    generate_visual_timeline,
)
from yasno_outages.image_generator import generate_outage_card_image
from yasno_outages.geocoding import (
    reverse_coords_to_address,
    nudge_coords,
    resolve_group_from_coords,
    find_ukrainian_settlement,
)

__all__ = [
    "yasno_client",
    "YasnoClient",
    "format_outage_card",
    "format_outage_caption",
    "generate_outage_card_image",
    "reverse_coords_to_address",
    "nudge_coords",
    "resolve_group_from_coords",
    "find_ukrainian_settlement",
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
