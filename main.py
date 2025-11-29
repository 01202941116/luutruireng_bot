import logging
import os
import sqlite3
import secrets
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------- CONFIG -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "YOUR_BOT_USERNAME")  # dùng để tạo link /getlink
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# set này dùng để ghi nhớ ai đang ở chế độ /upload (chỉ lưu trong RAM)
UPLOAD_MODE_USERS = set()


# ----------------- DATABASE -----------------
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
            full_name TEXT,
            username TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_unique_id TEXT UNIQUE,
            file_id TEXT,
            owner_telegram_id INTEGER,
            file_name TEXT,
            file_type TEXT,
            file_size INTEGER,
            mime_type TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS share_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_telegram_id INTEGER UNIQUE,
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


def save_file(owner_id, file_unique_id, file_id, file_name, file_type, file_size, mime_type):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR IGNORE INTO files
        (file_unique_id, file_id, owner_telegram_id, file_name, file_type, file_size, mime_type)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (file_unique_id, file_id, owner_id, file_name, file_type, file_size, mime_type),
    )
    conn.commit()
    conn.close()


def get_share_token(owner_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT token FROM share_tokens WHERE owner_telegram_id = ?", (owner_id,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row["token"]

    token = secrets.token_urlsafe(8)
    cur.execute(
        "INSERT INTO share_tokens (owner_telegram_id, token) VALUES (?, ?)",
        (owner_id, token),
    )
    conn.commit()
    conn.close()
    return token


def get_owner_by_token(token):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT owner_telegram_id FROM share_tokens WHERE token = ?",
        (token,),
    )
    row = cur.fetchone()
    conn.close()
    return row["owner_telegram_id"] if row else None


def get_files_of_owner(owner_id, limit=30):
    conn = get_conn()
    cur = conn.cursor()
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


# ----------------- HANDLERS -----------------
WELCOME_TEXT = (
    "Những điều bot có thể làm?\n\n"
    "• Lưu trữ hình ảnh, video, tài liệu, file bất kỳ.\n"
    "• Có thể tải lại bất cứ lúc nào, không lo mất dữ liệu!\n\n"
    "Cách sử dụng:\n"
    "• Gõ /upload để bắt đầu tải file lên.\n"
    "• Gõ /getlink để tạo link thư mục chia sẻ.\n"
    "• Gõ /myfiles để xem nhanh các file đã lưu của bạn.\n\n"
    "Ví dụ link chia sẻ: https://t.me/{username}?start=share_xxx\n\n"
    "提示：\n"
    "输入 /upload 命令并上传文件。\n"
    "输入 /getlink 命令生成分享链接。"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user)

    args = context.args
    if args:
        arg = args[0]
        if arg.startswith("share_"):
            token = arg[len("share_") :]
            owner_id = get_owner_by_token(token)
            if not owner_id:
                await update.message.reply_text("❌ Link chia sẻ không hợp lệ hoặc đã bị xóa.")
                return

            files = get_files_of_owner(owner_id, limit=30)
            if not files:
                await update.message.reply_text("📂 Thư mục này hiện chưa có file nào.")
                return

            text_lines = [f"📂 Danh sách file được chia sẻ ({len(files)}):\n"]
            for f in files:
                name = f["file_name"] or f["file_type"]
                created = f["created_at"]
                text_lines.append(f"• {name} ({created})")
            text_lines.append("\nBạn muốn tải file nào? Hãy báo chủ thư mục để họ gửi trực tiếp hoặc bổ sung tính năng tải về tự động.")
            await update.message.reply_text("\n".join(text_lines))
            return

    await update.message.reply_text(WELCOME_TEXT.format(username=BOT_USERNAME))


async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user)
    UPLOAD_MODE_USERS.add(user.id)
    await update.message.reply_text(
        "✅ Bạn đã bật chế độ upload.\n"
        "Bây giờ hãy gửi hình ảnh / video / tài liệu... cho bot.\n"
        "Khi xong, dùng /getlink để lấy link thư mục."
    )


async def myfiles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    files = get_files_of_owner(user.id, limit=30)
    if not files:
        await update.message.reply_text("Bạn chưa lưu file nào. Hãy dùng /upload để bắt đầu.")
        return

    text_lines = [f"📂 30 file mới nhất của bạn ({len(files)}):\n"]
    for f in files:
        name = f["file_name"] or f["file_type"]
        created = f["created_at"]
        size = f["file_size"] or 0
        text_lines.append(f"• {name} - {size} bytes - {created}")
    await update.message.reply_text("\n".join(text_lines))


async def getlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user)
    token = get_share_token(user.id)

    link = f"https://t.me/{BOT_USERNAME}?start=share_{token}"
    await update.message.reply_text(
        "🔗 Link thư mục chia sẻ của bạn:\n"
        f"{link}\n\n"
        "Ai có link này mở bot sẽ thấy danh sách file bạn đã lưu (tối đa 30 file gần nhất)."
    )


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user
    get_or_create_user(user)

    if user.id not in UPLOAD_MODE_USERS:
        # vẫn cho lưu luôn cho tiện
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
        # photo là list, lấy ảnh lớn nhất
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
        return

    file_unique_id = file_obj.file_unique_id
    file_id = file_obj.file_id

    save_file(
        owner_id=user.id,
        file_unique_id=file_unique_id,
        file_id=file_id,
        file_name=file_name,
        file_type=file_type,
        file_size=file_size,
        mime_type=mime_type,
    )

    await message.reply_text(
        f"✅ Đã lưu file: {file_name}\n"
        "Bạn có thể tiếp tục gửi thêm file.\n"
        "Dùng /getlink để tạo link thư mục chia sẻ."
    )


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Mình không hiểu lệnh này. Bạn hãy dùng:\n"
        "/upload - Bắt đầu tải file lên\n"
        "/getlink - Lấy link thư mục chia sẻ\n"
        "/myfiles - Xem file đã lưu"
    )


def main():
    if not BOT_TOKEN:
        logger.error("Chưa thiết lập biến môi trường BOT_TOKEN")
        raise SystemExit("Please set BOT_TOKEN env variable")

    init_db()
    logger.info("Database đã sẵn sàng: %s", DB_PATH)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("upload", upload_cmd))
    app.add_handler(CommandHandler("getlink", getlink_cmd))
    app.add_handler(CommandHandler("myfiles", myfiles_cmd))

    file_filter = (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VIDEO
        | filters.AUDIO
    )
    app.add_handler(MessageHandler(file_filter, handle_file))

    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
