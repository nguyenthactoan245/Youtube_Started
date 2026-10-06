from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from threading import Event
from weakref import WeakKeyDictionary

import flet as ft
import flet_video as ftv

from app_config import APP_VERSION, DATA_ROOT, RESOURCE_ROOT, configured_pexels_api_key
from services.api_keys import get_api_key_pool
from api_key_settings import ApiKeySettings
from models import DownloadEvent, Video
from script_view import ScriptWorkspace
from video_settings import build_video_settings
from services.assets import delete_videos, export_named_videos, export_videos, export_videos_zip
from services.catalog import select_unique
from services.database import VideoDatabase
from services.downloader import download_many, footage_filename, safe_folder_name
from services.pixabay import PixabayError, PixabayAccessPause
from services.pexels import PexelsError, select_unique as select_pexels_unique
from services.pexels_browser import (PexelsBrowserBlocked, PexelsBrowserSearchError,
                                     search_videos as search_pexels_videos_direct)
from services.pexels_link import PexelsLinkError, pexels_video_id, resolve_pexels_video
from services.pexels_verified import (download_matching_pexels_videos, orientation_matches,
                                      probe_video_dimensions)
from services.thumbnails import is_jpeg, thumbnail_bytes
from services.video_trim import trim_random_video

ROOT = DATA_ROOT
LIBRARY_ROOT = ROOT / "library"
ASSETS_ROOT = RESOURCE_ROOT / "assets"
WINDOW_ICON = ASSETS_ROOT / "stock-check.ico"
SIDEBAR_LOGO = "5166961.png"
PREVIEW_WIDTH = 1280
PREVIEW_HEIGHT = 720
LIBRARY_THUMBNAIL_CACHE: WeakKeyDictionary[VideoDatabase, dict[int, dict]] = WeakKeyDictionary()


def format_progress_timing(done: int, total: int, started_at: float) -> str:
    elapsed = max(0.0, time.monotonic() - started_at)
    hours, remainder = divmod(int(elapsed), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def build_preview_title(text: str) -> ft.Container:
    # Ellipsis alone does not constrain a dialog's intrinsic width.
    return ft.Container(
        width=PREVIEW_WIDTH,
        content=ft.Text(text, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, tooltip=text),
    )


def build_preview_frame(player: ftv.Video, poster: ft.Container) -> ft.Container:
    """Keep native media dimensions from changing the preview's layout."""
    poster.content.fit = ft.StackFit.EXPAND
    return ft.Container(
        width=PREVIEW_WIDTH,
        height=PREVIEW_HEIGHT,
        bgcolor=ft.Colors.BLACK,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        border_radius=10,
        content=ft.Stack([player, poster], fit=ft.StackFit.EXPAND),
    )


def load_env() -> None:
    """Minimal .env reader: avoids adding a dependency solely for one secret."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            # Preserve a real environment override, but do not let an empty value disable .env.
            if not os.environ.get(key):
                os.environ[key] = value.strip().strip('"').strip("'")


def format_size(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB" if size else "—"


def format_duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def build_video_thumbnail(video: Video, *, fit: ft.BoxFit = ft.BoxFit.COVER) -> ft.Container:
    """Return an image or a visible placeholder when a source has no poster URL."""
    content: ft.Control
    if video.thumbnail:
        content = ft.Image(src=video.thumbnail, fit=fit, expand=True)
    else:
        content = ft.Icon(ft.Icons.VIDEO_FILE_OUTLINED, size=42, color=ft.Colors.BLUE_GREY_300)
    return ft.Container(
        expand=True,
        bgcolor="#0b1020",
        alignment=ft.Alignment(0, 0),
        content=content,
    )


def list_library_videos(root: Path = LIBRARY_ROOT) -> list[Path]:
    """Return completed library videos, newest first."""
    if not root.exists():
        return []
    return sorted((path for path in root.rglob("*.mp4") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)


def library_poster(video_path: Path) -> bytes | None:
    """Load a full poster for preview, falling back to the cached grid thumbnail."""
    for candidate in (video_path.with_suffix(".jpg"), video_path.with_suffix(".thumb.jpg")):
        if is_jpeg(candidate):
            try:
                return candidate.read_bytes()
            except OSError:
                pass
    return None


def load_library_entries(database: VideoDatabase) -> list[dict]:
    """Read Library records and reuse poster bytes for unchanged local videos."""
    records = database.library()
    cached_records = LIBRARY_THUMBNAIL_CACHE.get(database, {})
    for record in records:
        file = Path(record["file_path"])
        try:
            file_stat = file.stat()
            record["exists_on_disk"] = file.is_file()
            record["size_on_disk"] = file_stat.st_size if record["exists_on_disk"] else 0
            record["file_mtime_ns"] = file_stat.st_mtime_ns if record["exists_on_disk"] else 0
        except OSError:
            record["size_on_disk"] = 0
            record["exists_on_disk"] = False
            record["file_mtime_ns"] = 0
        previous = cached_records.get(record["id"])
        unchanged = (
            record["exists_on_disk"] and previous
            and previous.get("exists_on_disk")
            and previous.get("file_path") == record["file_path"]
            and previous.get("size_on_disk") == record["size_on_disk"]
            and previous.get("file_mtime_ns") == record["file_mtime_ns"]
        )
        record["poster_bytes"] = (
            previous.get("poster_bytes") if unchanged
            else thumbnail_bytes(file) if record["exists_on_disk"] else None
        )
    LIBRARY_THUMBNAIL_CACHE[database] = {record["id"]: record for record in records}
    return records


def load_dashboard_stats(database: VideoDatabase, root: Path = LIBRARY_ROOT) -> tuple[int, int, int]:
    """Return the number and size of real MP4s in Library plus the project count."""
    videos = list_library_videos(root)
    total_size = 0
    for video in videos:
        try:
            total_size += video.stat().st_size
        except OSError:
            continue
    return len(videos), total_size, len(database.list_projects())


def load_project_entries(database: VideoDatabase, project_id: int) -> list[dict]:
    """Prepare project thumbnails off the UI thread, once per project refresh."""
    records = database.list_project_videos(project_id)
    media_cache: dict[str, tuple[int, bool, bytes | None]] = {}
    for record in records:
        file = Path(record["file_path"])
        if record["file_path"] not in media_cache:
            try:
                size = file.stat().st_size
                exists = file.is_file()
            except OSError:
                size, exists = 0, False
            media_cache[record["file_path"]] = (
                size, exists, thumbnail_bytes(file) if exists else None
            )
        record["size_on_disk"], record["exists_on_disk"], record["poster_bytes"] = (
            media_cache[record["file_path"]]
        )
    return records


def playable_resource(video: Video, root: Path = LIBRARY_ROOT) -> str:
    """Prefer a completed local MP4; fall back to its provider stream while downloading."""
    filename = footage_filename(video)
    if root.exists():
        local_file = next(root.rglob(filename), None)
        if local_file and local_file.stat().st_size > 0:
            return str(local_file.resolve())
    return video.url


def build_preview_dialog(
    video: Video,
    on_close: ft.ControlEventHandler,
) -> ft.AlertDialog:
    """Build a preview with a mounted player behind the poster overlay."""
    resource = playable_resource(video)
    playback = {"loaded": False, "requested": False}

    async def loaded(_: ft.ControlEvent) -> None:
        playback["loaded"] = True
        if playback["requested"]:
            await player.play()

    player = ftv.Video(
        playlist=[ftv.VideoMedia(resource=resource, http_headers={"User-Agent": "StockDownloader/1.0"} if resource.startswith("http") else None)],
        playlist_mode=ftv.PlaylistMode.SINGLE,
        autoplay=False,
        volume=100,
        fit=ft.BoxFit.CONTAIN,
        fill_color=ft.Colors.BLACK,
        expand=True,
        on_load=loaded,
    )
    poster = ft.Container(
        expand=True,
        content=ft.Stack([
            build_video_thumbnail(video),
            ft.Container(expand=True, alignment=ft.Alignment(0, 0)),
        ]),
    )

    async def play(_: ft.ControlEvent) -> None:
        playback["requested"] = True
        poster.visible = False
        poster.update()
        if playback["loaded"]:
            await player.play()

    poster.content.controls[1].content = ft.IconButton(
        icon=ft.Icons.PLAY_ARROW,
        icon_size=42,
        icon_color=ft.Colors.WHITE,
        bgcolor="#B0000000",
        tooltip="Chạy video",
        on_click=play,
    )
    frame = build_preview_frame(player, poster)
    dialog = ft.AlertDialog(
        modal=False,
        inset_padding=12,
        content_padding=12,
        title_padding=ft.Padding.only(left=12, right=12, top=20),
        title=build_preview_title(video.tags),
        content=ft.Container(
            width=PREVIEW_WIDTH,
            content=ft.Column(
                tight=True,
                controls=[
                    frame,
                    ft.Text(f"{video.width}×{video.height} · {format_duration(video.duration)} · {format_size(video.size)}", height=20, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, color=ft.Colors.BLUE_GREY_300),
                    ft.Text(f"Video by {video.author} on {video.source.title()}", height=20, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, size=12, color=ft.Colors.BLUE_GREY_300),
                ],
            ),
        ),
        actions=[
            ft.FilledButton("Chạy video", icon=ft.Icons.PLAY_ARROW, on_click=play),
            ft.TextButton("Đóng (Esc)", on_click=on_close),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )
    dialog.data = {"player": player, "poster": poster, "playback": playback, "frame": frame}
    return dialog


def build_library_preview_dialog(
    video_path: Path,
    poster_image: bytes | None,
    on_close: ft.ControlEventHandler,
) -> ft.AlertDialog:
    playback = {"loaded": False, "requested": False}

    async def loaded(_: ft.ControlEvent) -> None:
        playback["loaded"] = True
        if playback["requested"]:
            await player.play()

    player = ftv.Video(
        playlist=[ftv.VideoMedia(resource=str(video_path.resolve()))],
        playlist_mode=ftv.PlaylistMode.SINGLE,
        autoplay=False,
        volume=100,
        fit=ft.BoxFit.CONTAIN,
        fill_color=ft.Colors.BLACK,
        expand=True,
        on_load=loaded,
    )
    preview = ft.Image(src=poster_image, fit=ft.BoxFit.COVER, expand=True) if poster_image else ft.Container(bgcolor="#1d3158", alignment=ft.Alignment(0, 0), content=ft.Icon(ft.Icons.VIDEO_FILE_OUTLINED, size=64, color=ft.Colors.CYAN_200))
    poster = ft.Container(expand=True, content=ft.Stack([preview, ft.Container(expand=True, alignment=ft.Alignment(0, 0))]))

    async def play(_: ft.ControlEvent) -> None:
        playback["requested"] = True
        poster.visible = False
        poster.update()
        if playback["loaded"]:
            await player.play()

    poster.content.controls[1].content = ft.IconButton(icon=ft.Icons.PLAY_ARROW, icon_size=42, icon_color=ft.Colors.WHITE, bgcolor="#B0000000", tooltip="Chạy video", on_click=play)
    frame = build_preview_frame(player, poster)
    dialog = ft.AlertDialog(
        modal=False,
        inset_padding=12,
        content_padding=12,
        title_padding=ft.Padding.only(left=12, right=12, top=20),
        title=build_preview_title(video_path.name),
        content=ft.Container(width=PREVIEW_WIDTH, content=ft.Column(tight=True, controls=[
            frame,
            ft.Text(video_path.name, height=20, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, color=ft.Colors.BLUE_GREY_300),
            ft.Text("Library", height=20, max_lines=1, size=12, color=ft.Colors.BLUE_GREY_300),
        ])),
        actions=[
            ft.FilledButton("Chạy video", icon=ft.Icons.PLAY_ARROW, on_click=play),
            ft.TextButton("Đóng (Esc)", on_click=on_close),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )
    dialog.data = {"player": player, "poster": poster, "playback": playback, "frame": frame}
    return dialog


async def main(page: ft.Page) -> None:
    load_env()
    database = await asyncio.to_thread(VideoDatabase)
    await asyncio.to_thread(database.import_existing, LIBRARY_ROOT, ROOT / "downloads")
    page.title = f"Stock Downloader v{APP_VERSION}"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0
    page.bgcolor = "#0b1020"
    page.window.min_width = 720
    page.window.min_height = 560
    page.window.maximized = True
    page.window.icon = str(WINDOW_ICON)
    file_picker = ft.FilePicker()
    if hasattr(page, "services"):
        page.services.append(file_picker)

    keyword = ft.TextField(label="Keyword", hint_text="Ví dụ: Moscow, Scotland, Alaska", expand=True, autofocus=True)
    amount = ft.TextField(label="Số video", value="10", width=110, input_filter=ft.InputFilter(allow=True, regex_string=r"[0-9]"))
    start = ft.FilledButton("Tìm & tải", icon=ft.Icons.DOWNLOAD)
    cancel_button = ft.OutlinedButton("Hủy", icon=ft.Icons.CANCEL, disabled=True)
    resume_search = ft.OutlinedButton("Tiếp tục tìm kiếm", visible=False,
                                     on_click=lambda _: get_api_key_pool().resume())
    status = ft.Text("Nhập keyword và số lượng video cần tải.", color=ft.Colors.BLUE_200)
    overall = ft.ProgressBar(value=0, visible=False, expand=True)
    overall_count = ft.Text("", size=12, color=ft.Colors.BLUE_GREY_300, visible=False)
    download_progress = ft.Row([overall, overall_count], spacing=12,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER)
    grid = ft.GridView(expand=True, max_extent=350, child_aspect_ratio=0.95, spacing=14, run_spacing=14)
    download_list = ft.ListView(expand=True, spacing=8)
    download_results = ft.Container(expand=True, content=grid)
    download_mode = "grid"
    cancel_event: Event | None = None
    preview_dialog: ft.AlertDialog | None = None
    action_busy = False
    cards: dict[int, list[tuple[ft.ProgressBar, ft.Text]]] = {}
    thumbnail_slots: dict[int, list[ft.Container]] = {}
    download_videos: dict[int, Video] = {}
    selected_download: set[int] = set()
    download_select_controls: dict[int, list[tuple[ft.IconButton, ft.Container]]] = {}
    download_selected_count = ft.Text("0 đã chọn", size=12, color=ft.Colors.BLUE_GREY_300)
    video_preferences = {"quality": "2K", "orientation": "landscape", "source": "pexels",
                         "method": "direct", "workers": "5"}
    download_video_settings = build_video_settings(
        video_preferences, include_source=True, include_method=True, include_workers=True)
    pexels_url = ft.TextField(
        label="Link video Pexels",
        hint_text="https://www.pexels.com/video/ten-video-123456/",
        expand=True,
    )
    pexels_link_button = ft.OutlinedButton("Tải link Pexels", icon=ft.Icons.LINK, disabled=True)
    keyword_download_row = ft.Row(
        [keyword, amount, start, cancel_button, resume_search],
        vertical_alignment=ft.CrossAxisAlignment.END,
    )
    pexels_direct_row = ft.Row(
        [pexels_url, pexels_link_button],
        vertical_alignment=ft.CrossAxisAlignment.END,
    )

    def set_download_buttons_idle(*, busy: bool = False) -> None:
        source = video_preferences.get("source", "pexels")
        start.disabled = busy
        pexels_link_button.disabled = busy or source != "pexels"
        pexels_link_button.tooltip = (
            "Tải link Pexels" if source == "pexels"
            else "Crawl trực tiếp hiện chỉ hỗ trợ Pexels"
        )

    def update_download_method(_: ft.ControlEvent | None = None) -> None:
        if _ is not None:
            video_preferences["method"] = _.control.value or "direct"
        set_download_buttons_idle(busy=overall.visible)
        if _ is not None:
            page.update()

    method_dropdown = getattr(download_video_settings, "method_dropdown", None)
    if method_dropdown:
        method_dropdown.on_change = update_download_method
    source_dropdown = getattr(download_video_settings, "source_dropdown", None)
    if source_dropdown:
        def update_download_source(event: ft.ControlEvent) -> None:
            video_preferences["source"] = event.control.value or "pexels"
            set_download_buttons_idle(busy=overall.visible)
            page.update()

        source_dropdown.on_change = update_download_source
    set_download_buttons_idle()
    download_select_all = ft.IconButton(icon=ft.Icons.SELECT_ALL, tooltip="Chọn tất cả", disabled=True)
    download_delete = ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip="Xóa video đã chọn", disabled=True)
    download_save = ft.IconButton(icon=ft.Icons.DOWNLOAD, tooltip="Xuất video ra thư mục khác", disabled=True)
    download_move = ft.IconButton(icon=ft.Icons.DRIVE_FILE_MOVE_OUTLINE,
                                  tooltip="Gắn vào project/chapter", disabled=True)

    def update_download_selection() -> None:
        selected_download.intersection_update(download_videos)
        for video_id, controls in download_select_controls.items():
            chosen = video_id in selected_download
            for button, container in controls:
                button.icon = ft.Icons.CHECK_BOX if chosen else ft.Icons.CHECK_BOX_OUTLINE_BLANK
                button.icon_color = ft.Colors.CYAN_200 if chosen else ft.Colors.WHITE
                container.bgcolor = "#20335a" if chosen else "#131c33"
        count = len(selected_download)
        download_selected_count.value = f"{count} đã chọn"
        download_select_all.disabled = not download_videos
        download_select_all.icon = (ft.Icons.CHECK_BOX if count == len(download_videos) and count
                                    else ft.Icons.SELECT_ALL)
        for button in (download_delete, download_save, download_move):
            button.disabled = count == 0 or start.disabled or action_busy
        page.update()

    def toggle_download_selection(video_id: int) -> None:
        if video_id in selected_download:
            selected_download.remove(video_id)
        else:
            selected_download.add(video_id)
        update_download_selection()

    def toggle_all_download(_: ft.ControlEvent) -> None:
        selected_download.clear() if len(selected_download) == len(download_videos) else selected_download.update(download_videos)
        update_download_selection()

    download_select_all.on_click = toggle_all_download

    async def close_preview(_: ft.ControlEvent | None = None) -> None:
        nonlocal preview_dialog
        if preview_dialog and isinstance(preview_dialog.data, dict):
            player = preview_dialog.data.get("player")
            if player:
                try:
                    await player.stop()
                except Exception:
                    pass
        if preview_dialog and preview_dialog.open:
            page.pop_dialog()
        preview_dialog = None

    def show_preview(video: Video) -> None:
        nonlocal preview_dialog
        preview_dialog = build_preview_dialog(video, close_preview)
        page.show_dialog(preview_dialog)

    def show_library_preview(video_path: Path) -> None:
        nonlocal preview_dialog
        preview_dialog = build_library_preview_dialog(video_path, library_poster(video_path), close_preview)
        page.show_dialog(preview_dialog)

    async def on_keyboard(event: ft.KeyboardEvent) -> None:
        if event.key == "Escape":
            await close_preview()

    page.on_keyboard_event = on_keyboard

    def card(video: Video) -> ft.Control:
        progress = ft.ProgressBar(value=0, bar_height=5, color=ft.Colors.CYAN_300)
        state = ft.Text("Đang chờ", size=12, color=ft.Colors.BLUE_200)
        selector = ft.IconButton(icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK, icon_color=ft.Colors.WHITE,
                                 tooltip="Chọn video", on_click=lambda _: toggle_download_selection(video.id))
        cards.setdefault(video.id, []).append((progress, state))
        thumbnail = build_video_thumbnail(video)
        thumbnail_slots.setdefault(video.id, []).append(thumbnail)
        container = ft.Container(
            data=video.id,
            border_radius=12,
            bgcolor="#131c33",
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            on_click=lambda _: show_preview(video),
            content=ft.Column(
                spacing=8,
                controls=[
                    ft.Container(
                        aspect_ratio=16 / 9,
                        content=ft.Stack([
                            thumbnail,
                            ft.Container(
                                right=8, bottom=8, bgcolor="#99000000", border_radius=6,
                                padding=ft.Padding.symmetric(horizontal=7, vertical=3),
                                content=ft.Text(format_duration(video.duration), size=11),
                            ),
                            ft.Container(left=4, top=4, bgcolor="#99000000", border_radius=8,
                                         content=selector),
                        ]),
                    ),
                    ft.Container(
                        padding=ft.Padding.only(left=10, right=10, bottom=10),
                        content=ft.Column(spacing=4, controls=[
                            ft.Text(video.tags, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                                    tooltip=video.tags, weight=ft.FontWeight.W_600),
                            ft.Text(f"{video.width}×{video.height} · {format_size(video.size)}", size=11, color=ft.Colors.BLUE_GREY_300),
                            progress,
                            ft.Row([state, ft.TextButton(f"{video.source.title()} · {video.author}",
                                url=video.page_url, style=ft.ButtonStyle(padding=0))],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                        ]),
                    ),
                ],
            ),
        )
        download_select_controls.setdefault(video.id, []).append((selector, container))
        return container

    def list_item(video: Video) -> ft.Control:
        progress = ft.ProgressBar(value=0, bar_height=5, color=ft.Colors.CYAN_300)
        state = ft.Text("Đang chờ", size=12, color=ft.Colors.BLUE_200)
        selector = ft.IconButton(icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK, icon_color=ft.Colors.WHITE,
                                 tooltip="Chọn video", on_click=lambda _: toggle_download_selection(video.id))
        cards.setdefault(video.id, []).append((progress, state))
        thumbnail = build_video_thumbnail(video)
        thumbnail_slots.setdefault(video.id, []).append(thumbnail)
        container = ft.Container(
            data=video.id,
            padding=ft.Padding.all(10),
            border_radius=10,
            bgcolor="#131c33",
            on_click=lambda _: show_preview(video),
            content=ft.Row(controls=[
                selector,
                ft.Container(width=160, aspect_ratio=16 / 9, clip_behavior=ft.ClipBehavior.ANTI_ALIAS, border_radius=7, content=thumbnail),
                ft.Column(expand=True, spacing=5, controls=[
                    ft.Text(video.tags, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, weight=ft.FontWeight.W_600),
                    ft.Text(f"{video.width}×{video.height} · {format_duration(video.duration)} · {format_size(video.size)}", size=12, color=ft.Colors.BLUE_GREY_300),
                    progress,
                    state,
                ]),
            ]),
        )
        download_select_controls.setdefault(video.id, []).append((selector, container))
        return container

    async def begin_download(_: ft.ControlEvent) -> None:
        nonlocal cancel_event, library_loaded
        if start.disabled:
            return
        selected_method = getattr(download_video_settings, "method_dropdown", None)
        if selected_method and selected_method.value == "direct":
            await begin_direct_pexels_search()
            return
        if selected_method and selected_method.value != "api":
            status.value = 'Chọn "Crawl by API" để tìm video bằng keyword.'
            status.color = ft.Colors.AMBER_300
            page.update()
            return
        video_preferences["method"] = "api"
        term = keyword.value.strip()
        terms = [part.strip() for part in term.split(",") if part.strip()]
        selected_source = getattr(download_video_settings, "source_dropdown", None)
        source_name = ((selected_source.value if selected_source else None)
                       or video_preferences.get("source", "pixabay")).lower()
        video_preferences["source"] = source_name
        try:
            count = int(amount.value)
            if not terms or not 1 <= count <= 200:
                raise ValueError
            per_keyword_count = count
            count *= len(terms)
        except ValueError:
            status.value = "Keyword không được trống; số lượng phải từ 1 đến 200."
            status.color = ft.Colors.RED_300
            page.update()
            return

        pexels_key = configured_pexels_api_key() if source_name == "pexels" else ""
        if source_name == "pexels" and not pexels_key:
            status.value = "Hãy nhập Pexels API key trong Setup trước khi tải."
            status.color = ft.Colors.RED_300
            page.update()
            return

        start.disabled = True
        pexels_link_button.disabled = True
        cancel_button.disabled = False
        cancel_event = Event()
        batch = database.batch()
        owner = batch.__enter__()
        status.value = f"Đang tìm video trên {source_name.title()}..."
        status.color = ft.Colors.BLUE_200
        overall.visible = True
        overall.value = None
        grid.controls.clear()
        download_list.controls.clear()
        cards.clear()
        thumbnail_slots.clear()
        download_videos.clear()
        selected_download.clear()
        download_select_controls.clear()
        update_download_selection()
        page.update()
        progress_started_at = time.monotonic()
        overall_count.visible = True
        overall_count.value = format_progress_timing(0, count, progress_started_at)
        try:
            api_pool = get_api_key_pool() if source_name == "pixabay" else None
            if source_name == "pexels":
                search_worker = asyncio.create_task(asyncio.to_thread(
                    select_pexels_unique, pexels_key, term, per_keyword_count, LIBRARY_ROOT, database, owner, cancel_event,
                    quality=video_preferences["quality"], orientation=video_preferences["orientation"]))
            else:
                search_worker = asyncio.create_task(asyncio.to_thread(
                    select_unique, api_pool, term, per_keyword_count, LIBRARY_ROOT, database, owner, cancel_event,
                    quality=video_preferences["quality"],
                    orientation=video_preferences["orientation"]))
            while not search_worker.done():
                await asyncio.wait({search_worker}, timeout=0.25)
                if source_name == "pexels":
                    status.value = f"Đang tìm video trên Pexels · {term}..."
                    resume_search.visible = False
                else:
                    status.value = "Đang tìm video · " + " · ".join(api_pool.statuses())
                    resume_search.visible = bool(api_pool.pause_reason) and not api_pool.auto_retrying
                page.update()
            videos = await search_worker
            resume_search.visible = False
        except PixabayAccessPause:
            status.value = "Pixabay yêu cầu xác minh truy cập (Cloudflare). Kiểm tra trên trình duyệt rồi bấm Tiếp tục tìm kiếm."
            status.color = ft.Colors.AMBER_300
            while not cancel_event.is_set():
                current_pool = get_api_key_pool()
                resume_search.visible = bool(current_pool.pause_reason) and not current_pool.auto_retrying
                status.value = "Đang tìm video · " + " · ".join(get_api_key_pool().statuses())
                page.update()
                if search_worker.done():
                    break
                await asyncio.wait({search_worker}, timeout=0.25)
            try:
                videos = await search_worker
                resume_search.visible = False
            except (PixabayError, PexelsError, OSError, InterruptedError) as exc:
                resume_search.visible = False
                batch.__exit__(None, None, None)
                status.value = "Đã hủy." if cancel_event.is_set() else str(exc)
                status.color = ft.Colors.AMBER_300 if cancel_event.is_set() else ft.Colors.RED_300
                set_download_buttons_idle()
                cancel_button.disabled = True
                overall.visible = False
                page.update()
                return
        except (PixabayError, PexelsError, OSError) as exc:
            resume_search.visible = False
            batch.__exit__(None, None, None)
            status.value = str(exc)
            status.color = ft.Colors.RED_300
            set_download_buttons_idle()
            cancel_button.disabled = True
            overall.visible = False
            page.update()
            return
        if not videos:
            batch.__exit__(None, None, None)
            status.value = ("Đã hủy." if cancel_event.is_set() else
                            f"Không còn video mới phù hợp trên {source_name.title()} cho keyword này.")
            status.color = ft.Colors.AMBER_300
            set_download_buttons_idle()
            cancel_button.disabled = True
            overall.visible = False
            page.update()
            return

        download_videos.update((video.id, video) for video in videos)
        grid.controls.extend(card(video) for video in videos)
        download_list.controls.extend(list_item(video) for video in videos)
        update_download_selection()
        overall.value = 0
        status.value = f"Đã chọn {len(videos)}/{count} video chưa có. Đang tải vào library/{term}..."
        page.update()
        queue: asyncio.Queue[DownloadEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def report(event: DownloadEvent) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        worker_count = max(1, min(20, int(video_preferences.get("workers", "5"))))
        worker = asyncio.create_task(asyncio.to_thread(download_many, videos, LIBRARY_ROOT, term,
                                                       cancel_event, report, database, owner, "", worker_count))
        completed = 0
        failed = 0
        while not worker.done() or not queue.empty():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.15)
            except TimeoutError:
                continue
            for progress, label in cards[event.video_id]:
                progress.value = event.progress
                label.value = event.message
                label.color = {"done": ft.Colors.GREEN_300, "error": ft.Colors.RED_300, "cancelled": ft.Colors.AMBER_300}.get(event.state, ft.Colors.BLUE_200)
            if event.state in {"done", "error", "cancelled"}:
                completed += 1
                failed += event.state == "error"
                overall.value = completed / len(videos)
                overall_count.value = format_progress_timing(completed, len(videos), progress_started_at)
                status.value = f"Đang tải {completed}/{len(videos)}"
            page.update()
        try:
            destination = await worker
            library_loaded = False
            if current_view == "library":
                await refresh_library(force=True)
            if cancel_event.is_set():
                status.value = "Đã hủy tải. Video đã hoàn tất vẫn được giữ."
            elif failed:
                status.value = f"Đã tải {len(videos) - failed}/{len(videos)} video; {failed} video lỗi, có thể thử lại."
            else:
                status.value = f"Hoàn tất {len(videos)}/{count} video mới. Lưu tại: {destination}"
            status.color = ft.Colors.AMBER_300 if cancel_event.is_set() or failed else ft.Colors.GREEN_300
        except Exception as exc:
            status.value = f"Lỗi không mong đợi: {exc}"
            status.color = ft.Colors.RED_300
        finally:
            batch.__exit__(None, None, None)
            set_download_buttons_idle()
            cancel_button.disabled = True
            overall.visible = False
            overall_count.visible = False
            update_download_selection()
            page.update()

    async def download_pexels_link(_: ft.ControlEvent) -> None:
        nonlocal cancel_event, library_loaded
        if pexels_link_button.disabled:
            return
        selected_method = getattr(download_video_settings, "method_dropdown", None)
        if selected_method and selected_method.value != "direct":
            status.value = 'Chọn "Crawl trực tiếp" để tải bằng link Pexels.'
            status.color = ft.Colors.AMBER_300
            page.update()
            return
        video_preferences["method"] = "direct"
        selected_source = getattr(download_video_settings, "source_dropdown", None)
        if selected_source and selected_source.value != "pexels":
            status.value = "Crawl trực tiếp hiện chỉ hỗ trợ nguồn Pexels."
            status.color = ft.Colors.AMBER_300
            page.update()
            return
        try:
            pexels_video_id(pexels_url.value or "")
        except PexelsLinkError as exc:
            status.value = str(exc)
            status.color = ft.Colors.RED_300
            page.update()
            return

        start.disabled = True
        pexels_link_button.disabled = True
        cancel_button.disabled = False
        cancel_event = Event()
        batch = database.batch()
        owner = batch.__enter__()
        overall.visible = True
        overall.value = None
        status.value = "Đang lấy liên kết tải chính thức từ Pexels..."
        status.color = ft.Colors.BLUE_200
        grid.controls.clear()
        download_list.controls.clear()
        cards.clear()
        thumbnail_slots.clear()
        download_videos.clear()
        selected_download.clear()
        download_select_controls.clear()
        update_download_selection()
        page.update()
        video = None
        try:
            video = await asyncio.to_thread(resolve_pexels_video, pexels_url.value or "")
            if cancel_event.is_set():
                raise InterruptedError("Đã hủy tải.")
            target = LIBRARY_ROOT / safe_folder_name(video.tags) / footage_filename(video)
            reserved = await asyncio.to_thread(
                database.reserve, video, video.tags, target, owner)
            if not reserved:
                status.value = "Video này đã có hoặc đang được tải trong Library."
                status.color = ft.Colors.AMBER_300
                return

            download_videos[video.id] = video
            grid.controls.append(card(video))
            download_list.controls.append(list_item(video))
            update_download_selection()
            overall.value = 0
            status.value = f"Đang tải video Pexels vào Library/{video.tags}..."
            page.update()

            queue: asyncio.Queue[DownloadEvent] = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def report(event: DownloadEvent) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, event)

            worker = asyncio.create_task(asyncio.to_thread(
                download_many, [video], LIBRARY_ROOT, video.tags, cancel_event,
                report, database, owner, "", max(1, min(20, int(video_preferences.get("workers", "5"))))))
            completed = 0
            failed = 0
            while not worker.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.15)
                except TimeoutError:
                    continue
                for progress, label in cards[video.id]:
                    progress.value = event.progress
                    label.value = event.message
                    label.color = {
                        "done": ft.Colors.GREEN_300,
                        "error": ft.Colors.RED_300,
                        "cancelled": ft.Colors.AMBER_300,
                    }.get(event.state, ft.Colors.BLUE_200)
                if event.state in {"done", "error", "cancelled"}:
                    completed += 1
                    failed += event.state == "error"
                    overall.value = completed
                page.update()
            await worker
            if not failed and not cancel_event.is_set():
                local_video = LIBRARY_ROOT / safe_folder_name(video.tags) / footage_filename(video)
                try:
                    actual_width, actual_height = await asyncio.to_thread(
                        probe_video_dimensions, local_video)
                except Exception:
                    local_video.unlink(missing_ok=True)
                    local_video.with_suffix(".jpg").unlink(missing_ok=True)
                    local_video.with_suffix(".thumb.jpg").unlink(missing_ok=True)
                    database.discard_completed_download(video.source, video.id)
                    raise
                if not orientation_matches(actual_width, actual_height,
                                           video_preferences["orientation"]):
                    local_video.unlink(missing_ok=True)
                    local_video.with_suffix(".jpg").unlink(missing_ok=True)
                    local_video.with_suffix(".thumb.jpg").unlink(missing_ok=True)
                    database.discard_completed_download(video.source, video.id)
                    grid.controls = [control for control in grid.controls
                                     if control.data != video.id]
                    download_list.controls = [control for control in download_list.controls
                                              if control.data != video.id]
                    for mapping in (cards, thumbnail_slots, download_select_controls,
                                    download_videos):
                        mapping.pop(video.id, None)
                    status.value = (f"Downloaded video was removed because its actual orientation "
                                    f"is {actual_width}x{actual_height}.")
                    status.color = ft.Colors.AMBER_300
                    return

                verified = replace(video, width=actual_width, height=actual_height)
                verified_path = local_video.with_name(footage_filename(verified))
                if verified_path != local_video:
                    if verified_path.exists():
                        local_video.unlink(missing_ok=True)
                        database.discard_completed_download(video.source, video.id)
                        raise FileExistsError("Verified video filename already exists.")
                    local_video.replace(verified_path)
                    for suffix in (".jpg", ".thumb.jpg"):
                        old_sidecar = local_video.with_suffix(suffix)
                        if old_sidecar.exists():
                            old_sidecar.replace(verified_path.with_suffix(suffix))
                    local_video = verified_path
                database.update_completed_video_metadata(verified, local_video)
                video = verified
                grid.controls = [control for control in grid.controls
                                 if control.data != video.id]
                download_list.controls = [control for control in download_list.controls
                                          if control.data != video.id]
                for mapping in (cards, thumbnail_slots, download_select_controls,
                                download_videos):
                    mapping.pop(video.id, None)
                download_videos[video.id] = video
                grid.controls.append(card(video))
                download_list.controls.append(list_item(video))
                for progress, label in cards[video.id]:
                    progress.value = 1
                    label.value = f"Verified: {actual_width}x{actual_height}"
                    label.color = ft.Colors.GREEN_300
                update_download_selection()
            library_loaded = False
            if not failed and not cancel_event.is_set():
                local_video = LIBRARY_ROOT / safe_folder_name(video.tags) / footage_filename(video)
                poster = await asyncio.to_thread(thumbnail_bytes, local_video)
                if poster:
                    for slot in thumbnail_slots.get(video.id, []):
                        slot.content = ft.Image(src=poster, fit=ft.BoxFit.COVER, expand=True)
            if cancel_event.is_set():
                status.value = "Đã hủy tải. Video chưa hoàn tất đã được dọn khỏi Library."
                status.color = ft.Colors.AMBER_300
            elif failed:
                status.value = "Tải video Pexels thất bại; bạn có thể thử lại bằng link này."
                status.color = ft.Colors.RED_300
            else:
                status.value = f"Tải video Pexels hoàn tất: Library/{video.tags}"
                status.color = ft.Colors.GREEN_300
        except InterruptedError:
            status.value = "Đã hủy tải."
            status.color = ft.Colors.AMBER_300
        except Exception as exc:
            status.value = str(exc)
            status.color = ft.Colors.RED_300
        finally:
            batch.__exit__(None, None, None)
            set_download_buttons_idle()
            cancel_button.disabled = True
            overall.visible = False
            update_download_selection()

    async def begin_direct_pexels_search() -> None:
        """Search public Pexels pages with Playwright, then download selected videos."""
        nonlocal cancel_event, library_loaded
        term = keyword.value.strip()
        terms = [part.strip() for part in term.split(",") if part.strip()]
        try:
            count = int(amount.value)
            if not terms or not 1 <= count <= 200:
                raise ValueError
            per_keyword_count = count
            count *= len(terms)
        except ValueError:
            status.value = "Keyword không được trống; số lượng phải từ 1 đến 200."
            status.color = ft.Colors.RED_300
            page.update()
            return

        source_dropdown = getattr(download_video_settings, "source_dropdown", None)
        if source_dropdown and source_dropdown.value != "pexels":
            status.value = "Crawl trực tiếp chỉ hỗ trợ Pexels. Hãy chọn Pexels hoặc chuyển sang Crawl by API."
            status.color = ft.Colors.AMBER_300
            page.update()
            return

        video_preferences["method"] = "direct"
        video_preferences["source"] = "pexels"
        start.disabled = True
        pexels_link_button.disabled = True
        cancel_button.disabled = False
        cancel_event = Event()
        batch = database.batch()
        owner = batch.__enter__()
        overall.visible = True
        overall.value = None
        status.value = f"Đang tìm {count} video Pexels cho keyword “{term}” bằng Chrome..."
        status.color = ft.Colors.BLUE_200
        grid.controls.clear()
        download_list.controls.clear()
        cards.clear()
        thumbnail_slots.clear()
        download_videos.clear()
        selected_download.clear()
        download_select_controls.clear()
        update_download_selection()
        page.update()

        try:
            overall.value = 0
            progress_started_at = time.monotonic()
            overall_count.value = format_progress_timing(0, count, progress_started_at)
            overall_count.visible = True
            status.value = (f"Searching and checking Pexels results until {count} videos match "
                            f"{video_preferences['orientation']} orientation...")
            page.update()
            queue: asyncio.Queue[DownloadEvent] = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def report(event: DownloadEvent) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, event)

            accepted_videos: list[Video] = []
            attempted_ids: set[int] = set()
            attempted = rejected = skipped = 0
            active_term = terms[0]
            active_term_videos: list[Video] = []

            def process_candidate_batch(candidates: list[Video]) -> bool:
                nonlocal attempted, rejected, skipped
                fresh = [candidate for candidate in candidates
                         if candidate.id not in attempted_ids]
                attempted_ids.update(candidate.id for candidate in fresh)
                if not fresh or cancel_event.is_set():
                    return cancel_event.is_set()
                accepted, tried, discarded, existing = download_matching_pexels_videos(
                    fresh, per_keyword_count - len(active_term_videos), LIBRARY_ROOT, active_term,
                    video_preferences["orientation"], cancel_event, database, owner, report)
                accepted_videos.extend(accepted)
                active_term_videos.extend(accepted)
                attempted += tried
                rejected += discarded
                skipped += existing
                return cancel_event.is_set() or len(accepted_videos) >= count

            def search_and_download():
                nonlocal active_term, active_term_videos
                for active_term in terms:
                    active_term_videos = []
                    if cancel_event.is_set():
                        break
                    search_pexels_videos_direct(
                        active_term, per_keyword_count, cancel_event, exclude_ids=attempted_ids,
                        on_batch=process_candidate_batch,
                    )
                return accepted_videos, attempted, rejected, skipped

            worker = asyncio.create_task(asyncio.to_thread(search_and_download))
            verified_count = 0
            failed = 0
            terminal_states = {"verified", "rejected", "error", "cancelled"}
            while not worker.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.15)
                except TimeoutError:
                    continue
                for progress, label in cards.get(event.video_id, []):
                    progress.value = event.progress
                    label.value = event.message
                    label.color = {
                        "done": ft.Colors.CYAN_300,
                        "verified": ft.Colors.GREEN_300,
                        "rejected": ft.Colors.AMBER_300,
                        "error": ft.Colors.RED_300,
                        "cancelled": ft.Colors.AMBER_300,
                    }.get(event.state, ft.Colors.BLUE_200)
                if event.state in terminal_states:
                    failed += event.state == "error"
                if event.state == "verified":
                    verified_count += 1
                    overall.value = min(1.0, verified_count / count)
                    overall_count.value = format_progress_timing(verified_count, count, progress_started_at)
                status.value = f"Đang tải {verified_count}/{count}"
                page.update()

            selected_videos, attempted, rejected, skipped = await worker
            overall.value = min(1.0, len(selected_videos) / count)
            overall_count.value = format_progress_timing(len(selected_videos), count, progress_started_at)
            valid_ids = {video.id for video in selected_videos}
            grid.controls.clear()
            download_list.controls.clear()
            for mapping in (cards, thumbnail_slots, download_select_controls, download_videos):
                mapping.clear()
            download_videos.update((video.id, video) for video in selected_videos)
            grid.controls.extend(card(video) for video in selected_videos)
            download_list.controls.extend(list_item(video) for video in selected_videos)
            for video in selected_videos:
                for progress, label in cards[video.id]:
                    progress.value = 1
                    label.value = f"Verified: {video.width}x{video.height}"
                    label.color = ft.Colors.GREEN_300
            selected_download.intersection_update(valid_ids)
            update_download_selection()

            library_loaded = False
            if not failed and not cancel_event.is_set():
                for video in selected_videos:
                    local_video = LIBRARY_ROOT / safe_folder_name(term) / footage_filename(video)
                    poster = await asyncio.to_thread(thumbnail_bytes, local_video)
                    if poster:
                        for slot in thumbnail_slots.get(video.id, []):
                            slot.content = ft.Image(src=poster, fit=ft.BoxFit.COVER, expand=True)
            if current_view == "library":
                await refresh_library(force=True)
            if cancel_event.is_set():
                status.value = f"Cancelled after retaining {len(selected_videos)} matching videos."
                status.color = ft.Colors.AMBER_300
            elif not selected_videos and skipped and not attempted:
                status.value = "All discovered Pexels videos are already in the Library."
                status.color = ft.Colors.AMBER_300
            elif not selected_videos and rejected:
                status.value = (f"No videos matched the selected orientation after checking {attempted} downloads; "
                                f"{rejected} were discarded.")
                status.color = ft.Colors.AMBER_300
            elif failed:
                status.value = (f"Kept {len(selected_videos)} matching videos; "
                                f"{failed} candidate downloads failed.")
                status.color = ft.Colors.AMBER_300
            elif len(selected_videos) < count:
                status.value = (f"Downloaded {len(selected_videos)}/{count} matching videos after checking "
                                f"{attempted} downloads; more results may be unavailable or already saved.")
                status.color = ft.Colors.AMBER_300
            else:
                status.value = f"Downloaded and verified {len(selected_videos)}/{count} Pexels videos."
                status.color = ft.Colors.GREEN_300
        except InterruptedError:
            status.value = "Đã hủy tìm kiếm hoặc tải video."
            status.color = ft.Colors.AMBER_300
        except PexelsBrowserSearchError as exc:
            status.value = str(exc)
            status.color = ft.Colors.AMBER_300 if isinstance(exc, PexelsBrowserBlocked) else ft.Colors.RED_300
        except (PexelsLinkError, OSError, sqlite3.Error) as exc:
            status.value = str(exc)
            status.color = ft.Colors.RED_300
        except Exception as exc:
            status.value = f"Lỗi khi tìm/tải Pexels: {exc}"
            status.color = ft.Colors.RED_300
        finally:
            batch.__exit__(None, None, None)
            overall.visible = False
            overall_count.visible = False
            set_download_buttons_idle()
            cancel_button.disabled = True
            update_download_selection()
            page.update()

    def cancel_download(_: ft.ControlEvent) -> None:
        if cancel_event:
            cancel_event.set()
            cancel_button.disabled = True
            status.value = "Đang hủy các video chưa hoàn tất..."
            page.update()

    start.on_click = begin_download
    pexels_link_button.on_click = download_pexels_link
    cancel_button.on_click = cancel_download
    download_grid_button = ft.IconButton(icon=ft.Icons.GRID_VIEW, tooltip="Dạng lưới")
    download_list_button = ft.IconButton(icon=ft.Icons.FORMAT_LIST_BULLETED, tooltip="Dạng danh sách")

    def change_download_layout(mode: str) -> None:
        nonlocal download_mode
        download_mode = mode
        download_results.content = grid if mode == "grid" else download_list
        download_grid_button.bgcolor = "#20335a" if mode == "grid" else None
        download_list_button.bgcolor = "#20335a" if mode == "list" else None
        page.update()

    download_grid_button.on_click = lambda _: change_download_layout("grid")
    download_list_button.on_click = lambda _: change_download_layout("list")
    download_grid_button.bgcolor = "#20335a"
    download_view = ft.Column(expand=True, spacing=14, controls=[
            ft.Row([ft.Text("Stock Downloader", size=28, weight=ft.FontWeight.BOLD), ft.Row([download_grid_button, download_list_button], spacing=2)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Text("Tìm footage theo nguồn đã chọn và tải trực tiếp về máy.", color=ft.Colors.BLUE_GREY_300),
            download_video_settings,
            keyword_download_row,
            pexels_direct_row,
            status,
            download_progress,
            ft.Divider(color="#263250"),
            ft.Row([download_select_all, download_selected_count, download_delete,
                    download_save, download_move], spacing=4),
            download_results,
        ])

    dashboard_video_count = ft.Text("—", size=30, weight=ft.FontWeight.BOLD)
    dashboard_library_size = ft.Text("—", size=30, weight=ft.FontWeight.BOLD)
    dashboard_project_count = ft.Text("—", size=30, weight=ft.FontWeight.BOLD)
    dashboard_status = ft.Text("Mở Dashboard để cập nhật thống kê.",
                               color=ft.Colors.BLUE_GREY_300)
    dashboard_progress = ft.ProgressBar(value=None, visible=False)
    dashboard_task: asyncio.Task[None] | None = None

    def dashboard_card(icon: ft.IconData, label: str, value: ft.Text) -> ft.Container:
        return ft.Container(
            expand=True, padding=ft.Padding.all(18), border_radius=12, bgcolor="#131c33",
            content=ft.Row(spacing=14, controls=[
                ft.Icon(icon, size=32, color=ft.Colors.CYAN_200),
                ft.Column(spacing=3, controls=[value,
                                                ft.Text(label, color=ft.Colors.BLUE_GREY_300)]),
            ]),
        )

    async def refresh_dashboard() -> None:
        nonlocal dashboard_task
        if dashboard_task is not None:
            await dashboard_task
            return

        async def load() -> None:
            dashboard_progress.visible = True
            dashboard_status.value = "Đang cập nhật thống kê Library..."
            page.update()
            try:
                videos, total_size, projects = await asyncio.to_thread(load_dashboard_stats, database)
                dashboard_video_count.value = str(videos)
                dashboard_library_size.value = format_size(total_size) if total_size else "0 MB"
                dashboard_project_count.value = str(projects)
                dashboard_status.value = "Đã cập nhật."
                dashboard_status.color = ft.Colors.BLUE_GREY_300
            except (OSError, sqlite3.Error):
                dashboard_status.value = "Không thể cập nhật thống kê. Hãy thử lại."
                dashboard_status.color = ft.Colors.RED_300
            finally:
                dashboard_progress.visible = False
                page.update()

        dashboard_task = asyncio.create_task(load())
        try:
            await dashboard_task
        finally:
            dashboard_task = None

    async def on_dashboard_refresh(_: ft.ControlEvent) -> None:
        await refresh_dashboard()

    dashboard_view = ft.Column(spacing=14, controls=[
        ft.Row([ft.Text("Dashboard", size=28, weight=ft.FontWeight.BOLD),
                ft.IconButton(icon=ft.Icons.REFRESH, tooltip="Cập nhật thống kê",
                              on_click=on_dashboard_refresh)],
               alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Text("Tổng quan Library và Project.", color=ft.Colors.BLUE_GREY_300),
        dashboard_progress,
        dashboard_status,
        ft.Row(spacing=14, controls=[
            dashboard_card(ft.Icons.VIDEO_LIBRARY_OUTLINED, "Video trong Library", dashboard_video_count),
            dashboard_card(ft.Icons.STORAGE_OUTLINED, "Dung lượng Library", dashboard_library_size),
            dashboard_card(ft.Icons.FOLDER_COPY_OUTLINED, "Projects", dashboard_project_count),
        ]),
    ])
    api_settings = ApiKeySettings(page, ROOT)
    log_status = ft.Text()

    async def on_export_log(_):
        from services.diagnostics import export_log
        try:
            destination = await file_picker.save_file(
                dialog_title="Xuất log lỗi", file_name="stock-diagnostic.txt",
                file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=["txt"])
            if not destination:
                return
            await asyncio.to_thread(export_log, destination)
            log_status.value = "Đã xuất log. Bạn có thể gửi file này để kiểm tra lỗi."
        except (OSError, ValueError):
            log_status.value = "Không thể xuất log. Hãy chọn một vị trí lưu khác."
        page.update()

    setup_view = ft.Column(spacing=10, scroll=ft.ScrollMode.AUTO, controls=[
        ft.Text("Setup", size=28, weight=ft.FontWeight.BOLD),
        ft.Text(f"Stock Downloader v{APP_VERSION}", color=ft.Colors.BLUE_GREY_300),
        api_settings,
        ft.OutlinedButton("Xuất log lỗi", icon=ft.Icons.DOWNLOAD, on_click=on_export_log),
        log_status,
        ft.Text(f"Thư mục dữ liệu: {ROOT}", selectable=True, color=ft.Colors.BLUE_GREY_300),
        ft.Text(f"Video được lưu tại: {LIBRARY_ROOT}", selectable=True, color=ft.Colors.BLUE_GREY_300),
    ])
    library_count = ft.Text(color=ft.Colors.BLUE_GREY_300)
    library_list = ft.ListView(expand=True, spacing=8)
    library_grid = ft.GridView(expand=True, max_extent=350, child_aspect_ratio=1.17, spacing=14, run_spacing=14)
    library_results = ft.Container(expand=True, content=library_grid)
    library_mode = "grid"
    library_loaded = False
    library_task: asyncio.Task[None] | None = None
    library_progress = ft.ProgressBar(value=None, visible=False)
    library_action_status = ft.Text(size=12, color=ft.Colors.BLUE_GREY_300)
    library_records: dict[int, dict] = {}
    library_deleted_records: set[tuple[int, str, float]] = set()
    library_visible_ids: set[int] = set()
    selected_library: set[int] = set()
    library_select_controls: dict[int, list[tuple[ft.IconButton, ft.Container]]] = {}
    library_selected_count = ft.Text("0 đã chọn", size=12, color=ft.Colors.BLUE_GREY_300)
    library_select_all = ft.IconButton(icon=ft.Icons.SELECT_ALL, tooltip="Chọn tất cả", disabled=True)
    library_delete = ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip="Xóa video đã chọn", disabled=True)
    library_save = ft.IconButton(icon=ft.Icons.DOWNLOAD, tooltip="Xuất video ra thư mục khác", disabled=True)
    library_move = ft.IconButton(icon=ft.Icons.DRIVE_FILE_MOVE_OUTLINE,
                                 tooltip="Gắn vào project/chapter", disabled=True)
    library_search = ft.TextField(
        hint_text="Tìm video theo keyword...", prefix_icon=ft.Icons.SEARCH,
        tooltip="Tìm theo tags, tên file hoặc thư mục Library", expand=True,
    )

    def update_library_selection() -> None:
        selected_library.intersection_update(library_records)
        for record_id, controls in library_select_controls.items():
            chosen = record_id in selected_library
            for button, container in controls:
                button.icon = ft.Icons.CHECK_BOX if chosen else ft.Icons.CHECK_BOX_OUTLINE_BLANK
                button.icon_color = ft.Colors.CYAN_200 if chosen else ft.Colors.WHITE
                container.bgcolor = "#20335a" if chosen else "#131c33"
        count = len(selected_library)
        library_selected_count.value = f"{count} đã chọn"
        visible_selected = selected_library & library_visible_ids
        library_select_all.disabled = not library_visible_ids
        library_select_all.icon = (ft.Icons.CHECK_BOX if (library_visible_ids and
                                                           len(visible_selected) == len(library_visible_ids))
                                   else ft.Icons.SELECT_ALL)
        for button in (library_delete, library_save, library_move):
            button.disabled = count == 0 or action_busy
        page.update()

    def toggle_library_selection(record_id: int) -> None:
        if record_id in selected_library:
            selected_library.remove(record_id)
        else:
            selected_library.add(record_id)
        update_library_selection()

    def toggle_all_library(_: ft.ControlEvent) -> None:
        visible_selected = selected_library & library_visible_ids
        if library_visible_ids and len(visible_selected) == len(library_visible_ids):
            selected_library.difference_update(library_visible_ids)
        else:
            selected_library.update(library_visible_ids)
        update_library_selection()

    library_select_all.on_click = toggle_all_library

    def library_matches(record: dict, query: str) -> bool:
        if not query:
            return True
        details = record.get("details") or {}
        searchable = " ".join([
            str(details.get("tags") or ""),
            Path(record["file_path"]).name,
            str(Path(record["file_path"]).parent),
        ]).casefold()
        return all(term in searchable for term in query.casefold().split())

    def render_library_results() -> None:
        query = (library_search.value or "").strip()
        records = [record for record in library_records.values() if library_matches(record, query)]
        library_visible_ids.clear()
        library_visible_ids.update(record["id"] for record in records)
        library_select_controls.clear()
        library_list.controls.clear()
        library_grid.controls.clear()
        library_count.value = (f"{len(records)}/{len(library_records)} video trong lịch sử"
                               if query else f"{len(records)} video trong lịch sử")
        if not library_records:
            empty = "Library đang trống. Hãy tải video từ mục Download."
            library_list.controls.append(ft.Text(empty, color=ft.Colors.BLUE_GREY_300))
            library_grid.controls.append(ft.Text(empty, color=ft.Colors.BLUE_GREY_300))
        elif not records:
            empty = f'Không tìm thấy video cho "{query}".'
            library_list.controls.append(ft.Text(empty, color=ft.Colors.BLUE_GREY_300))
            library_grid.controls.append(ft.Text(empty, color=ft.Colors.BLUE_GREY_300))
        for record in records:
            file = Path(record["file_path"])
            relative_path = file.relative_to(LIBRARY_ROOT) if file.is_relative_to(LIBRARY_ROOT) else file
            exists = record["exists_on_disk"]
            poster = record["poster_bytes"]
            details = record["details"]
            caption = details.get("tags") or file.name
            size = record["size_on_disk"]
            note = (f"{relative_path.parent} · {format_size(size)}" if exists
                    else "File không còn trên đĩa · vẫn giữ lịch sử")
            if record["status"] == "review":
                note += " · Chưa rõ ID Pixabay"
            list_selector = ft.IconButton(icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK,
                                           tooltip="Chọn video", icon_color=ft.Colors.WHITE,
                                           on_click=lambda _, row_id=record["id"]: toggle_library_selection(row_id))
            list_card = ft.Container(
                data=record["id"], padding=ft.Padding.all(12), border_radius=8,
                bgcolor="#131c33", tooltip=str(relative_path),
                on_click=(lambda _, path=file: show_library_preview(path)) if exists else None,
                content=ft.Row(controls=[
                    list_selector,
                    ft.Container(width=160, aspect_ratio=16 / 9,
                                 content=ft.Image(src=poster, fit=ft.BoxFit.COVER, expand=True)
                                 if poster else ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINED,
                                                        color=ft.Colors.CYAN_200)),
                    ft.Column(expand=True, spacing=2, controls=[
                        ft.Text(caption, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(note, size=12, color=ft.Colors.BLUE_GREY_300),
                    ]),
                ]),
            )
            library_list.controls.append(list_card)
            grid_selector = ft.IconButton(icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK,
                                           tooltip="Chọn video", icon_color=ft.Colors.WHITE,
                                           on_click=lambda _, row_id=record["id"]: toggle_library_selection(row_id))
            image = (ft.Image(src=poster, fit=ft.BoxFit.COVER, expand=True) if poster
                     else ft.Container(expand=True, alignment=ft.Alignment(0, 0),
                                       content=ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINED,
                                                       size=42, color=ft.Colors.CYAN_200)))
            grid_card = ft.Container(
                data=record["id"], padding=ft.Padding.all(12), border_radius=10,
                bgcolor="#131c33", tooltip=str(relative_path),
                on_click=(lambda _, path=file: show_library_preview(path)) if exists else None,
                content=ft.Column(spacing=8, controls=[
                    ft.Container(aspect_ratio=16 / 9, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                                 border_radius=7, bgcolor="#1d3158",
                                 content=ft.Stack([image,
                                                   ft.Container(left=4, top=4, bgcolor="#99000000",
                                                                border_radius=8, content=grid_selector)],
                                                  fit=ft.StackFit.EXPAND)),
                    ft.Text(caption, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                            tooltip=caption, weight=ft.FontWeight.W_600),
                    ft.Text(note, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                            size=12, color=ft.Colors.BLUE_GREY_300),
                ]),
            )
            library_grid.controls.append(grid_card)
            library_select_controls[record["id"]] = [(list_selector, list_card),
                                                       (grid_selector, grid_card)]
        update_library_selection()

    def remove_library_results(record_ids: set[int]) -> None:
        """Remove deleted cards in place without rebuilding every Library card."""
        if not record_ids:
            return
        for record_id in record_ids:
            library_records.pop(record_id, None)
            library_visible_ids.discard(record_id)
            selected_library.discard(record_id)
            library_select_controls.pop(record_id, None)
            LIBRARY_THUMBNAIL_CACHE.get(database, {}).pop(record_id, None)
        library_list.controls[:] = [
            control for control in library_list.controls
            if getattr(control, "data", None) not in record_ids
        ]
        library_grid.controls[:] = [
            control for control in library_grid.controls
            if getattr(control, "data", None) not in record_ids
        ]
        query = (library_search.value or "").strip()
        visible_count = sum(library_matches(record, query)
                            for record in library_records.values())
        library_count.value = (f"{visible_count}/{len(library_records)} video trong lịch sử"
                               if query else f"{len(library_records)} video trong lịch sử")
        if not library_records:
            empty = "Library đang trống. Hãy tải video từ mục Download."
            library_list.controls.append(ft.Text(empty, color=ft.Colors.BLUE_GREY_300))
            library_grid.controls.append(ft.Text(empty, color=ft.Colors.BLUE_GREY_300))
        elif query and not visible_count:
            empty = f'Không tìm thấy video cho "{query}".'
            library_list.controls.append(ft.Text(empty, color=ft.Colors.BLUE_GREY_300))
            library_grid.controls.append(ft.Text(empty, color=ft.Colors.BLUE_GREY_300))
        # Deleted cards are already gone, so refresh only toolbar state. Walking
        # every remaining card here makes deletion slow for large libraries.
        selected_library.intersection_update(library_records)
        count = len(selected_library)
        library_selected_count.value = f"{count} đã chọn"
        visible_selected = selected_library & library_visible_ids
        library_select_all.disabled = not library_visible_ids
        library_select_all.icon = (ft.Icons.CHECK_BOX if (library_visible_ids and
                                                           len(visible_selected) == len(library_visible_ids))
                                   else ft.Icons.SELECT_ALL)
        for button in (library_delete, library_save, library_move):
            button.disabled = count == 0 or action_busy
        page.update()

    def on_library_search(_: ft.ControlEvent) -> None:
        render_library_results()

    library_search.on_change = on_library_search

    async def refresh_library(force: bool = False, reuse_thumbnails: bool = True) -> None:
        nonlocal library_loaded, library_task
        if library_task is not None:
            await library_task
            if not force:
                return
        if library_loaded and not force:
            return

        async def load() -> None:
            nonlocal library_loaded
            library_progress.visible = True
            library_count.value = "Đang nạp thumbnail trong Library..."
            page.update()
            try:
                if reuse_thumbnails:
                    LIBRARY_THUMBNAIL_CACHE[database] = dict(library_records)
                else:
                    LIBRARY_THUMBNAIL_CACHE.pop(database, None)
                records = await asyncio.to_thread(load_library_entries, database)
                if library_deleted_records:
                    records = [record for record in records
                               if (record["id"], record["file_path"], record["created_at"])
                               not in library_deleted_records]
                LIBRARY_THUMBNAIL_CACHE[database] = {
                    record["id"]: record for record in records
                }
                library_records.clear()
                library_records.update((record["id"], record) for record in records)
                library_deleted_records.clear()
                library_loaded = True
                render_library_results()
            except Exception as exc:
                library_count.value = f"Không thể nạp Library: {exc}"
            finally:
                library_progress.visible = False
                page.update()

        library_task = asyncio.create_task(load())
        try:
            await library_task
        finally:
            library_task = None
    library_grid_button = ft.IconButton(icon=ft.Icons.GRID_VIEW, tooltip="Dạng lưới")
    library_list_button = ft.IconButton(icon=ft.Icons.FORMAT_LIST_BULLETED, tooltip="Dạng danh sách")

    def change_library_layout(mode: str) -> None:
        nonlocal library_mode
        library_mode = mode
        library_results.content = library_grid if mode == "grid" else library_list
        library_grid_button.bgcolor = "#20335a" if mode == "grid" else None
        library_list_button.bgcolor = "#20335a" if mode == "list" else None
        page.update()

    library_grid_button.on_click = lambda _: change_library_layout("grid")
    library_list_button.on_click = lambda _: change_library_layout("list")
    library_grid_button.bgcolor = "#20335a"
    async def on_library_refresh(_: ft.ControlEvent) -> None:
        await refresh_library(force=True, reuse_thumbnails=False)

    library_view = ft.Column(expand=True, spacing=12, controls=[
        ft.Row([library_grid_button, library_list_button], alignment=ft.MainAxisAlignment.END, spacing=2),
        ft.Row([ft.Text("Library", size=28, weight=ft.FontWeight.BOLD), ft.IconButton(icon=ft.Icons.REFRESH, tooltip="Làm mới", on_click=on_library_refresh)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Row([library_count, library_search], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        library_progress,
        ft.Row([library_select_all, library_selected_count, library_delete,
                library_save, library_move], spacing=4),
        library_action_status,
        library_results,
    ])

    project_name = ft.TextField(label="Tên project", hint_text="Ví dụ: Russia", max_length=120)
    project_create = ft.FilledButton("Tạo project", icon=ft.Icons.ADD)
    project_feedback = ft.Text(size=12, color=ft.Colors.RED_300)
    project_list = ft.ListView(expand=True, spacing=6)
    chapter_title = ft.Text("Chọn hoặc tạo project", size=28, weight=ft.FontWeight.BOLD)
    chapter_count = ft.Text("Các chapter của project sẽ xuất hiện ở đây.", color=ft.Colors.BLUE_GREY_300)
    chapter_name = ft.TextField(label="Tên chapter", hint_text="Ví dụ: Chapter 1", expand=True, disabled=True)
    chapter_create = ft.FilledButton("Thêm chapter", icon=ft.Icons.ADD, disabled=True)
    chapter_feedback = ft.Text(size=12, color=ft.Colors.RED_300)
    project_grid = ft.GridView(expand=True, max_extent=350, child_aspect_ratio=1.17,
                               spacing=14, run_spacing=14)
    project_empty = ft.Text(visible=False, color=ft.Colors.BLUE_GREY_300)
    project_action_status = ft.Text(size=12, color=ft.Colors.BLUE_GREY_300)
    selected_project_videos: set[int] = set()
    visible_project_ids: set[int] = set()
    project_select_controls: dict[int, tuple[ft.IconButton, ft.Container]] = {}
    project_selected_count = ft.Text("0 đã chọn", size=12, color=ft.Colors.BLUE_GREY_300)
    project_select_all = ft.IconButton(icon=ft.Icons.SELECT_ALL, tooltip="Chọn tất cả", disabled=True)
    project_delete = ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip="Xóa video đã chọn", disabled=True)
    project_save = ft.IconButton(icon=ft.Icons.DOWNLOAD, tooltip="Xuất video ra thư mục khác", disabled=True)
    project_move = ft.IconButton(icon=ft.Icons.DRIVE_FILE_MOVE_OUTLINE,
                                 tooltip="Gắn vào project/chapter", disabled=True)
    edit_add_stock = ft.IconButton(icon=ft.Icons.CONTENT_CUT,
                                   tooltip="Trim video đã chọn", disabled=True)
    edit_status = ft.Text(size=12, color=ft.Colors.BLUE_GREY_300)
    edit_empty = ft.Text("Chưa có video trim. Chọn video ở tab Video stock rồi bấm nút Trim.",
                         color=ft.Colors.BLUE_GREY_300)
    edit_grid = ft.GridView(expand=True, max_extent=350, child_aspect_ratio=1.17,
                            spacing=14, run_spacing=14, visible=False)
    edit_sequences: dict[tuple[int, int | None], list[int]] = {}
    selected_edit_videos: set[int] = set()
    visible_edit_ids: set[int] = set()
    edit_select_controls: dict[int, tuple[ft.IconButton, ft.Container]] = {}
    edit_selected_count = ft.Text("0 đã chọn", size=12, color=ft.Colors.BLUE_GREY_300)
    edit_select_all = ft.IconButton(icon=ft.Icons.SELECT_ALL, tooltip="Chọn tất cả", disabled=True)
    edit_delete = ft.IconButton(icon=ft.Icons.DELETE_OUTLINE,
                                tooltip="Gỡ video đã chọn khỏi Edit", disabled=True)
    edit_trim_rows: dict[tuple[int, int | None], dict[int, dict]] = {}
    order_sequences: dict[tuple[int, int | None], list[int]] = {}
    order_pool = ft.GridView(expand=True, max_extent=280, child_aspect_ratio=1.65,
                             spacing=10, run_spacing=10)
    order_list = ft.Column(expand=True, spacing=8, scroll=ft.ScrollMode.AUTO)
    order_status = ft.Text(size=12, color=ft.Colors.BLUE_GREY_300)
    order_export = ft.IconButton(icon=ft.Icons.DOWNLOAD,
                                 tooltip="Tải video theo thứ tự", disabled=True)
    order_thumbnail_cache: dict[str, bytes | None] = {}
    active_order_drag: dict[str, int | str | None] = {"origin": None, "record_id": None}
    selected_project_id: int | None = None
    selected_chapter_id: int | None = None
    project_rows: list[dict] = []
    project_chapters: dict[int, list[dict]] = {}
    project_entries: list[dict] = []
    project_entry_cache: dict[int, list[dict]] = {}
    expanded_projects: set[int] = set()
    project_loaded = False
    selection_version = 0
    structure_busy = False

    def update_project_selection() -> None:
        selected_project_videos.intersection_update(visible_project_ids)
        for record_id, (button, container) in project_select_controls.items():
            chosen = record_id in selected_project_videos
            button.icon = ft.Icons.CHECK_BOX if chosen else ft.Icons.CHECK_BOX_OUTLINE_BLANK
            button.icon_color = ft.Colors.CYAN_200 if chosen else ft.Colors.WHITE
            container.bgcolor = "#20335a" if chosen else "#131c33"
        count = len(selected_project_videos)
        project_selected_count.value = f"{count} đã chọn"
        project_select_all.disabled = not visible_project_ids or action_busy
        edit_add_stock.disabled = count == 0 or action_busy
        project_select_all.icon = (ft.Icons.CHECK_BOX if count == len(visible_project_ids) and count
                                   else ft.Icons.SELECT_ALL)
        for button in (project_delete, project_save, project_move):
            button.disabled = count == 0 or action_busy
        page.update()

    def toggle_project_selection(record_id: int) -> None:
        if record_id in selected_project_videos:
            selected_project_videos.remove(record_id)
        elif record_id in visible_project_ids:
            selected_project_videos.add(record_id)
        update_project_selection()

    def toggle_all_project(_: ft.ControlEvent) -> None:
        if len(selected_project_videos) == len(visible_project_ids):
            selected_project_videos.clear()
        else:
            selected_project_videos.update(visible_project_ids)
        update_project_selection()

    project_select_all.on_click = toggle_all_project

    def update_edit_selection() -> None:
        selected_edit_videos.intersection_update(visible_edit_ids)
        for record_id, (button, container) in edit_select_controls.items():
            chosen = record_id in selected_edit_videos
            button.icon = ft.Icons.CHECK_BOX if chosen else ft.Icons.CHECK_BOX_OUTLINE_BLANK
            button.icon_color = ft.Colors.CYAN_200 if chosen else ft.Colors.WHITE
            container.bgcolor = "#20335a" if chosen else "#131c33"
        count = len(selected_edit_videos)
        edit_selected_count.value = f"{count} đã chọn"
        edit_select_all.disabled = not visible_edit_ids or action_busy
        edit_select_all.icon = (ft.Icons.CHECK_BOX if count == len(visible_edit_ids) and count
                                else ft.Icons.SELECT_ALL)
        edit_delete.disabled = count == 0 or action_busy

    def toggle_edit_selection(record_id: int) -> None:
        if record_id in selected_edit_videos:
            selected_edit_videos.remove(record_id)
        elif record_id in visible_edit_ids:
            selected_edit_videos.add(record_id)
        update_edit_selection()
        page.update()

    def toggle_all_edit(_: ft.ControlEvent) -> None:
        if len(selected_edit_videos) == len(visible_edit_ids):
            selected_edit_videos.clear()
        else:
            selected_edit_videos.update(visible_edit_ids)
        update_edit_selection()
        page.update()

    edit_select_all.on_click = toggle_all_edit

    def edit_video_card(record: dict) -> ft.Control:
        path = Path(record["file_path"])
        label = record["details"].get("tags") or path.name
        poster = record["poster_bytes"]
        image = (ft.Image(src=poster, fit=ft.BoxFit.COVER, expand=True) if poster
                 else ft.Container(expand=True, alignment=ft.Alignment(0, 0),
                                   content=ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINED,
                                                   size=42, color=ft.Colors.CYAN_200)))
        selector = ft.IconButton(icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK,
                                 tooltip="Chọn video", icon_color=ft.Colors.WHITE,
                                 on_click=lambda _, record_id=record["id"]: toggle_edit_selection(record_id))
        container = ft.Container(
            data=record["id"], padding=ft.Padding.all(12), border_radius=10,
            bgcolor="#131c33", tooltip=str(path),
            on_click=(lambda _, file=path: show_library_preview(file))
            if record["exists_on_disk"] else None,
            content=ft.Column(spacing=8, controls=[
                ft.Container(aspect_ratio=16 / 9, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                             border_radius=7, bgcolor="#1d3158",
                             content=ft.Stack([image,
                                               ft.Container(left=4, top=4, bgcolor="#99000000",
                                                            border_radius=8, content=selector)],
                                              fit=ft.StackFit.EXPAND)),
                ft.Text(label, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                        tooltip=label, weight=ft.FontWeight.W_600),
                ft.Text(format_size(record["size_on_disk"]) if record["exists_on_disk"]
                        else "Thiếu file MP4", size=12,
                        color=ft.Colors.BLUE_GREY_300 if record["exists_on_disk"]
                        else ft.Colors.AMBER_300),
            ]),
        )
        edit_select_controls[record["id"]] = (selector, container)
        return container

    def trimmed_video_card(record: dict, trim: dict) -> ft.Control:
        path = Path(trim["trimmed_path"])
        duration_text = f"{float(trim['trim_duration']):.1f}".replace(".", ",")
        start_text = f"{float(trim['trim_start']):.1f}".replace(".", ",")
        poster = thumbnail_bytes(path) if path.is_file() else None
        image = (ft.Image(src=poster, fit=ft.BoxFit.COVER, expand=True) if poster
                 else ft.Container(expand=True, alignment=ft.Alignment(0, 0),
                                   content=ft.Icon(ft.Icons.CONTENT_CUT,
                                                   size=42, color=ft.Colors.AMBER_300)))
        label = record["details"].get("tags") or Path(record["file_path"]).name
        selector = ft.IconButton(icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK,
                                 tooltip="Chọn video trim", icon_color=ft.Colors.WHITE,
                                 on_click=lambda _, record_id=record["id"]: toggle_edit_selection(record_id))
        container = ft.Container(
            data=f'trim:{record["id"]}', padding=ft.Padding.all(12), border_radius=10,
            bgcolor="#2a2438", tooltip=str(path),
            on_click=(lambda _, file=path: show_library_preview(file)) if path.is_file() else None,
            content=ft.Column(spacing=8, controls=[
                ft.Container(aspect_ratio=16 / 9, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                             border_radius=7, bgcolor="#1d3158",
                             content=ft.Stack([image,
                                               ft.Container(left=4, top=4, bgcolor="#99000000",
                                                            border_radius=8, content=selector)],
                                              fit=ft.StackFit.EXPAND)),
                ft.Text(f"Trim · {label}", max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                        tooltip=label, weight=ft.FontWeight.W_600),
                ft.Text(f"{duration_text}s · từ {start_text}s",
                        size=12, color=ft.Colors.AMBER_200),
            ]),
        )
        edit_select_controls[record["id"]] = (selector, container)
        return container

    def order_video_tile(record: dict, trim: dict, position: int | None = None) -> ft.Control:
        path = Path(trim["trimmed_path"])
        label = record["details"].get("tags") or path.name
        cache_key = str(path)
        if cache_key not in order_thumbnail_cache:
            try:
                order_thumbnail_cache[cache_key] = thumbnail_bytes(path) if path.is_file() else None
            except OSError:
                order_thumbnail_cache[cache_key] = None
        poster = order_thumbnail_cache[cache_key]
        preview = (ft.Image(src=poster, width=96, height=54, fit=ft.BoxFit.COVER)
                   if poster else ft.Icon(ft.Icons.MOVIE_OUTLINED,
                                          color=ft.Colors.AMBER_300))
        preview_button = ft.IconButton(
            icon=ft.Icons.PLAY_ARROW, icon_color=ft.Colors.WHITE, bgcolor="#99000000",
            tooltip="Preview video trim", disabled=not path.is_file(),
            on_click=(lambda _, file=path: show_library_preview(file)) if path.is_file() else None,
        )
        if position is None:
            large_preview = (ft.Image(src=poster, fit=ft.BoxFit.COVER, expand=True)
                             if poster else ft.Icon(ft.Icons.MOVIE_OUTLINED,
                                                    size=42, color=ft.Colors.AMBER_300))
            return ft.Container(
                padding=ft.Padding.all(8), border_radius=8, bgcolor="#17223a", tooltip=str(path),
                content=ft.Row(spacing=8, controls=[
                    ft.Icon(ft.Icons.DRAG_INDICATOR, color=ft.Colors.BLUE_GREY_300),
                    ft.Container(expand=True, aspect_ratio=16 / 9, border_radius=7,
                                 clip_behavior=ft.ClipBehavior.ANTI_ALIAS, bgcolor="#1d3158",
                                 content=ft.Stack([
                                     large_preview,
                                     ft.Container(expand=True, alignment=ft.Alignment(0, 0),
                                                  content=preview_button),
                                 ], fit=ft.StackFit.EXPAND)),
                ]),
            )
        leading = (ft.Container(width=34, height=34, border_radius=17, bgcolor="#29466f",
                                alignment=ft.Alignment(0, 0),
                                content=ft.Text(f"{position:02d}", weight=ft.FontWeight.BOLD)))
        return ft.Container(
            padding=ft.Padding.all(10), border_radius=8, bgcolor="#17223a", tooltip=str(path),
            content=ft.Row(spacing=10, controls=[
                leading,
                ft.Container(width=96, height=54, border_radius=6, bgcolor="#1d3158",
                             clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                             alignment=ft.Alignment(0, 0),
                             content=ft.Stack([
                                 preview,
                                 ft.Container(expand=True, alignment=ft.Alignment(0, 0),
                                              content=preview_button),
                             ], fit=ft.StackFit.EXPAND)),
                ft.Column(expand=True, spacing=2, controls=[
                    ft.Text(label, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(f"{float(trim['trim_duration']):.1f}s · {path.name}", size=11,
                            color=ft.Colors.BLUE_GREY_300, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS),
                ]),
                ft.Column(spacing=0, controls=[
                    ft.IconButton(
                        icon=ft.Icons.KEYBOARD_ARROW_UP, icon_size=18,
                        tooltip="Đưa lên một vị trí", disabled=position <= 1,
                        on_click=lambda _, rid=record["id"]: page.run_task(move_order_item, rid, -1),
                    ),
                    ft.IconButton(
                        icon=ft.Icons.KEYBOARD_ARROW_DOWN, icon_size=18,
                        tooltip="Đưa xuống một vị trí",
                        on_click=lambda _, rid=record["id"]: page.run_task(move_order_item, rid, 1),
                    ),
                ]),
            ]),
        )

    async def save_order_drop(source_id: int, target_index: int) -> None:
        if selected_project_id is None:
            return
        key = (selected_project_id, selected_chapter_id)
        sequence = order_sequences.setdefault(key, [])
        if source_id in sequence:
            old_index = sequence.index(source_id)
            sequence.pop(old_index)
            if old_index < target_index:
                target_index -= 1
        target_index = max(0, min(target_index, len(sequence)))
        sequence.insert(target_index, source_id)
        try:
            await asyncio.to_thread(database.save_ordered_video_ids, sequence, *key)
            order_status.value = "Đã lưu thứ tự video."
            order_status.color = ft.Colors.BLUE_200
        except (ValueError, sqlite3.Error) as exc:
            order_status.value = str(exc) or "Không thể lưu thứ tự video."
            order_status.color = ft.Colors.RED_300
            order_sequences[key] = await asyncio.to_thread(database.list_ordered_video_ids, *key)
        render_order_layout()
        page.update()

    def remember_order_drag(origin: str, record_id: int) -> None:
        active_order_drag["origin"] = origin
        active_order_drag["record_id"] = record_id

    def dragged_order_data(event: ft.DragTargetEvent) -> tuple[str, int] | None:
        source = getattr(event, "src", None)
        data = getattr(source, "data", None)
        if isinstance(data, (tuple, list)) and len(data) == 2:
            return str(data[0]), int(data[1])
        record_id = active_order_drag.get("record_id")
        origin = active_order_drag.get("origin")
        return (str(origin), int(record_id)) if origin and record_id is not None else None

    async def remove_order_drop(source_id: int) -> None:
        if selected_project_id is None:
            return
        key = (selected_project_id, selected_chapter_id)
        sequence = order_sequences.setdefault(key, [])
        if source_id not in sequence:
            return
        sequence.remove(source_id)
        try:
            await asyncio.to_thread(database.save_ordered_video_ids, sequence, *key)
            order_status.value = "Đã đưa video về kho video trim."
            order_status.color = ft.Colors.BLUE_200
        except (ValueError, sqlite3.Error) as exc:
            order_status.value = str(exc) or "Không thể cập nhật thứ tự video."
            order_status.color = ft.Colors.RED_300
            order_sequences[key] = await asyncio.to_thread(database.list_ordered_video_ids, *key)
        render_order_layout()
        page.update()

    async def move_order_item(record_id: int, delta: int) -> None:
        """Move an ordered clip one position up or down."""
        if selected_project_id is None:
            return
        key = (selected_project_id, selected_chapter_id)
        sequence = order_sequences.setdefault(key, [])
        try:
            index = sequence.index(record_id)
        except ValueError:
            return
        target = index + delta
        if target < 0 or target >= len(sequence):
            return
        sequence[index], sequence[target] = sequence[target], sequence[index]
        try:
            await asyncio.to_thread(database.save_ordered_video_ids, sequence, *key)
            order_status.value = "Đã lưu thứ tự video."
            order_status.color = ft.Colors.BLUE_200
        except (ValueError, sqlite3.Error) as exc:
            order_status.value = str(exc) or "Không thể lưu thứ tự video."
            order_status.color = ft.Colors.RED_300
            order_sequences[key] = await asyncio.to_thread(database.list_ordered_video_ids, *key)
        render_order_layout()
        page.update()

    def order_drop_target(index: int, content: ft.Control) -> ft.DragTarget:
        async def accept(event: ft.DragTargetEvent) -> None:
            data = dragged_order_data(event)
            if data is not None:
                await save_order_drop(data[1], index)
        return ft.DragTarget(group="order-video", data=index, content=content, on_accept=accept)

    async def accept_order_back(event: ft.DragTargetEvent) -> None:
        data = dragged_order_data(event)
        if data is not None and data[0] == "order":
            await remove_order_drop(data[1])

    async def accept_order_append(event: ft.DragTargetEvent) -> None:
        data = dragged_order_data(event)
        if data is not None:
            key = (selected_project_id, selected_chapter_id)
            await save_order_drop(data[1], len(order_sequences.get(key, [])))

    def render_order_layout() -> None:
        key = (selected_project_id, selected_chapter_id) if selected_project_id is not None else None
        records = {row["id"]: row for row in project_entries}
        trims = edit_trim_rows.get(key, {}) if key is not None else {}
        available = [record_id for record_id in edit_sequences.get(key, [])
                     if record_id in records and record_id in trims
                     and trims[record_id].get("trim_status") == "completed"
                     and Path(trims[record_id].get("trimmed_path") or "").is_file()]
        ordered = [record_id for record_id in order_sequences.get(key, []) if record_id in available]
        if key is not None:
            order_sequences[key] = ordered
        order_pool.controls[:] = [
            ft.Draggable(group="order-video", data=("pool", record_id),
                         affinity=ft.Axis.HORIZONTAL,
                         on_drag_start=lambda _, record_id=record_id:
                             remember_order_drag("pool", record_id),
                         content=order_video_tile(records[record_id], trims[record_id]),
                         content_feedback=ft.Container(width=260, opacity=0.85,
                                                       content=order_video_tile(records[record_id], trims[record_id])))
            for record_id in available if record_id not in ordered
        ]
        order_controls: list[ft.Control] = []
        for index, record_id in enumerate(ordered):
            order_controls.append(order_drop_target(
                index, ft.Container(height=28, border_radius=6, bgcolor="#16233b",
                                    tooltip="Thả vào vị trí này")))
            order_controls.append(ft.Draggable(
                group="order-video", data=("order", record_id),
                affinity=ft.Axis.VERTICAL,
                on_drag_start=lambda _, record_id=record_id:
                    remember_order_drag("order", record_id),
                content=order_video_tile(records[record_id], trims[record_id], index + 1),
                content_feedback=ft.Container(width=360, opacity=0.85,
                                              content=order_video_tile(records[record_id],
                                                                       trims[record_id], index + 1))))
        order_controls.append(order_drop_target(
            len(ordered), ft.Container(height=120, border=ft.Border.all(1, "#38527a"),
                                       border_radius=8, alignment=ft.Alignment(0, 0),
                                       content=ft.Text("Thả video vào đây để thêm xuống cuối", size=12,
                                                       color=ft.Colors.BLUE_GREY_300))))
        order_list.controls[:] = order_controls
        order_export.disabled = not ordered or action_busy

    def render_edit_grid() -> None:
        key = (selected_project_id, selected_chapter_id) if selected_project_id is not None else None
        ids = edit_sequences.get(key, []) if key is not None else []
        records = {row["id"]: row for row in project_entries}
        trims = edit_trim_rows.get(key, {}) if key is not None else {}
        edit_select_controls.clear()
        visible_edit_ids.clear()
        visible_edit_ids.update(record_id for record_id in ids if record_id in records
                                and (trims.get(record_id) or {}).get("trim_status") == "completed"
                                and (trims.get(record_id) or {}).get("trimmed_path")
                                and Path(trims[record_id]["trimmed_path"]).is_file())
        controls = []
        for record_id in ids:
            if record_id not in records:
                continue
            trim = trims.get(record_id)
            if (trim and trim.get("trim_status") == "completed"
                    and trim.get("trimmed_path") and Path(trim["trimmed_path"]).is_file()):
                controls.append(trimmed_video_card(records[record_id], trim))
        edit_grid.controls[:] = controls
        update_edit_selection()
        edit_grid.visible = bool(edit_grid.controls)
        edit_empty.visible = not edit_grid.controls
        render_order_layout()

    async def reload_edit_scope(project_id: int, chapter_id: int | None) -> None:
        rows = await asyncio.to_thread(database.list_edit_videos, project_id, chapter_id)
        key = (project_id, chapter_id)
        edit_sequences[key] = [row["video_record_id"] for row in rows]
        edit_trim_rows[key] = {row["video_record_id"]: row for row in rows}
        order_sequences[key] = await asyncio.to_thread(
            database.list_ordered_video_ids, project_id, chapter_id)

    async def remove_selected_from_edit(_: ft.ControlEvent) -> None:
        if selected_project_id is None or not selected_edit_videos:
            return
        key = (selected_project_id, selected_chapter_id)
        try:
            removed = await asyncio.to_thread(database.remove_edit_videos,
                                              sorted(selected_edit_videos), *key)
            await reload_edit_scope(*key)
        except (ValueError, sqlite3.Error) as exc:
            edit_status.value = str(exc) or "Không thể gỡ video khỏi Edit."
            edit_status.color = ft.Colors.RED_300
            page.update()
            return
        selected_edit_videos.clear()
        edit_status.value = f"Đã gỡ {removed} video khỏi Edit."
        edit_status.color = ft.Colors.BLUE_200
        render_edit_grid()
        page.update()

    edit_delete.on_click = remove_selected_from_edit

    async def add_selected_to_edit(_: ft.ControlEvent) -> None:
        if selected_project_id is None:
            return
        if not selected_project_videos:
            edit_status.value = "Hãy chọn ít nhất một video trong tab Video stock."
            edit_status.color = ft.Colors.AMBER_300
            page.update()
            return
        key = (selected_project_id, selected_chapter_id)
        ordered_ids = [row["id"] for row in project_entries
                       if row["id"] in selected_project_videos and row["id"] in visible_project_ids]
        try:
            added = await asyncio.to_thread(database.add_edit_videos, ordered_ids, *key)
            await reload_edit_scope(*key)
        except (ValueError, sqlite3.Error) as exc:
            edit_status.value = str(exc) or "Không thể thêm video vào Edit."
            edit_status.color = ft.Colors.RED_300
            chapter_tabs.selected_index = 1
            page.update()
            return
        edit_status.value = (f"Đã thêm {added} video vào Edit."
                             if added else "Các video đã chọn đã có trong Edit.")
        edit_status.color = ft.Colors.BLUE_200
        render_edit_grid()
        chapter_tabs.selected_index = 1
        page.update()

    def show_trim_setup(_: ft.ControlEvent | None, chosen_ids: list[int] | None = None) -> None:
        chosen_ids = chosen_ids or sorted(selected_edit_videos)
        if selected_project_id is None or not chosen_ids:
            return
        minimum = ft.TextField(label="Min duration (s)", value="5,0", width=155,
                               keyboard_type=ft.KeyboardType.NUMBER)
        maximum = ft.TextField(label="Max duration (s)", value="6,0", width=155,
                               keyboard_type=ft.KeyboardType.NUMBER)
        message = ft.Text(size=12, color=ft.Colors.RED_300)

        def close(_: ft.ControlEvent) -> None:
            page.pop_dialog()

        async def run(_: ft.ControlEvent) -> None:
            try:
                min_seconds = round(float((minimum.value or "").replace(",", ".")), 1)
                max_seconds = round(float((maximum.value or "").replace(",", ".")), 1)
                if min_seconds <= 0 or max_seconds < min_seconds:
                    raise ValueError
            except (TypeError, ValueError):
                message.value = "Duration phải lớn hơn 0 và Max phải lớn hơn hoặc bằng Min."
                page.update()
                return
            project_id, chapter_id = selected_project_id, selected_chapter_id
            chosen = chosen_ids.copy()
            records = {row["id"]: row for row in project_entries}
            page.pop_dialog()
            set_action_busy(True)
            completed = failed = 0
            try:
                for position, record_id in enumerate(chosen, 1):
                    record = records.get(record_id)
                    if record is None:
                        failed += 1
                        continue
                    edit_status.value = f"Đang trim {position}/{len(chosen)} video..."
                    edit_status.color = ft.Colors.BLUE_200
                    page.update()
                    source = Path(record["file_path"])
                    scope = str(chapter_id) if chapter_id is not None else "project"
                    destination = ROOT / "edits" / str(project_id) / scope / f"{record_id}_trimmed.mp4"
                    try:
                        output, start_time, duration = await asyncio.to_thread(
                            trim_random_video, source, destination, min_seconds, max_seconds)
                        await asyncio.to_thread(database.save_edit_trim, record_id, project_id,
                                                chapter_id, output, start_time, duration)
                        completed += 1
                    except Exception as exc:
                        failed += 1
                        try:
                            await asyncio.to_thread(database.save_edit_trim_error, record_id,
                                                    project_id, chapter_id, str(exc))
                        except sqlite3.Error:
                            pass
                await reload_edit_scope(project_id, chapter_id)
                render_edit_grid()
                edit_status.value = f"Trim hoàn tất: {completed} thành công, {failed} lỗi."
                edit_status.color = ft.Colors.BLUE_200 if not failed else ft.Colors.AMBER_300
            finally:
                set_action_busy(False)
                page.update()

        dialog = ft.AlertDialog(
            modal=True,
            title="Trim video ngẫu nhiên",
            content=ft.Container(width=350, content=ft.Column(tight=True, spacing=12, controls=[
                ft.Text(f"{len(chosen_ids)} video đã chọn",
                        color=ft.Colors.BLUE_GREY_300),
                ft.Row([minimum, maximum]),
                ft.Text("Mỗi video stock sẽ tạo một clip trim; video gốc vẫn được giữ trong Edit.",
                        size=12, color=ft.Colors.BLUE_GREY_300),
                message,
            ])),
            actions=[ft.TextButton("Hủy", on_click=close),
                     ft.FilledButton("Bắt đầu trim", icon=ft.Icons.CONTENT_CUT, on_click=run)],
        )
        page.show_dialog(dialog)

    async def prepare_trim_from_stock(_: ft.ControlEvent) -> None:
        if selected_project_id is None or not selected_project_videos:
            return
        chosen = [row["id"] for row in project_entries
                  if row["id"] in selected_project_videos and row["id"] in visible_project_ids]
        await add_selected_to_edit(None)
        show_trim_setup(None, chosen)

    edit_add_stock.on_click = prepare_trim_from_stock

    async def export_ordered(_: ft.ControlEvent) -> None:
        if selected_project_id is None:
            return
        key = (selected_project_id, selected_chapter_id)
        ordered = order_sequences.get(key, [])
        trims = edit_trim_rows.get(key, {})
        paths = [Path(trims[record_id]["trimmed_path"])
                 for record_id in ordered if record_id in trims]
        if not paths:
            return
        try:
            destination = await file_picker.get_directory_path(
                dialog_title="Chọn thư mục tải video theo thứ tự")
        except Exception:
            order_status.value = "Không mở được hộp chọn thư mục."
            order_status.color = ft.Colors.RED_300
            page.update()
            return
        if not destination:
            return
        width = max(2, len(str(len(paths))))
        names = [f"{position:0{width}d}_{path.name}"
                 for position, path in enumerate(paths, 1)]
        set_action_busy(True)
        try:
            copied, skipped = await asyncio.to_thread(
                export_named_videos, paths, names, Path(destination))
            order_status.value = f"Đã tải {copied} video; bỏ qua {skipped} file trùng hoặc bị thiếu."
            order_status.color = ft.Colors.BLUE_200
        except (OSError, ValueError):
            order_status.value = "Không thể tải danh sách video đã sắp xếp."
            order_status.color = ft.Colors.RED_300
        finally:
            set_action_busy(False)
            page.update()

    order_export.on_click = export_ordered

    def project_video_card(record: dict) -> ft.Control:
        path = Path(record["file_path"])
        label = record["details"].get("tags") or path.name
        exists = record["exists_on_disk"]
        poster = record["poster_bytes"]
        image = (ft.Image(src=poster, fit=ft.BoxFit.COVER, expand=True) if poster
                 else ft.Container(expand=True, alignment=ft.Alignment(0, 0),
                                   content=ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINED,
                                                   size=42, color=ft.Colors.CYAN_200)))
        selector = ft.IconButton(icon=ft.Icons.CHECK_BOX_OUTLINE_BLANK,
                                 tooltip="Chọn video", icon_color=ft.Colors.WHITE,
                                 on_click=lambda _, record_id=record["id"]: toggle_project_selection(record_id))
        container = ft.Container(
            data=record["id"], padding=ft.Padding.all(12), border_radius=10,
            bgcolor="#131c33", tooltip=str(path),
            on_click=(lambda _, file=path: show_library_preview(file)) if exists else None,
            content=ft.Column(spacing=8, controls=[
                ft.Container(aspect_ratio=16 / 9, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                             border_radius=7, bgcolor="#1d3158",
                             content=ft.Stack([image,
                                               ft.Container(left=4, top=4, bgcolor="#99000000",
                                                            border_radius=8, content=selector)],
                                              fit=ft.StackFit.EXPAND)),
                ft.Text(label, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                        tooltip=label, weight=ft.FontWeight.W_600),
                ft.Text(format_size(record["size_on_disk"]) if exists else "Thiếu file MP4",
                        size=12, color=ft.Colors.BLUE_GREY_300 if exists else ft.Colors.AMBER_300),
            ]),
        )
        project_select_controls[record["id"]] = (selector, container)
        return container

    def render_project_grid() -> None:
        project = next((row for row in project_rows if row["id"] == selected_project_id), None)
        if project is None:
            project_grid.controls.clear()
            project_select_controls.clear()
            visible_project_ids.clear()
            update_project_selection()
            render_edit_grid()
            project_grid.visible = False
            project_empty.visible = True
            project_empty.value = "Chưa có project."
            return
        chapter = next((row for row in project_chapters.get(project["id"], [])
                        if row["id"] == selected_chapter_id), None)
        chapter_title.value = chapter["title"] if chapter else project["name"]
        visible_records = []
        seen_video_ids: set[int] = set()
        for row in project_entries:
            if selected_chapter_id is not None and row["chapter_id"] != selected_chapter_id:
                continue
            if row["id"] not in seen_video_ids:
                visible_records.append(row)
                seen_video_ids.add(row["id"])
        chapter_count.value = (f"{len(visible_records)} video · {len(project_chapters.get(project['id'], []))} chapter"
                               if chapter is None else f"{len(visible_records)} video trong chapter")
        project_select_controls.clear()
        visible_project_ids.clear()
        visible_project_ids.update(row["id"] for row in visible_records)
        project_grid.controls[:] = [project_video_card(record) for record in visible_records]
        update_project_selection()
        render_edit_grid()
        project_grid.visible = bool(visible_records)
        project_empty.visible = not visible_records
        project_empty.value = ("Chapter này chưa có video. Gắn video từ Download hoặc Library."
                               if chapter else "Project này chưa có video. Gắn video từ Download hoặc Library.")

    def confirm_structure_delete(project_id: int, chapter_id: int | None = None) -> None:
        if structure_busy:
            return
        project = next((row for row in project_rows if row["id"] == project_id), None)
        chapter = next((row for row in project_chapters.get(project_id, [])
                        if row["id"] == chapter_id), None)
        if project is None or (chapter_id is not None and chapter is None):
            return
        kind = "chapter" if chapter_id is not None else "project"
        name = chapter["title"] if chapter else project["name"]
        warning = (f'Xóa chapter "{name}"? Video trong chapter sẽ chuyển về project '
                   "và vẫn còn trong Library. Không thể khôi phục chapter."
                   if chapter else
                   f'Xóa project "{name}" cùng các chapter? Video sẽ được gỡ khỏi project '
                   "nhưng MP4 vẫn còn trong Library. Không thể khôi phục project.")

        def cancel(_: ft.ControlEvent) -> None:
            page.pop_dialog()

        async def confirm(_: ft.ControlEvent) -> None:
            nonlocal structure_busy, selected_project_id, selected_chapter_id
            page.pop_dialog()
            if structure_busy:
                return
            structure_busy = True
            project_feedback.value = f"Đang xóa {kind}..."
            project_feedback.color = ft.Colors.BLUE_200
            render_project_sidebar()
            page.update()
            try:
                if chapter_id is None:
                    await asyncio.to_thread(database.delete_project, project_id)
                    expanded_projects.discard(project_id)
                    if selected_project_id == project_id:
                        selected_project_id = None
                        selected_chapter_id = None
                else:
                    await asyncio.to_thread(database.delete_chapter, project_id, chapter_id)
                    if selected_project_id == project_id and selected_chapter_id == chapter_id:
                        selected_chapter_id = None
                project_entry_cache.pop(project_id, None)
                if await refresh_projects():
                    project_feedback.value = f"Đã xóa {kind} \"{name}\". Video vẫn ở Library."
                    project_feedback.color = ft.Colors.GREEN_300
            except (ValueError, sqlite3.Error) as exc:
                project_feedback.value = str(exc)
                project_feedback.color = ft.Colors.RED_300
            finally:
                structure_busy = False
                render_project_sidebar()
                page.update()

        page.show_dialog(ft.AlertDialog(
            modal=True, title=f"Xác nhận xóa {kind}", content=ft.Text(warning),
            actions=[ft.TextButton("Hủy", on_click=cancel),
                     ft.FilledButton(f"Xóa {kind}", icon=ft.Icons.DELETE_OUTLINE,
                                     on_click=confirm)],
        ))

    def render_project_sidebar() -> None:
        project_list.controls.clear()
        if not project_rows:
            project_list.controls.append(ft.Text("Chưa có project.", color=ft.Colors.BLUE_GREY_300))
        for project in project_rows:
            project_id = project["id"]
            expanded = project_id in expanded_projects

            def toggle(_: ft.ControlEvent, row_id: int = project_id) -> None:
                if row_id in expanded_projects:
                    expanded_projects.remove(row_id)
                else:
                    expanded_projects.add(row_id)
                render_project_sidebar()
                page.update()

            async def on_select(_: ft.ControlEvent, row_id: int = project_id) -> None:
                await select_project(row_id)

            def on_delete_project(_: ft.ControlEvent, row_id: int = project_id) -> None:
                confirm_structure_delete(row_id)

            header = ft.Row(spacing=2, controls=[
                ft.IconButton(icon=ft.Icons.KEYBOARD_ARROW_DOWN if expanded else ft.Icons.KEYBOARD_ARROW_RIGHT,
                              tooltip="Thu gọn chapter" if expanded else "Mở chapter", on_click=toggle),
                ft.Container(expand=True, padding=ft.Padding.only(top=7, bottom=7, right=5),
                             on_click=on_select, content=ft.Column(spacing=2, controls=[
                                 ft.Text(project["name"], max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                                         tooltip=project["name"], weight=ft.FontWeight.W_600),
                                 ft.Text(f"{project['chapter_count']} chapter", size=11,
                                         color=ft.Colors.BLUE_GREY_300),
                             ])),
                ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip="Xóa project",
                              icon_color=ft.Colors.RED_300, disabled=structure_busy,
                              on_click=on_delete_project),
            ])
            children: list[ft.Control] = []
            if expanded:
                for chapter in project_chapters.get(project_id, []):
                    async def on_chapter(_: ft.ControlEvent, row_id: int = project_id,
                                         chapter_id: int = chapter["id"]) -> None:
                        await select_project(row_id, chapter_id)

                    def on_delete_chapter(_: ft.ControlEvent, row_id: int = project_id,
                                          chapter_id: int = chapter["id"]) -> None:
                        confirm_structure_delete(row_id, chapter_id)

                    children.append(ft.Container(
                        data=("chapter", chapter["id"]),
                        margin=ft.Margin.only(left=35, right=5, top=2),
                        padding=ft.Padding.all(8), border_radius=7,
                        bgcolor="#20335a" if (project_id == selected_project_id
                                              and chapter["id"] == selected_chapter_id) else "#17223a",
                        on_click=on_chapter,
                        content=ft.Row(spacing=2, controls=[
                            ft.Container(expand=True,
                                         content=ft.Text(chapter["title"], max_lines=2,
                                                         overflow=ft.TextOverflow.ELLIPSIS,
                                                         tooltip=chapter["title"])),
                            ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip="Xóa chapter",
                                          icon_color=ft.Colors.RED_300, disabled=structure_busy,
                                          on_click=on_delete_chapter),
                        ]),
                    ))
            project_list.controls.append(ft.Column(data=project_id, spacing=2, controls=[
                ft.Container(padding=ft.Padding.only(right=4), border_radius=8,
                             bgcolor="#20335a" if (project_id == selected_project_id
                                                  and selected_chapter_id is None) else "#17223a",
                             content=header),
                *children,
            ]))

    async def select_project(project_id: int, chapter_id: int | None = None,
                             force_reload: bool = False) -> None:
        nonlocal selected_project_id, selected_chapter_id, selection_version
        nonlocal project_entries
        project = next((row for row in project_rows if row["id"] == project_id), None)
        if project is None:
            return
        chapters = project_chapters.get(project_id, [])
        if chapter_id is not None and not any(row["id"] == chapter_id for row in chapters):
            return
        selected_project_id = project_id
        selected_chapter_id = chapter_id
        expanded_projects.add(project_id)
        selection_version += 1
        version = selection_version
        chapter_name.disabled = False
        chapter_create.disabled = False
        render_project_sidebar()
        if force_reload or project_id not in project_entry_cache:
            chapter_title.value = next((row["title"] for row in chapters if row["id"] == chapter_id),
                                       project["name"])
            chapter_count.value = "Đang nạp video..."
            project_grid.controls.clear()
            project_select_controls.clear()
            visible_project_ids.clear()
            update_project_selection()
            project_grid.visible = False
            project_empty.visible = False
            page.update()
            try:
                entries = await asyncio.to_thread(load_project_entries, database, project_id)
            except (OSError, sqlite3.Error):
                if version == selection_version:
                    chapter_count.value = "Không thể nạp video. Hãy chọn lại project để thử lại."
                    page.update()
                return
            if version != selection_version:
                return
            project_entry_cache[project_id] = entries
        project_entries = project_entry_cache[project_id]
        try:
            await reload_edit_scope(project_id, chapter_id)
        except sqlite3.Error:
            edit_sequences[(project_id, chapter_id)] = []
            edit_trim_rows[(project_id, chapter_id)] = {}
            edit_status.value = "Không thể nạp danh sách video Edit."
            edit_status.color = ft.Colors.RED_300
        render_project_grid()
        page.update()

    async def refresh_project_sidebar() -> None:
        nonlocal project_rows, project_loaded
        projects, chapters = await asyncio.gather(
            asyncio.to_thread(database.list_projects),
            asyncio.to_thread(database.list_all_chapters),
        )
        project_rows = projects
        project_chapters.clear()
        for chapter in chapters:
            project_chapters.setdefault(chapter["project_id"], []).append(chapter)
        project_loaded = True
        render_project_sidebar()
        page.update()

    async def refresh_projects(preferred_id: int | None = None) -> bool:
        nonlocal selected_project_id, selected_chapter_id, selection_version, project_loaded
        project_feedback.value = "Đang tải project..."
        project_feedback.color = ft.Colors.BLUE_200
        page.update()
        try:
            await refresh_project_sidebar()
            ids = {row["id"] for row in project_rows}
            target = preferred_id if preferred_id in ids else (
                selected_project_id if selected_project_id in ids else (project_rows[0]["id"] if project_rows else None))
            if target is None:
                selected_project_id = None
                selected_chapter_id = None
                project_entries.clear()
                selection_version += 1
                chapter_title.value = "Chọn hoặc tạo project"
                chapter_count.value = "Các chapter của project sẽ xuất hiện ở đây."
                chapter_name.disabled = True
                chapter_create.disabled = True
                render_project_grid()
            else:
                chapter_id = (selected_chapter_id if target == selected_project_id and
                              any(row["id"] == selected_chapter_id
                                  for row in project_chapters.get(target, [])) else None)
                await select_project(target, chapter_id, force_reload=True)
            project_feedback.value = ""
        except sqlite3.Error:
            project_loaded = False
            project_feedback.value = "Không thể nạp danh sách project."
            project_feedback.color = ft.Colors.RED_300
            page.update()
            return False
        page.update()
        return True

    async def on_create_project(_: ft.ControlEvent) -> None:
        if structure_busy:
            return
        name = project_name.value.strip()
        project_create.disabled = True
        project_feedback.value = "Đang tạo project..."
        project_feedback.color = ft.Colors.BLUE_200
        page.update()
        try:
            project_id = await asyncio.to_thread(database.create_project, name)
            project_name.value = ""
            await refresh_projects(preferred_id=project_id)
        except ValueError as exc:
            project_feedback.value = str(exc)
            project_feedback.color = ft.Colors.RED_300
        except sqlite3.Error:
            project_feedback.value = "Không thể lưu project."
            project_feedback.color = ft.Colors.RED_300
        finally:
            project_create.disabled = False
            page.update()

    async def on_create_chapter(_: ft.ControlEvent) -> None:
        if structure_busy:
            return
        project_id = selected_project_id
        if project_id is None:
            return
        chapter_create.disabled = True
        chapter_feedback.value = "Đang thêm chapter..."
        chapter_feedback.color = ft.Colors.BLUE_200
        page.update()
        try:
            new_chapter_id = await asyncio.to_thread(database.create_chapter, project_id, chapter_name.value)
            chapter_name.value = ""
            chapter_feedback.value = ""
            await refresh_project_sidebar()
            if selected_project_id == project_id:
                await select_project(project_id, new_chapter_id)
        except ValueError as exc:
            chapter_feedback.value = str(exc)
            chapter_feedback.color = ft.Colors.RED_300
        except sqlite3.Error:
            chapter_feedback.value = "Không thể lưu chapter."
            chapter_feedback.color = ft.Colors.RED_300
        finally:
            chapter_create.disabled = selected_project_id is None
            page.update()

    project_name.on_submit = on_create_project
    project_create.on_click = on_create_project
    chapter_name.on_submit = on_create_chapter
    chapter_create.on_click = on_create_chapter
    project_sidebar = ft.Container(
        width=260, bgcolor="#10182c", padding=ft.Padding.all(12),
        content=ft.Column(expand=True, spacing=12, controls=[
            ft.Text("Projects", size=20, weight=ft.FontWeight.BOLD),
            project_name, project_create, project_feedback,
            ft.Divider(color="#263250"), project_list,
        ]),
    )
    chapter_tabs = ft.Tabs(
        length=3,
        expand=True,
        content=ft.Column(expand=True, spacing=12, controls=[
            ft.TabBar(
                tabs=[ft.Tab(label="Video stock"), ft.Tab(label="Trim Video"),
                      ft.Tab(label="Order Video")],
                scrollable=False,
                indicator_color=ft.Colors.CYAN_300,
                label_color=ft.Colors.CYAN_200,
                unselected_label_color=ft.Colors.BLUE_GREY_300,
                divider_color="#263250",
            ),
            ft.TabBarView(expand=True, controls=[
                ft.Column(expand=True, spacing=12, controls=[
                    ft.Row([project_select_all, project_selected_count, project_delete,
                            project_save, project_move, edit_add_stock], spacing=4),
                    project_action_status, project_empty, project_grid,
                ]),
                ft.Column(expand=True, spacing=12, controls=[
                    ft.Row([edit_select_all, edit_selected_count, edit_delete], spacing=4),
                    edit_status, edit_empty, edit_grid,
                ]),
                ft.Column(expand=True, spacing=10, controls=[
                    ft.Row([ft.Text("Kho video trim", weight=ft.FontWeight.W_600, expand=True),
                            ft.Text("Thứ tự tải về", weight=ft.FontWeight.W_600, expand=True),
                            order_export], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    order_status,
                    ft.Row(expand=True, spacing=12, controls=[
                        ft.Container(expand=True, padding=ft.Padding.all(8),
                                     bgcolor="#10182c", border_radius=10,
                                     content=ft.DragTarget(
                                         group="order-video", expand=True, on_accept=accept_order_back,
                                         content=ft.Column(expand=True, spacing=8, controls=[
                                             ft.Text("Kéo video từ danh sách phải về đây để gỡ khỏi thứ tự.",
                                                     size=11, color=ft.Colors.BLUE_GREY_300),
                                             order_pool,
                                         ]))),
                        ft.Container(width=1, bgcolor="#263250"),
                        ft.Container(expand=True, padding=ft.Padding.all(8),
                                     bgcolor="#10182c", border_radius=10,
                                     content=ft.Column(expand=True, spacing=8, controls=[
                                         ft.Text("Kéo video vào vùng thả hoặc vào một vị trí trong danh sách.",
                                                 size=11, color=ft.Colors.BLUE_GREY_300),
                                         order_list,
                                     ])),
                    ]),
                ]),
            ]),
        ]),
    )
    project_view = ft.Row(expand=True, spacing=0, controls=[
        project_sidebar,
        ft.Container(width=1, bgcolor="#263250"),
        ft.Container(expand=True, padding=ft.Padding.all(20), content=ft.Column(
            expand=True, spacing=12, controls=[
                chapter_title, chapter_count,
                ft.Row([chapter_name, chapter_create], vertical_alignment=ft.CrossAxisAlignment.END),
                chapter_feedback, ft.Divider(color="#263250"),
                chapter_tabs,
            ],
        )),
    ])

    def action_message(section: str, message: str, error: bool = False) -> None:
        label = {"download": status, "library": library_action_status,
                 "project": project_action_status}[section]
        label.value = message
        label.color = ft.Colors.RED_300 if error else ft.Colors.BLUE_200
        page.update()

    def set_action_busy(busy: bool) -> None:
        nonlocal action_busy
        action_busy = busy
        update_download_selection()
        update_library_selection()
        update_project_selection()
        update_edit_selection()
        render_order_layout()

    async def selected_records(section: str) -> tuple[list[int], list[dict]]:
        selections = {"download": selected_download, "library": selected_library,
                      "project": selected_project_videos}
        ids = sorted(selections[section])
        if section == "download":
            source_ids = [(download_videos[video_id].source, video_id)
                          for video_id in ids if video_id in download_videos]
            records = await asyncio.to_thread(database.source_video_records, source_ids)
        else:
            records = await asyncio.to_thread(database.video_records, ids)
        return ids, records

    async def export_selected(section: str) -> None:
        ids, records = await selected_records(section)
        paths = [Path(row["file_path"]) for row in records if Path(row["file_path"]).is_file()]
        if not paths:
            action_message(section, "Video đã chọn chưa có file MP4 hoàn chỉnh để xuất.", error=True)
            return
        if len(paths) > 1:
            try:
                destination = await file_picker.save_file(
                    dialog_title="Lưu video đã chọn thành ZIP", file_name="stock-videos.zip",
                    file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=["zip"],
                )
            except Exception:
                action_message(section, "Không mở được hộp chọn nơi lưu ZIP.", error=True)
                return
            if not destination:
                return
            archive_path = Path(destination)
            if archive_path.suffix.lower() != ".zip":
                archive_path = archive_path.with_suffix(".zip")
            set_action_busy(True)
            action_message(section, "Đang tạo file ZIP...")
            try:
                archived, skipped = await asyncio.to_thread(export_videos_zip, paths, archive_path)
                unavailable = len(ids) - len(paths)
                action_message(section, f"Đã tạo ZIP gồm {archived} video; bỏ qua "
                                        f"{skipped + unavailable} video thiếu file hoặc không đọc được.")
            except FileExistsError:
                action_message(section, "File ZIP đã tồn tại. Hãy chọn tên khác.", error=True)
            except (OSError, ValueError, zipfile.BadZipFile):
                action_message(section, "Không thể tạo file ZIP tại vị trí đã chọn.", error=True)
            finally:
                set_action_busy(False)
            return
        try:
            destination = await file_picker.get_directory_path(dialog_title="Chọn thư mục xuất video")
        except Exception:
            action_message(section, "Không mở được hộp chọn thư mục.", error=True)
            return
        if not destination:
            return
        set_action_busy(True)
        action_message(section, "Đang sao chép video...")
        try:
            copied, skipped = await asyncio.to_thread(export_videos, paths, Path(destination))
            unavailable = len(ids) - len(paths)
            action_message(section, f"Đã xuất {copied} video; bỏ qua {skipped + unavailable} "
                                    "video thiếu file hoặc trùng tên ở thư mục đích.")
        except (OSError, ValueError):
            action_message(section, "Không thể sao chép video vào thư mục đã chọn.", error=True)
        finally:
            set_action_busy(False)

    async def delete_selected(section: str) -> None:
        selections = {"download": selected_download, "library": selected_library,
                      "project": selected_project_videos}
        ids = sorted(selections[section])
        if not ids:
            return

        def close_dialog(_: ft.ControlEvent) -> None:
            page.pop_dialog()

        async def confirm(_: ft.ControlEvent) -> None:
            nonlocal project_loaded
            page.pop_dialog()
            set_action_busy(True)
            action_message(section, "Đang xóa vĩnh viễn video...")
            try:
                _, records = await selected_records(section)
                removed, failed = await asyncio.to_thread(
                    delete_videos, database, records, (LIBRARY_ROOT, ROOT / "downloads"))
                removed_ids = set(removed)
                discard = {row["video_id"] for row in records if row["id"] in removed_ids}
                if section == "download":
                    failed_video_ids = {row["video_id"] for row in records if row["id"] in failed}
                    discard.update(set(ids) - failed_video_ids)
                grid.controls[:] = [control for control in grid.controls if control.data not in discard]
                download_list.controls[:] = [control for control in download_list.controls
                                             if control.data not in discard]
                for video_id in discard:
                    download_videos.pop(video_id, None)
                    download_select_controls.pop(video_id, None)
                    cards.pop(video_id, None)
                selected_download.difference_update(discard)
                selected_project_videos.difference_update(removed_ids)
                library_deleted_records.update(
                    (row["id"], row["file_path"], row["created_at"])
                    for row in records if row["id"] in removed_ids)
                project_loaded = False
                project_entry_cache.clear()
                remove_library_results(removed_ids)
                if current_view == "project" and selected_project_id is not None:
                    await select_project(selected_project_id, selected_chapter_id, force_reload=True)
                pending = len(ids) - len(records)
                if pending and section == "download":
                    action_message(section, f"Đã xóa vĩnh viễn {len(removed)} video; "
                                            f"bỏ {pending} video chưa tải khỏi kết quả. "
                                            f"{len(failed)} video không xóa được.")
                else:
                    action_message(section, f"Đã xóa vĩnh viễn {len(removed)} video. "
                                            f"{len(failed) + pending} video không xóa được.")
            except (OSError, sqlite3.Error):
                action_message(section, "Không thể xóa video đã chọn.", error=True)
            finally:
                set_action_busy(False)

        dialog = ft.AlertDialog(
            modal=True,
            title="Xóa vĩnh viễn video đã chọn?",
            content=ft.Text(f"Xóa vĩnh viễn file và dữ liệu của {len(ids)} video? "
                            "Không thể khôi phục từ thùng rác; ID Pixabay có thể được tải lại." +
                            (" Video chưa tải sẽ chỉ biến mất khỏi kết quả hiện tại."
                             if section == "download" else "")),
            actions=[ft.TextButton("Hủy", on_click=close_dialog),
                     ft.FilledButton("Xóa vĩnh viễn", icon=ft.Icons.DELETE_OUTLINE,
                                     on_click=confirm)],
        )
        page.show_dialog(dialog)

    async def move_selected(section: str) -> None:
        nonlocal project_loaded
        ids, records = await selected_records(section)
        ready = [row for row in records if Path(row["file_path"]).is_file()]
        if not ready:
            action_message(section, "Video đã chọn chưa có file MP4 hoàn chỉnh để gắn vào Project.", error=True)
            return
        projects, chapters = await asyncio.gather(
            asyncio.to_thread(database.list_projects),
            asyncio.to_thread(database.list_all_chapters),
        )
        if not projects:
            action_message(section, "Chưa có project. Hãy tạo project trong mục Project trước.", error=True)
            return
        project_names = {row["id"]: row["name"] for row in projects}
        chapter_names = {row["id"]: row["title"] for row in chapters}
        first_id = (selected_project_id if section == "project" and
                    selected_project_id in project_names else projects[0]["id"])
        project_choice = ft.Dropdown(label="Project", value=str(first_id),
            options=[ft.DropdownOption(key=str(row["id"]), text=row["name"])
                     for row in projects])

        def chapter_options(project_id: int) -> list[ft.DropdownOption]:
            return [ft.DropdownOption(key="root", text="In project"),
                    *[ft.DropdownOption(key=str(row["id"]), text=row["title"])
                      for row in chapters if row["project_id"] == project_id]]

        chapter_choice = ft.Dropdown(label="Chapter", value="root",
                                     options=chapter_options(first_id), expand=True)
        destinations: list[tuple[int, int | None]] = []
        selected_label = ft.Text("Vị trí đã thêm", size=12,
                                 color=ft.Colors.BLUE_GREY_300, visible=False)
        selected_list = ft.Column(spacing=4, height=0, scroll=ft.ScrollMode.AUTO,
                                  visible=False)
        message = ft.Text("", size=12, color=ft.Colors.RED_300)

        def current_destination() -> tuple[int, int | None]:
            project_id = int(project_choice.value)
            chapter_id = None if chapter_choice.value == "root" else int(chapter_choice.value)
            return project_id, chapter_id

        def render_destinations() -> None:
            selected_label.visible = bool(destinations)
            selected_list.visible = bool(destinations)
            selected_list.height = min(120, 44 * len(destinations))
            selected_list.controls.clear()
            for destination in destinations:
                project_id, chapter_id = destination
                label = (f"{project_names[project_id]} / " +
                         (chapter_names[chapter_id] if chapter_id is not None else "In project"))

                def remove(_: ft.ControlEvent, target: tuple[int, int | None] = destination) -> None:
                    destinations.remove(target)
                    render_destinations()
                    page.update()

                selected_list.controls.append(ft.Row(spacing=4, controls=[
                    ft.Text(label, expand=True, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS, tooltip=label),
                    ft.IconButton(icon=ft.Icons.CLOSE, tooltip="Bỏ vị trí", on_click=remove),
                ]))

        def change_project(_: ft.ControlEvent) -> None:
            try:
                project_id = int(project_choice.value)
            except (TypeError, ValueError):
                return
            chapter_choice.options = chapter_options(project_id)
            chapter_choice.value = "root"
            page.update()

        project_choice.on_select = change_project

        def add_destination(_: ft.ControlEvent) -> None:
            try:
                destination = current_destination()
            except (TypeError, ValueError):
                message.value = "Hãy chọn project và chapter."
                page.update()
                return
            if destination not in destinations:
                destinations.append(destination)
            message.value = ""
            render_destinations()
            page.update()

        add_button = ft.IconButton(icon=ft.Icons.ADD, tooltip="Thêm vị trí",
                                   on_click=add_destination)

        def close_dialog(_: ft.ControlEvent) -> None:
            page.pop_dialog()

        async def apply_move(_: ft.ControlEvent) -> None:
            nonlocal project_loaded
            try:
                targets = destinations.copy() if destinations else [current_destination()]
            except (TypeError, ValueError):
                message.value = "Hãy chọn project và chapter."
                page.update()
                return
            try:
                added = await asyncio.to_thread(database.assign_videos_to_destinations,
                                                [row["id"] for row in ready], targets)
            except (ValueError, sqlite3.Error) as exc:
                message.value = str(exc)
                page.update()
                return
            page.pop_dialog()
            project_loaded = False
            project_entry_cache.clear()
            if section == "project" and selected_project_id is not None:
                await select_project(selected_project_id, selected_chapter_id, force_reload=True)
            existing = len(ready) * len(targets) - added
            action_message(section, f"Đã thêm {added} liên kết video; {existing} liên kết đã có; "
                                    f"{len(ids) - len(ready)} video thiếu file được bỏ qua.")

        dialog = ft.AlertDialog(
            modal=True, title="Gắn video vào Project/Chapter",
            content=ft.Container(width=400, content=ft.Column(tight=True, spacing=10,
                controls=[project_choice,
                          ft.Row([chapter_choice, add_button]),
                          ft.Text("Bấm + để thêm nhiều vị trí; nếu không, dùng vị trí đang chọn.",
                                  size=12, color=ft.Colors.BLUE_GREY_300),
                          selected_label, selected_list, message])),
            actions=[ft.TextButton("Hủy", on_click=close_dialog),
                     ft.FilledButton("Gắn video", icon=ft.Icons.DRIVE_FILE_MOVE_OUTLINE,
                                     on_click=apply_move)],
        )
        page.show_dialog(dialog)

    async def export_download(_: ft.ControlEvent) -> None:
        await export_selected("download")

    async def export_library(_: ft.ControlEvent) -> None:
        await export_selected("library")

    async def delete_download(_: ft.ControlEvent) -> None:
        await delete_selected("download")

    async def delete_library(_: ft.ControlEvent) -> None:
        await delete_selected("library")

    async def move_download(_: ft.ControlEvent) -> None:
        await move_selected("download")

    async def move_library(_: ft.ControlEvent) -> None:
        await move_selected("library")

    async def export_project(_: ft.ControlEvent) -> None:
        await export_selected("project")

    async def delete_project(_: ft.ControlEvent) -> None:
        await delete_selected("project")

    async def move_project(_: ft.ControlEvent) -> None:
        await move_selected("project")

    download_save.on_click = export_download
    library_save.on_click = export_library
    download_delete.on_click = delete_download
    library_delete.on_click = delete_library
    download_move.on_click = move_download
    library_move.on_click = move_library
    project_save.on_click = export_project
    project_delete.on_click = delete_project
    project_move.on_click = move_project
    def script_download_complete():
        nonlocal library_loaded
        library_loaded = False

    script = ScriptWorkspace(page, file_picker, ROOT / "data", database,
                             LIBRARY_ROOT, script_download_complete, show_library_preview,
                             video_preferences)
    def on_disconnect(_):
        if cancel_event is not None:
            cancel_event.set()
        script.cancel_active()

    page.on_disconnect = on_disconnect
    views = {"dashboard": dashboard_view, "download": download_view, "library": library_view,
             "project": project_view, "setup": setup_view, "script": script.control}
    view_panels = {name: ft.Container(content=view, visible=name == "download") for name, view in views.items()}
    content_area = ft.Container(expand=True, padding=ft.Padding.all(20),
                                content=ft.Stack(controls=list(view_panels.values()), fit=ft.StackFit.EXPAND))
    current_view = "download"
    nav_buttons: dict[str, ft.IconButton] = {}

    async def change_view(name: str) -> None:
        nonlocal current_view
        if name == current_view:
            return
        if current_view == "script":
            await script.stop_playback()
        view_panels[current_view].visible = False
        view_panels[name].visible = True
        current_view = name
        for key, button in nav_buttons.items():
            button.bgcolor = "#20335a" if key == name else None
        page.update()
        if name == "library":
            await refresh_library()
        elif name == "dashboard":
            await refresh_dashboard()
        elif name == "project" and not project_loaded:
            await refresh_projects()
        elif name == "script":
            await script.load()

    def nav_button(name: str, label: str, icon: ft.IconData, disabled: bool = False) -> ft.IconButton:
        button = ft.IconButton(
            icon=icon,
            disabled=disabled,
            tooltip=label,
            width=56,
            height=48,
        )
        if not disabled:
            nav_buttons[name] = button
            async def on_navigate(_: ft.ControlEvent) -> None:
                await change_view(name)
            button.on_click = on_navigate
        return button

    download_button = nav_button("download", "Download", ft.Icons.DOWNLOAD)
    download_button.bgcolor = "#20335a"
    sidebar = ft.Container(
        width=72,
        bgcolor="#10182c",
        padding=ft.Padding.all(8),
        content=ft.Column(expand=True, controls=[
            ft.Container(height=48, alignment=ft.Alignment(0, 0), tooltip="Stock Downloader",
                         content=ft.Image(src=SIDEBAR_LOGO, width=38, height=38,
                                          fit=ft.BoxFit.CONTAIN)),
            nav_button("dashboard", "Dashboard", ft.Icons.DASHBOARD_OUTLINED),
            download_button,
            nav_button("library", "Library", ft.Icons.VIDEO_LIBRARY_OUTLINED),
            nav_button("project", "Project", ft.Icons.FOLDER_OUTLINED),
            nav_button("script", "Script", ft.Icons.DESCRIPTION_OUTLINED),
            ft.Container(expand=True),
            ft.Divider(color="#263250"),
            nav_button("setup", "Setup", ft.Icons.SETTINGS_OUTLINED),
        ]),
    )
    page.add(
        ft.Row(expand=True, spacing=0, controls=[
            sidebar,
            ft.Container(width=1, bgcolor="#263250"),
            content_area,
        ]),
    )


if __name__ == "__main__":
    from services.diagnostics import enable_logging
    enable_logging()
    ft.run(main, assets_dir=str(ASSETS_ROOT))
