"""Download one reserved stock video for a script scene."""
from pathlib import Path
import re
from threading import Event

from services.catalog import select_unique
from services.database import VideoDatabase
from services.downloader import download_many, footage_filename, safe_folder_name
from services.pexels import select_unique as select_pexels_unique
from services.pexels_browser import search_videos as search_pexels_videos_direct
from services.pexels_link import resolve_pexels_video
from services.pexels_verified import download_matching_pexels_videos


def video_id_from_clip(clip: str) -> int | None:
    value = str(clip or "").strip()
    match = re.search(r"(?:^|[/\\])(?:.*_)?(\d+)_\d+x\d+\.mp4(?:[?#].*)?$", value, re.IGNORECASE)
    if not match:
        match = re.search(r"(?:-|/)(\d+)/?(?:[?#].*)?$", value)
    return int(match[1]) if match else None


def download_scene(keyword: str, api_key: str, root: Path, database: VideoDatabase, cancel: Event,
                   report=None, previous_clip: str = "", quality: str = "4K",
                   orientation: str = "landscape", beat_id: str = "",
                   visual_direction: str = "", source: str = "pixabay",
                   exclude_ids: set[int] | None = None) -> str:
    if not keyword.strip():
        raise ValueError("Primary search keyword Ä‘ang trá»‘ng.")
    if cancel.is_set():
        raise InterruptedError("ÄÃ£ há»§y táº£i.")
    with database.batch() as owner:
        # Keep the current clip out of search results when retrying a completed row.
        excluded = set(exclude_ids or ())
        previous_id = video_id_from_clip(previous_clip)
        if previous_id is not None:
            excluded.add(previous_id)
        label = "_".join(part for part in (beat_id, visual_direction) if part.strip())
        if beat_id.strip() and not beat_id.strip().upper().startswith("VB"):
            label = "VB" + label
        safe_label = safe_folder_name(label) if label else ""
        if source == "pexels":
            videos = select_pexels_unique(api_key, keyword.strip(), 1, root, database, owner, cancel,
                                          quality=quality, orientation=orientation,
                                          exclude_ids=excluded)
        else:
            videos = select_unique(api_key, keyword.strip(), 1, root, database, owner, cancel,
                                   exclude_ids=excluded, quality=quality, orientation=orientation,
                                   filename_prefix=safe_label)
        if cancel.is_set():
            raise InterruptedError("ÄÃ£ há»§y táº£i.")
        if not videos:
            raise ValueError(f"KhÃ´ng cÃ²n video má»›i phÃ¹ há»£p trÃªn {source.title()}.")
        events = []

        def progress(event):
            if event.state in {"done", "error", "cancelled"}:
                events.append(event)
            if report:
                report(event)

        file_prefix = f"{safe_label}_{videos[0].id}" if safe_label else ""
        destination = download_many(videos, root, keyword.strip(), cancel, progress, database, owner,
                                    file_prefix)
        video = videos[0]
        result = next((e for e in reversed(events) if e.state in {"done", "error", "cancelled"}), None)
        if result is None or result.state != "done":
            if cancel.is_set():
                raise InterruptedError("ÄÃ£ há»§y táº£i.")
            raise OSError(result.message if result else "Táº£i video khÃ´ng hoÃ n táº¥t.")
        path = destination / footage_filename(video, file_prefix)
        if not path.is_file() or not path.stat().st_size:
            raise OSError("KhÃ´ng tÃ¬m tháº¥y file video Ä‘Ã£ táº£i.")
        database.set_file_path(video.id, path, source=video.source, owner=owner)
        return str(path.resolve())


def download_scene_direct_pexels(keyword: str, root: Path, database: VideoDatabase, cancel: Event,
                                 report=None, orientation: str = "landscape",
                                 exclude_ids: set[int] | None = None) -> str:
    """Find and download one public Pexels video without using an API key."""
    term = keyword.strip()
    if not term:
        raise ValueError("Primary search keyword đang trống.")
    if cancel.is_set():
        raise InterruptedError("Đã hủy tải.")

    attempted_ids = set(exclude_ids or ())
    accepted = []
    with database.batch() as owner:
        def process_candidates(candidates):
            fresh = [video for video in candidates if video.id not in attempted_ids]
            attempted_ids.update(video.id for video in fresh)
            if not fresh or cancel.is_set():
                return cancel.is_set()
            matching, _, _, _ = download_matching_pexels_videos(
                fresh, 1, root, term, orientation, cancel, database, owner,
                report or (lambda _event: None),
            )
            accepted.extend(matching)
            return bool(accepted) or cancel.is_set()

        search_pexels_videos_direct(
            term, 1, cancel, exclude_ids=attempted_ids, on_batch=process_candidates,
        )

    if cancel.is_set():
        raise InterruptedError("Đã hủy tải.")
    if not accepted:
        raise ValueError("Không tìm thấy video Pexels mới phù hợp với hướng đã chọn.")
    path = Path(root) / safe_folder_name(term) / footage_filename(accepted[0])
    if not path.is_file() or not path.stat().st_size:
        raise OSError("Không tìm thấy file video Pexels đã tải.")
    return str(path.resolve())


def download_pexels_link(url: str, root: Path, database: VideoDatabase, cancel: Event,
                         report=None, orientation: str = "all") -> str:
    """Download one exact public Pexels link and return its local Library path."""
    video = resolve_pexels_video(url)
    if cancel.is_set():
        raise InterruptedError("Đã hủy tải.")

    with database.connect() as connection:
        existing = connection.execute(
            "SELECT file_path FROM videos WHERE source=? AND video_id=? "
            "AND status IN ('completed','review') AND hidden_at IS NULL",
            (video.source, video.id),
        ).fetchone()
    if existing:
        existing_path = Path(existing["file_path"])
        if existing_path.is_file() and existing_path.stat().st_size:
            return str(existing_path.resolve())

    with database.batch() as owner:
        accepted, _, rejected, skipped = download_matching_pexels_videos(
            [video], 1, root, video.tags, orientation, cancel, database, owner,
            report or (lambda _event: None),
        )
    if cancel.is_set():
        raise InterruptedError("Đã hủy tải.")
    if not accepted:
        if rejected:
            raise ValueError("Video trong link không đúng hướng đã chọn.")
        if skipped:
            raise ValueError("Video này đã có trong Library nhưng không tìm thấy file cục bộ.")
        raise OSError("Không tải được video từ link Pexels.")
    path = Path(root) / safe_folder_name(video.tags) / footage_filename(accepted[0])
    if not path.is_file() or not path.stat().st_size:
        raise OSError("Không tìm thấy file video Pexels đã tải.")
    return str(path.resolve())
