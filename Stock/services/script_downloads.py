"""Download one reserved stock video for a script scene."""
from pathlib import Path
import re
from threading import Event

from services.catalog import select_unique
from services.database import VideoDatabase
from services.downloader import download_many, footage_filename, safe_folder_name
from services.pexels import select_unique as select_pexels_unique


def download_scene(keyword: str, api_key: str, root: Path, database: VideoDatabase, cancel: Event,
                   report=None, previous_clip: str = "", quality: str = "4K",
                   orientation: str = "landscape", beat_id: str = "",
                   visual_direction: str = "", source: str = "pixabay") -> str:
    if not keyword.strip():
        raise ValueError("Primary search keyword đang trống.")
    if cancel.is_set():
        raise InterruptedError("Đã hủy tải.")
    with database.batch() as owner:
        # Also exclude the current clip when its database record no longer exists.
        previous_id = re.search(r"(?:^|[/\\])(?:.*_)?(\d+)_\d+x\d+\.mp4$", previous_clip, re.IGNORECASE)
        if not previous_id:
            previous_id = re.search(r"(?:-|/)(\d+)/?(?:[?#].*)?$", previous_clip)
        excluded = {int(previous_id[1])} if previous_id else set()
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
            raise InterruptedError("Đã hủy tải.")
        if not videos:
            raise ValueError(f"Không còn video mới phù hợp trên {source.title()}.")
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
                raise InterruptedError("Đã hủy tải.")
            raise OSError(result.message if result else "Tải video không hoàn tất.")
        path = destination / footage_filename(video, f"{safe_label}_{video.id}" if safe_label else "")
        if not path.is_file() or not path.stat().st_size:
            raise OSError("Không tìm thấy file video đã tải.")
        database.set_file_path(video.id, path, source=video.source)
        return str(path.resolve())
