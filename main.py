# ---------------------- MAIN --------------------------- #


def main():
    if not TOKEN:
        print("❌ Thiếu Token trong biến môi trường 'Token'.")
        return

    # Khởi tạo DB
    db.init_db()
    print(Fore.GREEN + "DB SQLite đã được khởi tạo.")

    # Tạo application (thay cho Updater kiểu cũ)
    application = Application.builder().token(TOKEN).build()

    # ---------- COMMAND HANDLERS ----------
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("me", me_command))
    application.add_handler(CommandHandler("upload", upload_command))
    application.add_handler(CommandHandler("getlink", getlink_command))

    # Folder
    application.add_handler(CommandHandler("folder", folder_command))
    application.add_handler(CommandHandler("myfolders", myfolders_command))
    application.add_handler(CommandHandler("folderlink", folderlink_command))
    application.add_handler(CommandHandler("searchfolder", searchfolder_command))

    # Admin
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("block", block_command))

    # ---------- FILE HANDLERS ----------
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )
    application.add_handler(
        MessageHandler(filters.VIDEO, handle_video)
    )
    application.add_handler(
        MessageHandler(filters.AUDIO, handle_audio)
    )
    application.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )

    # ---------- TEXT ----------
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_fallback)
    )

    # ---------- ERROR ----------
    application.add_error_handler(error_handler)

    print(Fore.BLUE + "Bot is running..." + Fore.GREEN)
    application.run_polling(poll_interval=10)


if __name__ == "__main__":
    main()
