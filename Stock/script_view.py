"""Script import and paginated footage table."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import re
import math
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Event
import shutil
import subprocess
import time

import flet as ft

from services.scripts import load_tracker, read_tracker, save_tracker
from services.script_downloads import (download_pexels_link, download_scene,
                                       download_scene_direct_pexels, video_id_from_clip)
from services.video_trim import trim_video
from services.assets import export_videos_zip
from services.pixabay import PixabayRateLimitError, PixabayAccessPause
from services.pexels import PexelsError
from services.api_keys import ApiKeyPool, get_api_key_pool
from app_config import configured_api_keys, configured_pexels_api_key, configured_pexels_api_keys
from services.thumbnails import thumbnail_bytes
from script_video import ScriptVideo
from video_settings import build_video_settings


# Keep identifiers compact and reserve space for narration and shot descriptions.
COLUMN_WIDTHS = {
    "Scene ID": 88,
    "Beat ID": 92,
    "SRT Block": 82,
    "Start": 132,
    "End": 132,
    "Duration (s)": 100,
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
    "Thumbnail Video Duration": 110,
    "Trimed video Duration": 110,
    "Trimed video": 220,
    "Visual Direction": 190,
    "Shot Type": 150,
    "Editorial Purpose": 160,
    "Footage URL": 220,
    "File Name": 200,
    "Pixabay Search": 190,
    "Notes": 260,
}

HIDDEN_SCRIPT_COLUMNS = {"Visual Direction", "Pixabay Search"}


class ScriptView:
    def __init__(self, page, picker: ft.FilePicker, target: Path, database=None, library_root=None,
                 on_download_complete=None, open_preview=None, video_preferences=None):
        self.page, self.picker, self.target = page, picker, target
        self.data = None
        self.loaded = False
        self.offset = 0
        self.filter_mode = "all"
        self.selected: set[int] = set()
        self.selected_trimmed: set[int] = set()
        self.busy = False
        self.edit_mode = False
        self.edit_values = {}
        self.script_name = ""
        self.database = database
        self.library_root = library_root
        self.on_download_complete = on_download_complete
        self.cancel = Event()
        self.download_workers = 5
        self.posters = {}
        self.open_preview = open_preview
        self.video_preferences = video_preferences if video_preferences is not None else {
            "quality": "2K", "orientation": "landscape"}
        # Keep Script choices independent from the Download page while using
        # them for every Crawl action started in this view.
        self.video_settings_preferences = {
            "quality": self.video_preferences.get("quality", "2K"),
            "orientation": self.video_preferences.get("orientation", "landscape"),
            "source": self.video_preferences.get("source", "pixabay"),
            "method": self.video_preferences.get("method", "api"),
            "workers": str(self.video_preferences.get("workers", self.download_workers)),
        }
        self.video_settings = build_video_settings(
            self.video_settings_preferences, include_source=True, include_method=True,
            include_workers=True)
        self.video_tiles = {}
        self.progress = ft.ProgressBar(value=0, color=ft.Colors.CYAN_300)
        self.progress_label = ft.Text(size=12)
        self.elapsed_label = ft.Text("Thời gian: 00:00:00", size=12,
                                    color=ft.Colors.BLUE_GREY_300)
        self.operation_started_at = None
        self.file_progress = ft.ProgressBar(value=None, color=ft.Colors.BLUE_300)
        self.file_label = ft.Text(size=12)
        self.progress_panel = ft.Column(visible=False, spacing=4, controls=[
            ft.Row([self.progress_label, self.elapsed_label],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            self.progress, self.file_label, self.file_progress])
        self.trim_progress_label = ft.Text(size=12)
        self.trim_progress = ft.ProgressBar(value=0, color=ft.Colors.AMBER_300)
        self.trim_activity = ft.ProgressBar(value=None, color=ft.Colors.AMBER_300)
        self.trim_progress_panel = ft.Column(visible=False, spacing=4, controls=[
            self.trim_progress_label, self.trim_progress, self.trim_activity])
        self.error_details = ft.Column(visible=False, spacing=6, height=150, scroll=ft.ScrollMode.AUTO)
        self.dismissed_errors = set()
        self.clear_log_button = ft.IconButton(icon=ft.Icons.CLEAR_ALL, tooltip="Clear log",
                                              on_click=self.clear_log)
        self.message = ft.Text("Import file .xlsx có sheet Footage Tracker hoặc Visual Beat Tracker để bắt đầu.")
        self.counter = ft.Text()
        self.table = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO)
        self.import_button = ft.Button("Import Script", icon=ft.Icons.UPLOAD_FILE,
                                       tooltip="Import file Excel", on_click=self.import_file)
        self.previous = ft.IconButton(ft.Icons.CHEVRON_LEFT, tooltip="Trang trước", on_click=self.previous_page)
        self.next = ft.IconButton(ft.Icons.CHEVRON_RIGHT, tooltip="Trang sau", on_click=self.next_page)
        self.select_all = ft.IconButton(icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK,
                                       tooltip="Select all — Chọn tất cả dòng trên mọi trang",
                                       on_click=self.toggle_all)
        self.select_all_trimmed = ft.IconButton(icon=ft.Icons.VIDEO_LIBRARY_OUTLINED,
            tooltip="Chọn tất cả video đã trim", on_click=self.toggle_all_trimmed)
        self.export_selected_button = ft.IconButton(
            icon=ft.Icons.DOWNLOAD, tooltip="Tải video đã chọn thành file ZIP",
            on_click=self.export_selected_videos)
        self.selection_count = ft.Text("Đã chọn 0 dòng", size=13)
        self.delete_button = ft.IconButton(tooltip="Xóa line", icon=ft.Icons.DELETE_OUTLINE,
                                      on_click=self.ask_delete)
        self.edit_script_button = ft.IconButton(icon=ft.Icons.EDIT_OUTLINED,
            tooltip="Sửa Start và End", on_click=self.toggle_script_edit)
        self.save_script_edits_button = ft.IconButton(icon=ft.Icons.CHECK,
            tooltip="Lưu chỉnh sửa", visible=False, on_click=self.save_script_edits)
        self.cancel_script_edits_button = ft.IconButton(icon=ft.Icons.CLOSE,
            tooltip="Hủy chỉnh sửa", visible=False, on_click=self.cancel_script_edits)
        self.trim_button = ft.IconButton(icon=ft.Icons.CONTENT_CUT,
            tooltip="Trim video đã chọn", on_click=self.trim_selected_videos)
        self.crawl_button = ft.FilledButton("Crawl", icon=ft.Icons.DOWNLOAD,
                                            on_click=self.download_selected)
        self.pexels_link = ft.TextField(
            label="Link video Pexels",
            hint_text="https://www.pexels.com/video/ten-video-123456/",
            expand=True,
            on_change=self.update_pexels_link_action,
        )
        self.pexels_link_button = ft.OutlinedButton(
            "Crawl link", icon=ft.Icons.LINK, on_click=self.crawl_pexels_link, disabled=True)
        self.cancel_button = ft.IconButton(icon=ft.Icons.CANCEL_OUTLINED, tooltip="Hủy tải",
                                           visible=False, on_click=self.cancel_download)
        self.resume_search = ft.TextButton("Tiếp tục tìm kiếm", visible=False,
                                           on_click=lambda _: get_api_key_pool().resume())
        self.filter_all = ft.TextButton("Tất cả", on_click=lambda _: self.set_filter("all"))
        self.filter_missing = ft.TextButton("Chưa có video", on_click=lambda _: self.set_filter("missing"),
            tooltip="Các dòng chưa có URL hoặc đường dẫn trong Selected clip URL / file")
        self.filter_trimmed = ft.TextButton("Trimed video", on_click=lambda _: self.set_filter("trimmed"),
            tooltip="Các dòng có file video đã trim")
        self.filter_untrimmed = ft.TextButton("Chưa có trimed video",
            on_click=lambda _: self.set_filter("untrimmed"),
            tooltip="Các dòng chưa có file video đã trim")
        self.filter_errors = ft.TextButton("Dòng bị lỗi", on_click=lambda _: self.set_filter("errors"),
            tooltip="Các dòng có lỗi tải video, trim hoặc thời lượng footage không đủ")
        self.filter_count = ft.Text(size=12, color=ft.Colors.BLUE_GREY_300)
        self.control = ft.Column(expand=True, controls=[
            ft.Row([ft.Text("Script", size=28, weight=ft.FontWeight.BOLD),
                    self.import_button],
                   spacing=12, wrap=False, alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            self.message,
            ft.Row([self.video_settings, self.crawl_button, self.cancel_button,
                    self.resume_search], spacing=8, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([self.pexels_link, self.pexels_link_button], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.END),
            ft.Row([self.select_all, self.select_all_trimmed, self.export_selected_button,
                    self.trim_button, self.delete_button, self.edit_script_button,
                    self.save_script_edits_button, self.cancel_script_edits_button,
                    self.selection_count],
                   spacing=6, wrap=False, scroll=ft.ScrollMode.AUTO),
            ft.Row([ft.Icon(ft.Icons.FILTER_LIST), self.filter_all, self.filter_missing,
                    self.filter_trimmed, self.filter_untrimmed, self.filter_errors,
                    self.filter_count], spacing=8, scroll=ft.ScrollMode.AUTO),
            self.progress_panel,
            self.trim_progress_panel,
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
            self.ensure_trim_column()
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
            self.ensure_trim_column(data)
            await asyncio.to_thread(save_tracker, data, self.target)
            self.data, self.offset = data, 0
            self.selected.clear()
            self.selected_trimmed.clear()
            self.posters.clear()
            self.dismissed_errors.clear()
            self.progress_panel.visible = False
            await self.load_posters()
            self.render()
        except Exception as exc:
            self.message.value = f"Import không thành công: {exc}"
        finally:
            if self.message.value == "Đang import Script...":
                self.message.value = old_message
            self.busy = False
            self.refresh_controls()
            self.page.update()

    def toggle_row(self, index: int, checked: bool, checkbox=None):
        if self.busy:
            return
        if checked:
            self.selected.add(index)
        else:
            self.selected.discard(index)
        if checkbox is not None:
            checkbox.value = checked
        self.update_actions()
        self.page.update()

    def toggle_trimmed_row(self, index: int, checked: bool, checkbox=None):
        if self.busy:
            return
        if checked:
            self.selected_trimmed.add(index)
        else:
            self.selected_trimmed.discard(index)
        if checkbox is not None:
            checkbox.value = checked
        self.page.update()

    def toggle_all(self, _):
        if self.busy:
            return
        matching = set(self.filtered_indices())
        self.selected = matching if self.selected != matching else set()
        self.render()
        self.page.update()

    def trimmed_indices(self):
        if not self.data or "Trimed video" not in self.data["headers"]:
            return set()
        column = self.data["headers"].index("Trimed video")
        return {index for index, row in enumerate(self.data["rows"])
                if row[column].strip() and Path(row[column]).is_file()}

    def toggle_all_trimmed(self, _):
        if self.busy:
            return
        available = self.trimmed_indices()
        self.selected_trimmed = set() if available and available <= self.selected_trimmed else available
        self.render()
        self.page.update()

    def filtered_indices(self):
        if not self.data:
            return []
        headers = self.data["headers"]
        column = headers.index("Selected clip URL / file") if "Selected clip URL / file" in headers else None
        trimmed_column = headers.index("Trimed video") if "Trimed video" in headers else None
        return [i for i, row in enumerate(self.data["rows"])
                if (self.filter_mode == "all"
                    or self.filter_mode == "missing" and (column is None or not row[column].strip())
                    or self.filter_mode == "trimmed" and trimmed_column is not None
                    and bool(row[trimmed_column].strip()) and Path(row[trimmed_column]).is_file()
                    or self.filter_mode == "untrimmed" and (trimmed_column is None
                    or not row[trimmed_column].strip() or not Path(row[trimmed_column]).is_file())
                    or self.filter_mode == "errors" and self.row_has_error(row, headers))]

    @classmethod
    def row_has_error(cls, row, headers):
        if cls.duration_error_columns(row, headers):
            return True
        if "Status" not in headers:
            return False
        status = row[headers.index("Status")].strip().casefold()
        return status.startswith(("lỗi", "error")) or "lỗi duration:" in status

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
            self.mark_duration_errors(updated)
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
        available_trimmed = self.trimmed_indices()
        self.selected_trimmed.intersection_update(available_trimmed)
        self.filter_all.disabled = self.busy
        self.filter_missing.disabled = self.busy
        self.filter_trimmed.disabled = self.busy
        self.filter_untrimmed.disabled = self.busy
        self.filter_errors.disabled = self.busy
        self.filter_all.style = ft.ButtonStyle(bgcolor="#20335a" if self.filter_mode == "all" else None)
        self.filter_missing.style = ft.ButtonStyle(bgcolor="#20335a" if self.filter_mode == "missing" else None)
        self.filter_trimmed.style = ft.ButtonStyle(bgcolor="#20335a" if self.filter_mode == "trimmed" else None)
        self.filter_untrimmed.style = ft.ButtonStyle(
            bgcolor="#20335a" if self.filter_mode == "untrimmed" else None)
        self.filter_errors.style = ft.ButtonStyle(bgcolor="#20335a" if self.filter_mode == "errors" else None)
        total = len(self.data["rows"]) if self.data else 0
        self.filter_count.value = f"{count} / {total} dòng"
        all_selected = bool(count) and len(self.selected) == count
        self.select_all.icon = (ft.Icons.CHECK_BOX if all_selected else
                                ft.Icons.INDETERMINATE_CHECK_BOX if self.selected else
                                ft.Icons.CHECK_BOX_OUTLINE_BLANK)
        self.select_all.tooltip = "Bỏ chọn tất cả" if all_selected else "Select all — Chọn tất cả dòng khớp bộ lọc trên mọi trang"
        self.select_all.disabled = self.busy or not count
        all_trimmed_selected = bool(available_trimmed) and available_trimmed <= self.selected_trimmed
        self.select_all_trimmed.icon = (ft.Icons.VIDEO_LIBRARY if all_trimmed_selected else
            ft.Icons.VIDEO_LIBRARY_OUTLINED)
        self.select_all_trimmed.tooltip = ("Bỏ chọn tất cả video đã trim" if all_trimmed_selected
                                           else "Chọn tất cả video đã trim")
        self.select_all_trimmed.disabled = self.busy or not available_trimmed
        self.export_selected_button.disabled = self.busy or self.edit_mode or not self.selected_local_videos()
        self.trim_button.disabled = self.busy or self.edit_mode or not self.selected
        self.delete_button.disabled = self.busy or self.edit_mode or not self.selected
        self.crawl_button.disabled = self.busy or self.edit_mode or not self.selected or self.database is None
        self.pexels_link_button.disabled = (self.busy or self.edit_mode or len(self.selected) != 1
                                            or self.database is None
                                            or not (self.pexels_link.value or "").strip())
        self.import_button.disabled = self.busy or self.edit_mode
        self.edit_script_button.disabled = self.busy or self.edit_mode or not self.data
        self.save_script_edits_button.disabled = self.busy
        self.cancel_script_edits_button.disabled = self.busy
        self.selection_count.value = f"Đã chọn {len(self.selected)} / {count} dòng"

    @staticmethod
    def parse_script_timecode(value):
        match = re.fullmatch(r"\s*(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)\s*", value or "")
        if not match:
            raise ValueError("Dùng định dạng HH:MM:SS.mmm, ví dụ 00:00:04.087.")
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def toggle_script_edit(self, _):
        if self.busy or not self.data:
            return
        headers = self.data["headers"]
        if not all(name in headers for name in ("Start", "End", "Duration (s)")):
            self.message.value = "Script cần có các cột Start, End và Duration (s) để chỉnh sửa."
            self.page.update()
            return
        self.edit_values = {
            index: {name: row[headers.index(name)] for name in ("Start", "End")}
            for index, row in enumerate(self.data["rows"])
        }
        self.edit_mode = True
        self.edit_script_button.visible = False
        self.save_script_edits_button.visible = True
        self.cancel_script_edits_button.visible = True
        self.message.value = "Chỉnh sửa Start và End, sau đó bấm Lưu."
        self.render()
        self.page.update()

    def update_script_edit_value(self, index, header, value):
        if index in self.edit_values:
            self.edit_values[index][header] = value

    def show_script_edit_error(self, message):
        self.message.value = message
        close = lambda _: self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Không thể lưu chỉnh sửa"),
            content=ft.Text(message, selectable=True),
            actions=[ft.TextButton("Đã hiểu", on_click=close)],
        ))
        self.page.update()

    def cancel_script_edits(self, _):
        if self.busy:
            return
        self.edit_values.clear()
        self.edit_mode = False
        self.edit_script_button.visible = True
        self.save_script_edits_button.visible = False
        self.cancel_script_edits_button.visible = False
        self.message.value = "Đã hủy chỉnh sửa thời gian."
        self.render()
        self.page.update()

    async def save_script_edits(self, _):
        if self.busy or not self.edit_mode or not self.data:
            return
        headers = self.data["headers"]
        start_column, end_column = headers.index("Start"), headers.index("End")
        duration_column = headers.index("Duration (s)")
        intervals = []
        updated = {**self.data, "rows": [list(row) for row in self.data["rows"]]}
        for index, row in enumerate(updated["rows"]):
            values = self.edit_values.get(index, {})
            start_text = values.get("Start", row[start_column])
            end_text = values.get("End", row[end_column])
            if not start_text.strip() and not end_text.strip():
                row[duration_column] = ""
                continue
            try:
                start_seconds = self.parse_script_timecode(start_text)
                end_seconds = self.parse_script_timecode(end_text)
            except ValueError as exc:
                self.message.value = f"Dòng {index + 1}: {exc}"
                self.page.update()
                return
            if end_seconds <= start_seconds:
                self.message.value = f"Dòng {index + 1}: End phải sau Start."
                self.page.update()
                return
            row[start_column], row[end_column] = start_text.strip(), end_text.strip()
            duration = round(end_seconds - start_seconds, 1)
            row[duration_column] = f"{duration:.1f}"
            intervals.append((start_seconds, end_seconds, index))
        intervals.sort()
        furthest_end = -1.0
        furthest_index = -1
        for start_seconds, end_seconds, index in intervals:
            if start_seconds < furthest_end:
                self.show_script_edit_error(
                    f"Khung thời gian bị trùng giữa dòng {furthest_index + 1} và dòng {index + 1}. "
                    "Hãy chỉnh Start/End để các đoạn không chồng lấn rồi thử lưu lại.")
                return
            if end_seconds > furthest_end:
                furthest_end, furthest_index = end_seconds, index
        self.busy = True
        self.refresh_controls()
        try:
            self.mark_duration_errors(updated)
            await asyncio.to_thread(save_tracker, updated, self.target)
        except (OSError, ValueError) as exc:
            self.message.value = f"Không lưu được chỉnh sửa: {exc}"
            return
        else:
            self.data = updated
            self.edit_values.clear()
            self.edit_mode = False
            self.edit_script_button.visible = True
            self.save_script_edits_button.visible = False
            self.cancel_script_edits_button.visible = False
            self.message.value = "Đã lưu Start, End và cập nhật Duration (s)."
        finally:
            self.busy = False
            self.render()
            self.refresh_controls()
            self.page.update()

    def selected_local_videos(self) -> list[Path]:
        if not self.data or "Selected clip URL / file" not in self.data["headers"]:
            return []
        headers = self.data["headers"]
        column = headers.index("Selected clip URL / file")
        trim_column = headers.index("Trimed video") if "Trimed video" in headers else None
        paths = []
        for index in sorted(self.selected | self.selected_trimmed):
            if not 0 <= index < len(self.data["rows"]):
                continue
            row = self.data["rows"][index]
            trimmed = row[trim_column].strip() if trim_column is not None else ""
            value = trimmed if index in self.selected_trimmed and trimmed else row[column].strip()
            if not value:
                value = trimmed
            if not value:
                continue
            path = Path(value)
            if path.suffix.lower() == ".mp4" and path.is_file():
                paths.append(path)
        return paths

    async def export_selected_videos(self, _):
        if self.busy or not (self.selected or self.selected_trimmed):
            return
        headers = self.data["headers"]
        source_col = headers.index("Selected clip URL / file")
        trim_col = headers.index("Trimed video") if "Trimed video" in headers else None
        path_by_row = {}
        chosen_indices = self.selected | self.selected_trimmed
        for index in sorted(chosen_indices):
            if not 0 <= index < len(self.data["rows"]):
                continue
            row = self.data["rows"][index]
            trimmed = row[trim_col].strip() if trim_col is not None else ""
            source = trimmed if index in self.selected_trimmed and trimmed else row[source_col].strip()
            path = Path(source or trimmed)
            if path.suffix.lower() == ".mp4" and path.is_file():
                path_by_row[index] = path
        export_rows = list(path_by_row)
        paths = [path_by_row[index] for index in export_rows]
        beat_col = headers.index("Beat ID") if "Beat ID" in headers else None
        visual_col = headers.index("Visual Direction") if "Visual Direction" in headers else None
        filename_col = headers.index("File Name") if "File Name" in headers else None
        names = []
        export_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        script_name = "_".join(re.findall(r"[\w]+", self.script_name, flags=re.UNICODE))
        archive_name = f"{script_name}_{export_timestamp}" if script_name else export_timestamp
        for index in export_rows:
            row = self.data["rows"][index]
            beat = row[beat_col] if beat_col is not None else ""
            direction = row[visual_col] if visual_col is not None else ""
            beat = re.sub(r"^VB[-_ ]*", "", beat.strip(), flags=re.IGNORECASE)
            direction_words = re.findall(r"[\w]+", direction, flags=re.UNICODE)
            direction_part = "_".join(direction_words[:4])
            original_value = row[source_col].strip()
            original_stem = Path(original_value).stem if original_value else path_by_row[index].stem
            resolution = f"{original_stem.rsplit('_', 1)[-1]}"
            if not re.fullmatch(r"\d+x\d+", resolution):
                resolution = ""
            if beat and direction_part:
                label = f"VB{beat}_{direction_part}"
            elif filename_col is not None and row[filename_col].strip():
                label = Path(row[filename_col]).stem.rsplit("_", 1)[0]
            else:
                label = path_by_row[index].stem.rsplit("_", 1)[0]
            if resolution:
                label = f"{label}_{resolution}"
            names.append(f"{label}.mp4")
        if not paths:
            self.message.value = "Các dòng đã chọn chưa có file MP4 cục bộ để đóng gói."
            self.page.update()
            return
        try:
            destination = await self.picker.save_file(
                dialog_title="Lưu video Script thành ZIP",
                file_name=f"{archive_name}.zip",
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
            archived, skipped = await asyncio.to_thread(export_videos_zip, paths, archive_path, names)
            unavailable = len(chosen_indices) - len(paths)
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
        self.ensure_trim_column()
        source_column = self.data["headers"].index("Selected clip URL / file")
        trim_column = (self.data["headers"].index("Trimed video")
                       if "Trimed video" in self.data["headers"] else None)
        paths = []
        for row in self.data["rows"]:
            paths.append(row[source_column])
            if trim_column is not None:
                paths.append(row[trim_column])

        def read():
            result = {}
            for value in paths:
                # Only local MP4 files may supply thumbnails; never fetch workbook URLs.
                path = Path(value)
                if path.suffix.lower() == ".mp4" and path.is_file():
                    result[value] = thumbnail_bytes(path)
            return result

        self.posters = await asyncio.to_thread(read)
        changed = False
        original_duration_column = self.data["headers"].index("Thumbnail Video Duration")
        trimmed_duration_column = self.data["headers"].index("Trimed video Duration")
        for row in self.data["rows"]:
            original_path = row[source_column]
            trimmed_path = row[trim_column] if trim_column is not None else ""
            if original_path and not row[original_duration_column]:
                row[original_duration_column] = await asyncio.to_thread(self.read_video_duration, original_path)
                changed = changed or bool(row[original_duration_column])
            if trimmed_path and not row[trimmed_duration_column]:
                row[trimmed_duration_column] = await asyncio.to_thread(self.read_video_duration, trimmed_path)
                changed = changed or bool(row[trimmed_duration_column])
        changed = self.mark_duration_errors(self.data) or changed
        if changed:
            await asyncio.to_thread(save_tracker, self.data, self.target)

    def cancel_download(self, _):
        self.cancel.set()
        self.cancel_button.disabled = True
        self.message.value = "Đang hủy tải..."
        self.page.update()

    def update_pexels_link_action(self, _=None):
        self.pexels_link_button.disabled = (
            self.busy or self.edit_mode or len(self.selected) != 1
            or self.database is None or not (self.pexels_link.value or "").strip())
        self.page.update()

    async def crawl_pexels_link(self, _):
        if self.busy or len(self.selected) != 1 or not self.data or self.database is None:
            return
        url = (self.pexels_link.value or "").strip()
        if not url:
            return

        self.ensure_trim_column()
        index = next(iter(self.selected))
        self.busy = True
        self.cancel = Event()
        self.cancel_button.visible = True
        self.cancel_button.disabled = False
        self.progress_panel.visible = True
        self.operation_started_at = time.monotonic()
        self.update_elapsed()
        self.file_progress.visible = True
        self.file_progress.value = None
        self.progress.value = None
        self.progress_label.value = "Đang xử lý 1 dòng đã chọn"
        self.file_label.value = "Đang lấy và tải video từ link Pexels..."
        self.refresh_controls()
        self.page.update()
        try:
            worker = asyncio.create_task(asyncio.to_thread(
                download_pexels_link, url, self.library_root, self.database, self.cancel,
                None, self.video_settings_preferences.get("orientation", "all")))
            while not worker.done():
                await asyncio.wait({worker}, timeout=0.25)
                self.update_elapsed()
                self.page.update()
            path = await worker
            headers = list(self.data["headers"])
            rows = [list(row) for row in self.data["rows"]]
            for name in ("Status", "Selected clip URL / file", "File Name"):
                if name not in headers:
                    headers.append(name)
                    for row in rows:
                        row.append("")
            row = rows[index]
            row[headers.index("Selected clip URL / file")] = path
            row[headers.index("File Name")] = Path(path).name
            row[headers.index("Status")] = "Downloaded"
            await self.update_video_durations(row, headers, original_path=path)
            self.posters[path] = await asyncio.to_thread(thumbnail_bytes, Path(path))
            updated = {**self.data, "headers": headers, "rows": rows}
            self.mark_duration_errors(updated)
            await asyncio.to_thread(save_tracker, updated, self.target)
            self.data = updated
            self.progress.value = 1
            self.progress_label.value = "Đã xử lý 1/1 dòng · 100%"
            self.file_label.value = "Video Pexels đã được cập nhật vào dòng đã chọn."
            self.message.value = f"Đã cập nhật video từ link Pexels vào dòng {index + 1}."
            if self.on_download_complete:
                self.on_download_complete()
        except InterruptedError:
            self.message.value = "Đã hủy tải video từ link Pexels."
        except Exception as exc:
            self.message.value = f"Không thể cập nhật link Pexels: {self.error_text(exc)}"
        finally:
            self.busy = False
            self.update_elapsed()
            self.cancel_button.visible = False
            self.file_progress.visible = False
            self.refresh_controls()
            self.page.update()

    @staticmethod
    def error_text(exc):
        text = str(exc) or type(exc).__name__
        secrets = [*configured_api_keys(), *configured_pexels_api_keys()]
        for api_key in sorted((key for key in secrets if key), key=len, reverse=True):
            text = text.replace(api_key, "[ẩn API key]")
        return re.sub(r"(?i)([?&]key=)[^&\s]+", r"\1[ẩn]", text)

    def clear_log(self, _):
        if self.data and "Status" in self.data["headers"]:
            column = self.data["headers"].index("Status")
            self.dismissed_errors.update(tuple(row) for row in self.data["rows"]
                                         if row[column].startswith("Lỗi:")
                                         or "Lỗi duration:" in row[column])
        self.refresh_controls()
        self.page.update()

    def update_progress(self, processed, total):
        self.progress.value = processed / total if total else 0
        self.progress_label.value = f"Đã xử lý {processed}/{total} dòng · {self.progress.value:.0%}"

    def update_elapsed(self):
        elapsed = max(0, int(time.monotonic() - self.operation_started_at)) \
            if self.operation_started_at is not None else 0
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.elapsed_label.value = f"Thời gian: {hours:02d}:{minutes:02d}:{seconds:02d}"

    async def download_with_progress(self, keyword, api_key, previous_clip="", beat_id="", visual_direction="",
                                    exclude_ids=None):
        updates = deque(maxlen=1)
        worker = asyncio.create_task(asyncio.to_thread(
            download_scene, keyword, api_key, self.library_root, self.database, self.cancel,
            updates.append, previous_clip, self.video_settings_preferences["quality"],
            self.video_settings_preferences["orientation"], beat_id, visual_direction,
            self.video_settings_preferences.get("source", "pixabay"), exclude_ids))
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

    async def download_with_retry(self, keyword, api_key, previous_clip, beat_id="", visual_direction="",
                                 exclude_ids=None):
        if isinstance(api_key, ApiKeyPool):
            return await self.download_with_progress(keyword, api_key, previous_clip, beat_id,
                                                     visual_direction, exclude_ids)
        for attempt in range(4):
            if self.cancel.is_set():
                raise InterruptedError("Đã hủy tải.")
            try:
                return await self.download_with_progress(keyword, api_key, previous_clip, beat_id,
                                                         visual_direction, exclude_ids)
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
        if not any(name in self.data["headers"] for name in
                   ("Primary search keyword", "Primary Stock Keyword")):
            self.message.value = "Script thiếu cột từ khóa tìm kiếm."
            self.page.update()
            return
        self.ensure_trim_column()
        preferences = self.video_settings_preferences
        source = preferences.get("source", "pixabay").lower()
        method = preferences.get("method", "api").lower()
        if method == "direct" and source != "pexels":
            self.message.value = "Crawl trực tiếp chỉ hỗ trợ Pexels. Hãy chọn Pexels hoặc Crawl by API."
            self.page.update()
            return
        api_key = None if method == "direct" else (
            configured_pexels_api_key() if source == "pexels" else get_api_key_pool())
        if method == "api" and not api_key:
            provider = "Pexels" if source == "pexels" else "Pixabay"
            self.message.value = f"Hãy nhập {provider} API key trong Setup trước khi tải."
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
        self.operation_started_at = time.monotonic()
        self.update_elapsed()
        self.file_progress.visible = True
        self.file_progress.value = None
        worker_dropdown = getattr(self.video_settings, "workers_dropdown", None)
        selected_workers = (worker_dropdown.value if worker_dropdown is not None
                            else preferences.get("workers", self.download_workers))
        try:
            worker_count = min(20, max(1, int(selected_workers)))
        except (TypeError, ValueError):
            worker_count = self.download_workers
        preferences["workers"] = str(worker_count)
        self.file_label.value = (
            f"Đang chuẩn bị tối đa {worker_count} luồng crawl trực tiếp từ Pexels..."
            if method == "direct"
            else f"Đang chuẩn bị tối đa {worker_count} luồng tải...")
        self.update_progress(0, len(selected))
        self.render()
        self.page.update()
        try:
            headers = list(self.data["headers"])
            rows = [list(r) for r in self.data["rows"]]
            for name in ("Status", "Selected clip URL / file", "File Name"):
                if name not in headers:
                    headers.append(name)
                    for row in rows:
                        row.append("")
            keyword_header = ("Primary search keyword" if "Primary search keyword" in headers
                              else "Primary Stock Keyword")
            clip_column = headers.index("Selected clip URL / file")
            status_column = headers.index("Status")
            filename_column = headers.index("File Name")
            script_excluded_ids = {video_id for row in rows
                                   if (video_id := video_id_from_clip(row[clip_column])) is not None}
            tasks = {}
            loop = asyncio.get_running_loop()

            def start_scene(index):
                row = rows[index]
                if method == "direct":
                    return loop.run_in_executor(
                        executor, download_scene_direct_pexels,
                        row[headers.index(keyword_header)], self.library_root,
                        self.database, self.cancel, None,
                        preferences["orientation"], script_excluded_ids)
                return loop.run_in_executor(
                    executor, download_scene,
                    row[headers.index(keyword_header)], api_key, self.library_root,
                    self.database, self.cancel, None, row[clip_column],
                    preferences["quality"], preferences["orientation"],
                    row[headers.index("Beat ID")] if "Beat ID" in headers else "",
                    row[headers.index("Visual Direction")] if "Visual Direction" in headers else "",
                    source, script_excluded_ids)

            executor = ThreadPoolExecutor(max_workers=worker_count,
                                          thread_name_prefix="script-download")
            next_position = 0
            while next_position < len(selected) and len(tasks) < worker_count:
                index = selected[next_position]
                if self.cancel.is_set():
                    break
                tasks[start_scene(index)] = index
                next_position += 1
            rate_limited = False
            finished_indices = set()
            while tasks:
                done, _ = await asyncio.wait(
                    tasks, timeout=0.25, return_when=asyncio.FIRST_COMPLETED)
                self.update_elapsed()
                self.page.update()
                if not done:
                    continue
                for task in done:
                    index = tasks.pop(task)
                    finished_indices.add(index)
                    row = rows[index]
                    try:
                        path = task.result()
                    except InterruptedError:
                        if not self.cancel.is_set():
                            row[status_column] = "Lỗi: Đã hủy lượt tìm kiếm."
                            failed += 1
                    except PixabayAccessPause:
                        row[status_column] = "Paused: Pixabay access verification required"
                        self.resume_search.visible = True
                        self.message.value = "Pixabay yêu cầu xác minh truy cập. Kiểm tra trên trình duyệt rồi bấm Tiếp tục tìm kiếm."
                        self.page.update()
                        while not self.cancel.is_set() and get_api_key_pool().pause_reason:
                            await asyncio.sleep(0.1)
                        if not self.cancel.is_set():
                            try:
                                path = await self.download_with_retry(
                                    row[headers.index(keyword_header)], api_key, row[clip_column],
                                    row[headers.index("Beat ID")] if "Beat ID" in headers else "",
                                    row[headers.index("Visual Direction")] if "Visual Direction" in headers else "",
                                    script_excluded_ids)
                            except InterruptedError:
                                break
                            except Exception as exc:
                                row[status_column] = f"Lỗi: {self.error_text(exc)}"
                                failed += 1
                                rate_limited = isinstance(exc, PixabayRateLimitError)
                            else:
                                row[clip_column] = path
                                row[filename_column] = Path(path).name
                                await self.update_video_durations(row, headers, original_path=path)
                                row[status_column] = "Downloaded"
                                completed += 1
                                self.posters[path] = await asyncio.to_thread(thumbnail_bytes, Path(path))
                        else:
                            break
                    except Exception as exc:
                        row[status_column] = f"Lỗi: {self.error_text(exc)}"
                        self.dismissed_errors.discard(tuple(row))
                        failed += 1
                        rate_limited = rate_limited or isinstance(exc, PixabayRateLimitError)
                    else:
                        row[clip_column] = path
                        row[filename_column] = Path(path).name
                        await self.update_video_durations(row, headers, original_path=path)
                        row[status_column] = "Downloaded"
                        completed += 1
                        self.posters[path] = await asyncio.to_thread(thumbnail_bytes, Path(path))
                        if self.on_download_complete:
                            self.on_download_complete()
                    if self.cancel.is_set():
                        continue
                    updated = {**self.data, "headers": headers, "rows": rows}
                    self.mark_duration_errors(updated)
                    await asyncio.to_thread(save_tracker, updated, self.target)
                    self.data = updated
                    processed += 1
                    self.update_progress(processed, len(selected))
                    self.message.value = f"Đang tải · hoàn tất {processed}/{len(selected)} dòng"
                    self.refresh_controls()
                    self.page.update()
                    if rate_limited:
                        self.cancel.set()
                if not self.cancel.is_set():
                    while next_position < len(selected) and len(tasks) < worker_count:
                        index = selected[next_position]
                        tasks[start_scene(index)] = index
                        next_position += 1
            self.message.value = (f'{"Đã hủy. " if self.cancel.is_set() else ""}'
                                  f"Đã tải {completed}, lỗi {failed} dòng.")
            if rate_limited:
                self.message.value = ("Đã dừng lượt tải sau HTTP 429 từ API. "
                                      "Các dòng chưa xử lý được giữ nguyên; hãy thử lại sau.")
        except Exception as exc:
            self.message.value = f"Đã dừng: {self.error_text(exc)}. Video đã tải vẫn được giữ trong Library."
        finally:
            executor.shutdown(wait=False, cancel_futures=True) if "executor" in locals() else None
            self.busy = False
            self.update_elapsed()
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

    async def trim_selected_videos(self, _):
        if self.busy or not self.selected or not self.data:
            return
        headers = self.data["headers"]
        source_col = headers.index("Selected clip URL / file") if "Selected clip URL / file" in headers else None
        duration_col = headers.index("Duration (s)") if "Duration (s)" in headers else None
        if source_col is None or duration_col is None:
            self.message.value = "Trim cần cột Selected clip URL / file và Duration (s)."
            self.page.update()
            return
        self.ensure_trim_column()
        trim_col = headers.index("Trimed video")
        selected = sorted(self.selected)
        completed = failed = 0
        self.busy = True
        total = len(selected)
        self.trim_progress_panel.visible = True
        self.trim_progress.value = 0
        self.trim_progress_label.value = f"Chuẩn bị trim {total} video..."
        self.message.value = f"Đang trim 0/{total} video..."
        self.refresh_controls()
        self.page.update()
        try:
            for position, index in enumerate(selected, 1):
                row = self.data["rows"][index]
                scene = row[headers.index("Beat ID")] if "Beat ID" in headers else str(index + 1)
                self.trim_progress_label.value = (
                    f"Đang trim {scene} · {position - 1}/{total} hoàn tất; FFmpeg đang xử lý..."
                )
                self.page.update()
                try:
                    source = Path(row[source_col].strip())
                    if source.suffix.lower() != ".mp4" or not source.is_file():
                        raise ValueError("Dòng chưa có file MP4 cục bộ.")
                    duration = round(float(row[duration_col]), 1)
                    previous_trim = Path(row[trim_col]) if row[trim_col].strip() else None
                    destination = source.with_name(f"{source.stem}_trimmed_{duration:.1f}s.mp4")
                    result = await asyncio.to_thread(trim_video, source, destination, duration)
                    row[trim_col] = str(result.resolve())
                    await self.update_video_durations(row, headers, trimmed_path=result)
                    if previous_trim and previous_trim.resolve() != result.resolve():
                        previous_trim.unlink(missing_ok=True)
                        previous_trim.with_suffix(".jpg").unlink(missing_ok=True)
                        previous_trim.with_suffix(".thumb.jpg").unlink(missing_ok=True)
                    self.posters[row[trim_col]] = await asyncio.to_thread(thumbnail_bytes, result)
                    completed += 1
                except (OSError, ValueError, RuntimeError, TypeError) as exc:
                    failed += 1
                    row[trim_col] = ""
                    row[headers.index("Trimed video Duration")] = ""
                    if "Status" in headers:
                        row[headers.index("Status")] = f"Lỗi trim: {self.error_text(exc)}"
                updated = {**self.data, "rows": self.data["rows"], "headers": headers}
                self.mark_duration_errors(updated)
                await asyncio.to_thread(save_tracker, updated, self.target)
                self.data = updated
                self.trim_progress.value = position / total
                self.trim_progress_label.value = (
                    f"Đã xử lý {position}/{total} · thành công {completed}, lỗi {failed}"
                )
                self.message.value = f"Đang trim {position}/{total} video..."
                self.refresh_controls()
                self.page.update()
            self.message.value = f"Đã trim {completed} video, lỗi {failed} dòng."
        finally:
            self.busy = False
            self.trim_activity.visible = False
            self.trim_progress.value = 1 if completed + failed == total else completed / total
            self.trim_progress_label.value = f"Hoàn tất · thành công {completed}, lỗi {failed}"
            self.refresh_controls()
            self.page.update()

    def ensure_trim_column(self, data=None):
        data = data or self.data
        if not data:
            return
        headers = data["headers"]
        for name in ("Thumbnail Video Duration", "Trimed video Duration", "Trimed video"):
            if name not in headers:
                headers.append(name)
                for row in data["rows"]:
                    row.append("")

    @staticmethod
    def read_video_duration(path_value):
        if not path_value:
            return ""
        path = Path(path_value)
        if path.suffix.lower() != ".mp4" or not path.is_file():
            return ""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return ""
        try:
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
            duration = float(json.loads(probe.stdout)["format"]["duration"])
            return f"{duration:.1f}" if math.isfinite(duration) and duration >= 0 else ""
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            return ""

    async def update_video_durations(self, row, headers, original_path=None, trimmed_path=None):
        original_column = headers.index("Thumbnail Video Duration")
        trimmed_column = headers.index("Trimed video Duration")
        if original_path is not None:
            row[original_column] = await asyncio.to_thread(self.read_video_duration, original_path)
        if trimmed_path is not None:
            row[trimmed_column] = await asyncio.to_thread(self.read_video_duration, trimmed_path)

    @staticmethod
    def duration_error_columns(row, headers):
        required = {name: headers.index(name) for name in
                    ("Duration (s)", "Thumbnail Video Duration", "Trimed video Duration")
                    if name in headers}
        if "Duration (s)" not in required:
            return set()
        try:
            requested = float(row[required["Duration (s)"]])
        except (ValueError, TypeError):
            return set()
        return {name for name in ("Thumbnail Video Duration", "Trimed video Duration")
                if name in required and row[required[name]].strip()
                and ScriptView._duration_less_than(row[required[name]], requested)}

    @classmethod
    def mark_duration_errors(cls, data):
        headers = data["headers"]
        if not all(name in headers for name in
                   ("Duration (s)", "Thumbnail Video Duration", "Trimed video Duration")):
            return False
        status_name = "Status"
        status_exists = status_name in headers
        changed = False
        errors_by_row = [cls.duration_error_columns(row, headers) for row in data["rows"]]
        if any(errors_by_row) and not status_exists:
            headers.append(status_name)
            for row in data["rows"]:
                row.append("")
            status_exists = True
            changed = True
        if not status_exists:
            return changed
        status_column = headers.index(status_name)
        for row, error_columns in zip(data["rows"], errors_by_row):
            status = row[status_column]
            base_status = status.split("Lỗi duration:", 1)[0].rstrip(" |")
            if error_columns:
                details = []
                requested = row[headers.index("Duration (s)")]
                for name in sorted(error_columns):
                    details.append(f"{name} ({row[headers.index(name)]} s) < Duration (s) ({requested} s)")
                updated_status = f"{base_status} | Lỗi duration: {'; '.join(details)}" if base_status else \
                    f"Lỗi duration: {'; '.join(details)}"
            else:
                updated_status = base_status
            if row[status_column] != updated_status:
                row[status_column] = updated_status
                changed = True
        return changed

    @staticmethod
    def _duration_less_than(value, requested):
        try:
            return float(value) < requested
        except (ValueError, TypeError):
            return False

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
                if not (status.startswith("Lỗi:") or "Lỗi duration:" in status) \
                        or tuple(row) in self.dismissed_errors:
                    continue
                scene = row[headers.index("Scene ID")] if "Scene ID" in headers else str(index + 1)
                keyword_header = ("Primary search keyword" if "Primary search keyword" in headers
                                  else "Primary Stock Keyword")
                keyword = row[headers.index(keyword_header)] if keyword_header in headers else ""
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
        tracker_headers = list(self.data["headers"])
        display_headers = [header for header in tracker_headers
                           if header not in HIDDEN_SCRIPT_COLUMNS]
        if "Trimed video" in display_headers:
            display_headers.insert(0, display_headers.pop(display_headers.index("Trimed video")))
        for duration_header in ("Thumbnail Video Duration", "Trimed video Duration"):
            if duration_header in display_headers:
                display_headers.remove(duration_header)
        insert_at = display_headers.index("Trimed video") + 1 if "Trimed video" in display_headers else 0
        display_headers[insert_at:insert_at] = [
            name for name in ("Thumbnail Video Duration", "Trimed video Duration")
            if name in tracker_headers]
        keyword_header = next((name for name in ("Primary Stock Keyword",
                                                  "Primary search keyword")
                               if name in display_headers), None)
        if keyword_header and "Beat ID" in display_headers:
            display_headers.remove(keyword_header)
            display_headers.insert(display_headers.index("Beat ID") + 1, keyword_header)
        headers = ["STT", "Thumbnail video", *display_headers]
        widths = [52, 180, *[COLUMN_WIDTHS.get(h.strip(), 160) for h in display_headers]]
        self.table.width = sum(widths)

        def cell(text, width, heading=False):
            return ft.Container(width=width, padding=ft.Padding.symmetric(horizontal=8, vertical=10), content=ft.Text(
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
            row_duration_errors = self.duration_error_columns(row, tracker_headers)
            selector = ft.Checkbox(
                value=number - 1 in self.selected, disabled=self.busy, tooltip=f"Chọn dòng {number}",
                on_change=lambda e, index=number - 1: self.toggle_row(
                    index, bool(e.control.value), e.control))
            thumbnail = ft.Container(width=180, padding=10, content=ft.Container(
                width=160, height=90, bgcolor="#10182c", border_radius=6,
                alignment=ft.Alignment(0, 0), content=ft.Text("Chưa có video", size=12, color=ft.Colors.BLUE_GREY_300)))
            if "Selected clip URL / file" in self.data["headers"]:
                trim_col = (self.data["headers"].index("Trimed video")
                            if "Trimed video" in self.data["headers"] else None)
                original_path = row[self.data["headers"].index("Selected clip URL / file")]
                trim_path = row[trim_col].strip() if trim_col is not None else ""
                path = original_path
                poster = self.posters.get(path)
                if poster:
                    thumbnail.content.content = ft.Image(src=poster, width=160, height=90, fit=ft.BoxFit.COVER)
                elif path in self.posters:
                    thumbnail.content.content = ft.Text("Đã có video", size=12)
                if path and not path.lower().startswith(("https://", "http://")) and Path(path).suffix.lower() == ".mp4":
                    key = ("main", index, path)
                    tile = old_tiles.pop(key, None) or ScriptVideo(
                        path, poster, self.page, self.open_preview, self.stop_playback, self.playback_error)
                    self.video_tiles[key] = tile
                    thumbnail.content = tile.control
            thumbnail_stack = ft.Stack(width=180, height=110, controls=[
                thumbnail,
                ft.Container(left=8, top=8, width=36, height=36, bgcolor="#99000000",
                    border_radius=6, alignment=ft.Alignment(0, 0), content=selector),
            ])
            display_row = [row[tracker_headers.index(header)] for header in display_headers]
            display_cells = []
            for header, value, width in zip(display_headers, display_row, widths[2:]):
                if self.edit_mode and header in ("Start", "End"):
                    edit_value = self.edit_values.get(index, {}).get(header, value)
                    field = ft.TextField(
                        value=edit_value, dense=True, text_size=12, content_padding=6,
                        on_change=lambda e, index=index, header=header:
                            self.update_script_edit_value(index, header, e.control.value or ""))
                    display_cells.append(ft.Container(width=width, padding=6, content=field))
                elif header == "Trimed video" and value.strip():
                    trim_path = Path(value)
                    poster = self.posters.get(value)
                    if trim_path.is_file() and poster:
                        key = ("trim", index, value)
                        tile = old_tiles.pop(key, None) or ScriptVideo(
                            trim_path, poster, self.page, self.open_preview,
                            self.stop_playback, self.playback_error)
                        self.video_tiles[key] = tile
                        trim_selector = ft.Checkbox(
                            value=index in self.selected_trimmed, disabled=self.busy,
                            tooltip=f"Chọn video đã trim ở dòng {number}",
                            on_change=lambda e, index=index: self.toggle_trimmed_row(
                                index, bool(e.control.value), e.control))
                        content = ft.Stack(width=180, height=110, controls=[
                            tile.control,
                            ft.Container(left=8, top=8, width=36, height=36,
                                bgcolor="#99000000", border_radius=6,
                                alignment=ft.Alignment(0, 0), content=trim_selector),
                        ])
                    elif trim_path.is_file():
                        trim_selector = ft.Checkbox(
                            value=index in self.selected_trimmed, disabled=self.busy,
                            tooltip=f"Chọn video đã trim ở dòng {number}",
                            on_change=lambda e, index=index: self.toggle_trimmed_row(
                                index, bool(e.control.value), e.control))
                        content = ft.Row([trim_selector, ft.Text(trim_path.name, size=12, selectable=True)])
                    else:
                        content = ft.Text(value, size=12, selectable=True)
                    display_cells.append(ft.Container(width=width, padding=8, content=content))
                elif header in row_duration_errors:
                    display_cells.append(ft.Container(width=width, padding=8, bgcolor="#7f1d1d",
                        content=ft.Text(value, size=13, color=ft.Colors.RED_100, selectable=True)))
                else:
                    display_cells.append(cell(value, width))
            row_color = "#4a202b" if row_duration_errors else ("#162139" if number % 2 else "#10182c")
            self.table.controls.append(ft.Container(bgcolor=row_color,
                content=ft.Row([cell(str(number), widths[0]), thumbnail_stack,
                    *display_cells],
                    spacing=0, vertical_alignment=ft.CrossAxisAlignment.START)))
        for tile in old_tiles.values():
            # Removing a Video control disposes its native player. Invalidate callbacks too.
            tile.player = None


class ScriptWorkspace:
    """Sidebar for multiple named Script trackers, each backed by a ScriptView."""

    def __init__(self, page, picker, data_root: Path, database=None, library_root=None,
                 on_download_complete=None, open_preview=None, video_preferences=None):
        self.page, self.picker = page, picker
        self.data_root = Path(data_root)
        self.manifest = self.data_root / "scripts_index.json"
        self.database, self.library_root = database, library_root
        self.on_download_complete, self.open_preview = on_download_complete, open_preview
        self.video_preferences = video_preferences
        self.entries: list[dict] = []
        self.editors: dict[str, ScriptView] = {}
        self.active_id: str | None = None
        self.loaded = False
        self.name_field = ft.TextField(label="Tên script", hint_text="Ví dụ: Alaska", max_length=120)
        self.create_button = ft.FilledButton("Thêm script", icon=ft.Icons.ADD,
                                             on_click=self.create_from_field)
        self.feedback = ft.Text(size=12, color=ft.Colors.RED_300)
        self.script_list = ft.ListView(expand=True, spacing=6)
        self.editor_host = ft.Container(expand=True)
        self.control = ft.Row(expand=True, spacing=0, controls=[
            ft.Container(width=260, bgcolor="#10182c", padding=ft.Padding.all(12),
                content=ft.Column(expand=True, spacing=12, controls=[
                    ft.Text("Scripts", size=20, weight=ft.FontWeight.BOLD),
                    self.name_field, self.create_button, self.feedback,
                    ft.Divider(color="#263250"), self.script_list,
                ])),
            ft.Container(width=1, bgcolor="#263250"),
            ft.Container(expand=True, padding=ft.Padding.all(20), content=self.editor_host),
        ])
        self._show_empty()

    def _show_empty(self):
        self.editor_host.content = ft.Container(expand=True, alignment=ft.Alignment(0, 0),
            content=ft.Text("Tạo hoặc chọn script ở thanh bên trái để bắt đầu.",
                            color=ft.Colors.BLUE_GREY_300))

    def _write_manifest(self):
        self.data_root.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest.with_suffix(".tmp")
        temporary.write_text(json.dumps({"scripts": self.entries, "active_id": self.active_id},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.manifest)

    def _read_manifest(self):
        if not self.manifest.exists():
            return [{"id": "default", "name": "Script", "file": "script.json"}], "default"
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        entries = payload.get("scripts") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise ValueError("Danh sách script đã lưu không hợp lệ.")
        valid = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            script_id, name, filename = entry.get("id"), entry.get("name"), entry.get("file")
            if not all(isinstance(value, str) and value.strip() for value in (script_id, name, filename)):
                continue
            if Path(filename).name != filename or not filename.lower().endswith(".json") or script_id in seen:
                continue
            valid.append({"id": script_id, "name": name.strip(), "file": filename})
            seen.add(script_id)
        active_id = payload.get("active_id")
        if active_id not in seen:
            active_id = valid[0]["id"] if valid else None
        return valid, active_id

    def _target_for(self, entry):
        if entry["file"] == "script.json":
            return self.data_root / "script.json"
        return self.data_root / "scripts" / entry["file"]

    def _editor_for(self, entry):
        editor = self.editors.get(entry["id"])
        if editor is None:
            editor = ScriptView(self.page, self.picker, self._target_for(entry), self.database,
                self.library_root, self.on_download_complete, self.open_preview, self.video_preferences)
            self.editors[entry["id"]] = editor
        editor.script_name = entry["name"]
        return editor

    def render_sidebar(self):
        self.script_list.controls.clear()
        if not self.entries:
            self.script_list.controls.append(ft.Text("Chưa có script.", color=ft.Colors.BLUE_GREY_300))
        for entry in self.entries:
            async def choose(_, script_id=entry["id"]):
                await self.select_script(script_id)
            def rename(_, script_id=entry["id"], current_name=entry["name"]):
                self.show_rename_dialog(script_id, current_name)
            def delete(_, script_id=entry["id"], current_name=entry["name"]):
                self.show_delete_dialog(script_id, current_name)
            selected = entry["id"] == self.active_id
            self.script_list.controls.append(ft.Container(
                bgcolor="#20335a" if selected else "#17223a", border_radius=6,
                padding=ft.Padding.symmetric(horizontal=6, vertical=5),
                content=ft.Row([
                    ft.Icon(ft.Icons.DESCRIPTION_OUTLINED, size=18),
                    ft.Container(expand=True, on_click=choose, padding=4,
                        content=ft.Text(entry["name"], max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)),
                    ft.IconButton(icon=ft.Icons.EDIT_OUTLINED, icon_size=17,
                        tooltip="Đổi tên script", on_click=rename),
                    ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, icon_size=17,
                        tooltip="Xóa script", on_click=delete),
                ], spacing=2)))

    async def rename_script(self, script_id: str, name: str) -> bool:
        name = name.strip()
        entry = next((item for item in self.entries if item["id"] == script_id), None)
        if entry is None:
            return False
        if not name:
            self.feedback.value = "Tên script không được để trống."
            self.page.update()
            return False
        if any(item["id"] != script_id and item["name"].casefold() == name.casefold()
               for item in self.entries):
            self.feedback.value = "Tên script này đã tồn tại."
            self.page.update()
            return False
        previous = entry["name"]
        entry["name"] = name
        try:
            await asyncio.to_thread(self._write_manifest)
        except OSError as exc:
            entry["name"] = previous
            self.feedback.value = f"Không thể đổi tên script: {exc}"
            self.page.update()
            return False
        self.feedback.value = ""
        self.render_sidebar()
        self.page.update()
        return True

    def show_rename_dialog(self, script_id: str, current_name: str):
        name = ft.TextField(label="Tên script", value=current_name, max_length=120,
                            autofocus=True, on_submit=None)
        dialog = ft.AlertDialog(modal=True, title=ft.Text("Đổi tên script"), content=name)

        async def save(_):
            if await self.rename_script(script_id, name.value or ""):
                self.page.pop_dialog()

        dialog.actions = [ft.TextButton("Hủy", on_click=lambda _: self.page.pop_dialog()),
                          ft.FilledButton("Lưu", on_click=save)]
        name.on_submit = save
        self.page.show_dialog(dialog)

    def show_delete_dialog(self, script_id: str, name: str):
        async def confirm(_):
            await self.delete_script(script_id)
            self.page.pop_dialog()

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Xóa script?"),
            content=ft.Text(f'Xóa script "{name}" và tracker đã lưu? Video trong Library và file footage sẽ được giữ.'),
            actions=[ft.TextButton("Hủy", on_click=lambda _: self.page.pop_dialog()),
                     ft.FilledButton("Xóa script", icon=ft.Icons.DELETE_OUTLINE, on_click=confirm)],
        ))

    async def delete_script(self, script_id: str) -> bool:
        entry = next((item for item in self.entries if item["id"] == script_id), None)
        if entry is None:
            return False
        previous_entries, previous_active = list(self.entries), self.active_id
        if script_id == self.active_id:
            editor = self.editors.get(script_id)
            if editor:
                await editor.stop_playback()
        self.entries = [item for item in self.entries if item["id"] != script_id]
        if self.active_id == script_id:
            self.active_id = self.entries[0]["id"] if self.entries else None
        try:
            await asyncio.to_thread(self._write_manifest)
        except OSError as exc:
            self.entries, self.active_id = previous_entries, previous_active
            self.feedback.value = f"Không thể xóa script: {exc}"
            self.page.update()
            return False
        editor = self.editors.pop(script_id, None)
        target = editor.target if editor else self._target_for(entry)
        try:
            await asyncio.to_thread(target.unlink, missing_ok=True)
        except OSError as exc:
            self.feedback.value = f"Đã gỡ script khỏi danh sách nhưng chưa xóa được tracker: {exc}"
        else:
            self.feedback.value = ""
        self.render_sidebar()
        if self.active_id:
            await self.select_script(self.active_id, persist=False)
        else:
            self._show_empty()
        self.page.update()
        return True

    async def load(self):
        if self.loaded:
            return
        self.loaded = True
        try:
            self.entries, self.active_id = await asyncio.to_thread(self._read_manifest)
            await asyncio.to_thread(self._write_manifest)
            self.render_sidebar()
            if self.active_id:
                await self.select_script(self.active_id, persist=False)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.feedback.value = f"Không thể nạp danh sách script: {exc}"
        self.page.update()

    async def create_from_field(self, _):
        await self.add_script(self.name_field.value)

    async def add_script(self, name: str | None = None):
        name = (name if name is not None else self.name_field.value or "").strip()
        if not name:
            self.feedback.value = "Hãy nhập tên script."
            self.page.update()
            return False
        if any(entry["name"].casefold() == name.casefold() for entry in self.entries):
            self.feedback.value = "Tên script này đã tồn tại."
            self.page.update()
            return False
        script_id = uuid.uuid4().hex
        entry = {"id": script_id, "name": name, "file": f"{script_id}.json"}
        self.entries.append(entry)
        self.active_id = script_id
        try:
            await asyncio.to_thread(self._write_manifest)
        except OSError as exc:
            self.entries.pop()
            self.active_id = self.entries[-1]["id"] if self.entries else None
            self.feedback.value = f"Không thể tạo script: {exc}"
            self.page.update()
            return False
        self.name_field.value = ""
        self.feedback.value = ""
        self.render_sidebar()
        editor = self._editor_for(entry)
        self.editor_host.content = editor.control
        self.page.update()
        return True

    async def select_script(self, script_id: str, persist: bool = True):
        entry = next((item for item in self.entries if item["id"] == script_id), None)
        if entry is None:
            return
        if self.active_id in self.editors:
            await self.editors[self.active_id].stop_playback()
        self.active_id = script_id
        editor = self._editor_for(entry)
        self.editor_host.content = editor.control
        if persist:
            await asyncio.to_thread(self._write_manifest)
        self.render_sidebar()
        await editor.load()
        self.page.update()

    async def stop_playback(self):
        if self.active_id in self.editors:
            await self.editors[self.active_id].stop_playback()

    def cancel_active(self):
        if self.active_id in self.editors:
            self.editors[self.active_id].cancel.set()
