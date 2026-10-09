import asyncio
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
                expires_at TEXT,
                chat_id INTEGER,
                msg_id INTEGER,
                max_uses INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0
            )
        ''')
        for col_sql in (
            "ALTER TABLE invites ADD COLUMN expires_at TEXT",
            "ALTER TABLE invites ADD COLUMN chat_id INTEGER",
            "ALTER TABLE invites ADD COLUMN msg_id INTEGER",
            "ALTER TABLE invites ADD COLUMN max_uses INTEGER DEFAULT 1",
            "ALTER TABLE invites ADD COLUMN used_count INTEGER DEFAULT 0",
        ):
            try:
                cursor.execute(col_sql)
            except Exception:
                pass

        cursor.execute(
            "DELETE FROM invites WHERE is_used=0 AND expires_at IS NOT NULL AND expires_at != '' AND expires_at < ?",
            (datetime.utcnow().isoformat(),)
        )

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
        try:
            cursor.execute("ALTER TABLE alert_subscriptions ADD COLUMN sound_enabled INTEGER DEFAULT 1")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE alert_subscriptions ADD COLUMN putin_check_enabled INTEGER DEFAULT 1")
        except Exception:
            pass
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS alert_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                created_at TEXT
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_alert_messages_user ON alert_messages(user_id)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS outage_subscriptions (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                region_id INTEGER DEFAULT 25,
                region_name TEXT DEFAULT 'Київ',
                dso_id INTEGER DEFAULT 902,
                dso_name TEXT DEFAULT 'ДТЕК Київські електромережі',
                group_name TEXT,
                address TEXT,
                notifications_enabled INTEGER DEFAULT 1,
                last_msg_id INTEGER,
                last_schedule_hash TEXT,
                awaiting_input TEXT,
                updated_at TEXT
            )
        ''')
        conn.commit()

# --- АСИНХРОННИЙ КЕШ ФАЙЛІВ (ДЛЯ ПЛАГІНА МЕДІА) ---

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

async def create_single_use_invite(created_by: int, duration_minutes: int | None = 30, max_uses: int = 1) -> tuple[str, str | None]:
    code = uuid.uuid4().hex[:12]
    now = datetime.utcnow()
    now_str = now.isoformat()
    if duration_minutes is not None and duration_minutes > 0:
        expires_str = (now + timedelta(minutes=duration_minutes)).isoformat()
    else:
        expires_str = None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO invites (code, created_by, is_used, used_by, created_at, expires_at, max_uses, used_count)
            VALUES (?, ?, 0, NULL, ?, ?, ?, 0)
        ''', (code, int(created_by), now_str, expires_str, int(max_uses)))
        await db.commit()
    return code, expires_str

async def set_invite_message_info(code: str, chat_id: int, msg_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            UPDATE invites SET chat_id=?, msg_id=? WHERE code=?
        ''', (int(chat_id), int(msg_id), code))
        await db.commit()

async def redeem_invite(code: str, user_id: int, username: str = "") -> tuple[bool, str, dict]:
    now = datetime.utcnow()
    now_str = now.isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT created_by, is_used, expires_at, chat_id, msg_id, max_uses, used_count FROM invites WHERE code=?",
            (code,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False, "not_found", {}
            created_by, is_used, expires_at, chat_id, msg_id, max_uses, used_count = row
            max_uses = int(max_uses) if max_uses is not None else 1
            used_count = int(used_count) if used_count is not None else 0
            invite_meta = {
                "code": code,
                "created_by": created_by,
                "chat_id": chat_id,
                "msg_id": msg_id,
                "max_uses": max_uses,
                "used_count": used_count,
                "expires_at": expires_at,
                "fully_used": False,
            }
            if is_used or (max_uses > 0 and used_count >= max_uses):
                return False, "already_used", invite_meta

            if expires_at:
                try:
                    exp_dt = datetime.fromisoformat(expires_at)
                    if now > exp_dt:
                        return False, "expired", invite_meta
                except Exception:
                    pass

        new_used_count = used_count + 1
        fully_used = 1 if (max_uses > 0 and new_used_count >= max_uses) else 0
        invite_meta["used_count"] = new_used_count
        invite_meta["fully_used"] = bool(fully_used)

        await db.execute('''
            UPDATE invites SET used_count=?, is_used=?, used_by=? WHERE code=?
        ''', (new_used_count, fully_used, int(user_id), code))
        await db.execute('''
            INSERT OR REPLACE INTO allowed_users (user_id, username, invited_by, joined_at)
            VALUES (?, ?, ?, ?)
        ''', (int(user_id), username or "", created_by, now_str))
        await db.commit()
        return True, "ok", invite_meta

async def get_active_invites() -> list[dict]:
    now_iso = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Спочатку видалимо ті, в яких вже вийшов термін дії
        await db.execute(
            "DELETE FROM invites WHERE is_used=0 AND expires_at IS NOT NULL AND expires_at != '' AND expires_at < ?",
            (now_iso,)
        )
        await db.commit()

        async with db.execute('''
            SELECT code, created_by, created_at, expires_at, chat_id, msg_id, max_uses, used_count
            FROM invites
            WHERE is_used=0 AND (expires_at IS NULL OR expires_at = '' OR expires_at >= ?)
            ORDER BY created_at DESC
        ''', (now_iso,)) as cursor:
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                result.append({
                    "code": r[0],
                    "created_by": r[1],
                    "created_at": r[2],
                    "expires_at": r[3],
                    "chat_id": r[4],
                    "msg_id": r[5],
                    "max_uses": int(r[6]) if r[6] is not None else 1,
                    "used_count": int(r[7]) if r[7] is not None else 0,
                })
            return result

async def revoke_invite(code: str) -> tuple[bool, tuple[int, int] | None]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id, msg_id FROM invites WHERE code=?", (code,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False, None
            msg_pair = (int(row[0]), int(row[1])) if (row[0] is not None and row[1] is not None) else None
        await db.execute("DELETE FROM invites WHERE code=?", (code,))
        await db.commit()
        return True, msg_pair

async def clear_unused_invites() -> tuple[int, list[tuple[int, int]]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id, msg_id FROM invites WHERE is_used=0 AND chat_id IS NOT NULL AND msg_id IS NOT NULL") as cursor:
            rows = await cursor.fetchall()
            msgs_to_delete = [(int(r[0]), int(r[1])) for r in rows]
        cursor = await db.execute("DELETE FROM invites WHERE is_used=0")
        deleted_count = cursor.rowcount
        await db.commit()
        return deleted_count, msgs_to_delete

async def get_allowed_users_list() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT u.user_id, u.username, u.joined_at, s.oblast, s.locality, s.enabled
            FROM allowed_users u
            LEFT JOIN alert_subscriptions s ON u.user_id = s.user_id
            ORDER BY u.joined_at DESC
        ''') as cursor:
            return await cursor.fetchall()

async def remove_allowed_user(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM allowed_users WHERE user_id=?", (int(user_id),))
        await db.execute("UPDATE alert_subscriptions SET enabled=0 WHERE user_id=?", (int(user_id),))
        await db.commit()

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

def set_user_last_alert_msg_id_sync(user_id: int, msg_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE alert_subscriptions SET last_alert_msg_id=? WHERE user_id=?", (msg_id, int(user_id)))
        conn.commit()

async def set_user_last_alert_msg_id(user_id: int, msg_id: int):
    if aiosqlite is None:
        return await asyncio.to_thread(set_user_last_alert_msg_id_sync, user_id, msg_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE alert_subscriptions SET last_alert_msg_id=? WHERE user_id=?", (msg_id, int(user_id)))
        await db.commit()

def add_alert_message_record_sync(user_id: int, chat_id: int, message_id: int):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO alert_messages (user_id, chat_id, message_id, created_at)
            VALUES (?, ?, ?, ?)
        ''', (int(user_id), int(chat_id), int(message_id), now))
        conn.commit()

async def add_alert_message_record(user_id: int, chat_id: int, message_id: int):
    if aiosqlite is None:
        return await asyncio.to_thread(add_alert_message_record_sync, user_id, chat_id, message_id)
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO alert_messages (user_id, chat_id, message_id, created_at)
            VALUES (?, ?, ?, ?)
        ''', (int(user_id), int(chat_id), int(message_id), now))
        await db.commit()

def get_user_alert_message_ids_sync(user_id: int) -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT message_id FROM alert_messages WHERE user_id=? ORDER BY id ASC", (int(user_id),))
        rows = cursor.fetchall()
        return [r[0] for r in rows if r[0]]

async def get_user_alert_message_ids(user_id: int) -> list[int]:
    if aiosqlite is None:
        return await asyncio.to_thread(get_user_alert_message_ids_sync, user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT message_id FROM alert_messages WHERE user_id=? ORDER BY id ASC", (int(user_id),)) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows if r[0]]

def delete_alert_message_records_sync(user_id: int, message_ids: list[int] = None):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        if message_ids:
            placeholders = ",".join("?" for _ in message_ids)
            cursor.execute(f"DELETE FROM alert_messages WHERE user_id=? AND message_id IN ({placeholders})", [int(user_id)] + [int(m) for m in message_ids])
        else:
            cursor.execute("DELETE FROM alert_messages WHERE user_id=?", (int(user_id),))
        conn.commit()

async def delete_alert_message_records(user_id: int, message_ids: list[int] = None):
    if aiosqlite is None:
        return await asyncio.to_thread(delete_alert_message_records_sync, user_id, message_ids)
    async with aiosqlite.connect(DB_PATH) as db:
        if message_ids:
            placeholders = ",".join("?" for _ in message_ids)
            await db.execute(f"DELETE FROM alert_messages WHERE user_id=? AND message_id IN ({placeholders})", [int(user_id)] + [int(m) for m in message_ids])
        else:
            await db.execute("DELETE FROM alert_messages WHERE user_id=?", (int(user_id),))
        await db.commit()

async def get_user_subscription(user_id: int) -> dict:
    if aiosqlite is None:
        return await asyncio.to_thread(get_user_subscription_sync, user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT user_id, chat_id, oblast, raion, locality, lat, lon, awaiting_location, enabled, last_alert_msg_id, sound_enabled, putin_check_enabled
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
                "last_alert_msg_id": row[9],
                "sound_enabled": bool(row[10] if len(row) > 10 and row[10] is not None else 1),
                "putin_check_enabled": bool(row[11] if len(row) > 11 and row[11] is not None else 1)
            }

def get_user_subscription_sync(user_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, chat_id, oblast, raion, locality, lat, lon, awaiting_location, enabled, last_alert_msg_id, sound_enabled, putin_check_enabled
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
            "last_alert_msg_id": row[9],
            "sound_enabled": bool(row[10] if len(row) > 10 and row[10] is not None else 1),
            "putin_check_enabled": bool(row[11] if len(row) > 11 and row[11] is not None else 1)
        }

def get_all_enabled_subscriptions_sync() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, chat_id, oblast, raion, locality, lat, lon, last_alert_msg_id, sound_enabled, putin_check_enabled
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
                "last_alert_msg_id": r[7],
                "sound_enabled": bool(r[8] if len(r) > 8 and r[8] is not None else 1),
                "putin_check_enabled": bool(r[9] if len(r) > 9 and r[9] is not None else 1)
            })
        return result

async def get_all_enabled_subscriptions() -> list[dict]:
    if aiosqlite is None:
        return await asyncio.to_thread(get_all_enabled_subscriptions_sync)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT user_id, chat_id, oblast, raion, locality, lat, lon, last_alert_msg_id, sound_enabled, putin_check_enabled
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
                    "last_alert_msg_id": r[7],
                    "sound_enabled": bool(r[8] if len(r) > 8 and r[8] is not None else 1),
                    "putin_check_enabled": bool(r[9] if len(r) > 9 and r[9] is not None else 1)
                })
            return result

async def toggle_user_sound(user_id: int) -> bool:
    if aiosqlite is None:
        return await asyncio.to_thread(toggle_user_sound_sync, user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT sound_enabled FROM alert_subscriptions WHERE user_id=?", (int(user_id),)) as cursor:
            row = await cursor.fetchone()
            current_sound = bool(row[0] if row and row[0] is not None else 1)
        new_sound = 0 if current_sound else 1
        await db.execute("UPDATE alert_subscriptions SET sound_enabled=? WHERE user_id=?", (new_sound, int(user_id)))
        await db.commit()
        return bool(new_sound)

def toggle_user_sound_sync(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT sound_enabled FROM alert_subscriptions WHERE user_id=?", (int(user_id),))
        row = cursor.fetchone()
        current_sound = bool(row[0] if row and row[0] is not None else 1)
        new_sound = 0 if current_sound else 1
        cursor.execute("UPDATE alert_subscriptions SET sound_enabled=? WHERE user_id=?", (new_sound, int(user_id)))
        conn.commit()
        return bool(new_sound)

async def toggle_user_putin(user_id: int) -> bool:
    if aiosqlite is None:
        return await asyncio.to_thread(toggle_user_putin_sync, user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT putin_check_enabled FROM alert_subscriptions WHERE user_id=?", (int(user_id),)) as cursor:
            row = await cursor.fetchone()
            current_val = bool(row[0] if row and row[0] is not None else 1)
        new_val = 0 if current_val else 1
        await db.execute("UPDATE alert_subscriptions SET putin_check_enabled=? WHERE user_id=?", (new_val, int(user_id)))
        await db.commit()
        return bool(new_val)

def toggle_user_putin_sync(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT putin_check_enabled FROM alert_subscriptions WHERE user_id=?", (int(user_id),))
        row = cursor.fetchone()
        current_val = bool(row[0] if row and row[0] is not None else 1)
        new_val = 0 if current_val else 1
        cursor.execute("UPDATE alert_subscriptions SET putin_check_enabled=? WHERE user_id=?", (new_val, int(user_id)))
        conn.commit()
        return bool(new_val)

# --- ПЛАГІН ГРАФІКІВ ВІДКЛЮЧЕНЬ СВІТЛА (YASNO / DTEK) ---

def get_outage_subscription_sync(user_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, chat_id, region_id, region_name, dso_id, dso_name,
                   group_name, address, notifications_enabled, last_msg_id,
                   last_schedule_hash, awaiting_input, updated_at
            FROM outage_subscriptions WHERE user_id=?
        ''', (int(user_id),))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "user_id": row[0],
            "chat_id": row[1],
            "region_id": row[2],
            "region_name": row[3],
            "dso_id": row[4],
            "dso_name": row[5],
            "group_name": row[6],
            "address": row[7],
            "notifications_enabled": bool(row[8]),
            "last_msg_id": row[9],
            "last_schedule_hash": row[10],
            "awaiting_input": row[11],
            "updated_at": row[12]
        }

async def get_outage_subscription(user_id: int) -> dict:
    if aiosqlite is None:
        return await asyncio.to_thread(get_outage_subscription_sync, user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT user_id, chat_id, region_id, region_name, dso_id, dso_name,
                   group_name, address, notifications_enabled, last_msg_id,
                   last_schedule_hash, awaiting_input, updated_at
            FROM outage_subscriptions WHERE user_id=?
        ''', (int(user_id),)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "user_id": row[0],
                "chat_id": row[1],
                "region_id": row[2],
                "region_name": row[3],
                "dso_id": row[4],
                "dso_name": row[5],
                "group_name": row[6],
                "address": row[7],
                "notifications_enabled": bool(row[8]),
                "last_msg_id": row[9],
                "last_schedule_hash": row[10],
                "awaiting_input": row[11],
                "updated_at": row[12]
            }

def save_outage_group_sync(
    user_id: int,
    chat_id: int,
    region_id: int,
    region_name: str,
    dso_id: int,
    dso_name: str,
    group_name: str,
    address: str = None
):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO outage_subscriptions (
                user_id, chat_id, region_id, region_name, dso_id, dso_name,
                group_name, address, awaiting_input, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                region_id=excluded.region_id,
                region_name=excluded.region_name,
                dso_id=excluded.dso_id,
                dso_name=excluded.dso_name,
                group_name=excluded.group_name,
                address=coalesce(excluded.address, outage_subscriptions.address),
                awaiting_input=NULL,
                updated_at=excluded.updated_at
        ''', (
            int(user_id), int(chat_id), int(region_id), str(region_name),
            int(dso_id), str(dso_name), str(group_name), address, now
        ))
        conn.commit()

async def save_outage_group(
    user_id: int,
    chat_id: int,
    region_id: int,
    region_name: str,
    dso_id: int,
    dso_name: str,
    group_name: str,
    address: str = None
):
    if aiosqlite is None:
        return await asyncio.to_thread(
            save_outage_group_sync, user_id, chat_id, region_id, region_name, dso_id, dso_name, group_name, address
        )
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO outage_subscriptions (
                user_id, chat_id, region_id, region_name, dso_id, dso_name,
                group_name, address, awaiting_input, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                region_id=excluded.region_id,
                region_name=excluded.region_name,
                dso_id=excluded.dso_id,
                dso_name=excluded.dso_name,
                group_name=excluded.group_name,
                address=coalesce(excluded.address, outage_subscriptions.address),
                awaiting_input=NULL,
                updated_at=excluded.updated_at
        ''', (
            int(user_id), int(chat_id), int(region_id), str(region_name),
            int(dso_id), str(dso_name), str(group_name), address, now
        ))
        await db.commit()

async def set_outage_awaiting(user_id: int, chat_id: int, awaiting_input: str = None):
    now = datetime.utcnow().isoformat()
    if aiosqlite is None:
        def _sync():
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO outage_subscriptions (user_id, chat_id, awaiting_input, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        chat_id=excluded.chat_id,
                        awaiting_input=excluded.awaiting_input,
                        updated_at=excluded.updated_at
                ''', (int(user_id), int(chat_id), awaiting_input, now))
                conn.commit()
        return await asyncio.to_thread(_sync)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO outage_subscriptions (user_id, chat_id, awaiting_input, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                awaiting_input=excluded.awaiting_input,
                updated_at=excluded.updated_at
        ''', (int(user_id), int(chat_id), awaiting_input, now))
        await db.commit()

async def toggle_outage_notifications(user_id: int) -> bool:
    sub = await get_outage_subscription(user_id)
    curr = sub.get("notifications_enabled", True) if sub else True
    new_val = 0 if curr else 1
    if aiosqlite is None:
        def _sync():
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE outage_subscriptions SET notifications_enabled=? WHERE user_id=?", (new_val, int(user_id)))
                conn.commit()
        await asyncio.to_thread(_sync)
        return bool(new_val)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE outage_subscriptions SET notifications_enabled=? WHERE user_id=?", (new_val, int(user_id)))
        await db.commit()
    return bool(new_val)

async def set_outage_last_msg_id(user_id: int, msg_id: int):
    if aiosqlite is None:
        def _sync():
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE outage_subscriptions SET last_msg_id=? WHERE user_id=?", (int(msg_id) if msg_id else None, int(user_id)))
                conn.commit()
        return await asyncio.to_thread(_sync)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE outage_subscriptions SET last_msg_id=? WHERE user_id=?", (int(msg_id) if msg_id else None, int(user_id)))
        await db.commit()

async def update_outage_schedule_hash(user_id: int, schedule_hash: str):
    if aiosqlite is None:
        def _sync():
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE outage_subscriptions SET last_schedule_hash=? WHERE user_id=?", (schedule_hash, int(user_id)))
                conn.commit()
        return await asyncio.to_thread(_sync)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE outage_subscriptions SET last_schedule_hash=? WHERE user_id=?", (schedule_hash, int(user_id)))
        await db.commit()

async def get_all_enabled_outage_subscriptions() -> list[dict]:
    if aiosqlite is None:
        def _sync():
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT user_id, chat_id, region_id, region_name, dso_id, dso_name,
                           group_name, address, notifications_enabled, last_msg_id,
                           last_schedule_hash
                    FROM outage_subscriptions
                    WHERE notifications_enabled=1 AND group_name IS NOT NULL
                ''')
                rows = cursor.fetchall()
                res = []
                for r in rows:
                    res.append({
                        "user_id": r[0], "chat_id": r[1], "region_id": r[2], "region_name": r[3],
                        "dso_id": r[4], "dso_name": r[5], "group_name": r[6], "address": r[7],
                        "notifications_enabled": bool(r[8]), "last_msg_id": r[9], "last_schedule_hash": r[10]
                    })
                return res
        return await asyncio.to_thread(_sync)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT user_id, chat_id, region_id, region_name, dso_id, dso_name,
                   group_name, address, notifications_enabled, last_msg_id,
                   last_schedule_hash
            FROM outage_subscriptions
            WHERE notifications_enabled=1 AND group_name IS NOT NULL
        ''') as cursor:
            rows = await cursor.fetchall()
            res = []
            for r in rows:
                res.append({
                    "user_id": r[0], "chat_id": r[1], "region_id": r[2], "region_name": r[3],
                    "dso_id": r[4], "dso_name": r[5], "group_name": r[6], "address": r[7],
                    "notifications_enabled": bool(r[8]), "last_msg_id": r[9], "last_schedule_hash": r[10]
                })
            return res

init_db()
