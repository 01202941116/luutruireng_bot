CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    full_name TEXT,
    username TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_telegram_id INTEGER,
    name TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_current_folder (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_telegram_id INTEGER UNIQUE,
    folder_id INTEGER,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_unique_id TEXT UNIQUE,
    file_id TEXT,
    owner_telegram_id INTEGER,
    folder_id INTEGER,
    file_name TEXT,
    file_type TEXT,
    file_size INTEGER,
    mime_type TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS share_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_telegram_id INTEGER,
    folder_id INTEGER,
    token TEXT UNIQUE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
