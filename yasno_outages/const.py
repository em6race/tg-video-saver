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

# Pre-defined known regions & DSOs for quick access across all of Ukraine
DEFAULT_REGIONS = [
    {
        "id": 25,
        "name": "м. Київ",
        "short_name": "Київ",
        "dsos": [
            {"id": 902, "name": "ДТЕК Київські електромережі"}
        ],
        "feed_url": "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv.json",
    },
    {
        "id": 26,
        "name": "Київська область",
        "short_name": "Київщина",
        "dsos": [
            {"id": 901, "name": "ДТЕК Київські регіональні електромережі"}
        ],
        "feed_url": "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json",
    },
    {
        "id": 3,
        "name": "Дніпропетровська область",
        "short_name": "Дніпропетровщина",
        "dsos": [
            {"id": 301, "name": "ДТЕК Дніпровські електромережі (ДнЕМ)"},
            {"id": 303, "name": "ЦЕК"}
        ],
        "feed_url": "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/dnipro.json",
    },
    {
        "id": 51,
        "name": "Одеська область",
        "short_name": "Одещина",
        "dsos": [
            {"id": 5101, "name": "ДТЕК Одеські електромережі"}
        ],
        "feed_url": "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/odesa.json",
    },
    {
        "id": 46,
        "name": "Львівська область",
        "short_name": "Львівщина",
        "dsos": [
            {"id": 4601, "name": "ПрАТ Львівобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Lvivoblenerho.json",
    },
    {
        "id": 63,
        "name": "Харківська область",
        "short_name": "Харківщина",
        "dsos": [
            {"id": 6301, "name": "АТ Харківобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Kharkivoblenerho.json",
    },
    {
        "id": 53,
        "name": "Полтавська область",
        "short_name": "Полтавщина",
        "dsos": [
            {"id": 5301, "name": "АТ Полтаваобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Poltavaoblenergo.json",
    },
    {
        "id": 71,
        "name": "Черкаська область",
        "short_name": "Черкащина",
        "dsos": [
            {"id": 7101, "name": "ПАТ Черкасиобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Cherkasyoblenergo.json",
    },
    {
        "id": 74,
        "name": "Чернігівська область",
        "short_name": "Чернігівщина",
        "dsos": [
            {"id": 7401, "name": "АТ Чернігівобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Chernihivoblenergo.json",
    },
    {
        "id": 5,
        "name": "Вінницька область",
        "short_name": "Вінниччина",
        "dsos": [
            {"id": 501, "name": "АТ Вінницяобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/olnet93/gpv-voe-vinnytsia/main/data/Vinnytsiaoblenerho.json",
    },
    {
        "id": 18,
        "name": "Житомирська область",
        "short_name": "Житомирщина",
        "dsos": [
            {"id": 1801, "name": "АТ Житомиробленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Zhytomyroblenergo.json",
    },
    {
        "id": 23,
        "name": "Запорізька область",
        "short_name": "Запоріжжя",
        "dsos": [
            {"id": 2301, "name": "ПАТ Запоріжжяобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Zaporizhzhiaoblenergo.json",
    },
    {
        "id": 260,
        "name": "Івано-Франківська область",
        "short_name": "Прикарпаття",
        "dsos": [
            {"id": 2601, "name": "АТ Прикарпаттяобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Prykarpattiaoblenerho.json",
    },
    {
        "id": 21,
        "name": "Закарпатська область",
        "short_name": "Закарпаття",
        "dsos": [
            {"id": 2101, "name": "ПрАТ Закарпаттяобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Zakarpattiaoblenerho.json",
    },
    {
        "id": 56,
        "name": "Рівненська область",
        "short_name": "Рівненщина",
        "dsos": [
            {"id": 5601, "name": "ПрАТ Рівнеобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Rivneoblenergo.json",
    },
    {
        "id": 61,
        "name": "Тернопільська область",
        "short_name": "Тернопільщина",
        "dsos": [
            {"id": 6101, "name": "ВАТ Тернопільобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Ternopiloblenerho.json",
    },
    {
        "id": 68,
        "name": "Хмельницька область",
        "short_name": "Хмельниччина",
        "dsos": [
            {"id": 6801, "name": "АТ Хмельницькобленерго"}
        ],
        "feed_url": "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Khmelnytskoblenerho.json",
    },
    {
        "id": 7,
        "name": "Волинська область",
        "short_name": "Волинь",
        "dsos": [
            {"id": 701, "name": "ПрАТ Волиньобленерго"}
        ],
        "feed_url": None,
    },
    {
        "id": 59,
        "name": "Сумська область",
        "short_name": "Сумщина",
        "dsos": [
            {"id": 5901, "name": "АТ Сумиобленерго"}
        ],
        "feed_url": None,
    },
    {
        "id": 35,
        "name": "Кіровоградська область",
        "short_name": "Кіровоградщина",
        "dsos": [
            {"id": 3501, "name": "ПрАТ Кіровоградобленерго"}
        ],
        "feed_url": None,
    },
    {
        "id": 48,
        "name": "Миколаївська область",
        "short_name": "Миколаївщина",
        "dsos": [
            {"id": 4801, "name": "АТ Миколаївобленерго"}
        ],
        "feed_url": None,
    },
    {
        "id": 73,
        "name": "Чернівецька область",
        "short_name": "Буковина",
        "dsos": [
            {"id": 7301, "name": "АТ Чернівціобленерго"}
        ],
        "feed_url": None,
    },
    {
        "id": 14,
        "name": "Донецька область",
        "short_name": "Донеччина",
        "dsos": [
            {"id": 1401, "name": "ДТЕК Донецькі електромережі"}
        ],
        "feed_url": None,
    },
    {
        "id": 65,
        "name": "Херсонська область",
        "short_name": "Херсонщина",
        "dsos": [
            {"id": 6501, "name": "АТ Херсонобленерго"}
        ],
        "feed_url": None,
    },
]

# Quick mapping: region_id -> feed_url
REGION_FEEDS = {
    r["id"]: r["feed_url"] for r in DEFAULT_REGIONS if r.get("feed_url")
}

