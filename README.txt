# luutruireng_bot_uptime

📦 Bot Telegram lưu file nội bộ (ảnh, video, tài liệu...) chỉ dành cho bạn.
🌐 Kết hợp Flask để hoạt động liên tục trên Render.com + UptimeRobot.

## 🔧 Cài đặt

1. Cài thư viện:
```
pip install -r requirements.txt
```

2. Sửa file `bot.py`:
   - Dán Telegram bot token vào biến `TOKEN`
   - Thay `OWNER_ID` bằng Telegram user ID của bạn (xem tại @userinfobot)

## 🚀 Chạy bot:
```
python bot.py
```

## 💡 Dùng với Render + UptimeRobot

- Deploy bot lên Render.com
- UptimeRobot sẽ ping `https://<tên_app>.onrender.com/` để giữ bot không bị ngủ
