import os
import logging
from datetime import datetime

from dotenv import load_dotenv
from colorama import Fore

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import db

# ---------------------- CONFIG --------------------------- #

load_dotenv()
TOKEN = os.getenv("Token")           # biến môi trường: Token
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)  # ID chủ bot

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------- HELPERS --------------------------- #


def sanitize_filename(name: str) -> str:
    """Làm sạch tên file."""
    name = os.path.basename(name)
    return name.replace("\n", "_").replace("\r", "_")


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """
    Bàn phím phía dưới màn hình:
    | /upload | /getlink |
    """
    keyboard = [[KeyboardButton("/upload"), KeyboardButton("/getlink")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def register_user(update: Update):
    """Lưu user vào DB."""
    user = update.effective_user
    if user is None:
        return
    db.upsert_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )


async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Bot kín: chỉ user đã được OWNER duyệt mới được dùng các lệnh lưu trữ.
    /start, /help, /me vẫn dùng được để xem hướng dẫn.
    """
    user = update.effective_user
    msg = update.effective_message

    if user is None or msg is None:
        return False

    # Chủ bot luôn được phép
    if OWNER_ID and user.id == OWNER_ID:
        return True

    row = db.get_user_by_telegram_id(user.id)
    if row and row["is_approved"]:
        return True

    # Chưa được duyệt
    await msg.reply_text(
        "🔒 Đây là bot kín.\n"
        "Bạn chưa được admin duyệt sử dụng.\n"
        "Vui lòng chờ admin kiểm tra và mở quyền."
    )

    # Gửi thông báo tới owner (nếu có)
    if OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID,
                (
                    "🔔 Có người xin sử dụng bot:\n"
                    f"ID: <code>{user.id}</code>\n"
                    f"Username: @{user.username}\n\n"
                    f"Duyệt: /approve {user.id}\n"
                    f"Chặn: /block {user.id}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

    return False


async def save_file_to_db(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_obj,
    file_type: str,
    filename_hint: str | None,
    file_unique_id: str,
    file_id: str,
    file_size: int | None = None,
    mime_type: str | None = None,
):
    """Lưu thông tin file vào DB, trả về file_db_id."""
    user = update.effective_user
    msg = update.effective_message

    if user is None or msg is None:
        return None

    await register_user(update)

    current_folder_id = context.chat_data.get("current_folder_id")

    if filename_hint:
        filename = sanitize_filename(filename_hint)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{file_type}_{file_unique_id}_{ts}"

    # Ở đây mình chỉ lưu file_id, KHÔNG lưu BLOB để tiết kiệm
    file_db_id = db.insert_file(
        owner_telegram_id=user.id,
        folder_id=current_folder_id,
        file_type=file_type,
        file_unique_id=file_unique_id,
        file_id=file_id,
        filename=filename,
        file_bytes=None,
        file_size=file_size,
        mime_type=mime_type,
    )

    context.chat_data["last_file_db_id"] = file_db_id
    return file_db_id


def build_file_deeplink(bot_username: str, file_db_id: int) -> str:
    return f"https://t.me/{bot_username}?start=file{file_db_id}"


def build_folder_deeplink(bot_username: str, folder_id: int) -> str:
    return f"https://t.me/{bot_username}?start=folder{folder_id}"


# ---------------------- COMMAND HANDLERS --------------------------- #


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý /start + deep-link."""
    await register_user(update)
    msg = update.effective_message
    if msg is None:
        return

    args = context.args or []

    # Deep-link: /start file123 hoặc /start folder5
    if args:
        param = args[0]

        # Xem trực tiếp 1 file (ai có link đều xem được)
        if param.startswith("file"):
            try:
                file_db_id = int(param[4:])
            except ValueError:
                await msg.reply_text("Link file không hợp lệ.")
                return

            row = db.get_file_by_id(file_db_id)
            if not row:
                await msg.reply_text("Không tìm thấy file (có thể đã bị xoá).")
                return

            file_type = row["file_type"]
            file_id = row["file_id"]
            fname = row["filename"] or "file"

            caption = f"📁 File: <b>{fname}</b>\nID: <code>{file_db_id}</code>"

            try:
                if file_type == "video":
                    await msg.reply_video(file_id, caption=caption, parse_mode="HTML")
                elif file_type == "photo":
                    await msg.reply_photo(file_id, caption=caption, parse_mode="HTML")
                elif file_type == "audio":
                    await msg.reply_audio(file_id, caption=caption, parse_mode="HTML")
                elif file_type == "voice":
                    await msg.reply_voice(file_id, caption=caption, parse_mode="HTML")
                else:
                    await msg.reply_document(file_id, caption=caption, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Lỗi gửi file deeplink: {e}")
            return

        # Xem thư mục: gửi thẳng tất cả file trong thư mục
        if param.startswith("folder"):
            try:
                folder_id = int(param[6:])
            except ValueError:
                await msg.reply_text("Link thư mục không hợp lệ.")
                return

            folder = db.get_folder_by_id(folder_id)
            if not folder:
                await msg.reply_text("Không tìm thấy thư mục (có thể đã xoá).")
                return

            files = db.get_files_by_folder(folder_id)
            if not files:
                await msg.reply_text(
                    f"📂 Thư mục <b>{folder['name']}</b> hiện chưa có file nào.",
                    parse_mode="HTML",
                )
                return

            # Gửi 1 tin tiêu đề thư mục
            await msg.reply_text(
                f"📂 Thư mục: <b>{folder['name']}</b>\n"
                f"Số file: <b>{len(files)}</b>\n"
                "Bot sẽ gửi lần lượt các file bên dưới:",
                parse_mode="HTML",
            )

            # Gửi từng file trực tiếp để người xem xem/tải luôn
            for f in files[:50]:
                file_type = f["file_type"]
                file_id = f["file_id"]
                fname = f["filename"] or f"file_{f['id']}"
                caption = f"{fname}\nID: <code>{f['id']}</code>"

                try:
                    if file_type == "video":
                        await msg.reply_video(file_id, caption=caption, parse_mode="HTML")
                    elif file_type == "photo":
                        await msg.reply_photo(file_id, caption=caption, parse_mode="HTML")
                    elif file_type == "audio":
                        await msg.reply_audio(file_id, caption=caption, parse_mode="HTML")
                    elif file_type == "voice":
                        await msg.reply_voice(file_id, caption=caption, parse_mode="HTML")
                    else:
                        await msg.reply_document(
                            file_id, caption=caption, parse_mode="HTML"
                        )
                except Exception as e:
                    logger.error(f"Lỗi gửi file trong thư mục: {e}")

            return

    # /start bình thường
    text = (
        "🤖 Bot lưu trữ file, tất cả nằm trong 1 file SQLite.\n\n"
        "📤 Cách dùng nhanh:\n"
        "• Gửi 1 file cho bot → bot trả link luôn.\n"
        "• Muốn sắp xếp theo thư mục: /folder &lt;tên&gt; → gửi file → /folderlink.\n\n"
        "Bot là bot kín, admin phải /approve ID thì mới upload / tạo thư mục được."
    )
    await msg.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return

    await msg.reply_text(
        "📚 Lệnh bot:\n\n"
        "🔹 /start - Bắt đầu / xem hướng dẫn\n"
        "🔹 /help  - Xem lại hướng dẫn\n"
        "🔹 /me    - Xem ID + username Telegram\n\n"
        "📤 UPLOAD:\n"
        "🔹 Gửi file trực tiếp cho bot, bot tự trả link.\n"
        "🔹 /upload - Hiện bàn phím /upload + /getlink và nhắc cách dùng\n\n"
        "📁 THƯ MỤC:\n"
        "🔹 /folder &lt;tên&gt;          - Tạo hoặc chọn thư mục\n"
        "🔹 /myfolders                - Xem thư mục của bạn\n"
        "🔹 /folderlink               - Lấy link thư mục đang chọn\n"
        "🔹 /searchfolder &lt;từ khóa&gt; - Tìm thư mục theo tên\n\n"
        "👑 ADMIN (OWNER):\n"
        "🔹 /approve TELEGRAM_ID - Duyệt user dùng bot\n"
        "🔹 /block TELEGRAM_ID   - Chặn user dùng bot",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if msg is None:
        return

    text = (
        "Thông tin Telegram của bạn:\n"
        f"ID: <code>{user.id}</code>\n"
        f"Username: <code>{user.username or 'không có'}</code>\n\n"
        "Dùng ID này để admin /approve cho bạn hoặc set OWNER_ID cho bot."
    )
    await msg.reply_text(
        text, parse_mode="HTML", reply_markup=get_main_keyboard()
    )


async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_user(update)
    if not await check_access(update, context):
        return

    msg = update.effective_message
    if msg is None:
        return

    await msg.reply_text(
        "✅ Bấm nút /upload bên dưới hoặc gõ /upload cũng được.\n"
        "▶ Sau đó dùng nút 📎 của Telegram để chọn file (có thể chọn nhiều hình/video).\n"
        "📌 Nếu bạn đã chọn thư mục bằng /folder, mọi file sẽ được lưu vào thư mục đó.\n"
        "📌 Mỗi file gửi xong bot sẽ gửi link (hoặc link thư mục) cho bạn copy.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def getlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_user(update)
    if not await check_access(update, context):
        return

    msg = update.effective_message
    if msg is None:
        return

    user = update.effective_user
    row = db.get_last_file_by_owner(user.id)
    if not row:
        await msg.reply_text(
            "❌ Bạn chưa upload file nào.\n"
            "Hãy gửi 1 file cho bot (hoặc gõ /upload rồi gửi file) trước.",
            reply_markup=get_main_keyboard(),
        )
        return

    file_db_id = row["id"]
    bot_username = context.bot.username
    link = build_file_deeplink(bot_username, file_db_id)

    await msg.reply_text(
        "🔗 Link tải file gần nhất của bạn:\n"
        f"{link}\n\n"
        "Gửi link này cho người khác, họ bấm Start bot sẽ nhận được file.",
        reply_markup=get_main_keyboard(),
    )


# ---------- FOLDER COMMANDS ---------- #


async def folder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_user(update)
    if not await check_access(update, context):
        return

    msg = update.effective_message
    user = update.effective_user
    if msg is None:
        return

    if not context.args:
        await msg.reply_text(
            "Dùng: <code>/folder ten_thu_muc</code>\n"
            "Ví dụ: <code>/folder phim2025</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    name = " ".join(context.args).strip()
    if not name:
        await msg.reply_text(
            "Tên thư mục không hợp lệ.",
            reply_markup=get_main_keyboard(),
        )
        return

    folder_id = db.get_or_create_folder(user.id, name)
    context.chat_data["current_folder_id"] = folder_id

    bot_username = context.bot.username
    link = build_folder_deeplink(bot_username, folder_id)

    await msg.reply_text(
        "✅ Đã chọn thư mục:\n"
        f"📂 Tên: <b>{name}</b>\n"
        f"🆔 ID: <code>{folder_id}</code>\n\n"
        f"🔗 Link thư mục: {link}\n\n"
        "Giờ bạn có thể gửi file để up vào thư mục này.",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_main_keyboard(),
    )


async def myfolders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_user(update)
    if not await check_access(update, context):
        return

    msg = update.effective_message
    user = update.effective_user
    if msg is None:
        return

    folders = db.get_folders_by_owner(user.id)
    if not folders:
        await msg.reply_text(
            "Bạn chưa có thư mục nào. Dùng /folder để tạo.",
            reply_markup=get_main_keyboard(),
        )
        return

    bot_username = context.bot.username
    lines = ["📂 Các thư mục của bạn:\n"]
    for f in folders:
        link = build_folder_deeplink(bot_username, f["id"])
        lines.append(
            f"• <b>{f['name']}</b> (ID: <code>{f['id']}</code>)\n  Link: {link}"
        )

    await msg.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_main_keyboard(),
    )


async def folderlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_user(update)
    if not await check_access(update, context):
        return

    msg = update.effective_message
    if msg is None:
        return

    current_folder_id = context.chat_data.get("current_folder_id")
    if not current_folder_id:
        await msg.reply_text(
            "Bạn chưa chọn thư mục nào.\n"
            "Dùng /folder <tên> để tạo hoặc chọn thư mục trước.",
            reply_markup=get_main_keyboard(),
        )
        return

    folder = db.get_folder_by_id(current_folder_id)
    if not folder:
        await msg.reply_text(
            "Thư mục hiện tại không tồn tại (có thể đã xoá).",
            reply_markup=get_main_keyboard(),
        )
        return

    bot_username = context.bot.username
    link = build_folder_deeplink(bot_username, current_folder_id)

    await msg.reply_text(
        "📂 Thư mục hiện tại:\n"
        f"Tên: <b>{folder['name']}</b>\n"
        f"ID: <code>{folder['id']}</code>\n\n"
        f"🔗 Link thư mục: {link}",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_main_keyboard(),
    )


async def searchfolder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_user(update)
    if not await check_access(update, context):
        return

    msg = update.effective_message
    user = update.effective_user
    if msg is None:
        return

    if not context.args:
        await msg.reply_text(
            "Dùng: <code>/searchfolder tu_khoa</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    keyword = " ".join(context.args).strip()
    folders = db.search_folders(user.id, keyword)
    if not folders:
        await msg.reply_text(
            "Không tìm thấy thư mục nào khớp.",
            reply_markup=get_main_keyboard(),
        )
        return

    bot_username = context.bot.username
    lines = [f"Kết quả tìm thư mục với từ khóa <b>{keyword}</b>:\n"]
    for f in folders:
        link = build_folder_deeplink(bot_username, f["id"])
        lines.append(
            f"• <b>{f['name']}</b> (ID: <code>{f['id']}</code>)\n  Link: {link}"
        )

    await msg.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_main_keyboard(),
    )


# ---------- ADMIN COMMANDS (OWNER) ---------- #


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None:
        return

    if user is None or user.id != OWNER_ID:
        await msg.reply_text(
            "❌ Bạn không có quyền dùng lệnh này.",
            reply_markup=get_main_keyboard(),
        )
        return

    if not context.args:
        await msg.reply_text(
            "Dùng: <code>/approve TELEGRAM_ID</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await msg.reply_text(
            "ID không hợp lệ.",
            reply_markup=get_main_keyboard(),
        )
        return

    db.set_user_approved(target_id, True)
    await msg.reply_text(
        f"✅ Đã duyệt user {target_id} dùng bot.",
        reply_markup=get_main_keyboard(),
    )

    try:
        await context.bot.send_message(
            target_id,
            "✅ Admin đã duyệt cho bạn sử dụng bot. Bạn có thể dùng /upload, /folder...",
        )
    except Exception:
        pass


async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if msg is None:
        return

    if user is None or user.id != OWNER_ID:
        await msg.reply_text(
            "❌ Bạn không có quyền dùng lệnh này.",
            reply_markup=get_main_keyboard(),
        )
        return

    if not context.args:
        await msg.reply_text(
            "Dùng: <code>/block TELEGRAM_ID</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await msg.reply_text(
            "ID không hợp lệ.",
            reply_markup=get_main_keyboard(),
        )
        return

    db.set_user_approved(target_id, False)
    await msg.reply_text(
        f"⛔ Đã chặn user {target_id} dùng bot.",
        reply_markup=get_main_keyboard(),
    )

    try:
        await context.bot.send_message(
            target_id,
            "⛔ Admin đã chặn quyền sử dụng bot của bạn.",
        )
    except Exception:
        pass


# ---------------------- FILE HANDLERS --------------------------- #


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return

    msg = update.effective_message
    if msg is None:
        return

    doc = msg.document
    file_db_id = await save_file_to_db(
        update,
        context,
        file_obj=doc,
        file_type="document",
        filename_hint=doc.file_name,
        file_unique_id=doc.file_unique_id,
        file_id=doc.file_id,
        file_size=doc.file_size,
        mime_type=doc.mime_type,
    )

    if not file_db_id:
        return

    current_folder_id = context.chat_data.get("current_folder_id")

    # Nếu đang trong thư mục → chỉ trả link thư mục
    if current_folder_id:
        folder = db.get_folder_by_id(current_folder_id)
        folder_name = folder["name"] if folder else "không rõ"

        bot_username = context.bot.username
        folder_link = build_folder_deeplink(bot_username, current_folder_id)

        await msg.reply_text(
            "✅ File đã được lưu vào thư mục:\n"
            f"📂 <b>{folder_name}</b> (ID: <code>{current_folder
