import sqlite3
import uuid
from datetime import datetime

DB_PATH = "cache.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Таблиця кешу медіа-файлів
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS file_cache (
                url TEXT,
                type TEXT,
                file_id TEXT,
                PRIMARY KEY (url, type)
            )
        ''')
        # Таблиця дозволених користувачів (приватний режим)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                invited_by INTEGER,
                joined_at TEXT
            )
        ''')
        # Таблиця одноразових інвайт-кодів
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invites (
                code TEXT PRIMARY KEY,
                created_by INTEGER,
                is_used INTEGER DEFAULT 0,
                used_by INTEGER,
                created_at TEXT
            )
        ''')
        # Таблиця підписок на повітряні тривоги та геолокації
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS alert_subscriptions (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                oblast TEXT,
                raion TEXT,
                locality TEXT,
                lat REAL,
                lon REAL,
                awaiting_location INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 0,
                updated_at TEXT
            )
        ''')
        conn.commit()

# --- КЕШ ФАЙЛІВ ---

def get_cached_file_id(url: str, type: str) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT file_id FROM file_cache WHERE url=? AND type=?", (url, type))
        row = cursor.fetchone()
        if row:
            return row[0]
        return None

def set_cached_file_id(url: str, type: str, file_id: str):
    if not file_id:
        return
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO file_cache (url, type, file_id)
            VALUES (?, ?, ?)
        ''', (url, type, file_id))
        conn.commit()

# --- ПРИВАТНИЙ ДОСТУП ТА ІНВАЙТИ ---

def is_admin(user_id: int, admin_id_env: str = None) -> bool:
    if admin_id_env and str(user_id) == str(admin_id_env).strip():
        return True
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Якщо в таблиці allowed_users ще немає жодного запису і ADMIN_ID не заданий у .env,
        # перший користувач автоматично стає власником (invited_by = -1)
        cursor.execute("SELECT user_id FROM allowed_users WHERE invited_by = -1")
        admin_row = cursor.fetchone()
        if admin_row:
            return int(admin_row[0]) == int(user_id)
        if not admin_id_env:
            cursor.execute("SELECT COUNT(*) FROM allowed_users")
            count = cursor.fetchone()[0]
            if count == 0:
                now = datetime.utcnow().isoformat()
                cursor.execute('''
                    INSERT INTO allowed_users (user_id, username, invited_by, joined_at)
                    VALUES (?, 'Admin', -1, ?)
                ''', (int(user_id), now))
                conn.commit()
                return True
    return False

def is_user_allowed(user_id: int, admin_id_env: str = None) -> bool:
    if is_admin(user_id, admin_id_env):
        return True
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM allowed_users WHERE user_id=?", (int(user_id),))
        return cursor.fetchone() is not None

def add_allowed_user(user_id: int, username: str = "", invited_by: int = 0):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO allowed_users (user_id, username, invited_by, joined_at)
            VALUES (?, ?, ?, ?)
        ''', (int(user_id), username or "", int(invited_by), now))
        conn.commit()

def create_single_use_invite(created_by: int) -> str:
    code = uuid.uuid4().hex[:12]
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO invites (code, created_by, is_used, used_by, created_at)
            VALUES (?, ?, 0, NULL, ?)
        ''', (code, int(created_by), now))
        conn.commit()
    return code

def redeem_invite(code: str, user_id: int, username: str = "") -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT created_by, is_used FROM invites WHERE code=?", (code,))
        row = cursor.fetchone()
        if not row:
            return False
        created_by, is_used = row
        if is_used:
            return False
        now = datetime.utcnow().isoformat()
        cursor.execute('''
            UPDATE invites SET is_used=1, used_by=? WHERE code=?
        ''', (int(user_id), code))
        cursor.execute('''
            INSERT OR REPLACE INTO allowed_users (user_id, username, invited_by, joined_at)
            VALUES (?, ?, ?, ?)
        ''', (int(user_id), username or "", created_by, now))
        conn.commit()
        return True

def get_allowed_users_list() -> list:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, joined_at FROM allowed_users ORDER BY joined_at DESC")
        return cursor.fetchall()

# --- ПІДПИСКИ НА ПОВІТРЯНІ ТРИВОГИ ---

def set_awaiting_location(user_id: int, chat_id: int, awaiting: bool = True):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM alert_subscriptions WHERE user_id=?", (int(user_id),))
        if cursor.fetchone():
            cursor.execute('''
                UPDATE alert_subscriptions SET awaiting_location=?, chat_id=?, updated_at=?
                WHERE user_id=?
            ''', (1 if awaiting else 0, int(chat_id), now, int(user_id)))
        else:
            cursor.execute('''
                INSERT INTO alert_subscriptions (user_id, chat_id, awaiting_location, enabled, updated_at)
                VALUES (?, ?, ?, 0, ?)
            ''', (int(user_id), int(chat_id), 1 if awaiting else 0, now))
        conn.commit()

def save_user_location(user_id: int, chat_id: int, oblast: str, raion: str, locality: str, lat: float, lon: float):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO alert_subscriptions
            (user_id, chat_id, oblast, raion, locality, lat, lon, awaiting_location, enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?)
        ''', (int(user_id), int(chat_id), oblast, raion, locality, lat, lon, now))
        conn.commit()

def disable_user_alerts(user_id: int):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE alert_subscriptions SET enabled=0, awaiting_location=0, updated_at=?
            WHERE user_id=?
        ''', (now, int(user_id)))
        conn.commit()

def enable_user_alerts(user_id: int, chat_id: int = None) -> bool:
    """Увімкнути сповіщення про тривоги, якщо область вже була встановлена раніше."""
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT oblast FROM alert_subscriptions WHERE user_id=?", (int(user_id),))
        row = cursor.fetchone()
        if not row or not row[0]:
            return False
        if chat_id:
            cursor.execute('''
                UPDATE alert_subscriptions SET enabled=1, awaiting_location=0, chat_id=?, updated_at=?
                WHERE user_id=?
            ''', (int(chat_id), now, int(user_id)))
        else:
            cursor.execute('''
                UPDATE alert_subscriptions SET enabled=1, awaiting_location=0, updated_at=?
                WHERE user_id=?
            ''', (now, int(user_id)))
        conn.commit()
        return True


def get_user_subscription(user_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, chat_id, oblast, raion, locality, lat, lon, awaiting_location, enabled
            FROM alert_subscriptions WHERE user_id=?
        ''', (int(user_id),))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "user_id": row[0],
            "chat_id": row[1],
            "oblast": row[2],
            "raion": row[3],
            "locality": row[4],
            "lat": row[5],
            "lon": row[6],
            "awaiting_location": bool(row[7]),
            "enabled": bool(row[8])
        }

def get_all_enabled_subscriptions() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, chat_id, oblast, raion, locality, lat, lon
            FROM alert_subscriptions WHERE enabled=1 AND oblast IS NOT NULL
        ''')
        rows = cursor.fetchall()
        result = []
        for r in rows:
            result.append({
                "user_id": r[0],
                "chat_id": r[1],
                "oblast": r[2],
                "raion": r[3],
                "locality": r[4],
                "lat": r[5],
                "lon": r[6],
            })
        return result

init_db()
