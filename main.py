import logging
import os
import sqlite3
import secrets

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaVideo,
    InputMediaPhoto,
    InputMediaDocument,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ========================= CONFIG =========================

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("Token")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# phiên bản mới: share dạng album + tên thư mục + mật khẩu
APP_VERSION = "v6-folder-password"
MEDIA_GROUP_SIZE = 3  # muốn 10 file/lần thì đổi thành 10

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

UPLOAD_MODE_USERS = set()
FOLDER_NAME_WAIT_USERS = set()
# user_id -> (owner_id, folder_id) đang chờ nhập mật khẩu
PASS_WAIT_USERS = {}


# ========================= KEYBOARD =========================

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📁 Tạo thư mục mới"), KeyboardButton("/upload")],
        [KeyboardButton("/getlink"), KeyboardButton("/myfiles")],
        [KeyboardButton("/folders")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ========================= DATABASE =========================

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,
            full_name TEXT,
            username TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_telegram_id INTEGER,
            name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_current_folder (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_telegram_id INTEGER UNIQUE,
            folder_id INTEGER,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
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
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS share_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_telegram_id INTEGER,
            folder_id INTEGER,
            token TEXT UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # đảm bảo cột password tồn tại trong bảng folders
    cur.execute("PRAGMA table_info(folders)")
    cols = [row[1] for row in cur.fetchall()]
    if "password" not in cols:
        cur.execute("ALTER TABLE folders ADD COLUMN password TEXT")

    conn.commit()
    conn.close()
    logger.info("Database OK (có cột password).")


def get_or_create_user(tg_user):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_user.id,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row

    cur.execute(
        "INSERT INTO users (telegram_id, full_name, username) VALUES (?, ?, ?)",
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
        "SELECT * FROM folders WHERE owner_telegram_id = ? AND name = ?",
        (owner_id, name),
    )
    row = cur.fetchone()
    if row:
        conn.close()
        return row

    cur.execute(
        "INSERT INTO folders (owner_telegram_id, name) VALUES (?, ?)",
        (owner_id, name),
    )
    conn.commit()

    cur.execute(
        "SELECT * FROM folders WHERE owner_telegram_id = ? AND name = ?",
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
        "SELECT * FROM folders WHERE owner_telegram_id = ? ORDER BY created_at DESC",
        (owner_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_folder_by_id(folder_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM folders WHERE id = ?", (folder_id,))
    row = cur.fetchone()
    conn.close()
    return row


def update_folder_password(folder_id, password):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE folders SET password = ? WHERE id = ?",
        (password, folder_id),
    )
    conn.commit()
    conn.close()


def save_file(owner_id, folder_id, file_unique_id, file_id,
              file_name, file_type, file_size, mime_type):
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
        "SELECT owner_telegram_id, folder_id FROM share_tokens WHERE token = ?",
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


# ========================= TEXT =========================

WELCOME_TEXT = (
    "🌤 *Bot Lưu Trữ File*\n\n"
    "• Lưu hình ảnh, video, tài liệu, file bất kỳ.\n"
    "• Không lo mất dữ liệu.\n\n"
    "👉 Bấm *📁 Tạo thư mục mới* để tạo thư mục.\n"
    "👉 Dùng /upload để gửi file.\n"
    "👉 Dùng /getlink để lấy link chia sẻ.\n"
    "👉 Dùng /setpass <mật_khẩu> để đặt mật khẩu cho thư mục hiện tại,\n"
    "   hoặc /setpass off để tắt mật khẩu.\n"
)


# ========================= UTIL: gửi thư mục được chia sẻ =========================

async def send_shared_folder_files(chat_id: int, owner_id: int, folder_id: int,
                                   context: ContextTypes.DEFAULT_TYPE):
    folder = get_folder_by_id(folder_id)
    folder_name = folder["name"] if folder else "Không tên"

    files = get_files_of_owner(owner_id, folder_id=folder_id, limit=30)
    if not files:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📂 Thư mục *{folder_name}* chưa có file.",
            parse_mode="Markdown",
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📂 *Thư mục được chia sẻ:* {folder_name}\n"
            f"(tối đa 30 file mới nhất)\n"
            f"Bot sẽ gửi file theo lố {MEDIA_GROUP_SIZE} cái một lần."
        ),
        parse_mode="Markdown",
    )

    batch = []
    count_in_batch = 0

    for f in files:
        file_type = f["file_type"]
        file_id = f["file_id"]
        file_name = f["file_name"]
        file_size = f["file_size"]
        caption = f"{file_name} — {file_size} bytes"

        media = None
        if file_type == "video":
            media = InputMediaVideo(media=file_id, caption=caption)
        elif file_type == "photo":
            media = InputMediaPhoto(media=file_id, caption=caption)
        elif file_type == "document":
            media = InputMediaDocument(media=file_id, caption=caption)

        if media:
            batch.append(media)
            count_in_batch += 1

            if count_in_batch >= MEDIA_GROUP_SIZE:
                try:
                    await context.bot.send_media_group(chat_id=chat_id, media=batch)
                except Exception as e:
                    logger.exception("Lỗi khi gửi media group: %s", e)
                    # fallback: gửi từng file
                    for m in batch:
                        try:
                            if isinstance(m, InputMediaVideo):
                                await context.bot.send_video(
                                    chat_id=chat_id,
                                    video=m.media,
                                    caption=m.caption,
                                )
                            elif isinstance(m, InputMediaPhoto):
                                await context.bot.send_photo(
                                    chat_id=chat_id,
                                    photo=m.media,
                                    caption=m.caption,
                                )
                            elif isinstance(m, InputMediaDocument):
                                await context.bot.send_document(
                                    chat_id=chat_id,
                                    document=m.media,
                                    caption=m.caption,
                                )
                        except Exception as e2:
                            logger.exception("Lỗi khi gửi từng media: %s", e2)
                batch = []
                count_in_batch = 0
        else:
            # loại file không support media group
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Không gửi được trong album: {caption} (loại: {file_type})",
                )
            except Exception as e:
                logger.exception("Lỗi khi gửi message loại không hỗ trợ: %s", e)

    # phần còn lại
    if batch:
        try:
            await context.bot.send_media_group(chat_id=chat_id, media=batch)
        except Exception as e:
            logger.exception("Lỗi khi gửi media group cuối: %s", e)
            for m in batch:
                try:
                    if isinstance(m, InputMediaVideo):
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=m.media,
                            caption=m.caption,
                        )
                    elif isinstance(m, InputMediaPhoto):
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=m.media,
                            caption=m.caption,
                        )
                    elif isinstance(m, InputMediaDocument):
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=m.media,
                            caption=m.caption,
                        )
                except Exception as e2:
                    logger.exception("Lỗi khi gửi từng media (batch cuối): %s", e2)


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
    """
    /start
    - Nếu có arg share_xxx: kiểm tra mật khẩu (nếu có), sau đó gửi file.
    - Nếu không: hiển thị welcome.
    """
    user = update.effective_user
    get_or_create_user(user)

    # reset trạng thái nhập mật khẩu cũ (nếu có)
    PASS_WAIT_USERS.pop(user.id, None)

    args = context.args
    if args:
        arg = args[0]
        if arg.startswith("share_"):
            token = arg[len("share_"):]
            owner_id, folder_id = get_owner_and_folder_by_token(token)
            if not owner_id:
                await update.message.reply_text("❌ Link chia sẻ không hợp lệ.")
                return

            folder = get_folder_by_id(folder_id)
            if not folder:
                await update.message.reply_text("❌ Thư mục không tồn tại.")
                return

            folder_name = folder["name"]
            folder_pass = folder["password"]

            # nếu có mật khẩu -> yêu cầu nhập
            if folder_pass and folder_pass.strip():
                PASS_WAIT_USERS[user.id] = (owner_id, folder_id)
                await update.message.reply_text(
                    f"🔐 Thư mục *{folder_name}* đã được đặt mật khẩu.\n"
                    "Vui lòng nhập *mật khẩu* để xem file.",
                    reply_markup=get_main_keyboard(),
                    parse_mode="Markdown",
                )
                return

            # không có mật khẩu -> gửi thẳng
            await send_shared_folder_files(
                chat_id=update.effective_chat.id,
                owner_id=owner_id,
                folder_id=folder_id,
                context=context,
            )
            return

    # không có tham số share_
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

    # 1. Đang chờ nhập mật khẩu cho thư mục share
    if user.id in PASS_WAIT_USERS and not text.startswith("/"):
        owner_id, folder_id = PASS_WAIT_USERS[user.id]
        folder = get_folder_by_id(folder_id)
        real_pass = folder["password"] if folder else None

        if not real_pass:
            PASS_WAIT_USERS.pop(user.id, None)
            await update.message.reply_text(
                "Thư mục này hiện không còn đặt mật khẩu.",
                reply_markup=get_main_keyboard(),
            )
            return

        if text == real_pass:
            PASS_WAIT_USERS.pop(user.id, None)
            await update.message.reply_text(
                "✅ Mật khẩu đúng, đang gửi file...",
                reply_markup=get_main_keyboard(),
            )
            await send_shared_folder_files(
                chat_id=update.effective_chat.id,
                owner_id=owner_id,
                folder_id=folder_id,
                context=context,
            )
        else:
            await update.message.reply_text(
                "❌ Mật khẩu sai, vui lòng nhập lại.\n"
                "Hoặc gửi /start để thoát.",
                reply_markup=get_main_keyboard(),
            )
        return

    # 2. Đang chờ nhập tên thư mục mới
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
        has_pass = " 🔐" if f["password"] else ""
        lines.append(f"{mark} {f['name']}{has_pass} — {f['created_at']}")

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

    real_username = "luutruireng_bot"
    link = f"https://t.me/{real_username}?start=share_{token}"

    text = (
        f"🔗 Link chia sẻ thư mục *{folder['name']}*:\n"
        f"{link}\n\n"
        f"`{link}`"
    )

    await update.message.reply_text(
        text,
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )


async def setpass_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setpass <pass>  -> đặt mật khẩu cho thư mục hiện tại
    /setpass off     -> bỏ mật khẩu
    """
    user = update.effective_user
    folder = ensure_current_folder(user.id)

    if not context.args:
        await update.message.reply_text(
            "Cách dùng:\n"
            "/setpass <mật_khẩu> – đặt mật khẩu cho thư mục hiện tại.\n"
            "/setpass off – bỏ mật khẩu.\n"
            f"Thư mục hiện tại: *{folder['name']}*",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown",
        )
        return

    arg = " ".join(context.args).strip()
    if arg.lower() in ["off", "none", "0", "bo", "bỏ"]:
        update_folder_password(folder["id"], None)
        await update.message.reply_text(
            f"🔓 Đã *tắt mật khẩu* cho thư mục *{folder['name']}*.",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown",
        )
    else:
        update_folder_password(folder["id"], arg)
        await update.message.reply_text(
            f"🔐 Đã đặt mật khẩu cho thư mục *{folder['name']}*.",
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
        "/upload • /getlink • /myfiles • /folders • /setfolder • /setpass • /version",
        reply_markup=get_main_keyboard(),
    )


# ========================= MAIN =========================

def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ Chưa thiết lập BOT_TOKEN hoặc Token.")

    init_db()
    logger.info("Bot started.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("version", version_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("upload", upload_cmd))
    app.add_handler(CommandHandler("getlink", getlink_cmd))
    app.add_handler(CommandHandler("myfiles", myfiles_cmd))
    app.add_handler(CommandHandler("folders", folders_cmd))
    app.add_handler(CommandHandler("setfolder", setfolder_cmd))
    app.add_handler(CommandHandler("setpass", setpass_cmd))

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
