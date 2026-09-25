"""Download one reserved Pixabay video for a script scene."""
from pathlib import Path
import re
from threading import Event

from services.catalog import select_unique
from services.database import VideoDatabase
from services.downloader import download_many


def download_scene(keyword: str, api_key: str, root: Path, database: VideoDatabase, cancel: Event,
                   report=None, previous_clip: str = "", quality: str = "4K",
                   orientation: str = "landscape") -> str:
    if not keyword.strip():
        raise ValueError("Primary search keyword đang trống.")
    if cancel.is_set():
        raise InterruptedError("Đã hủy tải.")
    with database.batch() as owner:
        # Also exclude the current clip when its database record no longer exists.
        previous_id = re.search(r"(?:^|[/\\])(\d+)_\d+x\d+\.mp4$", previous_clip, re.IGNORECASE)
        if not previous_id:
            previous_id = re.search(r"(?:-|/)(\d+)/?(?:[?#].*)?$", previous_clip)
        excluded = {int(previous_id[1])} if previous_id else set()
        videos = select_unique(api_key, keyword.strip(), 1, root, database, owner, cancel,
                               exclude_ids=excluded, quality=quality, orientation=orientation)
        if cancel.is_set():
            raise InterruptedError("Đã hủy tải.")
        if not videos:
            raise ValueError("Không còn video mới phù hợp trên Pixabay.")
        events = []

        def progress(event):
            if event.state in {"done", "error", "cancelled"}:
                events.append(event)
            if report:
                report(event)

        destination = download_many(videos, root, keyword.strip(), cancel, progress, database, owner)
        video = videos[0]
        result = next((e for e in reversed(events) if e.state in {"done", "error", "cancelled"}), None)
        if result is None or result.state != "done":
            if cancel.is_set():
                raise InterruptedError("Đã hủy tải.")
            raise OSError(result.message if result else "Tải video không hoàn tất.")
        path = destination / f"{video.id}_{video.width}x{video.height}.mp4"
        if not path.is_file() or not path.stat().st_size:
            raise OSError("Không tìm thấy file video đã tải.")
        return str(path.resolve())
