# Telegram Storage Bot (AZ Style)

Bot lưu trữ file trên Telegram, tương tự bot AZ 云文件存储 (Trung Quốc).

## Tính năng

- Lưu trữ hình ảnh, video, tài liệu… gửi cho bot.
- Lệnh `/upload`: bật chế độ upload, sau đó gửi file để lưu.
- Lệnh `/getlink`: tạo 1 link thư mục chia sẻ dạng:

  `https://t.me/<BOT_USERNAME>?start=share_<token>`

- Lệnh `/myfiles`: xem nhanh tối đa 30 file gần nhất của bạn.
- Toàn bộ thông tin người dùng, file, token chia sẻ… đều lưu trong **SQLite** (`bot_data.db`).
  Bạn có thể backup file `.db` này, mang sang server khác vẫn giữ nguyên dữ liệu.

## Cấu trúc project

- `main.py` – mã nguồn bot (Python).
- `schema.sql` – file SQL schema (nếu muốn khởi tạo DB thủ công).
- `requirements.txt` – thư viện cần cài.
- `Procfile` – dùng cho Railway/Heroku (chạy bot ở dạng worker).
- `bot_data.db` – file SQLite sẽ được tạo tự động khi bot chạy lần đầu.

## Cài đặt local

1. Tạo bot qua BotFather, lấy **BOT_TOKEN** và ghi lại **username** của bot (ví dụ: `az_cloud_storage_bot`).
2. Cài Python 3.10+.
3. Cài thư viện:

   ```bash
   pip install -r requirements.txt
   ```

4. Tạo file `.env` hoặc export biến môi trường:

   - `BOT_TOKEN` – token của bot.
   - `BOT_USERNAME` – username bot, **không có @** (vd: `az_cloud_storage_bot`).

   Ví dụ trên Linux:

   ```bash
   export BOT_TOKEN="123456:ABC-DEF..."
   export BOT_USERNAME="az_cloud_storage_bot"
   ```

5. Chạy bot:

   ```bash
   python main.py
   ```

6. Mở Telegram, tìm bot theo username, gõ `/start` để bắt đầu.

## Deploy lên Railway (hoặc nền tảng tương tự)

1. Đẩy thư mục project này lên GitHub.
2. Tạo project mới trên Railway, kết nối với repo GitHub.
3. Railway sẽ nhận file `Procfile` và tạo service dạng **worker**.
4. Vào phần **Variables** trên Railway, thêm:

   - `BOT_TOKEN` – token bot Telegram.
   - `BOT_USERNAME` – username bot (không có @).
   - (tuỳ chọn) `DB_PATH` – đường dẫn file DB, mặc định `bot_data.db`.

5. Deploy, sau khi service chạy là bot hoạt động.

> Lưu ý: Railway free có thể xoá dữ liệu khi restart/di chuyển region.  
> Nếu muốn an toàn hơn, hãy backup file `bot_data.db` định kỳ hoặc dùng volume/storage của Railway (nếu có).

## Dùng file schema.sql

Nếu muốn tạo DB trước (thay vì để code tự tạo), bạn có thể chạy:

```bash
sqlite3 bot_data.db < schema.sql
```

Sau đó chạy bot như bình thường.
