# Stock Downloader

Ứng dụng desktop Flet tìm video từ Pixabay rồi tải về máy, hiển thị mỗi thumbnail theo tỉ lệ 16:9 trong grid responsive.

## Chạy ứng dụng

1. Mở PowerShell tại thư mục `Stock`.
2. Cài Flet nếu máy chưa có: `python -m pip install -r requirements.txt`.
3. Vào **Setup → Thêm key → Lưu danh sách key**, hoặc tạo `.env` từ `.env.example` và điền `PIXABAY_API_KEYS=key_1,key_2`. Cấu hình cũ `PIXABAY_API_KEY` vẫn được hỗ trợ nếu danh sách mới trống. File `.env` không được Git theo dõi.

### Nhiều API key

Khi nhận phản hồi Cloudflare, mọi request tìm kiếm mới trong cùng phiên ứng dụng tạm dừng; các request đã gửi có thể hoàn tất. Tiến độ và dòng hiện tại được giữ trong lúc chờ. Kiểm tra Pixabay trong trình duyệt, sau đó bấm **Tiếp tục tìm kiếm** tại Download hoặc Script để cho phép một request thử; nếu vẫn bị chặn, ứng dụng lại dừng. Nút Hủy vẫn hoạt động. HTTP 429 không rõ nguyên nhân và không có header thời gian chờ cũng tạm dừng để kiểm tra log. Chỉ phản hồi quota hoặc 429 có hướng dẫn thời gian chờ mới dùng cơ chế nghỉ/xoay key tự động.

Log ứng dụng mới dùng `logs/application-diagnostics.jsonl`; file log cũ được giữ lại nhưng không đưa vào bản xuất mới vì có thể chứa dữ liệu kiểm thử. Chỉ entrypoint ứng dụng (`main.py` hoặc `launcher.py`) bật ghi log; kiểm thử mặc định không ghi log ứng dụng.

Khi gặp lỗi: vào **Setup → Xuất log lỗi**, chọn nơi lưu `stock-diagnostic.txt`, rồi gửi file này để kiểm tra. Log tự ghi từ lúc chạy phiên bản có tính năng này; hãy khởi động lại app sau cập nhật. Log phân biệt tìm kiếm API và tải MP4, ghi mã HTTP, quota và phản hồi Cloudflare nếu có. Không ghi API key, URL, từ khóa, đường dẫn cá nhân hoặc nội dung phản hồi. Log nội bộ nằm trong `logs/`, giữ tối đa hai phần khoảng 1 MB; nhãn key chỉ có ý nghĩa trong từng phiên. Xuất file không tự gửi dữ liệu ra ngoài.

- Setup cho phép thêm, xóa và lưu key; nội dung được che mặc định. Danh sách trống sẽ xóa cấu hình key cũ. Không chia sẻ `.env` vì key được lưu dạng văn bản.
- Tìm kiếm thủ công và tải theo kịch bản dùng chung bộ xoay key. Cache tìm kiếm vẫn được giữ 24 giờ; chỉ gọi mạng khi cần. Các lượt gọi API cách nhau tối thiểu 0,75 giây trong một tiến trình ứng dụng.
- Key gặp HTTP 429 nghỉ theo `Retry-After`/`X-RateLimit-Reset` (mặc định 60 giây). Khi tất cả key nghỉ, ứng dụng chờ và vẫn cho phép hủy. Mỗi yêu cầu tìm kiếm dừng sau tối đa 3 lần lỗi 429 nhân với số key đang cấu hình; phần kịch bản không retry chồng thêm.
- Key có lỗi xác thực rõ ràng được đánh dấu “cần kiểm tra”; lỗi mạng hoặc HTTP 403 không tự động bị coi là key sai. Bấm **Cập nhật trạng thái** để xem tình trạng danh sách đã lưu.
- Lưu thay đổi áp dụng cho lượt gọi kế tiếp, không ngắt lượt đang chạy. Trạng thái quota/chờ chỉ tồn tại trong phiên ứng dụng; khởi động lại sẽ đọc lại giới hạn từ phản hồi tiếp theo.
- Xoay key chỉ áp dụng cho API tìm kiếm, không khắc phục giới hạn ở máy chủ tải MP4. Nhiều key không phải cam kết tăng hạn mức được Pixabay cho phép.
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

- **Script**: bấm **Import Excel** và chọn file `.xlsx` có sheet `Footage Tracker`, với cột `Scene ID` và `Voice-over (original)`. Bảng hiển thị ô chọn, STT, Thumbnail video, rồi toàn bộ cột gốc. Dùng cuộn ngang để xem các cột và nút trang trước/sau để xem từng 20 cảnh. Tích chọn từng dòng hoặc **Select all** để chọn toàn bộ dòng trên mọi trang; lựa chọn được giữ khi chuyển trang. **Download** tìm bằng `Primary search keyword`, tải một video Pixabay mới cho mỗi dòng, lưu vào Library, cập nhật `Status`, `Selected clip URL / file` và thumbnail nếu tạo được. Dòng đã có file được bỏ qua; dòng lỗi có thể chọn tải lại. Cần API key trong Setup; bấm **Hủy tải** để dừng các dòng chưa hoàn tất. **Xóa line** cần xác nhận, chỉ xóa dòng Script, giữ video trong Library và file Excel gốc. Dữ liệu lưu tại `data/script.json`, được nạp lại khi mở Script. Import thành công sẽ thay bảng hiện tại; hủy hoặc import lỗi giữ dữ liệu cũ. Chưa hỗ trợ chỉnh sửa ô; các sheet Overview và Instructions không được import.

- Trong Download, Library và Script có chung thiết lập tải video: chất lượng HD/2K/4K (mặc định 2K) và hướng ngang/dọc (mặc định ngang). Stock chọn rendition Pixabay gần mức yêu cầu nhất; nếu mức đó không có, dùng rendition khả dụng gần nhất. Pixabay không cung cấp rendition 2K riêng, nên mức 2K có thể dùng file 4K hoặc 1080p tùy video.
- Mỗi thẻ có liên kết về trang Pixabay và tên tác giả để ghi nhận nguồn.
- Mỗi trang kết quả được cache cục bộ 24 giờ theo keyword để tuân thủ giới hạn API Pixabay. Nếu Pixabay trả HTTP 429, ứng dụng hiển thị thời gian chờ.
- Tuân thủ điều khoản Pixabay; tránh dùng cho tải hàng loạt có hệ thống.
