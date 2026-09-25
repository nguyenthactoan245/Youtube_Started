"""Script import and paginated footage table."""
from __future__ import annotations

import asyncio
import re
import math
from collections import deque
from pathlib import Path
from threading import Event

import flet as ft

from services.scripts import load_tracker, read_tracker, save_tracker
from services.script_downloads import download_scene
from services.assets import export_videos_zip
from services.pixabay import PixabayRateLimitError, PixabayAccessPause
from services.api_keys import ApiKeyPool, get_api_key_pool
from app_config import configured_api_keys
from services.thumbnails import thumbnail_bytes
from script_video import ScriptVideo
from video_settings import build_video_settings


# Keep identifiers compact and reserve space for narration and shot descriptions.
COLUMN_WIDTHS = {
    "Scene ID": 88,
    "Chapter": 180,
    "Location": 160,
    "Voice-over (original)": 440,
    "Est. voice (sec) @120 WPM": 120,
    "Visual cue": 220,
    "Primary search keyword": 240,
    "Alternative search keyword": 240,
    "Suggested shot": 160,
    "Pexels search URL": 220,
    "Pixabay search URL": 220,
    "Status": 130,
    "Selected clip URL / file": 240,
    "Notes": 260,
}


class ScriptView:
    def __init__(self, page, picker: ft.FilePicker, target: Path, database=None, library_root=None,
                 on_download_complete=None, open_preview=None, video_preferences=None):
        self.page, self.picker, self.target = page, picker, target
        self.data = None
        self.loaded = False
        self.offset = 0
        self.filter_mode = "all"
        self.selected: set[int] = set()
        self.busy = False
        self.database = database
        self.library_root = library_root
        self.on_download_complete = on_download_complete
        self.cancel = Event()
        self.posters = {}
        self.open_preview = open_preview
        self.video_preferences = video_preferences if video_preferences is not None else {
            "quality": "2K", "orientation": "landscape"}
        self.video_settings = build_video_settings(self.video_preferences)
        self.video_tiles = {}
        self.progress = ft.ProgressBar(value=0, color=ft.Colors.CYAN_300)
        self.progress_label = ft.Text(size=12)
        self.file_progress = ft.ProgressBar(value=None, color=ft.Colors.BLUE_300)
        self.file_label = ft.Text(size=12)
        self.progress_panel = ft.Column(visible=False, spacing=4, controls=[
            self.progress_label, self.progress, self.file_label, self.file_progress])
        self.error_details = ft.Column(visible=False, spacing=6, height=150, scroll=ft.ScrollMode.AUTO)
        self.dismissed_errors = set()
        self.clear_log_button = ft.IconButton(icon=ft.Icons.CLEAR_ALL, tooltip="Clear log",
                                              on_click=self.clear_log)
        self.message = ft.Text("Import file .xlsx có sheet Footage Tracker để bắt đầu.")
        self.counter = ft.Text()
        self.table = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO)
        self.import_button = ft.Button("Import Script", icon=ft.Icons.UPLOAD_FILE,
                                       tooltip="Import file Excel", on_click=self.import_file)
        self.previous = ft.IconButton(ft.Icons.CHEVRON_LEFT, tooltip="Trang trước", on_click=self.previous_page)
        self.next = ft.IconButton(ft.Icons.CHEVRON_RIGHT, tooltip="Trang sau", on_click=self.next_page)
        self.select_all = ft.IconButton(icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK,
                                       tooltip="Select all — Chọn tất cả dòng trên mọi trang",
                                       on_click=self.toggle_all)
        self.export_selected_button = ft.IconButton(
            icon=ft.Icons.DOWNLOAD, tooltip="Tải video đã chọn thành file ZIP",
            on_click=self.export_selected_videos)
        self.selection_count = ft.Text("Đã chọn 0 dòng", size=13)
        self.delete_button = ft.IconButton(tooltip="Xóa line", icon=ft.Icons.DELETE_OUTLINE,
                                      on_click=self.ask_delete)
        self.crawl_button = ft.FilledButton("Crawl", icon=ft.Icons.DOWNLOAD,
                                            on_click=self.download_selected)
        self.cancel_button = ft.IconButton(icon=ft.Icons.CANCEL_OUTLINED, tooltip="Hủy tải",
                                           visible=False, on_click=self.cancel_download)
        self.resume_search = ft.TextButton("Tiếp tục tìm kiếm", visible=False,
                                           on_click=lambda _: get_api_key_pool().resume())
        self.filter_all = ft.TextButton("Tất cả", on_click=lambda _: self.set_filter("all"))
        self.filter_missing = ft.TextButton("Chưa có video", on_click=lambda _: self.set_filter("missing"),
            tooltip="Các dòng chưa có URL hoặc đường dẫn trong Selected clip URL / file")
        self.filter_count = ft.Text(size=12, color=ft.Colors.BLUE_GREY_300)
        self.control = ft.Column(expand=True, controls=[
            ft.Row([ft.Text("Script", size=28, weight=ft.FontWeight.BOLD),
                    self.import_button],
                   spacing=12, wrap=False, alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            self.message,
            ft.Row([self.video_settings, self.crawl_button, self.cancel_button,
                    self.resume_search], spacing=8, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([self.select_all, self.export_selected_button, self.delete_button,
                    self.selection_count],
                   spacing=6, wrap=False, scroll=ft.ScrollMode.AUTO),
            ft.Row([ft.Icon(ft.Icons.FILTER_LIST), self.filter_all, self.filter_missing,
                    self.filter_count], spacing=8, scroll=ft.ScrollMode.AUTO),
            self.progress_panel,
            self.error_details,
            ft.Text("Thumbnail video nằm sau STT. Import mới sẽ thay bảng Script hiện tại.",
                    size=12, color=ft.Colors.BLUE_GREY_300),
            ft.Row(
                expand=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                scroll=ft.Scrollbar(thumb_visibility=True, track_visibility=True,
                                    thickness=12, interactive=True),
                controls=[ft.Container(content=self.table, padding=ft.Padding.only(bottom=16))],
            ),
            ft.Row([self.previous, self.counter, self.next]),
        ])
        self.render()

    async def load(self):
        if self.loaded:
            return
        self.loaded = True
        self.busy = True
        self.update_actions()
        try:
            self.data = await asyncio.to_thread(load_tracker, self.target)
            await self.load_posters()
            self.render()
        except (OSError, ValueError) as exc:
            self.message.value = f"Không thể đọc Script: {exc}"
        finally:
            self.busy = False
            self.refresh_controls()
            self.page.update()

    async def import_file(self, _):
        if self.import_button.disabled:
            return
        self.busy = True
        self.render()
        self.page.update()
        old_message = self.message.value
        try:
            files = await self.picker.pick_files(dialog_title="Import Script Excel", allow_multiple=False,
                file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=["xlsx"])
            if not files:
                return
            if not files[0].path:
                raise ValueError("Không đọc được đường dẫn file đã chọn.")
            self.message.value = "Đang import Script..."
            self.page.update()
            data = await asyncio.to_thread(read_tracker, Path(files[0].path))
            await asyncio.to_thread(save_tracker, data, self.target)
            self.data, self.offset = data, 0
            self.selected.clear()
            self.posters.clear()
            self.dismissed_errors.clear()
            self.progress_panel.visible = False
            self.render()
        except Exception as exc:
            self.message.value = f"Import không thành công: {exc}"
        finally:
            if self.message.value == "Đang import Script...":
                self.message.value = old_message
            self.busy = False
            self.refresh_controls()
            self.page.update()

    def toggle_row(self, index: int, checked: bool):
        if self.busy:
            return
        if checked:
            self.selected.add(index)
        else:
            self.selected.discard(index)
        self.render()
        self.page.update()

    def toggle_all(self, _):
        if self.busy:
            return
        matching = set(self.filtered_indices())
        self.selected = matching if self.selected != matching else set()
        self.render()
        self.page.update()

    def filtered_indices(self):
        if not self.data:
            return []
        headers = self.data["headers"]
        column = headers.index("Selected clip URL / file") if "Selected clip URL / file" in headers else None
        return [i for i, row in enumerate(self.data["rows"])
                if self.filter_mode == "all" or column is None or not row[column].strip()]

    def set_filter(self, mode):
        if self.busy:
            return
        self.filter_mode = mode
        self.offset = 0
        self.refresh_controls()
        self.page.update()

    def ask_delete(self, _):
        if self.busy or not self.selected:
            return
        selected = set(self.selected)
        original = self.data

        def cancel(_):
            self.page.pop_dialog()

        async def confirm(_):
            self.page.pop_dialog()
            if self.busy or self.data is not original:
                return
            await self.delete_rows(selected)

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title="Xóa line đã chọn?",
            content=ft.Text(f"Xóa {len(selected)} dòng khỏi Script, gồm cả dòng ở các trang khác. "
                            "File Excel gốc và video trong Library vẫn được giữ."),
            actions=[ft.TextButton("Hủy", on_click=cancel),
                     ft.FilledButton("Xóa line", on_click=confirm)],
        ))

    async def delete_rows(self, selected: set[int]):
        if self.busy or not self.data or not selected:
            return
        self.busy = True
        self.render()
        self.page.update()
        try:
            updated = {**self.data, "rows": [row for i, row in enumerate(self.data["rows"]) if i not in selected]}
            await asyncio.to_thread(save_tracker, updated, self.target)
            self.data = updated
            self.selected.clear()
            self.offset = min(self.offset, max(0, (len(updated["rows"]) - 1) // 20 * 20))
            self.render()
            self.message.value = f"Đã xóa {len(selected)} dòng."
        except (OSError, ValueError) as exc:
            self.message.value = f"Không thể xóa line: {exc}"
        finally:
            self.busy = False
            self.refresh_controls()
            self.page.update()

    def update_actions(self):
        matching = self.filtered_indices()
        count = len(matching)
        self.selected.intersection_update(matching)
        self.filter_all.disabled = self.busy
        self.filter_missing.disabled = self.busy
        self.filter_all.style = ft.ButtonStyle(bgcolor="#20335a" if self.filter_mode == "all" else None)
        self.filter_missing.style = ft.ButtonStyle(bgcolor="#20335a" if self.filter_mode == "missing" else None)
        total = len(self.data["rows"]) if self.data else 0
        self.filter_count.value = f"{count} / {total} dòng"
        all_selected = bool(count) and len(self.selected) == count
        self.select_all.icon = (ft.Icons.CHECK_BOX if all_selected else
                                ft.Icons.INDETERMINATE_CHECK_BOX if self.selected else
                                ft.Icons.CHECK_BOX_OUTLINE_BLANK)
        self.select_all.tooltip = "Bỏ chọn tất cả" if all_selected else "Select all — Chọn tất cả dòng khớp bộ lọc trên mọi trang"
        self.select_all.disabled = self.busy or not count
        self.export_selected_button.disabled = self.busy or not self.selected_local_videos()
        self.delete_button.disabled = self.busy or not self.selected
        self.crawl_button.disabled = self.busy or not self.selected or self.database is None
        self.import_button.disabled = self.busy
        self.selection_count.value = f"Đã chọn {len(self.selected)} / {count} dòng"

    def selected_local_videos(self) -> list[Path]:
        if not self.data or "Selected clip URL / file" not in self.data["headers"]:
            return []
        column = self.data["headers"].index("Selected clip URL / file")
        paths = []
        for index in sorted(self.selected):
            if not 0 <= index < len(self.data["rows"]):
                continue
            value = self.data["rows"][index][column].strip()
            if not value:
                continue
            path = Path(value)
            if path.suffix.lower() == ".mp4" and path.is_file():
                paths.append(path)
        return paths

    async def export_selected_videos(self, _):
        if self.busy or not self.selected:
            return
        paths = self.selected_local_videos()
        if not paths:
            self.message.value = "Các dòng đã chọn chưa có file MP4 cục bộ để đóng gói."
            self.page.update()
            return
        try:
            destination = await self.picker.save_file(
                dialog_title="Lưu video Script thành ZIP",
                file_name="script-videos.zip",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["zip"],
            )
        except Exception:
            self.message.value = "Không mở được hộp thoại chọn nơi lưu ZIP."
            self.page.update()
            return
        if not destination:
            return
        archive_path = Path(destination)
        if archive_path.suffix.lower() != ".zip":
            archive_path = archive_path.with_suffix(".zip")
        self.busy = True
        self.refresh_controls()
        self.message.value = "Đang tạo file ZIP từ các video đã chọn..."
        self.page.update()
        try:
            archived, skipped = await asyncio.to_thread(export_videos_zip, paths, archive_path)
            unavailable = len(self.selected) - len(paths)
            self.message.value = (f"Đã lưu {archived} video vào ZIP; bỏ qua "
                                 f"{skipped + unavailable} dòng không có file phù hợp.")
        except FileExistsError:
            self.message.value = "File ZIP đã tồn tại. Hãy chọn tên khác."
        except (OSError, ValueError):
            self.message.value = "Không thể tạo file ZIP tại vị trí đã chọn."
        finally:
            self.busy = False
            self.refresh_controls()
            self.page.update()

    def refresh_controls(self):
        message = self.message.value
        self.render()
        self.message.value = message

    async def load_posters(self):
        if not self.data or "Selected clip URL / file" not in self.data["headers"]:
            return
        column = self.data["headers"].index("Selected clip URL / file")
        paths = [r[column] for r in self.data["rows"] if r[column]]

        def read():
            result = {}
            for value in paths:
                # Only local MP4 files may supply thumbnails; never fetch workbook URLs.
                path = Path(value)
                if path.suffix.lower() == ".mp4" and path.is_file():
                    result[value] = thumbnail_bytes(path)
            return result

        self.posters = await asyncio.to_thread(read)

    def cancel_download(self, _):
        self.cancel.set()
        self.cancel_button.disabled = True
        self.message.value = "Đang hủy tải..."
        self.page.update()

    @staticmethod
    def error_text(exc):
        text = str(exc) or type(exc).__name__
        for api_key in sorted(configured_api_keys(), key=len, reverse=True):
            text = text.replace(api_key, "[ẩn API key]")
        return re.sub(r"(?i)([?&]key=)[^&\s]+", r"\1[ẩn]", text)

    def clear_log(self, _):
        if self.data and "Status" in self.data["headers"]:
            column = self.data["headers"].index("Status")
            self.dismissed_errors.update(tuple(row) for row in self.data["rows"]
                                         if row[column].startswith("Lỗi:"))
        self.refresh_controls()
        self.page.update()

    def update_progress(self, processed, total):
        self.progress.value = processed / total if total else 0
        self.progress_label.value = f"Đã xử lý {processed}/{total} dòng · {self.progress.value:.0%}"

    async def download_with_progress(self, keyword, api_key, previous_clip=""):
        updates = deque(maxlen=1)
        worker = asyncio.create_task(asyncio.to_thread(
            download_scene, keyword, api_key, self.library_root, self.database, self.cancel,
            updates.append, previous_clip, self.video_preferences["quality"],
            self.video_preferences["orientation"]))
        while not worker.done():
            await asyncio.wait({worker}, timeout=0.1)
            if not updates and isinstance(api_key, ApiKeyPool):
                self.resume_search.visible = bool(api_key.pause_reason) and not api_key.auto_retrying
                self.file_label.value = "Đang tìm video · " + " · ".join(api_key.statuses())
                self.page.update()
            if updates:
                event = updates[-1]
                ratio = min(1.0, max(0.0, event.progress))
                self.file_progress.value = ratio if ratio > 0 else None
                self.file_label.value = (f"Đang tải video · {ratio:.0%}" if event.state == "downloading" else
                                        "Video đã tải xong" if event.state == "done" else
                                        self.error_text(event.message))
                self.page.update()
        self.resume_search.visible = False
        self.page.update()
        return await worker

    async def wait_rate_limit(self, seconds, attempt):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        self.file_progress.value = None
        while loop.time() < deadline:
            if self.cancel.is_set():
                raise InterruptedError("Đã hủy trong khi chờ API.")
            remaining = max(1, math.ceil(deadline - loop.time()))
            self.file_label.value = f"HTTP 429 · Chờ {remaining} giây · Thử lại {attempt}/3"
            self.page.update()
            await asyncio.sleep(min(0.25, max(0, deadline - loop.time())))
        if self.cancel.is_set():
            raise InterruptedError("Đã hủy trong khi chờ API.")

    async def download_with_retry(self, keyword, api_key, previous_clip):
        if isinstance(api_key, ApiKeyPool):
            return await self.download_with_progress(keyword, api_key, previous_clip)
        for attempt in range(4):
            if self.cancel.is_set():
                raise InterruptedError("Đã hủy tải.")
            try:
                return await self.download_with_progress(keyword, api_key, previous_clip)
            except PixabayRateLimitError as exc:
                if attempt == 3:
                    raise
                # Respect server delay and increase the minimum after repeated limits.
                await self.wait_rate_limit(max(exc.retry_after, 60 * 2 ** attempt), attempt + 1)
                self.file_label.value = "Đang thử lại tìm video trên Pixabay..."
                self.page.update()

    async def download_selected(self, _):
        if self.busy or not self.selected or not self.data or self.database is None:
            return
        if "Primary search keyword" not in self.data["headers"]:
            self.message.value = "Script thiếu cột Primary search keyword."
            self.page.update()
            return
        api_key = get_api_key_pool()
        if not api_key:
            self.message.value = "Hãy nhập Pixabay API key trong Setup trước khi tải."
            self.page.update()
            return
        self.busy = True
        self.cancel = Event()
        self.cancel_button.visible = True
        self.cancel_button.disabled = False
        selected = sorted(self.selected)
        completed = failed = 0
        processed = 0
        self.progress_panel.visible = True
        self.file_progress.visible = True
        self.file_progress.value = None
        self.file_label.value = "Đang chuẩn bị..."
        self.update_progress(0, len(selected))
        self.render()
        self.page.update()
        try:
            for position, index in enumerate(selected, 1):
                if self.cancel.is_set():
                    break
                headers = list(self.data["headers"])
                rows = [list(r) for r in self.data["rows"]]
                for name in ("Status", "Selected clip URL / file"):
                    if name not in headers:
                        headers.append(name)
                        for row in rows:
                            row.append("")
                row = rows[index]
                clip_column = headers.index("Selected clip URL / file")
                status_column = headers.index("Status")
                existing = row[clip_column]
                keyword = row[headers.index("Primary search keyword")]
                self.message.value = f"Đang tải {position}/{len(selected)} · dòng {index + 1}: {keyword}"
                self.file_label.value = f"Đang tìm video Pixabay cho dòng {index + 1}..."
                self.file_progress.value = None
                self.page.update()
                try:
                    path = await self.download_with_retry(keyword, api_key, existing)
                except InterruptedError:
                    break
                except PixabayAccessPause:
                    row[status_column] = "Paused: Pixabay access verification required"
                    self.dismissed_errors.discard(tuple(row))
                    self.busy = True
                    self.resume_search.visible = True
                    self.message.value = "Pixabay yêu cầu xác minh truy cập (Cloudflare). Kiểm tra trên trình duyệt rồi bấm Tiếp tục tìm kiếm."
                    self.page.update()
                    while not self.cancel.is_set():
                        await asyncio.sleep(0.1)
                        if not get_api_key_pool().pause_reason:
                            break
                    if not self.cancel.is_set():
                        row[status_column] = ""
                        try:
                            path = await self.download_with_retry(keyword, api_key, existing)
                        except InterruptedError:
                            break
                        except Exception as exc:
                            rate_limited = isinstance(exc, PixabayRateLimitError)
                            row[status_column] = f"Lỗi: {self.error_text(exc)}"
                            failed += 1
                        else:
                            rate_limited = False
                            row[clip_column] = path
                            row[status_column] = "Downloaded"
                            completed += 1
                            if self.on_download_complete:
                                self.on_download_complete()
                            self.posters[path] = await asyncio.to_thread(thumbnail_bytes, Path(path))
                    else:
                        break
                except Exception as exc:
                    rate_limited = isinstance(exc, PixabayRateLimitError)
                    row[status_column] = f"Lỗi: {self.error_text(exc)}"
                    self.dismissed_errors.discard(tuple(row))
                    failed += 1
                else:
                    rate_limited = False
                    row[clip_column] = path
                    row[status_column] = "Downloaded"
                    completed += 1
                    if self.on_download_complete:
                        self.on_download_complete()
                    self.posters[path] = await asyncio.to_thread(thumbnail_bytes, Path(path))
                updated = {**self.data, "headers": headers, "rows": rows}
                # Stop on a save error; do not silently continue with unrecorded assignments.
                await asyncio.to_thread(save_tracker, updated, self.target)
                self.data = updated
                processed += 1
                self.update_progress(processed, len(selected))
                self.refresh_controls()
                self.page.update()
                if rate_limited:
                    self.message.value = ("Đã dừng lượt tải sau các lần thử lại HTTP 429 từ API. "
                                          "Các dòng chưa xử lý được giữ nguyên; hãy thử lại sau.")
                    return
            self.message.value = (f'{"Đã hủy. " if self.cancel.is_set() else ""}'
                                  f"Đã tải {completed}, lỗi {failed} dòng.")
        except Exception as exc:
            self.message.value = f"Đã dừng: {self.error_text(exc)}. Video đã tải vẫn được giữ trong Library."
        finally:
            self.busy = False
            self.cancel_button.visible = False
            self.file_progress.visible = False
            self.file_label.value = "Đã hủy lượt tải." if self.cancel.is_set() else "Lượt tải đã kết thúc."
            self.refresh_controls()
            self.page.update()

    def previous_page(self, _):
        self.offset = max(0, self.offset - 20)
        self.render()
        self.page.update()

    async def stop_playback(self, except_tile=None):
        for tile in list(self.video_tiles.values()):
            if tile is not except_tile:
                await tile.stop()

    def playback_error(self, message):
        self.message.value = self.error_text(message)

    def next_page(self, _):
        if self.offset + 20 < len(self.filtered_indices()):
            self.offset += 20
        self.render()
        self.page.update()

    def render(self):
        self.update_actions()
        self.error_details.controls.clear()
        if self.data and "Status" in self.data["headers"]:
            headers = self.data["headers"]
            for index, row in enumerate(self.data["rows"]):
                status = row[headers.index("Status")]
                if not status.startswith("Lỗi:") or tuple(row) in self.dismissed_errors:
                    continue
                scene = row[headers.index("Scene ID")] if "Scene ID" in headers else str(index + 1)
                keyword = row[headers.index("Primary search keyword")] if "Primary search keyword" in headers else ""
                self.error_details.controls.append(ft.Text(
                    f"Dòng {index + 1} · {scene} · Keyword: {keyword}\n{self.error_text(status)}",
                    selectable=True, size=13, color=ft.Colors.RED_300))
        self.error_details.visible = bool(self.error_details.controls)
        if self.error_details.visible:
            self.error_details.controls.insert(0, ft.Row([
                ft.Text("Chi tiết lỗi", weight=ft.FontWeight.BOLD), self.clear_log_button],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN))
        self.table.controls.clear()
        matching = self.filtered_indices()
        count = len(matching)
        self.offset = min(self.offset, max(0, (count - 1) // 20 * 20))
        self.previous.disabled = self.offset == 0
        self.next.disabled = self.offset + 20 >= count
        self.counter.value = f"{self.offset + 1}–{min(self.offset + 20, count)} / {count} cảnh" if count else "0 cảnh"
        if not self.data:
            return
        self.message.value = f'{self.data["filename"]} • {len(self.data["rows"])} cảnh'
        headers = ["", "STT", "Thumbnail video", *self.data["headers"]]
        widths = [48, 52, 180, *[COLUMN_WIDTHS.get(h.strip(), 220) for h in self.data["headers"]]]
        self.table.width = sum(widths)

        def cell(text, width, heading=False):
            return ft.Container(width=width, padding=12, content=ft.Text(
                text, size=13, selectable=True, no_wrap=False,
                weight=ft.FontWeight.BOLD if heading else None))

        self.table.controls.append(ft.Container(bgcolor="#20335a", content=ft.Row(
            [cell(h, w, True) for h, w in zip(headers, widths)], spacing=0)))
        if not matching:
            self.table.controls.append(ft.Container(padding=16, content=ft.Text("Không có dòng nào khớp bộ lọc.")))
        old_tiles = self.video_tiles
        self.video_tiles = {}
        for index in matching[self.offset:self.offset + 20]:
            number = index + 1
            row = self.data["rows"][index]
            selector = ft.Container(width=48, padding=ft.Padding.only(top=8), content=ft.Checkbox(
                value=number - 1 in self.selected, disabled=self.busy, tooltip=f"Chọn dòng {number}",
                on_change=lambda e, index=number - 1: self.toggle_row(index, bool(e.control.value))))
            thumbnail = ft.Container(width=180, padding=10, content=ft.Container(
                width=160, height=90, bgcolor="#10182c", border_radius=6,
                alignment=ft.Alignment(0, 0), content=ft.Text("Chưa có video", size=12, color=ft.Colors.BLUE_GREY_300)))
            if "Selected clip URL / file" in self.data["headers"]:
                path = row[self.data["headers"].index("Selected clip URL / file")]
                poster = self.posters.get(path)
                if poster:
                    thumbnail.content.content = ft.Image(src=poster, width=160, height=90, fit=ft.BoxFit.COVER)
                elif path in self.posters:
                    thumbnail.content.content = ft.Text("Đã có video", size=12)
                if path and not path.lower().startswith(("https://", "http://")) and Path(path).suffix.lower() == ".mp4":
                    key = (index, path)
                    tile = old_tiles.pop(key, None) or ScriptVideo(
                        path, poster, self.page, self.open_preview, self.stop_playback, self.playback_error)
                    self.video_tiles[key] = tile
                    thumbnail.content = tile.control
            self.table.controls.append(ft.Container(bgcolor="#162139" if number % 2 else "#10182c",
                content=ft.Row([selector, cell(str(number), widths[1]), thumbnail,
                    *[cell(value, width) for value, width in zip(row, widths[3:])]],
                    spacing=0, vertical_alignment=ft.CrossAxisAlignment.START)))
        for tile in old_tiles.values():
            # Removing a Video control disposes its native player. Invalidate callbacks too.
            tile.player = None
