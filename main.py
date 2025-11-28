import os
import logging
from datetime import datetime
from io import BytesIO

from dotenv import load_dotenv
from colorama import Fore

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    CallbackContext,
    Filters,
)

import db

# ---------------------- CONFIG --------------------------- #

load_dotenv()
TOKEN = os.getenv("Token")
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


def register_user(update: Update):
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


def check_access(update: Update, context: CallbackContext) -> bool:
    """
    Bot kín: chỉ user đã được OWNER duyệt mới được dùng các lệnh lưu trữ.
    /start, /help, /me vẫn dùng được để xem hướng dẫn.
    """
    user = update.effective_user
    message = update.message

    if user is None or message is None:
        return False

    # Chủ bot luôn được phép
    if OWNER_ID and user.id == OWNER_ID:
        return True

    row = db.get_user_by_telegram_id(user.id)
    if row and row["is_approved"]:
        return True

    # Chưa được duyệt
    message.reply_text(
        "🔒 Đây là bot kín.\n"
        "Bạn chưa được admin duyệt sử dụng.\n"
        "Vui lòng chờ admin kiểm tra và mở quyền."
    )

    # Gửi thông báo tới owner (nếu có)
    if OWNER_ID:
        try:
            context.bot.send_message(
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


def save_file_to_db(
    update: Update,
    context: CallbackContext,
    file_obj,
    file_type: str,
    filename_hint: str | None,
    file_unique_id: str,
    file_id: str,
    file_size: int | None = None,
    mime_type: str | None = None,
):
    """Tải file, lưu BLOB vào DB, trả về file_db_id."""
    user = update.effective_user
    message = update.message

    if user is None or message is None:
        if message:
            message.reply_text("Lỗi: không lấy được thông tin user.")
        return None

    register_user(update)

    # current_folder_id lưu trong context.chat_data
    chat_data = context.chat_data
    current_folder_id = chat_data.get("current_folder_id")

    if filename_hint:
        filename = sanitize_filename(filename_hint)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{file_type}_{file_unique_id}_{ts}"

    # tải file từ Telegram
    tg_file = file_obj.get_file()
    file_bytes = tg_file.download_as_bytearray()

    file_db_id = db.insert_file(
        owner_telegram_id=user.id,
        folder_id=current_folder_id,
        file_type=file_type,
        file_unique_id=file_unique_id,
        file_id=file_id,
        filename=filename,
        file_bytes=file_bytes,
        file_size=file_size,
        mime_type=mime_type,
    )

    chat_data["last_file_db_id"] = file_db_id
    return file_db_id


def build_file_deeplink(bot_username: str, file_db_id: int) -> str:
    return f"https://t.me/{bot_username}?start=file{file_db_id}"


def build_folder_deeplink(bot_username: str, folder_id: int) -> str:
    return f"https://t.me/{bot_username}?start=folder{folder_id}"


# ---------------------- COMMAND HANDLERS --------------------------- #


def start_command(update: Update, context: CallbackContext):
    register_user(update)

    args = context.args or []
    message = update.message
    if message is None:
        return

    # Deep-link: /start file123 hoặc /start folder5
    if args:
        param = args[0]

        # Xem file (cho phép cả người chưa được duyệt – chỉ xem được khi có link)
        if param.startswith("file"):
            try:
                file_db_id = int(param[4:])
            except ValueError:
                message.reply_text("Link file không hợp lệ.")
                return

            row = db.get_file_by_id(file_db_id)
            if not row:
                message.reply_text("Không tìm thấy file (có thể đã bị xoá).")
                return

            blob = row["file_blob"]
            if blob is None:
                message.reply_text("Dữ liệu file không tồn tại.")
                return

            bio = BytesIO(blob)
            fname = row["filename"] or "file"
            bio.name = fname

            message.reply_document(
                document=bio,
                filename=fname,
                caption=f"📁 File ID: {file_db_id}",
            )
            return

        # Xem thư mục
        if param.startswith("folder"):
            try:
                folder_id = int(param[6:])
            except ValueError:
                message.reply_text("Link thư mục không hợp lệ.")
                return

            folder = db.get_folder_by_id(folder_id)
            if not folder:
                message.reply_text("Không tìm thấy thư mục (có thể đã xoá).")
                return

            files = db.get_files_by_folder(folder_id)
            if not files:
                message.reply_text(
                    f"📂 Thư mục <b>{folder['name']}</b> hiện chưa có file nào.",
                    parse_mode="HTML",
                )
                return

            bot_username = context.bot.username
            lines = [
                f"📂 Thư mục: <b>{folder['name']}</b>\n",
                "Danh sách file:",
            ]
            for f in files[:50]:
                link = build_file_deeplink(bot_username, f["id"])
                fname = f["filename"] or f"file_{f['id']}"
                lines.append(f"• <a href=\"{link}\">{fname}</a>")

            message.reply_text(
                "\n".join(lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

    # /start bình thường
    text = (
        "🤖 Bot lưu trữ file, tất cả nằm trong 1 file SQLite.\n\n"
        "📤 Cách dùng nhanh:\n"
        "• Gửi 1 file cho bot → bot trả link luôn.\n"
        "• Muốn sắp xếp theo thư mục: /folder <tên> → gửi file → /folderlink.\n\n"
        "Bot là bot kín, admin phải /approve ID thì mới upload / tạo thư mục được."
    )
    message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


def help_command(update: Update, context: CallbackContext):
    message = update.message
    if message is None:
        return

    message.reply_text(
        "📚 Lệnh bot:\n\n"
        "🔹 /start - Bắt đầu / xem hướng dẫn\n"
        "🔹 /help - Xem lại hướng dẫn\n"
        "🔹 /me - Xem ID + username Telegram\n\n"
        "📤 UPLOAD:\n"
        "🔹 Gửi file trực tiếp cho bot, bot tự trả link.\n"
        "🔹 /upload - Nhắc cách dùng upload + hiện bàn phím\n\n"
        "📁 THƯ MỤC:\n"
        "🔹 /folder <tên> - Tạo hoặc chọn thư mục\n"
        "🔹 /myfolders - Xem thư mục của bạn\n"
        "🔹 /folderlink - Lấy link thư mục đang chọn\n"
        "🔹 /searchfolder <từ khóa> - Tìm thư mục theo tên\n\n"
        "👑 ADMIN (OWNER):\n"
        "🔹 /approve TELEGRAM_ID - Duyệt user dùng bot\n"
        "🔹 /block TELEGRAM_ID   - Chặn user dùng bot",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


def me_command(update: Update, context: CallbackContext):
    user = update.effective_user
    message = update.message
    if message is None or user is None:
        return

    text = (
        "Thông tin Telegram của bạn:\n"
        f"ID: <code>{user.id}</code>\n"
        f"Username: <code>{user.username or 'không có'}</code>\n\n"
        "Dùng ID này để admin /approve cho bạn hoặc set OWNER_ID cho bot."
    )
    message.reply_text(
        text, parse_mode="HTML", reply_markup=get_main_keyboard()
    )


def upload_command(update: Update, context: CallbackContext):
    register_user(update)
    if not check_access(update, context):
        return

    message = update.message
    if message is None:
        return

    message.reply_text(
        "✅ Bạn cứ gửi file cho bot (dùng nút 📎 để chọn file / ảnh / video ...).\n"
        "📌 Mỗi file gửi xong bot sẽ tự gửi link cho bạn copy.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


def getlink_command(update: Update, context: CallbackContext):
    register_user(update)
    if not check_access(update, context):
        return

    message = update.message
    if message is None:
        return

    user = update.effective_user
    row = db.get_last_file_by_owner(user.id)
    if not row:
        message.reply_text(
            "❌ Bạn chưa upload file nào.\n"
            "Hãy gửi 1 file cho bot (hoặc gõ /upload rồi gửi file) trước.",
            reply_markup=get_main_keyboard(),
        )
        return

    file_db_id = row["id"]
    bot_username = context.bot.username
    link = build_file_deeplink(bot_username, file_db_id)

    message.reply_text(
        "🔗 Link tải file gần nhất của bạn:\n"
        f"{link}\n\n"
        "Gửi link này cho người khác, họ bấm Start bot sẽ nhận được file.",
        reply_markup=get_main_keyboard(),
    )


# ---------- FOLDER COMMANDS ---------- #


def folder_command(update: Update, context: CallbackContext):
    register_user(update)
    if not check_access(update, context):
        return

    message = update.message
    if message is None:
        return

    user = update.effective_user
    args = context.args or []

    if not args:
        message.reply_text(
            "Dùng: <code>/folder ten_thu_muc</code>\n"
            "Ví dụ: <code>/folder phim2025</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    name = " ".join(args).strip()
    if not name:
        message.reply_text(
            "Tên thư mục không hợp lệ.",
            reply_markup=get_main_keyboard(),
        )
        return

    folder_id = db.get_or_create_folder(user.id, name)
    context.chat_data["current_folder_id"] = folder_id

    bot_username = context.bot.username
    link = build_folder_deeplink(bot_username, folder_id)

    message.reply_text(
        "✅ Đã chọn thư mục:\n"
        f"📂 Tên: <b>{name}</b>\n"
        f"🆔 ID: <code>{folder_id}</code>\n\n"
        f"🔗 Link thư mục: {link}\n\n"
        "Giờ bạn có thể gửi file để up vào thư mục này.",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_main_keyboard(),
    )


def myfolders_command(update: Update, context: CallbackContext):
    register_user(update)
    if not check_access(update, context):
        return

    message = update.message
    if message is None:
        return

    user = update.effective_user

    folders = db.get_folders_by_owner(user.id)
    if not folders:
        message.reply_text(
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

    message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_main_keyboard(),
    )


def folderlink_command(update: Update, context: CallbackContext):
    register_user(update)
    if not check_access(update, context):
        return

    message = update.message
    if message is None:
        return

    current_folder_id = context.chat_data.get("current_folder_id")
    if not current_folder_id:
        message.reply_text(
            "Bạn chưa chọn thư mục nào.\n"
            "Dùng /folder <tên> để tạo hoặc chọn thư mục trước.",
            reply_markup=get_main_keyboard(),
        )
        return

    folder = db.get_folder_by_id(current_folder_id)
    if not folder:
        message.reply_text(
            "Thư mục hiện tại không tồn tại (có thể đã xoá).",
            reply_markup=get_main_keyboard(),
        )
        return

    bot_username = context.bot.username
    link = build_folder_deeplink(bot_username, current_folder_id)

    message.reply_text(
        "📂 Thư mục hiện tại:\n"
        f"Tên: <b>{folder['name']}</b>\n"
        f"ID: <code>{folder['id']}</code>\n\n"
        f"🔗 Link thư mục: {link}",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_main_keyboard(),
    )


def searchfolder_command(update: Update, context: CallbackContext):
    register_user(update)
    if not check_access(update, context):
        return

    message = update.message
    if message is None:
        return

    user = update.effective_user
    args = context.args or []

    if not args:
        message.reply_text(
            "Dùng: <code>/searchfolder tu_khoa</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    keyword = " ".join(args).strip()
    folders = db.search_folders(user.id, keyword)
    if not folders:
        message.reply_text(
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

    message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_main_keyboard(),
    )


# ---------- ADMIN COMMANDS (OWNER) ---------- #


def approve_command(update: Update, context: CallbackContext):
    message = update.message
    user = update.effective_user
    if message is None or user is None or user.id != OWNER_ID:
        if message:
            message.reply_text(
                "❌ Bạn không có quyền dùng lệnh này.",
                reply_markup=get_main_keyboard(),
            )
        return

    args = context.args or []
    if not args:
        message.reply_text(
            "Dùng: <code>/approve TELEGRAM_ID</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        message.reply_text(
            "ID không hợp lệ.",
            reply_markup=get_main_keyboard(),
        )
        return

    db.set_user_approved(target_id, True)
    message.reply_text(
        f"✅ Đã duyệt user {target_id} dùng bot.",
        reply_markup=get_main_keyboard(),
    )

    try:
        context.bot.send_message(
            target_id,
            "✅ Admin đã duyệt cho bạn sử dụng bot. Bạn có thể dùng /upload, /folder...",
        )
    except Exception:
        pass


def block_command(update: Update, context: CallbackContext):
    message = update.message
    user = update.effective_user
    if message is None or user is None or user.id != OWNER_ID:
        if message:
            message.reply_text(
                "❌ Bạn không có quyền dùng lệnh này.",
                reply_markup=get_main_keyboard(),
            )
        return

    args = context.args or []
    if not args:
        message.reply_text(
            "Dùng: <code>/block TELEGRAM_ID</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        message.reply_text(
            "ID không hợp lệ.",
            reply_markup=get_main_keyboard(),
        )
        return

    db.set_user_approved(target_id, False)
    message.reply_text(
        f"⛔ Đã chặn user {target_id} dùng bot.",
        reply_markup=get_main_keyboard(),
    )

    try:
        context.bot.send_message(
            target_id,
            "⛔ Admin đã chặn quyền sử dụng bot của bạn.",
        )
    except Exception:
        pass


# ---------------------- FILE HANDLERS --------------------------- #


def handle_document(update: Update, context: CallbackContext):
    if not check_access(update, context):
        return

    message = update.message
    if message is None or not message.document:
        return

    doc = message.document
    file_db_id = save_file_to_db(
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

    if file_db_id:
        bot_username = context.bot.username
        link = build_file_deeplink(bot_username, file_db_id)
        message.reply_text(
            "✅ File đã được lưu!\n"
            f"🆔 ID: <code>{file_db_id}</code>\n"
            f"🔗 Link: {link}\n\n"
            "Bạn có thể copy link này để chia sẻ.\n"
            "Hoặc gõ /getlink để lấy lại link file gần nhất.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )


def handle_photo(update: Update, context: CallbackContext):
    if not check_access(update, context):
        return

    message = update.message
    if message is None or not message.photo:
        return

    photo = message.photo[-1]
    file_db_id = save_file_to_db(
        update,
        context,
        file_obj=photo,
        file_type="photo",
        filename_hint=None,
        file_unique_id=photo.file_unique_id,
        file_id=photo.file_id,
        file_size=photo.file_size,
        mime_type=None,
    )

    if file_db_id:
        bot_username = context.bot.username
        link = build_file_deeplink(bot_username, file_db_id)
        message.reply_text(
            "✅ Ảnh đã được lưu!\n"
            f"🆔 ID: <code>{file_db_id}</code>\n"
            f"🔗 Link: {link}",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )


def handle_video(update: Update, context: CallbackContext):
    if not check_access(update, context):
        return

    message = update.message
    if message is None or not message.video:
        return

    video = message.video
    file_db_id = save_file_to_db(
        update,
        context,
        file_obj=video,
        file_type="video",
        filename_hint=video.file_name,
        file_unique_id=video.file_unique_id,
        file_id=video.file_id,
        file_size=video.file_size,
        mime_type=video.mime_type,
    )

    if file_db_id:
        bot_username = context.bot.username
        link = build_file_deeplink(bot_username, file_db_id)
        message.reply_text(
            "✅ Video đã được lưu!\n"
            f"🆔 ID: <code>{file_db_id}</code>\n"
            f"🔗 Link: {link}",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )


def handle_audio(update: Update, context: CallbackContext):
    if not check_access(update, context):
        return

    message = update.message
    if message is None or not message.audio:
        return

    audio = message.audio
    file_db_id = save_file_to_db(
        update,
        context,
        file_obj=audio,
        file_type="audio",
        filename_hint=audio.file_name,
        file_unique_id=audio.file_unique_id,
        file_id=audio.file_id,
        file_size=audio.file_size,
        mime_type=audio.mime_type,
    )

    if file_db_id:
        bot_username = context.bot.username
        link = build_file_deeplink(bot_username, file_db_id)
        message.reply_text(
            "✅ Audio đã được lưu!\n"
            f"🆔 ID: <code>{file_db_id}</code>\n"
            f"🔗 Link: {link}",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )


def handle_voice(update: Update, context: CallbackContext):
    if not check_access(update, context):
        return

    message = update.message
    if message is None or not message.voice:
        return

    voice = message.voice
    file_db_id = save_file_to_db(
        update,
        context,
        file_obj=voice,
        file_type="voice",
        filename_hint=None,
        file_unique_id=voice.file_unique_id,
        file_id=voice.file_id,
        file_size=voice.file_size,
        mime_type=None,
    )

    if file_db_id:
        bot_username = context.bot.username
        link = build_file_deeplink(bot_username, file_db_id)
        message.reply_text(
            "✅ Voice đã được lưu!\n"
            f"🆔 ID: <code>{file_db_id}</code>\n"
            f"🔗 Link: {link}",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )


def text_fallback(update: Update, context: CallbackContext):
    message = update.message
    if message is None or not message.text:
        return

    msg = message.text.lower().strip()
    if msg in ("hi", "hello", "chào", "alo"):
        message.reply_text(
            "Chào bạn 👋\n"
            "Gửi file cho bot, bot sẽ trả link để bạn copy.\n"
            "Muốn sắp xếp theo thư mục: /folder <tên> → gửi file → /folderlink.\n"
            "Bot kín: admin phải /approve ID mới upload được.",
            reply_markup=get_main_keyboard(),
        )


def error_handler(update: object, context: CallbackContext):
    logger.error("Exception while handling an update:", exc_info=context.error)


# ---------------------- MAIN --------------------------- #


def main():
    if not TOKEN:
        print("❌ Thiếu Token trong biến môi trường 'Token'.")
        return

    db.init_db()
    print(Fore.GREEN + "DB SQLite đã được khởi tạo.")

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Command handlers
    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("me", me_command))
    dp.add_handler(CommandHandler("upload", upload_command))
    dp.add_handler(CommandHandler("getlink", getlink_command))
    dp.add_handler(CommandHandler("folder", folder_command))
    dp.add_handler(CommandHandler("myfolders", myfolders_command))
    dp.add_handler(CommandHandler("folderlink", folderlink_command))
    dp.add_handler(CommandHandler("searchfolder", searchfolder_command))
    dp.add_handler(CommandHandler("approve", approve_command))
    dp.add_handler(CommandHandler("block", block_command))

    # File handlers
    dp.add_handler(MessageHandler(Filters.document, handle_document))
    dp.add_handler(MessageHandler(Filters.photo, handle_photo))
    dp.add_handler(MessageHandler(Filters.video, handle_video))
    dp.add_handler(MessageHandler(Filters.audio, handle_audio))
    dp.add_handler(MessageHandler(Filters.voice, handle_voice))

    # Text
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_fallback))

    # Error handler
    dp.add_error_handler(error_handler)

    print(Fore.BLUE + "Bot is running..." + Fore.GREEN)
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
