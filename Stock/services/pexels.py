"""Search and reserve Pexels videos for the Download view."""
from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from models import Video
from services.database import VideoDatabase
from services.diagnostics import http_fields, record
from services.downloader import footage_filename, safe_folder_name

API_URL = "https://api.pexels.com/v1/videos/search"


class PexelsError(RuntimeError):
    pass


def search_page(api_key: str, query: str, page: int = 1, per_page: int = 80,
                quality: str = "2K", orientation: str = "landscape") -> tuple[list[Video], int]:
    if not api_key.strip():
        raise PexelsError("Hãy nhập Pexels API key trong Setup trước khi tải.")
    params = {"query": query, "orientation": orientation, "per_page": min(80, max(1, per_page)),
              "page": page}
    size = {"HD": "small", "2K": "medium", "4K": "large"}.get(quality)
    if size:
        params["size"] = size
    request = Request(f"{API_URL}?{urlencode(params)}",
                      headers={"Authorization": api_key, "User-Agent": "StockDownloader/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        record(stage="pexels_search", **http_fields(exc.code, exc.headers))
        if exc.code in (401, 403):
            raise PexelsError("Pexels từ chối API key. Hãy kiểm tra key trong Setup.") from exc
        if exc.code == 429:
            raise PexelsError("Pexels API đang giới hạn request. Hãy chờ rồi thử lại.") from exc
        raise PexelsError(f"Pexels trả về HTTP {exc.code}.") from exc
    except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        record(stage="pexels_search", kind="network_error", error_type=type(exc).__name__)
        raise PexelsError(f"Không thể kết nối Pexels: {type(exc).__name__}.") from exc

    videos = []
    target_width = {"HD": 1280, "2K": 2560, "4K": 3840}.get(quality, 2560)
    for item in payload.get("videos", []):
        files = [file for file in item.get("video_files", [])
                 if file.get("file_type") == "video/mp4" and file.get("link")
                 and isinstance(file.get("width"), int) and isinstance(file.get("height"), int)]
        if orientation == "landscape":
            files = [file for file in files if file["width"] >= file["height"]]
        elif orientation == "portrait":
            files = [file for file in files if file["height"] > file["width"]]
        if not files:
            continue
        selected_file = min(files, key=lambda file: (
            0 if file["width"] >= target_width else 1,
            abs(file["width"] - target_width) if file["width"] >= target_width else -file["width"]))
        user = item.get("user") or {}
        videos.append(Video(
            id=int(item["id"]), tags=str(item.get("alt") or query),
            duration=int(item.get("duration") or 0), author=str(user.get("name") or "Pexels contributor"),
            page_url=str(item.get("url") or "https://www.pexels.com/videos/"),
            url=str(selected_file["link"]), thumbnail=str(item.get("image") or ""),
            width=selected_file["width"], height=selected_file["height"],
            size=int(selected_file.get("file_size") or 0), source="pexels"))
    return videos, int(payload.get("total_results") or 0)


def select_unique(api_key: str, keyword: str, amount: int, root: Path,
                  database: VideoDatabase, owner: str, cancel: Event,
                  quality: str = "2K", orientation: str = "landscape",
                  exclude_ids: set[int] | None = None) -> list[Video]:
    selected: list[Video] = []
    seen: set[int] = set()
    excluded = exclude_ids or set()
    page = 1
    while len(selected) < amount and not cancel.is_set():
        videos, total = search_page(api_key, keyword, page, min(80, amount - len(selected)),
                                    quality, orientation)
        for video in videos:
            if cancel.is_set() or len(selected) >= amount:
                break
            if video.id in seen or video.id in excluded:
                continue
            seen.add(video.id)
            target = Path(root) / safe_folder_name(keyword) / footage_filename(video)
            if database.reserve(video, keyword, target, owner):
                selected.append(video)
        if not videos or page * 80 >= total:
            break
        page += 1
    return selected
