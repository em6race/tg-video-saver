import sqlite3
import uuid
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

try:
    import aiosqlite
except ImportError:
    aiosqlite = None
    logger.warning("aiosqlite package not found. Async operations will fallback to sqlite3.")

DB_PATH = "cache.db"

def init_db():
    """Синхронна ініціалізація бази даних при запуску додатка."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS file_cache (
                url TEXT,
                type TEXT,
                file_id TEXT,
                PRIMARY KEY (url, type)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                invited_by INTEGER,
                joined_at TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invites (
                code TEXT PRIMARY KEY,
                created_by INTEGER,
                is_used INTEGER DEFAULT 0,
                used_by INTEGER,
                created_at TEXT,
                expires_at TEXT
            )
        ''')
        try:
            cursor.execute("ALTER TABLE invites ADD COLUMN expires_at TEXT")
        except Exception:
            pass

        cursor.execute("DELETE FROM invites WHERE expires_at IS NULL")
        cursor.execute("DELETE FROM invites WHERE is_used=0 AND expires_at < ?", (datetime.utcnow().isoformat(),))

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
                last_alert_msg_id INTEGER,
                updated_at TEXT
            )
        ''')
        try:
            cursor.execute("ALTER TABLE alert_subscriptions ADD COLUMN last_alert_msg_id INTEGER")
        except Exception:
            pass
        conn.commit()

# --- АСИНХРОННИЙ КЕШ ФАЙЛІВ ---

def _get_cached_file_id_sync(url: str, type: str) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT file_id FROM file_cache WHERE url=? AND type=?", (url, type))
        row = cursor.fetchone()
        return row[0] if row else None

def _set_cached_file_id_sync(url: str, type: str, file_id: str):
    if not file_id:
        return
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO file_cache (url, type, file_id)
            VALUES (?, ?, ?)
        ''', (url, type, file_id))
        conn.commit()

async def get_cached_file_id(url: str, type: str) -> str:
    if aiosqlite is None:
        return await asyncio.to_thread(_get_cached_file_id_sync, url, type)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT file_id FROM file_cache WHERE url=? AND type=?", (url, type)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def set_cached_file_id(url: str, type: str, file_id: str):
    if not file_id:
        return
    if aiosqlite is None:
        return await asyncio.to_thread(_set_cached_file_id_sync, url, type, file_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO file_cache (url, type, file_id)
            VALUES (?, ?, ?)
        ''', (url, type, file_id))
        await db.commit()

# --- АСИНХРОННИЙ ПРИВАТНИЙ ДОСТУП ТА ІНВАЙТИ ---

async def is_admin(user_id: int, admin_id_env: str = None) -> bool:
    if aiosqlite is None:
        return await asyncio.to_thread(is_admin_sync, user_id, admin_id_env)
    if admin_id_env and str(user_id) == str(admin_id_env).strip():
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM allowed_users WHERE invited_by = -1") as cursor:
            admin_row = await cursor.fetchone()
            if admin_row:
                return int(admin_row[0]) == int(user_id)
        if not admin_id_env:
            async with db.execute("SELECT COUNT(*) FROM allowed_users") as cursor:
                count_row = await cursor.fetchone()
                if count_row and count_row[0] == 0:
                    now = datetime.utcnow().isoformat()
                    await db.execute('''
                        INSERT INTO allowed_users (user_id, username, invited_by, joined_at)
                        VALUES (?, 'Admin', -1, ?)
                    ''', (int(user_id), now))
                    await db.commit()
                    return True
    return False

def is_admin_sync(user_id: int, admin_id_env: str = None) -> bool:
    if admin_id_env and str(user_id) == str(admin_id_env).strip():
        return True
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
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

async def is_user_allowed(user_id: int, admin_id_env: str = None) -> bool:
    if aiosqlite is None:
        return await asyncio.to_thread(is_user_allowed_sync, user_id, admin_id_env)
    if await is_admin(user_id, admin_id_env):
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM allowed_users WHERE user_id=?", (int(user_id),)) as cursor:
            return (await cursor.fetchone()) is not None

def is_user_allowed_sync(user_id: int, admin_id_env: str = None) -> bool:
    if is_admin_sync(user_id, admin_id_env):
        return True
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM allowed_users WHERE user_id=?", (int(user_id),))
        return cursor.fetchone() is not None

async def add_allowed_user(user_id: int, username: str = "", invited_by: int = 0):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT OR REPLACE INTO allowed_users (user_id, username, invited_by, joined_at)
            VALUES (?, ?, ?, ?)
        ''', (int(user_id), username or "", int(invited_by), now))
        await db.commit()

async def create_single_use_invite(created_by: int, duration_minutes: int = 30) -> tuple[str, str]:
    code = uuid.uuid4().hex[:12]
    now = datetime.utcnow()
    expires = now + timedelta(minutes=duration_minutes)
    now_str = now.isoformat()
    expires_str = expires.isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO invites (code, created_by, is_used, used_by, created_at, expires_at)
            VALUES (?, ?, 0, NULL, ?, ?)
        ''', (code, int(created_by), now_str, expires_str))
        await db.commit()
    return code, expires_str

async def redeem_invite(code: str, user_id: int, username: str = "") -> tuple[bool, str]:
    now = datetime.utcnow()
    now_str = now.isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT created_by, is_used, expires_at FROM invites WHERE code=?", (code,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False, "not_found"
            created_by, is_used, expires_at = row
            if is_used:
                return False, "already_used"

            if expires_at:
                try:
                    exp_dt = datetime.fromisoformat(expires_at)
                    if now > exp_dt:
                        return False, "expired"
                except Exception:
                    pass

        await db.execute('''
            UPDATE invites SET is_used=1, used_by=? WHERE code=?
        ''', (int(user_id), code))
        await db.execute('''
            INSERT OR REPLACE INTO allowed_users (user_id, username, invited_by, joined_at)
            VALUES (?, ?, ?, ?)
        ''', (int(user_id), username or "", created_by, now_str))
        await db.commit()
        return True, "ok"

async def clear_unused_invites() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM invites WHERE is_used=0")
        deleted_count = cursor.rowcount
        await db.commit()
        return deleted_count

async def get_allowed_users_list() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, joined_at FROM allowed_users ORDER BY joined_at DESC") as cursor:
            return await cursor.fetchall()

# --- АСИНХРОННІ ПІДПИСКИ НА ПОВІТРЯНІ ТРИВОГИ ---

async def set_awaiting_location(user_id: int, chat_id: int, awaiting: bool = True):
    if aiosqlite is None:
        return await asyncio.to_thread(set_awaiting_location_sync, user_id, chat_id, awaiting)
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM alert_subscriptions WHERE user_id=?", (int(user_id),)) as cursor:
            row = await cursor.fetchone()
            if row:
                await db.execute('''
                    UPDATE alert_subscriptions SET awaiting_location=?, chat_id=?, updated_at=?
                    WHERE user_id=?
                ''', (1 if awaiting else 0, int(chat_id), now, int(user_id)))
            else:
                await db.execute('''
                    INSERT INTO alert_subscriptions (user_id, chat_id, awaiting_location, enabled, updated_at)
                    VALUES (?, ?, ?, 0, ?)
                ''', (int(user_id), int(chat_id), 1 if awaiting else 0, now))
        await db.commit()

def set_awaiting_location_sync(user_id: int, chat_id: int, awaiting: bool = True):
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

async def save_user_location(user_id: int, chat_id: int, oblast: str, raion: str, locality: str, lat: float, lon: float):
    if aiosqlite is None:
        return await asyncio.to_thread(save_user_location_sync, user_id, chat_id, oblast, raion, locality, lat, lon)
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO alert_subscriptions
            (user_id, chat_id, oblast, raion, locality, lat, lon, awaiting_location, enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                oblast=excluded.oblast,
                raion=excluded.raion,
                locality=excluded.locality,
                lat=excluded.lat,
                lon=excluded.lon,
                awaiting_location=0,
                enabled=1,
                updated_at=excluded.updated_at
        ''', (int(user_id), int(chat_id) if chat_id else None, oblast, raion, locality, lat, lon, now))
        await db.commit()

def save_user_location_sync(user_id: int, chat_id: int, oblast: str, raion: str, locality: str, lat: float, lon: float):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO alert_subscriptions
            (user_id, chat_id, oblast, raion, locality, lat, lon, awaiting_location, enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                oblast=excluded.oblast,
                raion=excluded.raion,
                locality=excluded.locality,
                lat=excluded.lat,
                lon=excluded.lon,
                awaiting_location=0,
                enabled=1,
                updated_at=excluded.updated_at
        ''', (int(user_id), int(chat_id) if chat_id else None, oblast, raion, locality, lat, lon, now))
        conn.commit()

async def disable_user_alerts(user_id: int):
    if aiosqlite is None:
        return await asyncio.to_thread(disable_user_alerts_sync, user_id)
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            UPDATE alert_subscriptions SET enabled=0, awaiting_location=0, updated_at=?
            WHERE user_id=?
        ''', (now, int(user_id)))
        await db.commit()

def disable_user_alerts_sync(user_id: int):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE alert_subscriptions SET enabled=0, awaiting_location=0, updated_at=?
            WHERE user_id=?
        ''', (now, int(user_id)))
        conn.commit()

async def enable_user_alerts(user_id: int, chat_id: int = None) -> bool:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT oblast FROM alert_subscriptions WHERE user_id=?", (int(user_id),)) as cursor:
            row = await cursor.fetchone()
            if not row or not row[0]:
                return False
        if chat_id:
            await db.execute('''
                UPDATE alert_subscriptions SET enabled=1, awaiting_location=0, chat_id=?, updated_at=?
                WHERE user_id=?
            ''', (int(chat_id), now, int(user_id)))
        else:
            await db.execute('''
                UPDATE alert_subscriptions SET enabled=1, awaiting_location=0, updated_at=?
                WHERE user_id=?
            ''', (now, int(user_id)))
        await db.commit()
        return True

async def set_user_last_alert_msg_id(user_id: int, msg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE alert_subscriptions SET last_alert_msg_id=? WHERE user_id=?", (msg_id, int(user_id)))
        await db.commit()

async def get_user_subscription(user_id: int) -> dict:
    if aiosqlite is None:
        return await asyncio.to_thread(get_user_subscription_sync, user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT user_id, chat_id, oblast, raion, locality, lat, lon, awaiting_location, enabled, last_alert_msg_id
            FROM alert_subscriptions WHERE user_id=?
        ''', (int(user_id),)) as cursor:
            row = await cursor.fetchone()
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
                "enabled": bool(row[8]),
                "last_alert_msg_id": row[9]
            }

def get_user_subscription_sync(user_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, chat_id, oblast, raion, locality, lat, lon, awaiting_location, enabled, last_alert_msg_id
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
            "enabled": bool(row[8]),
            "last_alert_msg_id": row[9]
        }

async def get_all_enabled_subscriptions() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT user_id, chat_id, oblast, raion, locality, lat, lon, last_alert_msg_id
            FROM alert_subscriptions WHERE enabled=1 AND oblast IS NOT NULL
        ''') as cursor:
            rows = await cursor.fetchall()
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
                    "last_alert_msg_id": r[7]
                })
            return result

init_db()
