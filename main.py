import logging
import os
import sqlite3
import secrets
from datetime import datetime

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------- CONFIG -----------------
# Railway: có thể dùng BOT_TOKEN hoặc Token
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("Token")

# Username bot KHÔNG có @, ví dụ: luutruireng_bot
BOT_USERNAME = os.getenv("BOT_USERNAME", "YOUR_BOT_USERNAME")

# File SQLite để lưu dữ liệu
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

# ID Telegram của bạn (admin) – không bắt buộc
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Lưu những user đang ở chế độ upload
UPLOAD_MODE_USERS = set()


# ----------------- KEYBOARD -----------------
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📁 Tạo thư mục mới"), KeyboardButton("/upload")],
        [KeyboardButton("/getlink"), KeyboardButton("/myfiles")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ----------------- DATABASE -----------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # Người dùng
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,
            full_name TEXT,
            username TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Thư mục
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

    # Thư mục hiện tại của từng user
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_current_folder (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_telegram_id INTEGER UNIQUE,
            folder_id INTEGER,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # File
    cur.execute(
        """
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
        )
        """
    )

    # Token chia sẻ cho từng thư mục
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS share_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_telegram_id INTEGER,
            folder_id INTEGER,
            token TEXT UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()
    conn.close()


def get_or_create_user(tg_user):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_user.id,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row

    cur.execute(
        """
        INSERT INTO users (telegram_id, full_name, username)
        VALUES (?, ?, ?)
        """,
        (tg_user.id, tg_user.full_name, tg_user.username),
    )
    conn.commit()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_user.id,))
    row = cur.fetchone()
    conn.close()
    return row


def create_or_get_folder(owner_id, name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM folders
        WHERE owner_telegram_id = ? AND name = ?
        """,
        (owner_id, name),
    )
    row = cur.fetchone()
    if row:
        conn.close()
        return row

    cur.execute(
        """
        INSERT INTO folders (owner_telegram_id, name)
        VALUES (?, ?)
        """,
        (owner_id, name),
    )
    conn.commit()
    cur.execute(
        """
        SELECT * FROM folders
        WHERE owner_telegram_id = ? AND name = ?
        """,
        (owner_id, name),
    )
    row = cur.fetchone()
    conn.close()
    return row


def set_current_folder(owner_id, folder_id):
    conn = get_conn()
    cur = conn.cursor()
    # upsert theo owner_telegram_id
    cur.execute(
        """
        INSERT INTO user_current_folder (owner_telegram_id, folder_id, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(owner_telegram_id) DO UPDATE SET
            folder_id = excluded.folder_id,
            updated_at = excluded.updated_at
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
        WHERE u.owner_telegram_id = ?
        """,
        (owner_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def ensure_current_folder(owner_id):
    """Luôn đảm bảo user có thư mục hiện tại.
       Nếu chưa có thì tạo thư mục 'Mặc định' và chọn nó.
    """
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
        """
        SELECT * FROM folders
        WHERE owner_telegram_id = ?
        ORDER BY created_at DESC
        """,
        (owner_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def save_file(
    owner_id,
    folder_id,
    file_unique_id,
    file_id,
    file_name,
    file_type,
    file_size,
    mime_type,
):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR IGNORE INTO files
        (file_unique_id, file_id, owner_telegram_id, folder_id,
         file_name, file_type, file_size, mime_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        WHERE owner_telegram_id = ? AND folder_id = ?
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
        VALUES (?, ?, ?)
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
        """
        SELECT owner_telegram_id, folder_id
        FROM share_tokens
        WHERE token = ?
        """,
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
    if folder_id is not None:
        cur.execute(
            """
            SELECT * FROM files
            WHERE owner_telegram_id = ? AND folder_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (owner_id, folder_id, limit),
        )
    else:
        cur.execute(
            """
            SELECT * FROM files
            WHERE owner_telegram_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (owner_id, limit),
        )
    rows = cur.fetchall()
    conn.close()
    return rows


# ----------------- TEXT HƯỚNG DẪN -----------------
WELCOME_TEXT = (
    "Những điều bot có thể làm?\n\n"
    "• Lưu trữ hình ảnh, video, tài liệu, file bất kỳ.\n"
    "• Có thể tải lại bất cứ lúc nào, không lo mất dữ liệu!\n\n"
    "Cách sử dụng:\n"
    "• Bấm nút 📁 Tạo thư mục mới để tạo thư mục và bắt đầu upload.\n"
    "• Hoặc gõ /upload để bật chế độ tải file lên.\n"
    "• Gõ /getlink để tạo link chia sẻ thư mục hiện tại.\n"
    "• Gõ /myfiles để xem các file trong thư mục hiện tại.\n\n"
    f"Ví dụ link chia sẻ: https://t.me/{BOT_USERNAME}?start=share_xxx\n"
)


# ----------------- HANDLERS -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user)

    args = context.args
    if args:
        arg = args[0]
        # /start share_xxx
        if arg.startswith("share_"):
            token = arg[len("share_") :]
            owner_id, folder_id = get_owner_and_folder_by_token(token)
            if not owner_id or not folder_id:
                await update.message.reply_text(
                    "❌ Link chia sẻ không hợp lệ hoặc đã bị xóa."
                )
                return

            files = get_files_of_owner(owner_id, folder_id=folder_id, limit=30)
            if not files:
                await update.message.reply_text(
                    "📂 Thư mục này hiện chưa có file nào."
                )
                return

            text_lines = [
                f"📂 Danh sách file được chia sẻ ({len(files)} file):\n"
            ]
            for f in files:
                name = f["file_name"] or f["file_type"]
                created = f["created_at"]
                size = f["file_size"] or 0
                text_lines.append(f"• {name} - {size} bytes - {created}")

            await update.message.reply_text(
                "\n".join(text_lines),
                reply_markup=get_main_keyboard(),
            )
            return

    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=get_main_keyboard(),
    )


async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user)
    folder = ensure_current_folder(user.id)

    UPLOAD_MODE_USERS.add(user.id)

    await update.message.reply_text(
        f"✅ Đang ở thư mục: {folder['name']}\n"
        "Bây giờ hãy gửi hình ảnh / video / tài liệu... cho bot.\n"
        "Khi xong, dùng /getlink để lấy link chia sẻ thư mục này.",
        reply_markup=get_main_keyboard(),
    )


async def new_folder_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Khi bấm nút '📁 Tạo thư mục mới':
       → tạo folder mới + chọn nó + bật luôn chế độ upload.
    """
    user = update.effective_user
    get_or_create_user(user)

    folder_name = datetime.now().strftime("Thư mục %Y-%m-%d %H:%M:%S")
    folder = create_or_get_folder(user.id, folder_name)
    set_current_folder(user.id, folder["id"])

    UPLOAD_MODE_USERS.add(user.id)

    await update.message.reply_text(
        f"📁 Đã tạo thư mục mới: *{folder_name}*\n"
        "Thư mục này đang được chọn.\n\n"
        "✅ Bạn đang ở chế độ upload, hãy gửi file cho bot.",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def setfolder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh nâng cao: /setfolder tên_thư_mục (tự đặt tên thư mục)."""
    user = update.effective_user
    get_or_create_user(user)

    if not context.args:
        await update.message.reply_text(
            "Cách dùng:\n/setfolder Tên_thư_mục_mới",
            reply_markup=get_main_keyboard(),
        )
        return

    folder_name = " ".join(context.args).strip()
    if not folder_name:
        await update.message.reply_text(
            "Tên thư mục không hợp lệ.",
            reply_markup=get_main_keyboard(),
        )
        return

    folder = create_or_get_folder(user.id, folder_name)
    set_current_folder(user.id, folder["id"])
    UPLOAD_MODE_USERS.add(user.id)

    await update.message.reply_text(
        f"📁 Đã chọn thư mục: *{folder_name}*\n"
        "Giờ bạn có thể gửi file để upload.",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def folders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    folders = list_folders(user.id)
    current = get_current_folder(user.id)

    if not folders:
        await update.message.reply_text(
            "Bạn chưa có thư mục nào. Hãy bấm nút *📁 Tạo thư mục mới*.",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown",
        )
        return

    text_lines = ["📂 Các thư mục của bạn:\n"]
    for f in folders:
        mark = "⭐" if current and current["id"] == f["id"] else "•"
        text_lines.append(f"{mark} {f['name']} (tạo lúc {f['created_at']})")

    text_lines.append(
        "\nBạn có thể dùng lệnh:\n"
        "/setfolder Tên_thư_mục\n"
        "để chuyển sang thư mục khác."
    )

    await update.message.reply_text(
        "\n".join(text_lines),
        reply_markup=get_main_keyboard(),
    )


async def myfiles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    folder = ensure_current_folder(user.id)
    files = get_files_of_owner(user.id, folder_id=folder["id"], limit=30)

    if not files:
        await update.message.reply_text(
            f"Thư mục *{folder['name']}* chưa có file nào.\n"
            "Hãy gửi file cho bot để lưu trữ.",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown",
        )
        return

    text_lines = [
        f"📂 30 file mới nhất trong thư mục *{folder['name']}* ({len(files)} file):\n"
    ]
    for f in files:
        name = f["file_name"] or f["file_type"]
        created = f["created_at"]
        size = f["file_size"] or 0
        text_lines.append(f"• {name} - {size} bytes - {created}")

    await update.message.reply_text(
        "\n".join(text_lines),
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def getlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    folder = ensure_current_folder(user.id)

    token = get_share_token(user.id, folder["id"])
    link = f"https://t.me/{BOT_USERNAME}?start=share_{token}"

    await update.message.reply_text(
        f"🔗 Link chia sẻ cho thư mục *{folder['name']}*:\n"
        f"{link}\n\n"
        "Ai có link này mở bot sẽ thấy danh sách file trong thư mục này "
        "(tối đa 30 file gần nhất).",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user
    get_or_create_user(user)

    # Đảm bảo đã có thư mục hiện tại
    folder = ensure_current_folder(user.id)

    if user.id not in UPLOAD_MODE_USERS:
        UPLOAD_MODE_USERS.add(user.id)

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
    elif message.video:
        file_obj = message.video
        file_type = "video"
        file_name = "video.mp4"
        file_size = file_obj.file_size
        mime_type = "video/mp4"
    elif message.photo:
        file_obj = message.photo[-1]
        file_type = "photo"
        file_name = "photo.jpg"
        file_size = file_obj.file_size
        mime_type = "image/jpeg"
    elif message.audio:
        file_obj = message.audio
        file_type = "audio"
        file_name = file_obj.file_name or "audio.mp3"
        file_size = file_obj.file_size
        mime_type = file_obj.mime_type
    else:
        return  # không phải file thì bỏ qua

    file_unique_id = file_obj.file_unique_id
    file_id = file_obj.file_id

    save_file(
        owner_id=user.id,
        folder_id=folder["id"],
        file_unique_id=file_unique_id,
        file_id=file_id,
        file_name=file_name,
        file_type=file_type,
        file_size=file_size,
        mime_type=mime_type,
    )

    await message.reply_text(
        f"✅ Đã lưu file vào thư mục *{folder['name']}*: {file_name}",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Mình không hiểu lệnh này. Bạn hãy dùng:\n"
        "/upload - Bắt đầu tải file lên\n"
        "/getlink - Lấy link chia sẻ thư mục hiện tại\n"
        "/myfiles - Xem file trong thư mục hiện tại\n"
        "Hoặc bấm nút bên dưới.",
        reply_markup=get_main_keyboard(),
    )


def main():
    if not BOT_TOKEN:
        logger.error("Chưa thiết lập biến môi trường BOT_TOKEN hoặc Token")
        raise SystemExit("Please set BOT_TOKEN or Token env variable")

    init_db()
    logger.info("Database đã sẵn sàng: %s", DB_PATH)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Lệnh
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("upload", upload_cmd))
    app.add_handler(CommandHandler("getlink", getlink_cmd))
    app.add_handler(CommandHandler("myfiles", myfiles_cmd))
    app.add_handler(CommandHandler("folders", folders_cmd))
    app.add_handler(CommandHandler("setfolder", setfolder_cmd))

    # Nút "📁 Tạo thư mục mới"
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^📁 Tạo thư mục mới$"),
            new_folder_button,
        )
    )

    # Nhận file
    file_filter = (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VIDEO
        | filters.AUDIO
    )
    app.add_handler(MessageHandler(file_filter, handle_file))

    # Lệnh lạ
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
