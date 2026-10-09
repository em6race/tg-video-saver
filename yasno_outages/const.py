"""Constants and endpoints for Yasno Outages API."""

from typing import Final

# API Endpoints
REGIONS_ENDPOINT: Final = (
    "https://app.yasno.ua/api/blackout-service/public/shutdowns/addresses/v2/regions"
)
PLANNED_OUTAGES_ENDPOINT: Final = (
    "https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/{region_id}/dsos/{dso_id}/planned-outages"
)
PROBABLE_OUTAGES_ENDPOINT: Final = (
    "https://app.yasno.ua/api/blackout-service/public/shutdowns/probable-outages"
)
CITIES_ENDPOINT: Final = (
    "https://app.yasno.ua/api/blackout-service/public/shutdowns/addresses/v2/cities"
)
STREETS_ENDPOINT: Final = (
    "https://app.yasno.ua/api/blackout-service/public/shutdowns/addresses/v2/streets"
)
HOUSES_ENDPOINT: Final = (
    "https://app.yasno.ua/api/blackout-service/public/shutdowns/addresses/v2/houses"
)
GROUP_BY_ADDRESS_ENDPOINT: Final = (
    "https://app.yasno.ua/api/blackout-service/public/shutdowns/addresses/v2/group"
)

# Event Names
EVENT_NAME_OUTAGE: Final = "Definite"
EVENT_NAME_NOT_PLANNED: Final = "NotPlanned"
EVENT_NAME_PROBABLE: Final = "Probable"

# Status values from Yasno
API_STATUS_NO_OUTAGES: Final = "NoOutages"
API_STATUS_SCHEDULE_APPLIES: Final = "ScheduleApplies"
API_STATUS_WAITING_FOR_SCHEDULE: Final = "WaitingForSchedule"
API_STATUS_EMERGENCY_SHUTDOWNS: Final = "EmergencyShutdowns"

# Pre-defined known regions & DSOs for quick access
DEFAULT_REGIONS = [
    {
        "id": 25,
        "name": "Київ",
        "dsos": [
            {"id": 902, "name": "ДТЕК Київські електромережі"}
        ]
    },
    {
        "id": 3,
        "name": "Дніпро",
        "dsos": [
            {"id": 301, "name": "ДТЕК Дніпровські електромережі (ДнЕМ)"},
            {"id": 303, "name": "ЦЕК"}
        ]
    }
]
