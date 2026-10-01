"""Resolve public Pexels video pages and read rendition names without their API."""
from __future__ import annotations

import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from models import Video


class PexelsLinkError(RuntimeError):
    pass


def _dimensions_from_download_url(download_url: str) -> tuple[int, int] | None:
    """Read rendition dimensions from Pexels' official MP4 filename."""
    filename = urlparse(download_url).path.rsplit("/", 1)[-1]
    pairs = re.findall(r"(?<!\d)(\d{3,5})[_x-](\d{3,5})(?=[_.-])", filename)
    for width, height in reversed(pairs):
        width, height = int(width), int(height)
        if width >= 240 and height >= 240:
            return width, height
    return None


def pexels_video_id(page_url: str) -> int:
    """Return the numeric ID from a canonical Pexels video URL."""
    try:
        parsed = urlparse(page_url.strip())
    except (AttributeError, ValueError) as exc:
        raise PexelsLinkError("Enter a valid Pexels video URL.") from exc
    if parsed.scheme not in {"https", "http"} or parsed.hostname not in {"pexels.com", "www.pexels.com"}:
        raise PexelsLinkError("Only video links from pexels.com are supported.")
    match = re.fullmatch(r"/video/[^/]*-(\d{1,12})/?", parsed.path)
    if not match:
        raise PexelsLinkError("Use a URL like https://www.pexels.com/video/name-123456/.")
    return int(match.group(1))


def resolve_pexels_video(page_url: str) -> Video:
    """Resolve Pexels' official download redirect and use its rendition filename."""
    video_id = pexels_video_id(page_url)
    path = urlparse(page_url.strip()).path
    slug = path.rstrip("/").rsplit("/", 1)[-1][:-len(str(video_id))].rstrip("-")
    canonical_page = f"https://www.pexels.com/video/{path.rstrip('/').rsplit('/', 1)[-1]}/"
    request = Request(
        f"https://www.pexels.com/download/video/{video_id}/",
        headers={"User-Agent": "StockDownloader/1.0", "Accept": "video/mp4,*/*;q=0.8"},
        method="HEAD",
    )
    try:
        with urlopen(request, timeout=30) as response:
            direct_url = response.geturl()
            content_type = response.headers.get_content_type()
            size = int(response.headers.get("Content-Length", "0") or 0)
    except HTTPError as exc:
        if exc.code == 404:
            raise PexelsLinkError("Pexels could not find this public video.") from exc
        if exc.code == 429:
            raise PexelsLinkError("Pexels is rate limiting requests. Please try again later.") from exc
        raise PexelsLinkError(f"Pexels returned HTTP {exc.code} while resolving the download.") from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise PexelsLinkError(f"Could not connect to Pexels ({type(exc).__name__}).") from exc

    resolved = urlparse(direct_url)
    if (resolved.scheme != "https" or resolved.hostname != "videos.pexels.com"
            or content_type != "video/mp4"):
        raise PexelsLinkError("Pexels did not return a public MP4 download for this video.")
    if not re.fullmatch(rf"/video-files/{video_id}/.+\.mp4", resolved.path):
        raise PexelsLinkError("Pexels did not return a recognized MP4 file.")
    dimensions = _dimensions_from_download_url(direct_url)
    width, height = dimensions or (0, 0)
    return Video(
        id=video_id,
        tags=slug.replace("-", " ") or f"Pexels {video_id}",
        duration=0,
        author="Pexels contributor",
        page_url=canonical_page,
        url=direct_url,
        thumbnail="",
        width=width,
        height=height,
        size=size,
        source="pexels",
    )
