"""
SQLite 存储 chat_id -> session_id 映射
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "sessions.db"


def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            chat_id TEXT PRIMARY KEY,
            session_id TEXT
        )
    """)
    conn.commit()
    return conn


def get_session(chat_id: str) -> str | None:
    """获取 session_id"""
    conn = _get_conn()
    cursor = conn.execute("SELECT session_id FROM sessions WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def save_session(chat_id: str, session_id: str):
    """保存 session_id"""
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO sessions (chat_id, session_id)
        VALUES (?, ?)
    """, (chat_id, session_id))
    conn.commit()
    conn.close()


def delete_session(chat_id: str):
    """删除 session_id"""
    conn = _get_conn()
    conn.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
