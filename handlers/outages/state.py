"""Ephemeral state for Outages handlers."""

from typing import Dict

# User ID -> timestamp of last manual refresh
_user_last_power_refresh: Dict[int, float] = {}

# User ID -> currently viewed day ('today' or 'tomorrow')
_user_last_view_day: Dict[int, str] = {}

# User ID -> last notified upcoming outage slot start time
_user_last_upcoming_notified: Dict[int, int] = {}
