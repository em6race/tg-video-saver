import sqlite3
import os

DB_PATH = "cache.db"

def init_db():
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
        conn.commit()

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

init_db()
