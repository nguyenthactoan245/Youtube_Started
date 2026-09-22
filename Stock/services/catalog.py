"""Select unseen Pixabay videos, reserving IDs before any download starts."""
from __future__ import annotations

from pathlib import Path
from threading import Event

from models import Video
from services.database import VideoDatabase
from services.downloader import safe_folder_name
from services.pixabay import search_page


def select_unique(api_key: str, keyword: str, amount: int, root: Path,
                  database: VideoDatabase, owner: str, cancel: Event) -> list[Video]:
    selected: list[Video] = []
    seen: set[int] = set()
    page = 1
    while len(selected) < amount and not cancel.is_set():
        videos, total = search_page(api_key, keyword, page)
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
