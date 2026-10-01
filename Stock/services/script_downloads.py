"""Download one reserved stock video for a script scene."""
from pathlib import Path
import re
from threading import Event

from services.catalog import select_unique
from services.database import VideoDatabase
from services.downloader import download_many, footage_filename, safe_folder_name
from services.pexels import select_unique as select_pexels_unique


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
