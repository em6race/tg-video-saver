from dataclasses import dataclass, field, asdict
from typing import Optional, List

@dataclass
class ThreatItem:
    id: Optional[str]
    type: str
    title: str
    explanation: str
    distance_km: Optional[int]
    advisory: bool
    heading: Optional[int]
    direction_name: Optional[str]
    direction_arrow: Optional[str]
    relative_dir_name: Optional[str]
    relative_dir_arrow: Optional[str]
    is_towards_user: bool
    confidence: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ThreatItem":
        return cls(
            id=data.get("id"),
            type=data.get("type", "unknown"),
            title=data.get("title", "Повітряна ціль"),
            explanation=data.get("explanation", ""),
            distance_km=data.get("distance_km"),
            advisory=bool(data.get("advisory", False)),
            heading=data.get("heading"),
            direction_name=data.get("direction_name"),
            direction_arrow=data.get("direction_arrow"),
            relative_dir_name=data.get("relative_dir_name"),
            relative_dir_arrow=data.get("relative_dir_arrow"),
            is_towards_user=bool(data.get("is_towards_user", False)),
            confidence=data.get("confidence")
        )

@dataclass
class AlertStateData:
    active: bool
    level: str
    reasons: List[str]
    since: Optional[str]
    matched_names: List[str]
    nearby_threats: List[ThreatItem] = field(default_factory=list)
    is_dorozvidka: bool = False

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "level": self.level,
            "reasons": self.reasons,
            "since": self.since,
            "matched_names": self.matched_names,
            "nearby_threats": [t.to_dict() if isinstance(t, ThreatItem) else t for t in self.nearby_threats],
            "is_dorozvidka": self.is_dorozvidka
        }

UKRAINE_OBLASTS = [
    "м. Київ",
    "Київська область",
    "Вінницька область",
    "Волинська область",
    "Дніпропетровська область",
    "Донецька область",
    "Житомирська область",
    "Закарпатська область",
    "Запорізька область",
    "Івано-Франківська область",
    "Кіровоградська область",
    "Львівська область",
    "Миколаївська область",
    "Одеська область",
    "Полтавська область",
    "Рівненська область",
    "Сумська область",
    "Тернопільська область",
    "Харківська область",
    "Херсонська область",
    "Хмельницька область",
    "Черкаська область",
    "Чернівецька область",
    "Чернігівська область",
]
