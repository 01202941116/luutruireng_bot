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
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import db

# ---------------------- CONFIG --------------------------- #

load_dotenv()
TOKEN = os.getenv("Token")  # đặt trong .env
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)  # ID chủ bot

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------- HELPERS --------------------------- #


def sanitize_filename(name: str) -> str:
    """Làm sạch tên file để lưu DB."""
    name = os.path.basename(name)
    return name.replace("\n", "_").replace("\r", "_")


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Bàn phím bên dưới màn hình."""
    keyboard = [[KeyboardButton("/upload"), KeyboardButton("/getlink")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def register_user(update: Update):
    """Lưu / cập nhật user vào DB."""
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
    if user is None:
        return False

    # Chủ bot luôn được phép
    if OWNER_ID and user.id == OWNER_ID:
        return True

    row = db.get_user_by_telegram_id(user.id)
    if row and row["is_approved"]:
        return True

    # Chưa được duyệt
    message = update.effective_message
    await message.reply_text(
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
    """Download file vào RAM, lưu BLOB vào DB, trả về file_db_id."""
    user = update.effective_user
    message = update.effective_message

    if user is None:
        await message.reply_text("Lỗi: không lấy được thông tin user.")
        return None

    await register_user(update)

    current_folder_id = context.chat_data.get("current_folder_id")

    if filename_hint:
        filename = sanitize_filename(filename_hint)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{file_type}_{file_unique_id}_{ts}"

    tg_file = await file_obj.get_file()
    file_bytes = await tg_file.download_as_bytearray()

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

    context.chat_data["last_file_db_id"] = file_db_id
    return file_db_id


def build_file_deeplink(bot_username: str, file_db_id: int) -> str:
    return f"https://t.me/{bot_username}?start=file{file_db_id}"


def build_folder_deeplink(bot_username: str, folder_id: int) -> str:
    return f"https://t.me/{bot_username}?start=folder{folder_id}"


# ---------------------- COMMAND HANDLERS --------------------------- #


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start bình thường → hiện hướng dẫn + menu.
    /start file123    → gửi trực tiếp file.
    /start folder5    → gửi lần lượt tất cả file trong thư mục.
    """
    await register_user(update)
    message = update.effective_message
    args = context.args or []

    # Deep-link
    if args:
        param = args[0]

        # --------- XEM 1 FILE QUA LINK --------- #
        if param.startswith("file"):
            try:
                file_db_id = int(param[4:])
            except ValueError:
                await message.reply_text("Link file không hợp lệ.")
                return

            row = db.get_file_by_id(file_db_id)
            if not row:
                await message.reply_text("Không tìm thấy file (có thể đã bị xoá).")
                return

            file_type = row["file_type"]
            file_id = row["file_id"]
            fname = row["filename"] or "file"

            caption = f"📁 File: <b>{fname}</b>\nID: <code>{file_db_id}</code>"

            try:
                # Ưu tiên dùng file_id Telegram (nhanh, không tốn băng thông)
                if file_type == "video":
                    await message.reply_video(file_id, caption=caption, parse_mode="HTML")
                elif file_type == "photo":
                    await message.reply_photo(file_id, caption=caption, parse_mode="HTML")
                elif file_type == "audio":
                    await message.reply_audio(file_id, caption=caption, parse_mode="HTML")
                elif file_type == "voice":
                    await message.reply_voice(file_id, caption=caption, parse_mode="HTML")
                else:
                    await message.reply_document(file_id, caption=caption, parse_mode="HTML")
                return
            except Exception as e:
                # Nếu gửi bằng file_id lỗi → fallback dùng BLOB
                logger.error(f"Lỗi gửi file bằng file_id, fallback sang BLOB: {e}")

            blob = row["file_blob"]
            if blob is None:
                await message.reply_text("Không thể gửi file: thiếu dữ liệu BLOB.")
                return

            bio = BytesIO(blob)
            bio.name = fname
            await message.reply_document(
                document=bio,
                filename=fname,
                caption=caption,
                parse_mode="HTML",
            )
            return

        # --------- XEM 1 THƯ MỤC QUA LINK --------- #
        if param.startswith("folder"):
            try:
                folder_id = int(param[6:])
            except ValueError:
                await message.reply_text("Link thư mục không hợp lệ.")
                return

            folder = db.get_folder_by_id(folder_id)
            if not folder:
                await message.reply_text("Không tìm thấy thư mục (có thể đã xoá).")
                return

            files = db.get_files_by_folder(folder_id)
            if not files:
                await message.reply_text(
                    f"📂 Thư mục <b>{folder['name']}</b> hiện chưa có file nào.",
                    parse_mode="HTML",
                )
                return

            # Tin tiêu đề thư mục
            await message.reply_text(
                f"📂 Thư mục: <b>{folder['name']}</b>\n"
                f"Số file: <b>{len(files)}</b>\n"
                "Bot sẽ gửi lần lượt các file bên dưới:",
                parse_mode="HTML",
            )

            # Gửi từng file để người xem xem / tải trực tiếp
            for f in files[:50]:
                file_type = f["file_type"]
                file_id = f["file_id"]
                fname = f["filename"] or f"file_{f['id']}"
                caption = f"{fname}\nID: <code>{f['id']}</code>"

                try:
                    if file_type == "video":
                        await message.reply_video(file_id, caption=caption, parse_mode="HTML")
                    elif file_type == "photo":
                        await message.reply_photo(file_id, caption=caption, parse_mode="HTML")
                    elif file_type == "audio":
                        await message.reply_audio(file_id, caption=caption, parse_mode="HTML")
                    elif file_type == "voice":
                        await message.reply_voice(file_id, caption=caption, parse_mode="HTML")
                    else:
                        await message.reply_document(
                            file_id, caption=caption, parse_mode="HTML"
                        )
                except Exception as e:
                    logger.error(f"Lỗi gửi file trong thư mục: {e}")

            return

    # --------- /start BÌNH THƯỜNG --------- #
    text = (
        "🤖 Bot lưu trữ file (BLOB SQLite).\n\n"
        "📤 Cách dùng nhanh:\n"
        "• Gửi 1 file cho bot → bot trả link ngay.\n"
        "• Muốn sắp xếp theo thư mục:\n"
        "   /folder tên_thư_mục → gửi file → /folderlink để lấy link thư mục.\n\n"
        "🔗 Khi gửi link thư mục cho khách:\n"
        "   Khách bấm Start bot → bot gửi lần lượt TẤT CẢ file trong thư mục đó\n"
        "   để họ xem / tải trực tiếp (không phải nhấn thêm link con nữa).\n\n"
        "⚠️ Bot kín, admin phải /approve ID thì mới được upload / tạo thư mục."
    )
    await message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📚 Lệnh bot:\n\n"
        "🔹 /start - Bắt đầu / xem hướng dẫn\n"
        "🔹 /help  - Xem lại hướng dẫn\n"
        "🔹 /me    - Xem ID + username Telegram\n\n"
        "📤 UPLOAD:\n"
        "🔹 Gửi file trực tiếp cho bot → bot trả link.\n"
        "🔹 /upload - Nhắc lại cách dùng.\n\n"
        "📁 THƯ MỤC:\n"
        "🔹 /folder <tên>       - Tạo hoặc chọn thư mục\n"
        "🔹 /myfolders          - Xem thư mục của bạn + link\n"
        "🔹 /folderlink         - Lấy link thư mục đang chọn\n"
        "🔹 /searchfolder <từ>  - Tìm thư mục theo tên\n\n"
        "👑 ADMIN (OWNER):\n"
        "🔹 /approve TELEGRAM_ID - Duyệt user dùng bot\n"
        "🔹 /block TELEGRAM_ID   - Chặn user dùng bot",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.effective_message.reply_text(
        "Thông tin Telegram của bạn:\n"
        f"ID: <code>{user.id}</code>\n"
        f"Username: <code>{user.username or 'không có'}</code>\n\n"
        "Dùng ID này để admin /approve cho bạn hoặc set OWNER_ID cho bot.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_user(update)
    if not await check_access(update, context):
        return

    await update.effective_message.reply_text(
        "✅ Bấm nút /upload hoặc gõ /upload.\n"
        "▶ Sau đó dùng nút 📎 của Telegram để chọn file (có thể chọn nhiều).\n"
        "📌 Nếu đã chọn thư mục bằng /folder, mọi file sẽ được lưu vào thư mục đó.\n"
        "📌 Mỗi file gửi xong bot sẽ trả link (hoặc link thư mục) cho bạn copy.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def getlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_user(update)
    if not await check_access(update, context):
        return

    user = update.effective_user
    row = db.get_last_file_by_owner(user.id)
    if not row:
        await update.effective_message.reply_text(
            "❌ Bạn chưa upload file nào.\n"
            "Hãy gửi 1 file cho bot (hoặc gõ /upload rồi gửi file) trước.",
            reply_markup=get_main_keyboard(),
        )
        return

    file_db_id = row["id"]
    bot_username = context.bot.username
    link = build_file_deeplink(bot_username, file_db_id)

    await update.effective_message.reply_text(
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

    message = update.effective_message
    user = update.effective_user

    if not context.args:
        await message.reply_text(
            "Dùng: <code>/folder ten_thu_muc</code>\n"
            "Ví dụ: <code>/folder phim2025</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    name = " ".join(context.args).strip()
    if not name:
        await message.reply_text(
            "Tên thư mục không hợp lệ.",
            reply_markup=get_main_keyboard(),
        )
        return

    folder_id = db.get_or_create_folder(user.id, name)
    context.chat_data["current_folder_id"] = folder_id

    bot_username = context.bot.username
    link = build_folder_deeplink(bot_username, folder_id)

    await message.reply_text(
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

    user = update.effective_user
    message = update.effective_message

    folders = db.get_folders_by_owner(user.id)
    if not folders:
        await message.reply_text(
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

    await message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_main_keyboard(),
    )


async def folderlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await register_user(update)
    if not await check_access(update, context):
        return

    message = update.effective_message
    current_folder_id = context.chat_data.get("current_folder_id")
    if not current_folder_id:
        await message.reply_text(
            "Bạn chưa chọn thư mục nào.\n"
            "Dùng /folder <tên> để tạo hoặc chọn thư mục trước.",
            reply_markup=get_main_keyboard(),
        )
        return

    folder = db.get_folder_by_id(current_folder_id)
    if not folder:
        await message.reply_text(
            "Thư mục hiện tại không tồn tại (có thể đã xoá).",
            reply_markup=get_main_keyboard(),
        )
        return

    bot_username = context.bot.username
    link = build_folder_deeplink(bot_username, current_folder_id)

    await message.reply_text(
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

    message = update.effective_message
    user = update.effective_user

    if not context.args:
        await message.reply_text(
            "Dùng: <code>/searchfolder tu_khoa</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    keyword = " ".join(context.args).strip()
    folders = db.search_folders(user.id, keyword)
    if not folders:
        await message.reply_text(
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

    await message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_main_keyboard(),
    )


# ---------- ADMIN COMMANDS (OWNER) ---------- #


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await message.reply_text(
            "❌ Bạn không có quyền dùng lệnh này.",
            reply_markup=get_main_keyboard(),
        )
        return

    if not context.args:
        await message.reply_text(
            "Dùng: <code>/approve TELEGRAM_ID</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await message.reply_text(
            "ID không hợp lệ.",
            reply_markup=get_main_keyboard(),
        )
        return

    db.set_user_approved(target_id, True)
    await message.reply_text(
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
    message = update.effective_message
    user = update.effective_user
    if user is None or user.id != OWNER_ID:
        await message.reply_text(
            "❌ Bạn không có quyền dùng lệnh này.",
            reply_markup=get_main_keyboard(),
        )
        return

    if not context.args:
        await message.reply_text(
            "Dùng: <code>/block TELEGRAM_ID</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await message.reply_text(
            "ID không hợp lệ.",
            reply_markup=get_main_keyboard(),
        )
        return

    db.set_user_approved(target_id, False)
    await message.reply_text(
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

    doc = update.effective_message.document
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

    bot_username = context.bot.username
    link = build_file_deeplink(bot_username, file_db_id)
    await update.effective_message.reply_text(
        "✅ File đã được lưu!\n"
        f"🆔 ID: <code>{file_db_id}</code>\n"
        f"🔗 Link: {link}\n\n"
        "Bạn có thể copy link này để chia sẻ.\n"
        "Hoặc gõ /getlink để lấy lại link file gần nhất.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return

    photo = update.effective_message.photo[-1]
    file_db_id = await save_file_to_db(
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

    if not file_db_id:
        return

    bot_username = context.bot.username
    link = build_file_deeplink(bot_username, file_db_id)
    await update.effective_message.reply_text(
        "✅ Ảnh đã được lưu!\n"
        f"🆔 ID: <code>{file_db_id}</code>\n"
        f"🔗 Link: {link}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return

    video = update.effective_message.video
    file_db_id = await save_file_to_db(
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

    if not file_db_id:
        return

    bot_username = context.bot.username
    link = build_file_deeplink(bot_username, file_db_id)
    await update.effective_message.reply_text(
        "✅ Video đã được lưu!\n"
        f"🆔 ID: <code>{file_db_id}</code>\n"
        f"🔗 Link: {link}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return

    audio = update.effective_message.audio
    file_db_id = await save_file_to_db(
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

    if not file_db_id:
        return

    bot_username = context.bot.username
    link = build_file_deeplink(bot_username, file_db_id)
    await update.effective_message.reply_text(
        "✅ Audio đã được lưu!\n"
        f"🆔 ID: <code>{file_db_id}</code>\n"
        f"🔗 Link: {link}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return

    voice = update.effective_message.voice
    file_db_id = await save_file_to_db(
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

    if not file_db_id:
        return

    bot_username = context.bot.username
    link = build_file_deeplink(bot_username, file_db_id)
    await update.effective_message.reply_text(
        "✅ Voice đã được lưu!\n"
        f"🆔 ID: <code>{file_db_id}</code>\n"
        f"🔗 Link: {link}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (update.effective_message.text or "").lower().strip()
    if msg in ("hi", "hello", "chào", "alo"):
        await update.effective_message.reply_text(
            "Chào bạn 👋\n"
            "Gửi file cho bot, bot sẽ trả link để bạn copy.\n"
            "Muốn sắp xếp theo thư mục: /folder <tên> → gửi file → /folderlink.\n"
            "Bot kín: admin phải /approve ID mới upload được.",
            reply_markup=get_main_keyboard(),
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)


# ---------------------- MAIN --------------------------- #


def main():
    if not TOKEN:
        print("❌ Thiếu Token trong biến môi trường 'Token'.")
        return

    db.init_db()
    print(Fore.GREEN + "DB SQLite đã được khởi tạo.")

    app = Application.builder().token(TOKEN).build()

    # Command
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("me", me_command))
    app.add_handler(CommandHandler("upload", upload_command))
    app.add_handler(CommandHandler("getlink", getlink_command))
    app.add_handler(CommandHandler("folder", folder_command))
    app.add_handler(CommandHandler("myfolders", myfolders_command))
    app.add_handler(CommandHandler("folderlink", folderlink_command))
    app.add_handler(CommandHandler("searchfolder", searchfolder_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("block", block_command))

    # File handlers
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_fallback))

    # Error
    app.add_error_handler(error_handler)

    print(Fore.BLUE + "Bot is running..." + Fore.GREEN)
    app.run_polling(poll_interval=10)


if __name__ == "__main__":
    main()
