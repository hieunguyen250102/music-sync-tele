# Music Sync – Telegram → thư viện nhạc

Quét nhạc trong các group Telegram (mỗi topic/kênh = 1 DJ/producer), gộp các topic khác tên
nhưng cùng nghệ sĩ giữa nhiều group, đối chiếu với thư viện offline và chỉ tải bài còn thiếu.

## Chạy

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Mở http://127.0.0.1:8777 (tự mở trình duyệt). Server chỉ nghe ở máy bạn.

## Giao diện

| Trang | Dùng để |
|---|---|
| **Tổng quan** | Danh sách các bước cần làm khi dùng lần đầu, ô thống kê (bấm để lọc), các nghệ sĩ có nhiều bài mới, nút **Đồng bộ ngay** (quét + đối chiếu) |
| **Bài mới** | Lọc theo Mới / Cần xem lại / Đã có, theo nghệ sĩ, tìm kiếm, chỉ hiện file lossless. Tải hoặc bỏ qua từng bài hay nhiều bài đã chọn |
| **Nguồn & nghệ sĩ** | Chọn group cần theo dõi. **Gộp nghệ sĩ**: bấm *Tự gợi ý gộp* để app tự nhận ra `ARTBAT` / `🎧 ARTBAT (UA)` / `Artbat Music` là cùng một người |
| **Cài đặt** | API và đăng nhập Telegram, chọn thư mục nhạc bằng hộp duyệt thư mục, độ nhạy so khớp, số luồng tải và quét |

Bảng tiến trình nổi ở góc phải dưới, có nút *Dừng* và *Nhật ký*. Nút ở góc trên bên phải đổi giữa giao diện sáng, tối và theo hệ thống.

## Nghe thử trước khi tải

Bấm nút ▶ ở mỗi dòng để nghe ngay, không cần chờ tải cả bài. Nhạc phát thẳng từ Telegram và tua được.
Các đoạn đã nghe được giữ lại (tối đa ~200 MB) nên tua lại không phải tải lại.

- **So sánh A/B** (bài *Cần xem lại*): chuyển giữa *Bản Telegram* và *Bản trong máy*, vẫn giữ nguyên vị trí đang nghe.
- **Tải** / **Bỏ qua** ngay trên thanh phát; app tự chuyển sang bài kế tiếp. Bài cần tải được xếp vào hàng chờ, tải dần ở chế độ nền.
- Phím tắt: `Space` phát/dừng · `←` `→` tua 15s · `1`–`9` nhảy tới 10–90% · `N`/`P` bài tiếp/trước ·
  `D` tải · `X` bỏ qua · `C` đổi bản Telegram/trong máy · `Esc` đóng.
- Trình duyệt phát được MP3, FLAC, WAV, M4A, OGG/Opus. Riêng AIFF/WMA/ALAC thường không phát được, app sẽ báo trước.

## Cách so khớp tên bài

Mỗi tên được tách thành **nghệ sĩ**, **tên bài chính**, **phần trong ngoặc** (remixer…) và **phiên bản** (extended/radio/vip…):

- Chuẩn hoá: bỏ dấu, hoa/thường, đuôi file, số thứ tự, `[320kbps]`, `[Label]`, link, `@kênh`, `(Original Mix)`, `feat.`.
- Viết tắt: `rmx` = `remix`, `ext` = `extended mix`, `boot` = `bootleg`… → `you - veedee remix.mp3` = `you (veedee) rmx.wav`.
- Tên file không có nghệ sĩ → dùng tên nghệ sĩ của kênh.
- **Cần xem lại**: khác phiên bản, khác remixer, khác nghệ sĩ nhưng trùng tên bài, tên bài quá ngắn, lệch thời lượng.
- **Mới**: tên bài chính khác hẳn (kể cả khi cùng nghệ sĩ và cùng "Extended Mix").
- Cùng bài ở nhiều group → 1 dòng (×N), chọn bản lossless/lớn nhất.

## Hiệu năng

- So khớp dùng chỉ mục từ khoá: ~0,2 ms/bài (5.000 bài với thư viện 20.000 file < 2 giây).
- Thư viện offline được cache (sqlite + bộ nhớ); chỉ đọc lại tag của file mới/đổi, đọc nhiều luồng.
- Quét Telegram: nhiều topic song song, bỏ khoảng chờ 1s/100 tin mặc định của Telethon, chỉ đọc tin mới.
- Tải: nhiều bài song song, mã hoá bằng `cryptg` (C) thay vì Python thuần.

## Dòng lệnh (tuỳ chọn, sau khi đã đăng nhập bằng GUI)

```bash
.venv/bin/python music_sync.py scan
.venv/bin/python music_sync.py diff
.venv/bin/python music_sync.py download
```

`music_sync.session` là phiên đăng nhập Telegram (tương đương quyền vào tài khoản), **không chia sẻ**.
