# DramaStudio v1.0.0

## Cài đặt

Chạy `releases/DramaStudio-Setup-v1.0.0-GPU.exe`. Bộ cài đặt theo tài khoản Windows hiện tại, tạo lối tắt trong Start Menu và có tùy chọn tạo lối tắt Desktop. Bản GPU kèm các DLL CUDA cần để ưu tiên Faster-Whisper CUDA FP16 và MADLAD CUDA INT8/FP16; máy không có NVIDIA/CUDA vẫn dùng CPU.

## Model

Installer không kèm model. Ứng dụng vẫn tải và ghép video với chế độ **Không Sub** ngay sau khi cài. Khi chọn **Sub mặc định** hoặc **Dịch Sub**, mở **Cấu hình đồng bộ** và tải Faster-Whisper (khoảng 1,4 GiB). Khi chọn **Dịch Sub**, tải thêm MADLAD-400 INT8 (khoảng 2,8 GiB). Nên có ít nhất 5 GiB dung lượng trống cho cả hai model và file tải tạm.

Model và video lưu ở `%LOCALAPPDATA%\DramaStudio`, tách khỏi thư mục cài đặt để không bị thay thế khi cập nhật ứng dụng. Model đã tải có thể dùng offline. Nếu CUDA 12 và các thư viện cần thiết không có trên máy, bước nhận dạng và dịch sẽ dùng CPU; mã hóa video vẫn tự dò GPU qua FFmpeg.

## Build

Chạy `powershell -NoProfile -ExecutionPolicy Bypass -File .\build_installer.ps1` từ thư mục DramaStudio với Python 3.12, PyInstaller, FFmpeg và Inno Setup tại `..\Stock\.build-tools\inno\ISCC.exe`.

Trên macOS, cài Python 3.12 cùng các gói trong `requirements.txt` và `pyobjc`, rồi chạy `bash build_mac.sh`. Script tải FFmpeg tự chứa cho kiến trúc máy (hoặc dùng `FFMPEG_BIN` nếu đã đặt), tạo `.app` và `releases/DramaStudio-macOS-arm64-v1.0.0.dmg` hoặc `DramaStudio-macOS-x86_64-v1.0.0.dmg`. Model Mac được tải về `~/Library/Application Support/DramaStudio/models`. Có thể chạy workflow `Build DramaStudio macOS v1.0.0` trên GitHub Actions để build cả hai kiến trúc. Cần build riêng trên macOS; bản Windows không thể tạo `.app` bằng PyInstaller.

Bản Mac dùng CTranslate2 trên CPU cho nhận dạng và dịch vì wheel macOS không hỗ trợ CUDA; FFmpeg thử Apple VideoToolbox để mã hóa video. DMG hiện chưa được ký và notarize bằng Apple Developer ID, nên người dùng Mac có thể phải cho phép mở ứng dụng trong phần Cài đặt bảo mật.
