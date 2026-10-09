"""Data models for Yasno Outages."""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional, List

class YasnoApiError(Exception):
    """Raised when Yasno API request fails."""
    pass

class YasnoNotFoundError(YasnoApiError):
    """Raised when Yasno API returns 404."""
    pass

@dataclass
class OutageSlot:
    """Represents a time slot in minutes from midnight (0 to 1440)."""
    start: int          # Minutes from midnight (e.g. 480 = 08:00)
    end: int            # Minutes from midnight (e.g. 690 = 11:30)
    slot_type: str      # 'Definite', 'NotPlanned', 'Probable'

    @property
    def start_time_str(self) -> str:
        h = self.start // 60
        m = self.start % 60
        return f"{h:02d}:{m:02d}"

    @property
    def end_time_str(self) -> str:
        h = self.end // 60
        m = self.end % 60
        if h == 24 and m == 0:
            return "24:00"
        return f"{h:02d}:{m:02d}"

    @property
    def duration_minutes(self) -> int:
        return max(0, self.end - self.start)

@dataclass
class DaySchedule:
    """Represents the schedule for a single day."""
    day_date: Optional[date]
    status: str                         # 'ScheduleApplies', 'NoOutages', 'WaitingForSchedule', 'EmergencyShutdowns'
    slots: List[OutageSlot]
    raw_date_str: str = ""

    @property
    def is_schedule_available(self) -> bool:
        return self.status == "ScheduleApplies" and len(self.slots) > 0

    @property
    def is_emergency(self) -> bool:
        return self.status == "EmergencyShutdowns"

    @property
    def is_no_outages(self) -> bool:
        return self.status == "NoOutages"

    @property
    def total_outage_minutes(self) -> int:
        return sum(s.duration_minutes for s in self.slots if s.slot_type == "Definite")

    @property
    def total_probable_minutes(self) -> int:
        return sum(s.duration_minutes for s in self.slots if s.slot_type == "Probable")

@dataclass
class GroupSchedule:
    """Schedule data for a specific group (today and tomorrow)."""
    group: str
    region_id: int
    dso_id: int
    today: DaySchedule
    tomorrow: DaySchedule
    updated_on: Optional[datetime] = None
