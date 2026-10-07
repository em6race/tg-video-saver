"""
Shared runtime state for air raid alerts monitoring and user cooldowns.
"""

_user_last_alert_state = {}         # user_id -> рівень ("green", "yellow", "red")
_user_last_threat_signature = {}    # user_id -> кортеж-сигнатура загроз
_user_last_auto_edit_time = {}      # user_id -> час останнього редагування ботом
_user_last_manual_refresh = {}      # user_id -> час останнього запиту користувача
_user_last_putin_date = {}          # user_id -> дата останнього оновлення статусу путіна ("ДД.ММ.РРРР")
