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


def _agent_session_key(chat_id: str, backend: str) -> str:
    return f"{backend}:{chat_id}"


def get_agent_session(chat_id: str, backend: str) -> str | None:
    """获取指定后端的 session_id，Claude 兼容旧 key。"""
    session_id = get_session(_agent_session_key(chat_id, backend))
    if session_id is None and backend == "claude":
        session_id = get_session(chat_id)
    return session_id


def save_agent_session(chat_id: str, backend: str, session_id: str):
    """保存指定后端的 session_id。"""
    save_session(_agent_session_key(chat_id, backend), session_id)


def delete_agent_session(chat_id: str, backend: str):
    """删除指定后端的 session_id。"""
    delete_session(_agent_session_key(chat_id, backend))
    if backend == "claude":
        delete_session(chat_id)


def _ensure_settings_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id TEXT,
            key TEXT,
            value TEXT,
            PRIMARY KEY (chat_id, key)
        )
    """)
    conn.commit()


def get_chat_setting(chat_id: str, key: str) -> str | None:
    """获取聊天级配置。"""
    conn = _get_conn()
    _ensure_settings_table(conn)
    cursor = conn.execute(
        "SELECT value FROM chat_settings WHERE chat_id = ? AND key = ?",
        (chat_id, key),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def save_chat_setting(chat_id: str, key: str, value: str):
    """保存聊天级配置。"""
    conn = _get_conn()
    _ensure_settings_table(conn)
    conn.execute("""
        INSERT OR REPLACE INTO chat_settings (chat_id, key, value)
        VALUES (?, ?, ?)
    """, (chat_id, key, value))
    conn.commit()
    conn.close()


def delete_chat_setting(chat_id: str, key: str):
    """删除聊天级配置。"""
    conn = _get_conn()
    _ensure_settings_table(conn)
    conn.execute(
        "DELETE FROM chat_settings WHERE chat_id = ? AND key = ?",
        (chat_id, key),
    )
    conn.commit()
    conn.close()


def delete_session(chat_id: str):
    """删除 session_id"""
    conn = _get_conn()
    conn.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
