# Video Merger

Ứng dụng desktop Windows dùng Python + Flet để ghép video theo tên file.

## Chạy ứng dụng

Máy hiện tại đã có Flet và `bin/ffmpeg.exe`, `bin/ffprobe.exe`: mở **run.bat**.

Trên máy mới: cài Python 3.10 trở lên (có Python Launcher `py`), chạy **install.bat** một lần,
rồi mở **run.bat**. Cài đặt cần Internet để tải Flet và FFmpeg. Ứng dụng xử lý video tại máy,
không tải video lên mạng. Bản này chạy bằng Python, chưa đóng gói thành EXE độc lập.

## Sử dụng

1. Browse thư mục đầu vào; hoặc dán đường dẫn và nhấn Enter / Làm mới.
2. Browse thư mục đầu ra và nhập tên file MP4.
3. Chọn tên tăng dần hoặc giảm dần. Số trong tên được xếp tự nhiên: 1, 2, 10.
4. Chọn chế độ và nhấn MERGE VIDEO. Kết quả theo đúng thứ tự đang hiển thị.
5. Nhấn Mở thư mục đầu ra sau khi hoàn tất. Có thể Hủy trong lúc ghép.

Tick **Tắt toàn bộ âm thanh** để bỏ cả nhạc nền và giọng đọc khỏi file kết quả.
Lựa chọn được nhớ ngay khi thay đổi, áp dụng cho cả ba chế độ và không sửa video nguồn.
Khi tắt âm thanh, chế độ tự động chỉ xét thông số hình ảnh để chọn ghép nhanh;
ghép nhanh có thêm bước bỏ track âm thanh bằng sao chép video, không mã hóa lại và không cần VGA.

Mặc định quét cả thư mục con; bỏ chọn **Quét thư mục con** để chỉ đọc tầng ngoài.
Khi quét thư mục con, bỏ qua thư mục đầu ra nằm bên trong đầu vào và thư mục tạm của app.
Hỗ trợ MP4, MOV, MKV, AVI,
WEBM, M4V, MTS và TS nếu FFmpeg đọc được. File lỗi hiện màu đỏ và chặn ghép cho đến khi sửa
hoặc chuyển file đó khỏi thư mục rồi Làm mới. Danh sách được giữ nguyên trong một lần ghép.

## Chế độ

- **Tự động:** ghép nhanh với H.264/AAC có cùng thông số; nếu khác thì chuẩn hóa.
- **Ghép nhanh:** không mã hóa lại, báo lỗi nếu nguồn không tương thích.
- **Chuẩn hóa:** H.264 CRF 20, AAC 192 kbps stereo 48 kHz; kích thước và FPS theo clip đầu tiên
  trong danh sách. Giữ tỷ lệ, thêm viền đen; clip thiếu âm thanh được thêm khoảng lặng nếu
  danh sách có âm thanh. Nếu tất cả clip không có tiếng thì đầu ra cũng không có tiếng.

Chỉ lấy luồng video chính đầu tiên và luồng âm thanh đầu tiên. Không giữ phụ đề, các track
âm thanh bổ sung hoặc chapter. Chuẩn hóa có mã hóa lại nên có thể giảm chất lượng; chưa có
xử lý màu HDR chuyên biệt. Ghép nhanh phụ thuộc timestamp của nguồn; nếu nguồn có timestamp
bất thường hoặc phát bị lệch tiếng, hãy chọn Chuẩn hóa.

## Xuất phụ đề SRT từ lời nói

## Tắt nhạc nền

Chọn **Merge** và **Tắt nhạc nền (giữ lời nói)** để tách nhạc bằng AI sau khi ghép.
Tùy chọn này giữ luồng hình ảnh, thay âm thanh bằng phần giọng nói được nhận dạng.
Nếu chọn **Tắt toàn bộ âm thanh**, app ưu tiên bỏ toàn bộ tiếng và không chạy tách nhạc.
SRT vẫn được nhận dạng từ âm thanh nguồn. Tách nhạc không đảm bảo loại sạch nhạc hoàn toàn
và có thể ảnh hưởng chất lượng giọng nói; xử lý video dài bằng CPU có thể mất nhiều thời gian.
Lần đầu cần Internet tải mô hình. Nếu thiếu bộ tách nhạc, chạy `install_music.bat`.

Khi dùng **Chọn file**, tick **Tắt nhạc nền (giữ lời nói)** rồi nhập các đoạn cần xử lý,
mỗi dòng theo dạng `00:01:20 - 00:01:28`. Có thể nhập nhiều dòng; các đoạn chồng nhau sẽ tự gộp.
App chỉ chạy tách nhạc trong các đoạn đã nhập, giữ nguyên âm thanh ở phần còn lại và lưu thành video mới.
Khoảng kết thúc không được vượt quá thời lượng video. Tính năng chọn đoạn hiện chỉ áp dụng cho một video được
mở bằng **Chọn file**; luồng Merge folder vẫn tắt nhạc nền trên toàn bộ video kết quả.

## Tạo SRT

- Chọn **Xuất SRT từ lời nói**, ngôn ngữ nguồn (hoặc Tự nhận dạng) và mô hình Base/Small,
  rồi nhấn **MERGE VIDEO**. SRT được lưu cạnh MP4 sau khi ghép, theo cùng thứ tự clip.
- Base xử lý nhanh hơn; Small thường nhận dạng tốt hơn nhưng tốn thời gian hơn. Cả hai chạy
  trên CPU, không yêu cầu card NVIDIA. Đây là phiên âm ngôn ngữ gốc, không dịch phụ đề.
- Lần đầu dùng mỗi mô hình cần Internet để tải vào `models/`. Những lần sau dùng bản đã tải;
  âm thanh được xử lý tại máy. Nếu thiếu thư viện, chạy lại `install.bat`.
- Vẫn tạo SRT từ âm thanh nguồn khi chọn **Tắt toàn bộ âm thanh**. Clip không có tiếng vẫn
  chiếm thời gian trong phụ đề. Không có âm thanh ở tất cả clip sẽ được báo trước khi ghép.
- Có thể Hủy khi nhận dạng. Video đã ghép vẫn được giữ nếu tạo SRT lỗi hoặc bị hủy.
  Không nhận dạng được lời nói sẽ báo lỗi, không xuất SRT rỗng. File SRT đã có không bị ghi đè.
- Nên kiểm tra lại chữ và thời gian, nhất là âm thanh có nhạc nền, nhiều người nói hoặc nguồn
  có timestamp bất thường. Nhận dạng dùng âm thanh từng clip và cộng thời lượng clip trước đó.
- Triển khai nhận dạng dùng [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

## Lưu trữ kết quả

- Không ghi đè: tự thêm `_1`, `_2` nếu tên đã tồn tại. Không sửa video nguồn.
- Ghi tạm trong thư mục đầu ra, chỉ công bố file cuối sau khi ghép và kiểm tra đọc được.
- Chuẩn hóa cần dung lượng cho các clip tạm và file kết quả. Thư mục tạm được dọn khi hoàn tất,
  lỗi hoặc hủy; nếu bị tắt nguồn có thể còn thư mục `.merge-*` cần xóa khi không có job chạy.
- Khi Làm mới, bỏ qua tên đầu ra đã cấu hình và kết quả vừa tạo trong phiên hiện tại.
  Nên dùng thư mục đầu ra riêng để tránh quét nhầm các kết quả cũ có tên khác.
- `settings.json`: nhớ hai đường dẫn, tên file, sắp xếp và chế độ sau khi quét/chọn đầu ra/ghép.
- `merge.log`: thông tin lỗi để chẩn đoán.

## Phát triển và kiểm thử

## Chọn CPU hoặc GPU

Trong giao diện, mục **Thiết bị xử lý** có hai lựa chọn:

- **CPU**: tương thích rộng nhất; chuẩn hóa video bằng `libx264`, tạo SRT bằng Whisper `int8`
  và tách nhạc bằng Demucs trên CPU.
- **GPU (NVIDIA)**: chuẩn hóa video bằng NVENC (`h264_nvenc`), tạo SRT bằng CUDA `float16`
  và tách nhạc bằng Demucs CUDA. Máy cần card NVIDIA, driver phù hợp và bản FFmpeg có NVENC.

Chế độ **Ghép nhanh** chỉ sao chép luồng video nên không dùng bộ mã hóa CPU/GPU. Nếu chọn GPU
nhưng CUDA hoặc NVENC không khả dụng, tác vụ sẽ dừng và hiển thị lỗi; ứng dụng không tự chuyển về CPU.
Ứng dụng mặc định mở toàn màn hình và chọn **GPU (NVIDIA)** ở lần chạy đầu tiên. Lựa chọn CPU/GPU
đã lưu trong `settings.json` sẽ được giữ cho những lần chạy sau.

```powershell
py -m unittest discover -s tests -v
```

`main.py` là giao diện; `merger.py` là engine độc lập. Các bài kiểm thử tạo clip nhỏ để kiểm
tra thứ tự hình thực tế, ghép nhanh, chuẩn hóa, clip thiếu tiếng, tên Unicode có dấu nháy,
trùng tên đầu ra, file lỗi và dọn dữ liệu khi hủy.

FFmpeg Windows được tải từ https://www.gyan.dev/ffmpeg/builds/ và kiểm tra SHA-256 công bố.
Giấy phép và README của bản FFmpeg nằm trong `bin/`. Khi phân phối lại FFmpeg cần tuân thủ
giấy phép đi kèm, bao gồm các nghĩa vụ cung cấp mã nguồn tương ứng.
