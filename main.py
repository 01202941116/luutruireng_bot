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
    keyboard = [[KeyboardButton("/upload"), KeyboardButton("/getlink")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def register_user(update: Update):
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
    user = update.effective_user
    if user is None:
        return False

    if OWNER_ID and user.id == OWNER_ID:
        return True

    row = db.get_user_by_telegram_id(user.id)
    if row and row["is_approved"]:
        return True

    message = update.effective_message
    await message.reply_text(
        "🔒 Đây là bot kín.\n"
        "Bạn chưa được admin duyệt sử dụng.\n"
        "Vui lòng chờ admin kiểm tra và mở quyền."
    )

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
    """Xử lý /start + deep-link."""
    logger.info("Received /start from user %s, args=%s",
                update.effective_user.id if update.effective_user else "?", context.args)
    await register_user(update)
    message = update.effective_message

    args = context.args or []

    # --- deeplink /start file123 ---
    if args:
        param = args[0]

        if param.startswith("file"):
            try:
                file_db_id = int(param[4:])
            except ValueError:
                await message.reply_text("Link file không hợp lệ.")
                return

            row = db.get_file_by_id(file_db_id)
            if not row:
                await message.reply_text("Không tìm thấy file (có thể đã xoá).")
                return

            file_type = row["file_type"]
            file_id = row["file_id"]
            fname = row["filename"] or "file"

            caption = f"📁 File: <b>{fname}</b>\nID: <code>{file_db_id}</code>"

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
                    await message.reply_document(file_id, caption=caption, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Lỗi gửi file deeplink: {e}")
            return

        # --- deeplink /start folder5 ---
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

            await message.reply_text(
                f"📂 Thư mục: <b>{folder['name']}</b>\n"
                f"Số file: <b>{len(files)}</b>\n"
                "Bot sẽ gửi lần lượt các file bên dưới:",
                parse_mode="HTML",
            )

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

    # --- /start bình thường ---
    text = (
        "🤖 Bot lưu trữ file, tất cả nằm trong 1 file SQLite.\n\n"
        "📤 Cách dùng nhanh:\n"
        "• Gửi 1 file cho bot → bot trả link luôn.\n"
        "• Muốn sắp xếp theo thư mục: /folder <tên> → gửi file → /folderlink.\n\n"
        "Bot là bot kín, admin phải /approve ID thì mới upload / tạo thư mục được."
    )
    await message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )

# (giữ nguyên các handler còn lại của bạn – folder, upload, file handlers, v.v. – không cần đổi)
# ---------------------- PHẦN CUỐI MAIN Y NHƯ CŨ -------------------- #

def main():
    if not TOKEN:
        print("❌ Thiếu Token trong biến môi trường 'Token'.")
        return

    db.init_db()
    print(Fore.GREEN + "DB SQLite đã được khởi tạo.")

    app = Application.builder().token(TOKEN).build()

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

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_fallback))

    app.add_error_handler(error_handler)

    print(Fore.BLUE + "Bot is running..." + Fore.GREEN)
    app.run_polling(poll_interval=10)


if __name__ == "__main__":
    main()
