import logging
import os
import secrets

import psycopg2
import psycopg2.extras
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ========================= CONFIG =========================

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("Token")

# Lấy chuỗi kết nối Postgres từ Railway
DATABASE_URL = os.getenv("DATABASE_URL")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))

APP_VERSION = "v3-getlink-fix-pg"  # dùng để check code đã lên chưa

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

UPLOAD_MODE_USERS = set()
FOLDER_NAME_WAIT_USERS = set()


# ========================= KEYBOARD =========================

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📁 Tạo thư mục mới"), KeyboardButton("/upload")],
        [KeyboardButton("/getlink"), KeyboardButton("/myfiles")],
        [KeyboardButton("/folders")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ========================= DATABASE (POSTGRES) =========================

def get_conn():
    """
    Kết nối Postgres dùng DATABASE_URL.
    Dùng RealDictCursor để row trả về có thể truy cập kiểu row["field"].
    """
    if not DATABASE_URL:
        raise RuntimeError("❌ Chưa thiết lập biến môi trường DATABASE_URL")

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # Lưu ý: dùng SERIAL cho Postgres, placeholder là %s
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE,
            full_name TEXT,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id SERIAL PRIMARY KEY,
            owner_telegram_id BIGINT,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_current_folder (
            id SERIAL PRIMARY KEY,
            owner_telegram_id BIGINT UNIQUE,
            folder_id INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id SERIAL PRIMARY KEY,
            file_unique_id TEXT UNIQUE,
            file_id TEXT,
            owner_telegram_id BIGINT,
            folder_id INTEGER,
            file_name TEXT,
            file_type TEXT,
            file_size BIGINT,
            mime_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS share_tokens (
            id SERIAL PRIMARY KEY,
            owner_telegram_id BIGINT,
            folder_id INTEGER,
            token TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
    logger.info("Database (Postgres) OK.")


def get_or_create_user(tg_user):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE telegram_id = %s", (tg_user.id,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row

    cur.execute(
        "INSERT INTO users (telegram_id, full_name, username) VALUES (%s, %s, %s)",
        (tg_user.id, tg_user.full_name, tg_user.username),
    )
    conn.commit()
    cur.execute("SELECT * FROM users WHERE telegram_id = %s", (tg_user.id,))
    row = cur.fetchone()
    conn.close()
    return row


def create_or_get_folder(owner_id, name):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM folders WHERE owner_telegram_id = %s AND name = %s",
        (owner_id, name),
    )
    row = cur.fetchone()
    if row:
        conn.close()
        return row

    cur.execute(
        "INSERT INTO folders (owner_telegram_id, name) VALUES (%s, %s)",
        (owner_id, name),
    )
    conn.commit()

    cur.execute(
        "SELECT * FROM folders WHERE owner_telegram_id = %s AND name = %s",
        (owner_id, name),
    )
    row = cur.fetchone()
    conn.close()
    return row


def set_current_folder(owner_id, folder_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO user_current_folder (owner_telegram_id, folder_id, updated_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (owner_telegram_id) DO UPDATE SET
            folder_id = EXCLUDED.folder_id,
            updated_at = EXCLUDED.updated_at
        """,
        (owner_id, folder_id),
    )
    conn.commit()
    conn.close()


def get_current_folder(owner_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT f.*
        FROM user_current_folder u
        JOIN folders f ON f.id = u.folder_id
        WHERE u.owner_telegram_id = %s
        """,
        (owner_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def ensure_current_folder(owner_id):
    folder = get_current_folder(owner_id)
    if folder:
        return folder

    folder = create_or_get_folder(owner_id, "Mặc định")
    set_current_folder(owner_id, folder["id"])
    return folder


def list_folders(owner_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM folders WHERE owner_telegram_id = %s ORDER BY created_at DESC",
        (owner_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def save_file(owner_id, folder_id, file_unique_id, file_id,
              file_name, file_type, file_size, mime_type):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO files
        (file_unique_id, file_id, owner_telegram_id, folder_id,
         file_name, file_type, file_size, mime_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (file_unique_id) DO NOTHING
        """,
        (
            file_unique_id,
            file_id,
            owner_id,
            folder_id,
            file_name,
            file_type,
            file_size,
            mime_type,
        ),
    )
    conn.commit()
    conn.close()


def get_share_token(owner_id, folder_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT token FROM share_tokens
        WHERE owner_telegram_id = %s AND folder_id = %s
        """,
        (owner_id, folder_id),
    )
    row = cur.fetchone()
    if row:
        conn.close()
        return row["token"]

    token = secrets.token_urlsafe(8)
    cur.execute(
        """
        INSERT INTO share_tokens (owner_telegram_id, folder_id, token)
        VALUES (%s, %s, %s)
        """,
        (owner_id, folder_id, token),
    )
    conn.commit()
    conn.close()
    return token


def get_owner_and_folder_by_token(token):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT owner_telegram_id, folder_id FROM share_tokens WHERE token = %s",
        (token,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, None
    return row["owner_telegram_id"], row["folder_id"]


def get_files_of_owner(owner_id, folder_id=None, limit=30):
    conn = get_conn()
    cur = conn.cursor()
    if folder_id:
        cur.execute(
            """
            SELECT * FROM files
            WHERE owner_telegram_id = %s AND folder_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (owner_id, folder_id, limit),
        )
    else:
        cur.execute(
            """
            SELECT * FROM files
            WHERE owner_telegram_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (owner_id, limit),
        )
    rows = cur.fetchall()
    conn.close()
    return rows


# ========================= TEXT =========================

WELCOME_TEXT = (
    "🌤 *Bot Lưu Trữ File*\n\n"
    "• Lưu hình ảnh, video, tài liệu, file bất kỳ.\n"
    "• Không lo mất dữ liệu (Postgres trên Railway).\n\n"
    "👉 Bấm *📁 Tạo thư mục mới* để tạo thư mục.\n"
    "👉 Dùng /upload để gửi file.\n"
    "👉 Dùng /getlink để lấy link chia sẻ.\n"
)


# ========================= HANDLERS =========================

async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Bot version: {APP_VERSION}")


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    real_username = context.bot.username
    await update.message.reply_text(
        "DEBUG INFO:\n"
        f"- bot.username (thật): {real_username}\n"
        f"- version: {APP_VERSION}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user)

    args = context.args
    if args:
        arg = args[0]
        if arg.startswith("share_"):
            token = arg[len("share_"):]
            owner_id, folder_id = get_owner_and_folder_by_token(token)
            if not owner_id:
                await update.message.reply_text("❌ Link chia sẻ không hợp lệ.")
                return

            files = get_files_of_owner(owner_id, folder_id=folder_id, limit=30)
            if not files:
                await update.message.reply_text("📂 Thư mục này chưa có file.")
                return

            text_lines = ["📂 *Danh sách file được chia sẻ:*\n"]
            for f in files:
                text_lines.append(
                    f"• {f['file_name']} — {f['file_size']} bytes"
                )

            await update.message.reply_text(
                "\n".join(text_lines),
                reply_markup=get_main_keyboard(),
                parse_mode="Markdown",
            )
            return

    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    folder = ensure_current_folder(user.id)
    UPLOAD_MODE_USERS.add(user.id)
    await update.message.reply_text(
        f"📁 Đang lưu vào thư mục: *{folder['name']}*\n"
        "➡ Gửi file cho bot.",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def new_folder_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    FOLDER_NAME_WAIT_USERS.add(user.id)
    await update.message.reply_text(
        "✏️ Nhập *tên thư mục mới* bạn muốn tạo:",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    if user.id in FOLDER_NAME_WAIT_USERS and not text.startswith("/"):
        FOLDER_NAME_WAIT_USERS.remove(user.id)

        folder = create_or_get_folder(user.id, text)
        set_current_folder(user.id, folder["id"])
        UPLOAD_MODE_USERS.add(user.id)

        await update.message.reply_text(
            f"📁 Đã tạo / chọn thư mục: *{text}*\n"
            "➡ Bây giờ hãy gửi file cho bot.",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown",
        )
        return


async def setfolder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Cách dùng:\n/setfolder Tên_thư_mục",
            reply_markup=get_main_keyboard(),
        )
        return

    name = " ".join(context.args).strip()
    folder = create_or_get_folder(user.id, name)
    set_current_folder(user.id, folder["id"])
    UPLOAD_MODE_USERS.add(user.id)

    await update.message.reply_text(
        f"📁 Đã chuyển sang thư mục: *{name}*",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def folders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    folders = list_folders(user.id)
    cur = get_current_folder(user.id)

    if not folders:
        await update.message.reply_text(
            "Bạn chưa có thư mục nào. Hãy bấm *📁 Tạo thư mục mới*.",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown",
        )
        return

    lines = ["📂 *Các thư mục của bạn:*\n"]
    for f in folders:
        mark = "⭐" if cur and cur["id"] == f["id"] else "•"
        lines.append(f"{mark} {f['name']} — {f['created_at']}")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def myfiles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    folder = ensure_current_folder(user.id)
    files = get_files_of_owner(user.id, folder_id=folder["id"], limit=30)

    if not files:
        await update.message.reply_text(
            f"Thư mục *{folder['name']}* chưa có file nào.",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown",
        )
        return

    lines = [
        f"📂 *30 file mới nhất trong thư mục {folder['name']}:*\n"
    ]
    for f in files:
        lines.append(f"• {f['file_name']} — {f['file_size']} bytes")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def getlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    folder = ensure_current_folder(user.id)
    token = get_share_token(user.id, folder["id"])

    real_username = context.bot.username  # ví dụ: 'luutruireng_bot'
    link = f"https://t.me/{real_username}?start=share_{token}"

    await update.message.reply_text(
        f"🔗 Link chia sẻ thư mục *{folder['name']}*:\n{link}",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user
    folder = ensure_current_folder(user.id)

    file_obj = None
    file_type = None
    file_name = None
    file_size = None
    mime_type = None

    if message.document:
        file_obj = message.document
        file_type = "document"
        file_name = file_obj.file_name
        file_size = file_obj.file_size
        mime_type = file_obj.mime_type
    elif message.photo:
        file_obj = message.photo[-1]
        file_type = "photo"
        file_name = "photo.jpg"
        file_size = file_obj.file_size
        mime_type = "image/jpeg"
    elif message.video:
        file_obj = message.video
        file_type = "video"
        file_name = "video.mp4"
        file_size = file_obj.file_size
        mime_type = "video/mp4"
    elif message.audio:
        file_obj = message.audio
        file_type = "audio"
        file_name = file_obj.file_name or "audio.mp3"
        file_size = file_obj.file_size
        mime_type = file_obj.mime_type
    else:
        return

    save_file(
        user.id,
        folder["id"],
        file_obj.file_unique_id,
        file_obj.file_id,
        file_name,
        file_type,
        file_size,
        mime_type,
    )

    await message.reply_text(
        f"✅ Đã lưu file vào thư mục *{folder['name']}*:\n• {file_name}",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Lệnh không tồn tại. Hãy dùng:\n"
        "/upload • /getlink • /myfiles • /folders • /version",
        reply_markup=get_main_keyboard(),
    )


# ========================= MAIN =========================

def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ Chưa thiết lập BOT_TOKEN hoặc Token.")
    if not DATABASE_URL:
        raise SystemExit("❌ Chưa thiết lập DATABASE_URL cho Postgres.")

    init_db()
    logger.info("Bot started with PostgreSQL.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("version", version_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("upload", upload_cmd))
    app.add_handler(CommandHandler("getlink", getlink_cmd))
    app.add_handler(CommandHandler("myfiles", myfiles_cmd))
    app.add_handler(CommandHandler("folders", folders_cmd))
    app.add_handler(CommandHandler("setfolder", setfolder_cmd))

    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^📁 Tạo thư mục mới$"),
            new_folder_button,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    file_filter = (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VIDEO
        | filters.AUDIO
    )
    app.add_handler(MessageHandler(file_filter, handle_file))

    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    app.run_polling()


if __name__ == "__main__":
    main()
