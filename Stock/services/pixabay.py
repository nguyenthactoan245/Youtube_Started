from __future__ import annotations

import json
import hashlib
import time
import math
import tempfile
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from models import Video
from app_config import DATA_ROOT
from services.api_keys import ApiKeyPool
from services.diagnostics import record, key_label, http_fields

API_URL = "https://pixabay.com/api/videos/"
CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_CACHE_DIR = DATA_ROOT / ".cache" / "pixabay"
QUALITY_TARGETS = {"HD": 1920, "2K": 2560, "4K": 3840}


class PixabayError(RuntimeError):
    """A friendly error returned while searching Pixabay."""


class PixabayAccessPause(PixabayError):
    pass


class PixabayRateLimitError(PixabayError):
    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(f"Pixabay đang giới hạn số lượt gọi API (HTTP 429). Thử lại sau {retry_after} giây.")


def rate_limit_delay(headers) -> int:
    delays = []
    for name in ("Retry-After", "X-RateLimit-Reset"):
        value = (headers or {}).get(name)
        if value is None:
            continue
        try:
            seconds = float(value)
            if name == "X-RateLimit-Reset" and seconds > 1_000_000_000:
                seconds -= time.time()
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(seconds):
            delays.append(max(1, math.ceil(seconds)))
    return max(delays) if delays else 60


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
    # Concurrent search/script requests must not share the same temporary filename.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_file.parent,
                                     suffix=".part", delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(payload, output)
        except BaseException:
            output.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(cache_file)
    finally:
        temporary.unlink(missing_ok=True)


def _fetch_payload(api_key: str, keyword: str, page: int = 1, on_headers=None) -> dict[str, Any]:
    params = {
        "key": api_key,
        "q": keyword,
        "per_page": 200,
        "safesearch": "true",
        "order": "popular",
        "page": page,
    }
    request = Request(f"{API_URL}?{urlencode(params)}", headers={"User-Agent": "StockDownloader/1.0"})
    started = time.monotonic()
    label = key_label(api_key)
    try:
        with urlopen(request, timeout=30) as response:
            record(stage='search', key_label=label, elapsed_ms=round((time.monotonic()-started)*1000),
                   **http_fields(getattr(response, 'status', 200), response.headers))
            if on_headers is not None:
                on_headers(response.headers)
            return json.load(response)
    except HTTPError as exc:
        try:
            error_body = exc.read(8192) if exc.fp else b''
        except OSError:
            error_body = b''
        record(stage='search', key_label=label, elapsed_ms=round((time.monotonic()-started)*1000),
               **http_fields(exc.code, exc.headers, error_body))
        kind = http_fields(exc.code, exc.headers, error_body)['kind']
        if kind == 'cloudflare_challenge' or (exc.code == 429 and kind != 'api_quota'
                and not exc.headers.get('X-RateLimit-Reset')
                and not exc.headers.get('Retry-After')):
            exc.close()
            raise PixabayAccessPause(
                'Pixabay yêu cầu xác minh truy cập (Cloudflare). Kiểm tra trên trình duyệt rồi bấm Tiếp tục tìm kiếm.'
                if kind == 'cloudflare_challenge' else
                'HTTP 429 chưa rõ nguyên nhân. Đã tạm dừng; hãy xuất log để kiểm tra.') from None
        if exc.code == 429:
            delay = rate_limit_delay(exc.headers)
            exc.close()
            raise PixabayRateLimitError(delay) from None
        error = PixabayError(f"Pixabay từ chối yêu cầu (HTTP {exc.code}).")
        # Inspect explicit invalid-key messages; do not mislabel every 403/400.
        try:
            body = error_body.decode("utf-8", errors="replace")
        except OSError:
            body = ""
        finally:
            exc.close()
        error.invalid_key = exc.code == 401 or (exc.code == 400 and "invalid" in body.lower()
                                               and "key" in body.lower())
        raise error from None
    except Exception as exc:  # urllib errors intentionally become a UI-safe message
        record(stage='search', key_label=label, kind='network_or_response_error', error_type=type(exc).__name__)
        raise PixabayError("Không thể kết nối tới Pixabay. Hãy kiểm tra mạng và thử lại.") from None


def _choose_rendition(renditions: dict[str, Any], quality: str) -> dict[str, Any] | None:
    target = QUALITY_TARGETS.get(quality, QUALITY_TARGETS["2K"])
    available = [item for item in renditions.values()
                 if item and item.get("url") and int(item.get("width", 0)) > 0
                 and int(item.get("height", 0)) > 0]
    if not available:
        return None
    above_target = [item for item in available
                    if max(int(item["width"]), int(item["height"])) >= target]
    if above_target:
        return min(above_target, key=lambda item: int(item["width"]) * int(item["height"]))
    return max(available, key=lambda item: int(item["width"]) * int(item["height"]))


def search_page(api_key: str | ApiKeyPool, keyword: str, page: int = 1,
                cache_dir: Path | None = None, cancel=None, quality: str = "2K",
                orientation: str = "landscape") -> tuple[list[Video], int]:
    """Read one 200-hit page, reusing the shared 24-hour search cache."""
    if not (bool(api_key) if isinstance(api_key, ApiKeyPool) else api_key.strip()):
        raise PixabayError("Chưa có PIXABAY_API_KEYS. Hãy thêm key trong Setup.")

    cache_file = _cache_file(cache_dir or DEFAULT_CACHE_DIR, keyword, page)
    payload = _read_cache(cache_file)
    if payload is None:
        if isinstance(api_key, ApiKeyPool):
            payload = api_key.fetch(lambda key, headers: _fetch_payload(key, keyword, page, headers), cancel)
        else:
            payload = _fetch_payload(api_key, keyword) if page == 1 else _fetch_payload(api_key, keyword, page)
        _write_cache(cache_file, payload)

    videos: list[Video] = []
    for hit in payload.get("hits", []):
        renditions = hit.get("videos", {})
        rendition = _choose_rendition(renditions, quality)
        if not rendition or not rendition.get("url"):
            continue
        width, height = int(rendition.get("width", 0)), int(rendition.get("height", 0))
        if orientation == "landscape" and width < height:
            continue
        if orientation == "portrait" and height < width:
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
                width=width,
                height=height,
                size=int(rendition.get("size", 0)),
            )
        )
    return videos, int(payload.get("totalHits", len(payload.get("hits", []))))


def search_videos(api_key: str, keyword: str, amount: int, cache_dir: Path | None = None,
                  quality: str = "2K", orientation: str = "landscape") -> list[Video]:
    videos, _ = search_page(api_key, keyword, cache_dir=cache_dir, quality=quality,
                            orientation=orientation)
    return videos[:amount]
