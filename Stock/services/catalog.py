"""Select unseen Pixabay videos, reserving IDs before any download starts."""
from __future__ import annotations

from pathlib import Path
from threading import Event

from models import Video
from services.database import VideoDatabase
from services.downloader import footage_filename, safe_folder_name
from services.pixabay import search_page
from services.api_keys import ApiKeyPool


def select_unique(api_key: str, keyword: str, amount: int, root: Path,
                  database: VideoDatabase, owner: str, cancel: Event,
                  exclude_ids: set[int] | None = None, quality: str = "2K",
                  orientation: str = "landscape", filename_prefix: str = "") -> list[Video]:
    keywords = [part.strip() for part in keyword.split(",") if part.strip()]
    if len(keywords) > 1:
        combined: list[Video] = []
        seen = set(exclude_ids or ())
        for term in keywords:
            if cancel.is_set():
                break
            found = select_unique(api_key, term, amount, root, database, owner, cancel,
                                  exclude_ids=seen, quality=quality, orientation=orientation,
                                  filename_prefix=filename_prefix)
            combined.extend(found)
            seen.update(video.id for video in found)
        return combined
    keyword = keywords[0] if keywords else keyword.strip()
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
            target = root / safe_folder_name(keyword) / footage_filename(video)
            if database.reserve(video, keyword, target, owner, filename_prefix):
                selected.append(video)
        if not videos or page * 200 >= total:
            break
        page += 1
    return selected
