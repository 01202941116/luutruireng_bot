import logging
import os
import secrets

import psycopg2
import psycopg2.extras
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

# Railway: DATABASE_URL = ${Postgres.DATABASE_URL}
DATABASE_URL = os.getenv("DATABASE_URL")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))

APP_VERSION = "v7-mediagroup-folder-pass-whitelist-pg"
MEDIA_GROUP_SIZE = 3  # muốn 10 file 1 lần thì đổi thành 10

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

UPLOAD_MODE_USERS = set()
FOLDER_NAME_WAIT_USERS = set()
# user_id -> (owner_id, folder_id) đang chờ nhập mật khẩu khi mở link share_
PASS_WAIT_USERS = {}


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
    if not DATABASE_URL:
        raise RuntimeError("❌ Chưa thiết lập DATABASE_URL")
    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # USERS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id              SERIAL PRIMARY KEY,
            telegram_id     BIGINT UNIQUE,
            full_name       TEXT,
            username        TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # FOLDERS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id               SERIAL PRIMARY KEY,
            owner_telegram_id BIGINT,
            name             TEXT,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # thêm cột password nếu chưa có
    cur.execute("""
        ALTER TABLE folders
        ADD COLUMN IF NOT EXISTS password TEXT;
    """)

    # CURRENT FOLDER
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_current_folder (
            id               SERIAL PRIMARY KEY,
            owner_telegram_id BIGINT UNIQUE,
            folder_id        INTEGER,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # FILES
    cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id               SERIAL PRIMARY KEY,
            file_unique_id   TEXT UNIQUE,
            file_id          TEXT,
            owner_telegram_id BIGINT,
            folder_id        INTEGER,
            file_name        TEXT,
            file_type        TEXT,
            file_size        BIGINT,
            mime_type        TEXT,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # SHARE TOKENS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS share_tokens (
            id               SERIAL PRIMARY KEY,
            owner_telegram_id BIGINT,
            folder_id        INTEGER,
            token            TEXT UNIQUE,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # WHITELIST
    cur.execute("""
        CREATE TABLE IF NOT EXISTS allowed_users (
            id          SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE,
            added_by    BIGINT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # ADS (quảng cáo ghim)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ads (
            id          SERIAL PRIMARY KEY,
            code        TEXT UNIQUE,        -- ví dụ: qc1, qc2
            chat_id     BIGINT,
            message_id  BIGINT,
            content     TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.commit()
    conn.close()
    logger.info("Database OK (PostgreSQL, password + whitelist + ads).")


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


def get_all_user_ids():
    """
    Lấy toàn bộ telegram_id của user đã từng start bot.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id FROM users;")
    rows = cur.fetchall()
    conn.close()
    return [r["telegram_id"] for r in rows]


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
            updated_at = EXCLUDED.updated_at;
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
        WHERE u.owner_telegram_id = %s;
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


def get_folder_by_id(folder_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM folders WHERE id = %s", (folder_id,))
    row = cur.fetchone()
    conn.close()
    return row


def update_folder_password(folder_id, password):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE folders SET password = %s WHERE id = %s",
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
        INSERT INTO files
        (file_unique_id, file_id, owner_telegram_id, folder_id,
         file_name, file_type, file_size, mime_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (file_unique_id) DO NOTHING;
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


# ============ ADS (QUẢNG CÁO GHIM) ============

def create_ad(chat_id: int, message_id: int, content: str) -> str:
    """
    Tạo bản ghi quảng cáo, trả về code dạng qc1, qc2...
    content: nội dung QUẢNG CÁO (không có prefix [QC qc1])
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ads (code, chat_id, message_id, content)
        VALUES (%s, %s, %s, %s)
        RETURNING id;
        """,
        ("", chat_id, message_id, content),
    )
    row = cur.fetchone()
    ad_id = row["id"]
    code = f"qc{ad_id}"
    cur.execute("UPDATE ads SET code = %s WHERE id = %s", (code, ad_id))
    conn.commit()
    conn.close()
    return code


def get_ad_by_code(code: str, chat_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM ads WHERE code = %s AND chat_id = %s",
        (code, chat_id),
    )
    row = cur.fetchone()
    conn.close()
    return row


def delete_ad(code: str, chat_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM ads WHERE code = %s AND chat_id = %s",
        (code, chat_id),
    )
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_latest_ad():
    """
    Lấy quảng cáo mới nhất (dùng cho user mới /start).
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ads ORDER BY id DESC LIMIT 1;")
    row = cur.fetchone()
    conn.close()
    return row


# ============ WHITELIST ============

def is_user_allowed(user_id: int) -> bool:
    if OWNER_ID and user_id == OWNER_ID:
        return True
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 AS ok FROM allowed_users WHERE telegram_id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def add_allowed_user(user_id: int, added_by: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO allowed_users (telegram_id, added_by)
        VALUES (%s, %s)
        ON CONFLICT (telegram_id) DO NOTHING
        """,
        (user_id, added_by),
    )
    conn.commit()
    conn.close()


async def ensure_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat_id = update.effective_chat.id

    if OWNER_ID and user.id == OWNER_ID:
        return True

    if is_user_allowed(user.id):
        return True

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🔒 Bot riêng tư, chỉ người được duyệt mới sử dụng.\n"
                f"ID Telegram của bạn: `{user.id}`\n"
                "Gửi ID này cho admin để được cấp quyền."
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Lỗi gửi thông báo không có quyền: %s", e)
    return False


# ========================= TEXT =========================

WELCOME_TEXT = (
    "🌤 *Bot Lưu Trữ File*\n\n"
    "• Lưu hình ảnh, video, tài liệu, file bất kỳ.\n"
    "• Dữ liệu lưu trên PostgreSQL – không lo mất.\n\n"
    "👉 Bấm *📁 Tạo thư mục mới* để tạo thư mục.\n"
    "👉 Dùng /upload để gửi file.\n"
    "👉 Dùng /getlink để lấy link chia sẻ.\n"
    "👉 Dùng /setpass <mật khẩu> để đặt mật khẩu thư mục.\n"
    "👉 Dùng /setpass off để tắt mật khẩu.\n"
)


# ========================= UTIL: gửi file chia sẻ =========================

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
        reply_markup=get_main_keyboard(),
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
                    # fallback: gửi từng cái
                    for m in batch:
                        try:
                            if isinstance(m, InputMediaVideo):
                                await context.bot.send_video(
                                    chat_id=chat_id, video=m.media, caption=m.caption
                                )
                            elif isinstance(m, InputMediaPhoto):
                                await context.bot.send_photo(
                                    chat_id=chat_id, photo=m.media, caption=m.caption
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
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Không gửi được trong album: {caption} (loại: {file_type})",
                )
            except Exception as e:
                logger.exception("Lỗi khi gửi message loại không hỗ trợ: %s", e)

    if batch:
        try:
            await context.bot.send_media_group(chat_id=chat_id, media=batch)
        except Exception as e:
            logger.exception("Lỗi khi gửi media group cuối: %s", e)
            for m in batch:
                try:
                    if isinstance(m, InputMediaVideo):
                        await context.bot.send_video(
                            chat_id=chat_id, video=m.media, caption=m.caption
                        )
                    elif isinstance(m, InputMediaPhoto):
                        await context.bot.send_photo(
                            chat_id=chat_id, photo=m.media, caption=m.caption
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


async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID and user.id != OWNER_ID:
        await update.message.reply_text("❌ Bạn không có quyền dùng lệnh này.")
        return

    if not context.args:
        await update.message.reply_text(
            "Cách dùng:\n"
            "/allow <telegram_id>\n\n"
            "Ví dụ:\n"
            "/allow 123456789",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID không hợp lệ, phải là số.")
        return

    add_allowed_user(target_id, user.id)
    await update.message.reply_text(
        f"✅ Đã thêm ID {target_id} vào danh sách được phép dùng bot."
    )


async def ad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /ad Nội dung quảng cáo
    Chỉ OWNER dùng.
    - Bot gửi tin trong chat của owner, ghim.
    - Lưu DB (mã qc1, qc2...)
    - Gửi + ghim QC đó cho TẤT CẢ user đã từng dùng bot.
    """
    user = update.effective_user
    chat = update.effective_chat

    if OWNER_ID and user.id != OWNER_ID:
        await update.message.reply_text("❌ Bạn không có quyền dùng lệnh /ad.")
        return

    if not context.args:
        await update.message.reply_text("Thiếu nội dung quảng cáo.")
        return

    ad_text = " ".join(context.args).strip()

    # 1) gửi tin quảng cáo ở chat hiện tại (thường là chat với OWNER)
    msg = await chat.send_message(ad_text)

    # 2) lưu vào DB, sinh mã qc1, qc2...
    code = create_ad(chat.id, msg.message_id, ad_text)

    # 3) sửa lại nội dung để có mã qc ở đầu
    final_text = f"[QC {code}] {ad_text}"
    try:
        await msg.edit_text(final_text)
    except Exception as e:
        logger.exception("Không edit được nội dung QC: %s", e)

    # 4) ghim tin trong chat của OWNER
    try:
        await context.bot.pin_chat_message(
            chat_id=chat.id,
            message_id=msg.message_id,
            disable_notification=True,
        )
    except Exception as e:
        logger.exception("Không ghim được QC ở chat owner: %s", e)

    # 5) GỬI & GHIM TỚI TẤT CẢ USER ĐÃ TỪNG DÙNG BOT
    all_user_ids = get_all_user_ids()
    for uid in all_user_ids:
        # đã có rồi trong bước 4
        if uid == chat.id:
            continue
        try:
            sent = await context.bot.send_message(chat_id=uid, text=final_text)
            try:
                await context.bot.pin_chat_message(
                    chat_id=uid,
                    message_id=sent.message_id,
                    disable_notification=True,
                )
            except Exception as e_pin:
                logger.exception("Không ghim được QC ở user %s: %s", uid, e_pin)
        except Exception as e_send:
            logger.exception("Không gửi QC tới user %s: %s", uid, e_send)

    await update.message.reply_text(f"✅ Đã đăng & ghim quảng cáo với mã: {code}")


async def delad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /delad qc1  hoặc  /delad 1  (hiểu là qc1)
    -> chỉ xoá & bỏ ghim QC ở chat owner (không broadcast xóa).
    """
    user = update.effective_user
    chat = update.effective_chat

    if OWNER_ID and user.id != OWNER_ID:
        await update.message.reply_text("❌ Bạn không có quyền dùng lệnh /delad.")
        return

    if not context.args:
        await update.message.reply_text("Thiếu mã quảng cáo. Ví dụ: /delad qc1")
        return

    raw_code = context.args[0].strip().lower()
    if raw_code.startswith("#"):
        raw_code = raw_code[1:]
    if not raw_code.startswith("qc"):
        code = "qc" + raw_code
    else:
        code = raw_code

    ad = get_ad_by_code(code, chat.id)
    if not ad:
        await update.message.reply_text(f"❌ Không tìm thấy quảng cáo với mã {code}.")
        return

    msg_id = ad["message_id"]

    # bỏ ghim + xoá message nếu được (chỉ ở chat này)
    try:
        await context.bot.unpin_chat_message(chat_id=chat.id, message_id=msg_id)
    except Exception as e:
        logger.exception("Không unpin được QC: %s", e)

    try:
        await context.bot.delete_message(chat_id=chat.id, message_id=msg_id)
    except Exception as e:
        logger.exception("Không xoá được message QC: %s", e)

    delete_ad(code, chat.id)

    await update.message.reply_text(f"✅ Đã xoá quảng cáo {code} trong chat này.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user)

    # reset trạng thái chờ nhập mật khẩu
    PASS_WAIT_USERS.pop(user.id, None)

    args = context.args or []

    # 🔹 Nếu có share_ → cho xem, KHÔNG kiểm tra whitelist
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

            # có mật khẩu → yêu cầu nhập
            if folder_pass and folder_pass.strip():
                PASS_WAIT_USERS[user.id] = (owner_id, folder_id)
                await update.message.reply_text(
                    f"🔐 Thư mục *{folder_name}* đã được đặt mật khẩu.\n"
                    "Vui lòng nhập mật khẩu để xem file.",
                    reply_markup=get_main_keyboard(),
                    parse_mode="Markdown",
                )
                return

            # không có mật khẩu → gửi file luôn
            await send_shared_folder_files(
                chat_id=update.effective_chat.id,
                owner_id=owner_id,
                folder_id=folder_id,
                context=context,
            )
            return

    # 🔹 /start bình thường (không share_) → phải qua whitelist
    if not await ensure_allowed(update, context):
        return

    # gửi welcome
    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )

    # TỰ ĐỘNG GỬI + GHIM QUẢNG CÁO MỚI NHẤT (NẾU CÓ)
    latest_ad = get_latest_ad()
    if latest_ad:
        final_text = f"[QC {latest_ad['code']}] {latest_ad['content']}"
        try:
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=final_text,
            )
            try:
                await context.bot.pin_chat_message(
                    chat_id=update.effective_chat.id,
                    message_id=msg.message_id,
                    disable_notification=True,
                )
            except Exception as e_pin:
                logger.exception("Không ghim được QC trong start: %s", e_pin)
        except Exception as e_send:
            logger.exception("Không gửi QC trong start: %s", e_send)


async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_allowed(update, context):
        return

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
    if not await ensure_allowed(update, context):
        return

    user = update.effective_user
    FOLDER_NAME_WAIT_USERS.add(user.id)
    await update.message.reply_text(
        "✏️ Nhập tên thư mục mới bạn muốn tạo:",
        reply_markup=get_main_keyboard(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    # 1) ĐANG NHẬP MẬT KHẨU CHO LINK share_
    #    → KHÔNG kiểm tra whitelist
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

    # 2) Các trường hợp còn lại mới cần check whitelist
    if not await ensure_allowed(update, context):
        return

    # 3) ĐANG CHỜ TÊN THƯ MỤC MỚI
    if user.id in FOLDER_NAME_WAIT_USERS and not text.startswith("/"):
        FOLDER_NAME_WAIT_USERS.remove(user.id)

        folder = create_or_get_folder(user.id, text)
        set_current_folder(user.id, folder["id"])
        UPLOAD_MODE_USERS.add(user.id)

        await update.message.reply_text(
            f"📁 Đã tạo / chọn thư mục: *{text}*\n"
            "➡ Bây giờ hãy gửi file cho bot.",
            reply_markup=get_main_keyboard(),
        )
        return


async def setfolder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_allowed(update, context):
        return

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
    if not await ensure_allowed(update, context):
        return

    user = update.effective_user
    folders = list_folders(user.id)
    cur = get_current_folder(user.id)

    if not folders:
        await update.message.reply_text(
            "Bạn chưa có thư mục nào. Hãy bấm 📁 Tạo thư mục mới.",
            reply_markup=get_main_keyboard(),
        )
        return

    lines = ["📂 Các thư mục của bạn:\n"]
    for f in folders:
        mark = "⭐" if cur and cur["id"] == f["id"] else "•"
        has_pass = " 🔐" if f["password"] else ""
        lines.append(f"{mark} {f['name']}{has_pass} — {f['created_at']}")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=get_main_keyboard(),
    )


async def myfiles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_allowed(update, context):
        return

    user = update.effective_user
    folder = ensure_current_folder(user.id)
    files = get_files_of_owner(user.id, folder_id=folder["id"], limit=30)

    if not files:
        await update.message.reply_text(
            f"Thư mục {folder['name']} chưa có file nào.",
            reply_markup=get_main_keyboard(),
        )
        return

    lines = [f"📂 30 file mới nhất trong thư mục {folder['name']}:\n"]
    for f in files:
        lines.append(f"• {f['file_name']} — {f['file_size']} bytes")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=get_main_keyboard(),
    )


async def getlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_allowed(update, context):
        return

    user = update.effective_user
    folder = ensure_current_folder(user.id)
    token = get_share_token(user.id, folder["id"])

    real_username = os.getenv("BOT_USERNAME") or context.bot.username
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
    if not await ensure_allowed(update, context):
        return

    user = update.effective_user
    folder = ensure_current_folder(user.id)

    if not context.args:
        await update.message.reply_text(
            "Cách dùng:\n"
            "/setpass <mật khẩu> – đặt mật khẩu cho thư mục hiện tại.\n"
            "/setpass off – bỏ mật khẩu.\n"
            f"Thư mục hiện tại: {folder['name']}",
            reply_markup=get_main_keyboard(),
        )
        return

    arg = " ".join(context.args).strip()
    if arg.lower() in ["off", "none", "0", "bo", "bỏ"]:
        update_folder_password(folder["id"], None)
        await update.message.reply_text(
            f"🔓 Đã tắt mật khẩu cho thư mục {folder['name']}.",
            reply_markup=get_main_keyboard(),
        )
    else:
        update_folder_password(folder["id"], arg)
        await update.message.reply_text(
            f"🔐 Đã đặt mật khẩu cho thư mục {folder['name']}.",
            reply_markup=get_main_keyboard(),
        )


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_allowed(update, context):
        return

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
        f"✅ Đã lưu file vào thư mục {folder['name']}:\n• {file_name}",
        reply_markup=get_main_keyboard(),
    )


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_allowed(update, context):
        return

    await update.message.reply_text(
        "Lệnh không tồn tại. Hãy dùng:\n"
        "/upload /getlink /myfiles /folders /setfolder /setpass /version /ad /delad",
        reply_markup=get_main_keyboard(),
    )


# ========================= MAIN =========================

def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ Chưa thiết lập BOT_TOKEN hoặc Token.")
    if not DATABASE_URL:
        raise SystemExit("❌ Chưa thiết lập DATABASE_URL.")

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
    app.add_handler(CommandHandler("setpass", setpass_cmd))
    app.add_handler(CommandHandler("allow", allow_cmd))
    app.add_handler(CommandHandler("ad", ad_cmd))
    app.add_handler(CommandHandler("delad", delad_cmd))

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
