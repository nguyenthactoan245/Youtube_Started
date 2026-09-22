from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from models import Video

API_URL = "https://pixabay.com/api/videos/"
CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "pixabay"


class PixabayError(RuntimeError):
    """A friendly error returned while searching Pixabay."""


def _cache_file(cache_dir: Path, keyword: str, page: int = 1) -> Path:
    normalized = keyword.strip().casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}{f'-{page}' if page != 1 else ''}.json"


def _read_cache(cache_file: Path) -> dict[str, Any] | None:
    try:
        if time.time() - cache_file.stat().st_mtime > CACHE_TTL_SECONDS:
            return None
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _write_cache(cache_file: Path, payload: dict[str, Any]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_file.with_suffix(".json.part")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(cache_file)


def _fetch_payload(api_key: str, keyword: str, page: int = 1) -> dict[str, Any]:
    params = {
        "key": api_key,
        "q": keyword,
        "per_page": 200,
        "safesearch": "true",
        "order": "popular",
        "page": page,
    }
    request = Request(f"{API_URL}?{urlencode(params)}", headers={"User-Agent": "StockDownloader/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 429:
            wait_seconds = exc.headers.get("X-RateLimit-Reset")
            wait_note = f" Thử lại sau khoảng {wait_seconds} giây." if wait_seconds else " Hãy thử lại sau ít phút."
            raise PixabayError(f"Pixabay đang giới hạn số lượt gọi API (HTTP 429).{wait_note}") from exc
        raise PixabayError(f"Pixabay từ chối yêu cầu (HTTP {exc.code}).") from exc
    except Exception as exc:  # urllib errors intentionally become a UI-safe message
        raise PixabayError(f"Không thể kết nối tới Pixabay: {exc}") from exc


def search_page(api_key: str, keyword: str, page: int = 1, cache_dir: Path | None = None) -> tuple[list[Video], int]:
    """Read one 200-hit page; totalHits is the accessible result count."""
    """Return up to ``amount`` videos, reusing an API-compliant 24-hour search cache."""
    if not api_key.strip():
        raise PixabayError("Chưa có PIXABAY_API_KEY. Hãy tạo file .env từ .env.example.")

    cache_file = _cache_file(cache_dir or DEFAULT_CACHE_DIR, keyword, page)
    payload = _read_cache(cache_file)
    if payload is None:
        payload = _fetch_payload(api_key, keyword) if page == 1 else _fetch_payload(api_key, keyword, page)
        _write_cache(cache_file, payload)

    videos: list[Video] = []
    for hit in payload.get("hits", []):
        renditions = hit.get("videos", {})
        rendition = next((item for quality in ("medium", "small", "tiny")
                          if (item := renditions.get(quality)) and item.get("url")), None)
        if not rendition or not rendition.get("url"):
            continue
        videos.append(
            Video(
                id=int(hit["id"]),
                tags=hit.get("tags", "Untitled"),
                duration=int(hit.get("duration", 0)),
                author=hit.get("user", "Pixabay contributor"),
                page_url=hit["pageURL"],
                url=rendition["url"],
                thumbnail=rendition.get("thumbnail", ""),
                width=int(rendition.get("width", 0)),
                height=int(rendition.get("height", 0)),
                size=int(rendition.get("size", 0)),
            )
        )
    return videos, int(payload.get("totalHits", len(payload.get("hits", []))))


def search_videos(api_key: str, keyword: str, amount: int, cache_dir: Path | None = None) -> list[Video]:
    videos, _ = search_page(api_key, keyword, cache_dir=cache_dir)
    return videos[:amount]
