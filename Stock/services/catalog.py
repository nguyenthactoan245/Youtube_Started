"""Select unseen Pixabay videos, reserving IDs before any download starts."""
from __future__ import annotations

from pathlib import Path
from threading import Event

from models import Video
from services.database import VideoDatabase
from services.downloader import safe_folder_name
from services.pixabay import search_page
from services.api_keys import ApiKeyPool


def select_unique(api_key: str, keyword: str, amount: int, root: Path,
                  database: VideoDatabase, owner: str, cancel: Event,
                  exclude_ids: set[int] | None = None, quality: str = "2K",
                  orientation: str = "landscape") -> list[Video]:
    selected: list[Video] = []
    seen: set[int] = set(exclude_ids or ())
    page = 1
    while len(selected) < amount and not cancel.is_set():
        if isinstance(api_key, ApiKeyPool):
            videos, total = search_page(api_key, keyword, page, cancel=cancel,
                                        quality=quality, orientation=orientation)
        elif quality == "2K" and orientation == "landscape":
            videos, total = search_page(api_key, keyword, page)
        else:
            videos, total = search_page(api_key, keyword, page, quality=quality,
                                        orientation=orientation)
        for video in videos:
            if cancel.is_set() or len(selected) >= amount:
                break
            if video.id in seen:
                continue
            seen.add(video.id)
            target = root / safe_folder_name(keyword) / f"{video.id}_{video.width}x{video.height}.mp4"
            if database.reserve(video, keyword, target, owner):
                selected.append(video)
        if not videos or page * 200 >= total:
            break
        page += 1
    return selected
