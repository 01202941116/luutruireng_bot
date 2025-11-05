from telegram import Update, File
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import os
from flask import Flask
import threading

TOKEN = "7874099431:AAFsfOxcO1cKNpLU2YUTjzO2D6jj3HmslhA"  # ← Token bot từ BotFather
OWNER_ID = 123456789  # ← Thay bằng Telegram user ID của bạn (@userinfobot để lấy)
SAVE_DIR = "downloads"

if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

def is_owner(user_id):
    return user_id == OWNER_ID

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return await update.message.reply_text("🚫 Bot này dùng nội bộ.")
    await update.message.reply_text("📥 Gửi file bất kỳ (ảnh, video, tài liệu...), tôi sẽ lưu lại!")

async def save_file(file: File, filename: str):
    path = os.path.join(SAVE_DIR, filename)
    await file.download_to_drive(path)
    return path

async def handle_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return await update.message.reply_text("🚫 Không được phép.")

    msg = update.message
    saved_path = ""

    if msg.document:
        file = await context.bot.get_file(msg.document.file_id)
        saved_path = await save_file(file, msg.document.file_name)
    elif msg.video:
        file = await context.bot.get_file(msg.video.file_id)
        filename = f"video_{msg.video.file_unique_id}.mp4"
        saved_path = await save_file(file, filename)
    elif msg.photo:
        file = await context.bot.get_file(msg.photo[-1].file_id)
        filename = f"photo_{msg.photo[-1].file_unique_id}.jpg"
        saved_path = await save_file(file, filename)
    elif msg.audio:
        file = await context.bot.get_file(msg.audio.file_id)
        filename = f"audio_{msg.audio.file_unique_id}.mp3"
        saved_path = await save_file(file, filename)
    elif msg.voice:
        file = await context.bot.get_file(msg.voice.file_id)
        filename = f"voice_{msg.voice.file_unique_id}.ogg"
        saved_path = await save_file(file, filename)

    if saved_path:
        await msg.reply_text(f"✅ Đã lưu: `{saved_path}`", parse_mode="Markdown")

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_owner(update.effective_user.id):
        await update.message.reply_text("❓ Lệnh không hợp lệ. Gửi file để lưu.")
    else:
        await update.message.reply_text("🚫 Bạn không có quyền dùng bot.")

def telegram_bot():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_files))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    print("🤖 Bot Telegram đang chạy...")
    app.run_polling()

# Tạo Flask endpoint để UptimeRobot giữ cho bot luôn hoạt động
flask_app = Flask(__name__)
@flask_app.route("/")
def home():
    return "✅ Bot is alive!"

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    telegram_bot()
