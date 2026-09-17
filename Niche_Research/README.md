# YouTube channel research

## Giao diện Flet — YouTube Spy

Nhấp đúp **`run_ui.bat`** để mở ứng dụng. Giao diện dùng Flet 0.86.5, cùng phiên bản với ETSY_SPY.

1. Nhập `@InfinitePlanet4K`, tên handle hoặc URL kênh.
2. Đặt giới hạn `0` để lấy tất cả, hoặc nhập số video muốn thử trên mỗi tab.
3. Bấm **Crawl dữ liệu**. Có thể bấm **Dừng** để lưu phần đã lấy được.
4. Chọn lần crawl trong **Dữ liệu đã lưu**; tìm tiêu đề, sắp xếp theo lượt xem/ngày đăng.
5. Bấm **Mở CSV** để mở bằng ứng dụng bảng tính, hoặc **Thư mục** để lấy JSON và thumbnail.

Ứng dụng ưu tiên hiển thị lần crawl không giới hạn mới nhất khi mở. Lần crawl mới được lưu riêng, không ghi đè dữ liệu cũ. Thumbnail dùng file trên máy nên vẫn xem được khi offline. Giờ trên thẻ video là giờ Việt Nam (UTC+7); `created_time` trong file xuất giữ UTC, hoặc ngày nếu nguồn chỉ có ngày. Lượt xem là số liệu tại thời điểm thu thập.

Máy hiện tại đã có Python và Flet. Trên máy khác cần Python 3.10+ rồi chạy `python -m pip install -r requirements.txt`, sau đó `python main.py`. Giữ `tools/yt-dlp.exe` đi cùng ứng dụng. `run_ui.bat` ưu tiên `.venv`, tiếp đến Python 3.14 tại máy hiện tại, rồi Python trong PATH.

Mã nguồn: `main.py` (giao diện), `crawler.py` (crawl/xuất dữ liệu). Kiểm tra: `python -m unittest -v test_app`.

Tham khảo: [Flet](https://flet.dev/docs/controls/page/), [yt-dlp](https://github.com/yt-dlp/yt-dlp).

## Chạy script PowerShell

Chay trong PowerShell tai thu muc nay:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\crawl-channel.ps1 -Channel '@InfinitePlanet4K'
# Thu voi toi da 3 video moi playlist/tab:
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\crawl-channel.ps1 -Channel '@InfinitePlanet4K' -Limit 3
```

Moi lan chay luu rieng vao `<channel>/<YYYYMMDD_HHMMSS_mmm>/`:
- `videos.csv`: bang UTF-8 mo bang Excel.
- `videos.json`: du lieu co cau truc.
- `thumbnails/`: anh thumbnail goc, ten file la video ID.
- `metadata/`: metadata goc de doi chieu.
- `summary.json`, `crawl.log`: thong ke va loi/nhung video bi bo qua.

`created_time` la thoi gian dang video cong khai, khong phai thoi gian tao file. `created_time_precision` phan biet timestamp UTC, ngay dang, hoac khong co du lieu. Khong tu suy ra gio neu chi co ngay. `view_count` la luot xem tai thoi diem thu thap, co the thay doi. Gia tri thieu de trong/null.

URL kenh goc lay cac tab video ma yt-dlp ho tro (co the gom Videos, Shorts, Streams). Video rieng tu/da xoa/bi gioi han co the khong lay duoc; xem log. Script khong tai video, khong yeu cau cookie hay API key.

Cong cu: [yt-dlp chinh thuc](https://github.com/yt-dlp/yt-dlp). Ban Windows standalone duoc luu tai `tools/yt-dlp.exe`. Neu chua co, tai tu https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe. YouTube co the thay doi hoac chan request; cap nhat cong cu khi can.
