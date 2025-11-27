
import sqlite3
from pathlib import Path

DB_PATH = Path("files.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_approved INTEGER DEFAULT 0
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_telegram_id INTEGER,
            name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_telegram_id INTEGER,
            folder_id INTEGER,
            file_type TEXT,
            file_unique_id TEXT,
            file_id TEXT,
            filename TEXT,
            file_bytes BLOB,
            file_size INTEGER,
            mime_type TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()
    conn.close()


def upsert_user(telegram_id, username, first_name, last_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (telegram_id, username, first_name, last_name, is_approved)
        VALUES (?, ?, ?, ?, 0)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name
        """,
        (telegram_id, username, first_name, last_name),
    )
    conn.commit()
    conn.close()


def get_user_by_telegram_id(telegram_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return row


def set_user_approved(telegram_id, approved: bool):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET is_approved = ? WHERE telegram_id = ?",
        (1 if approved else 0, telegram_id),
    )
    conn.commit()
    conn.close()


def insert_file(
    owner_telegram_id,
    folder_id,
    file_type,
    file_unique_id,
    file_id,
    filename,
    file_bytes,
    file_size,
    mime_type,
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO files (
            owner_telegram_id, folder_id, file_type, file_unique_id,
            file_id, filename, file_bytes, file_size, mime_type
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            owner_telegram_id,
            folder_id,
            file_type,
            file_unique_id,
            file_id,
            filename,
            file_bytes,
            file_size,
            mime_type,
        ),
    )
    file_db_id = cur.lastrowid
    conn.commit()
    conn.close()
    return file_db_id


def get_file_by_id(file_db_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM files WHERE id = ?", (file_db_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_last_file_by_owner(owner_telegram_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM files WHERE owner_telegram_id = ? ORDER BY id DESC LIMIT 1",
        (owner_telegram_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_or_create_folder(owner_telegram_id, name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM folders WHERE owner_telegram_id = ? AND name = ?",
        (owner_telegram_id, name),
    )
    row = cur.fetchone()
    if row:
        folder_id = row["id"]
    else:
        cur.execute(
            "INSERT INTO folders (owner_telegram_id, name) VALUES (?, ?)",
            (owner_telegram_id, name),
        )
        folder_id = cur.lastrowid
        conn.commit()
    conn.close()
    return folder_id


def get_folder_by_id(folder_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM folders WHERE id = ?", (folder_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_files_by_folder(folder_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM files WHERE folder_id = ? ORDER BY id ASC",
        (folder_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_folders_by_owner(owner_telegram_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM folders WHERE owner_telegram_id = ? ORDER BY id ASC",
        (owner_telegram_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def search_folders(owner_telegram_id, keyword):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM folders
        WHERE owner_telegram_id = ? AND name LIKE ?
        ORDER BY id ASC
        """,
        (owner_telegram_id, f"%{keyword}%"),
    )
    rows = cur.fetchall()
    conn.close()
    return rows
