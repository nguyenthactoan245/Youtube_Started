# Stock Downloader

Ứng dụng desktop Flet tìm video từ Pixabay rồi tải về máy, hiển thị mỗi thumbnail theo tỉ lệ 16:9 trong grid responsive.

## Chạy ứng dụng

1. Mở PowerShell tại thư mục `Stock`.
2. Cài Flet nếu máy chưa có: `python -m pip install -r requirements.txt`.
3. Tạo `.env` từ `.env.example`, sau đó điền `PIXABAY_API_KEY`. File `.env` không được Git theo dõi.
4. Chạy: `python main.py`.

Nhập keyword, số lượng từ 1 đến 200 và nhấn **Tìm & tải**. Ứng dụng chọn video chưa có trong database theo ID Pixabay trên toàn bộ thư viện (kể cả keyword khác), duyệt các trang kết quả nếu cần. Video được tải song song tối đa ba file vào `library/<keyword>/`. Nếu kết quả khả dụng không đủ, ứng dụng tải số video mới tìm được. Lượt tải lỗi/hủy có thể thử lại. Xóa video bằng nút Xóa trong ứng dụng sẽ xóa cả file và bản ghi, nên lần tìm sau có thể tải lại cùng ID Pixabay.

Lịch sử lưu trong `data/stock.db` (SQLite), tách với MP4/JPG. Lần chạy đầu, app nhập các MP4 cũ trong `library/` và `downloads/` mà không chuyển hay xóa file. File có tên `<Pixabay ID>_<rộng>x<cao>.mp4` được nhận diện bằng ID; tên không rõ nguồn vẫn hiển thị trong Library để xem xét, nhưng không thể dùng để chứng minh trùng ID. Không xóa database nếu muốn giữ khả năng chống trùng. Mở mục **Library** để xem lịch sử và mở các MP4 còn trên đĩa.

Nếu app bị đóng đột ngột giữa lượt tải, chờ tối đa khoảng hai phút để lượt giữ chỗ cũ hết hạn rồi thử lại; lượt tải đang chạy ở một cửa sổ khác không bị lấy mất.

Bấm vào một thẻ để mở preview. Bấm biểu tượng play giữa ảnh hoặc **Chạy video** để phát; nhấn **Esc** để đóng.

Library tạo ảnh thu nhỏ `.thumb.jpg` (rộng 480 px) từ JPG đã lưu hoặc trực tiếp từ MP4 khi thiếu poster. Việc tạo ảnh chạy nền và có thanh tiến trình khi mở Library lần đầu; chuyển qua lại các mục sau đó dùng dữ liệu đã nạp, không đọc lại toàn bộ ảnh. Bấm **Làm mới** khi file bị thay đổi ngoài ứng dụng. Cần có `ffmpeg` trong `PATH` để tạo ảnh từ MP4; nếu thiếu FFmpeg, app vẫn dùng JPG có sẵn và hiện biểu tượng thay thế cho video chưa có ảnh.

Mục **Project** có sidebar phụ để tạo và chọn project. Bấm mũi tên cạnh project để mở/thu gọn các chapter; bấm tên project để xem tất cả video trong project, hoặc bấm chapter để xem riêng video của chapter đó dưới dạng lưới 16:9. Bấm thẻ video để mở preview như ở Library. Trong project đang chọn, nhập tên chapter rồi bấm **Thêm chapter**; chapter mới sẽ được mở ngay. Icon Xóa cạnh project/chapter luôn mở hộp xác nhận: xóa chapter đưa video của nó về cấp project; xóa project gỡ toàn bộ liên kết video và xóa các chapter. Cả hai thao tác đều giữ MP4 và bản ghi Library. Project và chapter được lưu trong cùng `data/stock.db` nên vẫn còn khi mở lại app.

Lưới video trong Project/chapter cũng có ô tích trên từng thẻ và thanh icon chọn tất cả, xóa, xuất MP4, gắn thêm vào project/chapter. Chọn tất cả chỉ chọn các video đang hiển thị; khi chuyển chapter, các video ngoài chapter đó tự bỏ chọn. Xóa ở đây cũng xóa vĩnh viễn file và bản ghi trong Library, không chỉ gỡ video khỏi project.

Trong **Download** và **Library**, bấm ô tích trên từng thẻ hoặc biểu tượng chọn tất cả để chọn nhiều video (có thể dùng ở cả dạng grid và list). Thanh công cụ dùng biểu tượng: Xóa để xóa vĩnh viễn MP4, thumbnail và bản ghi database; mũi tên tải xuống để **sao chép MP4 sang thư mục khác trên máy**; biểu tượng thư mục để gắn thêm video vào project/chapter. Hộp thoại có hai dropdown Project và Chapter: chọn một đích rồi bấm **Gắn video**, hoặc bấm **+** để thêm nhiều đích trước khi gắn. Video có thể thuộc nhiều project/chapter, nhưng MP4 chỉ lưu một bản trong Library; gắn lại cùng đích không tạo bản sao. Lưới cấp project chỉ hiển thị mỗi video một lần, dù video thuộc nhiều chapter. Thao tác xuất không ghi đè file trùng tên ở thư mục đích. Xóa cần xác nhận và không thể khôi phục từ Recycle Bin; video chưa tải xong ở Download chỉ bị bỏ khỏi kết quả hiện tại.

## Ghi chú

- Ứng dụng chọn bản `medium` của Pixabay; tự hạ xuống `small`/`tiny` nếu cần.
- Mỗi thẻ có liên kết về trang Pixabay và tên tác giả để ghi nhận nguồn.
- Mỗi trang kết quả được cache cục bộ 24 giờ theo keyword để tuân thủ giới hạn API Pixabay. Nếu Pixabay trả HTTP 429, ứng dụng hiển thị thời gian chờ.
- Tuân thủ điều khoản Pixabay; tránh dùng cho tải hàng loạt có hệ thống.
