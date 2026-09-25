from __future__ import annotations

import asyncio
import os
import sqlite3
import zipfile
from pathlib import Path
from threading import Event

import flet as ft
import flet_video as ftv

from app_config import APP_VERSION, DATA_ROOT, RESOURCE_ROOT
from services.api_keys import get_api_key_pool
from api_key_settings import ApiKeySettings
from models import DownloadEvent, Video
from script_view import ScriptView
from video_settings import build_video_settings
from services.assets import delete_videos, export_videos, export_videos_zip
from services.catalog import select_unique
from services.database import VideoDatabase
from services.downloader import download_many
from services.pixabay import PixabayError, PixabayAccessPause
from services.thumbnails import is_jpeg, thumbnail_bytes

ROOT = DATA_ROOT
LIBRARY_ROOT = ROOT / "library"
ASSETS_ROOT = RESOURCE_ROOT / "assets"
WINDOW_ICON = ASSETS_ROOT / "stock-check.ico"
SIDEBAR_LOGO = "5166961.png"
PREVIEW_WIDTH = 1280
PREVIEW_HEIGHT = 720


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
    """All disk and image work for a Library refresh runs in a worker thread."""
    records = database.library()
    for record in records:
        file = Path(record["file_path"])
        try:
            record["size_on_disk"] = file.stat().st_size
            record["exists_on_disk"] = file.is_file()
        except OSError:
            record["size_on_disk"] = 0
            record["exists_on_disk"] = False
        record["poster_bytes"] = thumbnail_bytes(file) if record["exists_on_disk"] else None
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
    """Prefer a completed local MP4; fall back to the Pixabay stream while downloading."""
    filename = f"{video.id}_{video.width}x{video.height}.mp4"
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
            ft.Image(src=video.thumbnail, fit=ft.BoxFit.COVER, expand=True),
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
                    ft.Text(f"Video by {video.author} on Pixabay", height=20, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, size=12, color=ft.Colors.BLUE_GREY_300),
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

    keyword = ft.TextField(label="Keyword", hint_text="Ví dụ: Moscow", expand=True, autofocus=True)
    amount = ft.TextField(label="Số video", value="12", width=110, input_filter=ft.InputFilter(allow=True, regex_string=r"[0-9]"))
    start = ft.FilledButton("Tìm & tải", icon=ft.Icons.DOWNLOAD)
    cancel_button = ft.OutlinedButton("Hủy", icon=ft.Icons.CANCEL, disabled=True)
    resume_search = ft.OutlinedButton("Tiếp tục tìm kiếm", visible=False,
                                     on_click=lambda _: get_api_key_pool().resume())
    status = ft.Text("Nhập keyword và số lượng video cần tải.", color=ft.Colors.BLUE_200)
    overall = ft.ProgressBar(value=0, visible=False)
    grid = ft.GridView(expand=True, max_extent=350, child_aspect_ratio=0.95, spacing=14, run_spacing=14)
    download_list = ft.ListView(expand=True, spacing=8)
    download_results = ft.Container(expand=True, content=grid)
    download_mode = "grid"
    cancel_event: Event | None = None
    preview_dialog: ft.AlertDialog | None = None
    action_busy = False
    cards: dict[int, list[tuple[ft.ProgressBar, ft.Text]]] = {}
    download_videos: dict[int, Video] = {}
    selected_download: set[int] = set()
    download_select_controls: dict[int, list[tuple[ft.IconButton, ft.Container]]] = {}
    download_selected_count = ft.Text("0 đã chọn", size=12, color=ft.Colors.BLUE_GREY_300)
    video_preferences = {"quality": "2K", "orientation": "landscape"}
    download_video_settings = build_video_settings(video_preferences)
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
                            ft.Image(src=video.thumbnail, fit=ft.BoxFit.COVER, expand=True),
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
                            ft.Row([state, ft.TextButton("Pixabay", url=video.page_url, style=ft.ButtonStyle(padding=0))], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
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
        container = ft.Container(
            data=video.id,
            padding=ft.Padding.all(10),
            border_radius=10,
            bgcolor="#131c33",
            on_click=lambda _: show_preview(video),
            content=ft.Row(controls=[
                selector,
                ft.Container(width=160, aspect_ratio=16 / 9, clip_behavior=ft.ClipBehavior.ANTI_ALIAS, border_radius=7, content=ft.Image(src=video.thumbnail, fit=ft.BoxFit.COVER, expand=True)),
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
        term = keyword.value.strip()
        try:
            count = int(amount.value)
            if not term or not 1 <= count <= 200:
                raise ValueError
        except ValueError:
            status.value = "Keyword không được trống; số lượng phải từ 1 đến 200."
            status.color = ft.Colors.RED_300
            page.update()
            return

        start.disabled = True
        cancel_button.disabled = False
        cancel_event = Event()
        batch = database.batch()
        owner = batch.__enter__()
        status.value = "Đang tìm video trên Pixabay..."
        status.color = ft.Colors.BLUE_200
        overall.visible = True
        overall.value = None
        grid.controls.clear()
        download_list.controls.clear()
        cards.clear()
        download_videos.clear()
        selected_download.clear()
        download_select_controls.clear()
        update_download_selection()
        page.update()
        try:
            api_pool = get_api_key_pool()
            search_worker = asyncio.create_task(asyncio.to_thread(
                select_unique, api_pool, term, count, LIBRARY_ROOT, database, owner, cancel_event,
                quality=video_preferences["quality"],
                orientation=video_preferences["orientation"]))
            while not search_worker.done():
                await asyncio.wait({search_worker}, timeout=0.25)
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
            except (PixabayError, OSError, InterruptedError) as exc:
                resume_search.visible = False
                batch.__exit__(None, None, None)
                status.value = "Đã hủy." if cancel_event.is_set() else str(exc)
                status.color = ft.Colors.AMBER_300 if cancel_event.is_set() else ft.Colors.RED_300
                start.disabled = False
                cancel_button.disabled = True
                overall.visible = False
                page.update()
                return
        except (PixabayError, OSError) as exc:
            resume_search.visible = False
            batch.__exit__(None, None, None)
            status.value = str(exc)
            status.color = ft.Colors.RED_300
            start.disabled = False
            cancel_button.disabled = True
            overall.visible = False
            page.update()
            return
        if not videos:
            batch.__exit__(None, None, None)
            status.value = "Đã hủy." if cancel_event.is_set() else "Không còn video mới phù hợp trên Pixabay cho keyword này."
            status.color = ft.Colors.AMBER_300
            start.disabled = False
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

        worker = asyncio.create_task(asyncio.to_thread(download_many, videos, LIBRARY_ROOT, term,
                                                       cancel_event, report, database, owner))
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
            start.disabled = False
            cancel_button.disabled = True
            overall.visible = False
            update_download_selection()
            page.update()

    def cancel_download(_: ft.ControlEvent) -> None:
        if cancel_event:
            cancel_event.set()
            cancel_button.disabled = True
            status.value = "Đang hủy các video chưa hoàn tất..."
            page.update()

    start.on_click = begin_download
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
            ft.Text("Tìm video Pixabay và tải trực tiếp về máy.", color=ft.Colors.BLUE_GREY_300),
            ft.Row([keyword, amount, start, cancel_button, resume_search], vertical_alignment=ft.CrossAxisAlignment.END),
            download_video_settings,
            status,
            overall,
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

    setup_view = ft.Column(spacing=10, controls=[
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

    def on_library_search(_: ft.ControlEvent) -> None:
        render_library_results()

    library_search.on_change = on_library_search

    async def refresh_library(force: bool = False) -> None:
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
                records = await asyncio.to_thread(load_library_entries, database)
                library_records.clear()
                library_records.update((record["id"], record) for record in records)
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
    library_video_settings = build_video_settings(video_preferences)
    async def on_library_refresh(_: ft.ControlEvent) -> None:
        await refresh_library(force=True)

    library_view = ft.Column(expand=True, spacing=12, controls=[
        ft.Row([library_grid_button, library_list_button], alignment=ft.MainAxisAlignment.END, spacing=2),
        ft.Row([ft.Text("Library", size=28, weight=ft.FontWeight.BOLD), ft.IconButton(icon=ft.Icons.REFRESH, tooltip="Làm mới", on_click=on_library_refresh)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Row([library_count, library_search], vertical_alignment=ft.CrossAxisAlignment.CENTER),
        library_video_settings,
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
    project_view = ft.Row(expand=True, spacing=0, controls=[
        project_sidebar,
        ft.Container(width=1, bgcolor="#263250"),
        ft.Container(expand=True, padding=ft.Padding.all(20), content=ft.Column(
            expand=True, spacing=12, controls=[
                chapter_title, chapter_count,
                ft.Row([chapter_name, chapter_create], vertical_alignment=ft.CrossAxisAlignment.END),
                chapter_feedback, ft.Divider(color="#263250"),
                ft.Row([project_select_all, project_selected_count, project_delete,
                        project_save, project_move], spacing=4),
                project_action_status, project_empty, project_grid,
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

    async def selected_records(section: str) -> tuple[list[int], list[dict]]:
        selections = {"download": selected_download, "library": selected_library,
                      "project": selected_project_videos}
        ids = sorted(selections[section])
        records = await asyncio.to_thread(database.video_records, ids, section == "download")
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
            nonlocal library_loaded, project_loaded
            page.pop_dialog()
            set_action_busy(True)
            action_message(section, "Đang xóa vĩnh viễn video...")
            try:
                records = await asyncio.to_thread(database.video_records, ids, section == "download")
                removed, failed = await asyncio.to_thread(
                    delete_videos, database, records, (LIBRARY_ROOT, ROOT / "downloads"))
                removed_ids = set(removed)
                discard = {row["video_id"] for row in records
                           if row["id"] in removed_ids and row["source"] == "pixabay"}
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
                selected_library.difference_update(removed_ids)
                selected_project_videos.difference_update(removed_ids)
                library_loaded = False
                project_loaded = False
                project_entry_cache.clear()
                if current_view == "library":
                    await refresh_library(force=True)
                elif current_view == "project" and selected_project_id is not None:
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
            return [ft.DropdownOption(key="root", text="Toàn project"),
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
                         (chapter_names[chapter_id] if chapter_id is not None else "Toàn project"))

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

    script = ScriptView(page, file_picker, ROOT / "data" / "script.json", database,
                        LIBRARY_ROOT, script_download_complete, show_library_preview,
                        video_preferences)
    def on_disconnect(_):
        if cancel_event is not None:
            cancel_event.set()
        script.cancel.set()

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
