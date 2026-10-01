"""User-authorized discovery of public Pexels video links with Playwright."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import quote, urlparse

from services.pexels_link import PexelsLinkError, resolve_pexels_video


class PexelsBrowserSearchError(RuntimeError):
    pass


class PexelsBrowserBlocked(PexelsBrowserSearchError):
    pass


_VIDEO_PATH = re.compile(r"/video/[^/]+-(\d{1,12})/?$")


def search_video_links(keyword: str, count: int, cancel: Event | None = None,
                       *, max_scrolls: int = 40) -> list[str]:
    """Read up to ``count`` video-page links from a visible, standard Chrome session.

    The function does not retry, solve challenges, alter browser fingerprints, or
    call Pexels' API. It stops immediately when the site presents a block page.
    """
    term = keyword.strip()
    if not term:
        raise PexelsBrowserSearchError("Hãy nhập keyword để tìm video Pexels.")
    if count < 1:
        raise PexelsBrowserSearchError("Số lượng video phải lớn hơn 0.")

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PexelsBrowserSearchError(
            "Thiếu Playwright. Hãy cài các package trong requirements.txt rồi khởi động lại app."
        ) from exc

    links: list[str] = []
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel="chrome", headless=False)
            except PlaywrightError as exc:
                raise PexelsBrowserSearchError(
                    "Không mở được Google Chrome. Hãy cài Chrome rồi thử lại."
                ) from exc
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                response = page.goto(
                    f"https://www.pexels.com/search/videos/{quote(term, safe='')}/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(1800)
                body = page.locator("body").inner_text(timeout=10000).casefold()
                if (response and response.status in {403, 429}) or any(
                    marker in body for marker in (
                        "performing security verification",
                        "just a moment",
                        "verify you are human",
                    )
                ):
                    raise PexelsBrowserBlocked(
                        "Pexels/Cloudflare đã chặn lượt tìm kiếm tự động. Không có link nào được thu thập."
                    )

                unchanged_scrolls = 0
                previous_height = 0
                for _ in range(max_scrolls + 1):
                    if cancel and cancel.is_set():
                        raise InterruptedError("Đã hủy tìm kiếm Pexels.")
                    found = page.locator('a[href*="/video/"]').evaluate_all(
                        "nodes => nodes.map(node => node.href)"
                    )
                    for href in found:
                        parsed = urlparse(href)
                        match = _VIDEO_PATH.fullmatch(parsed.path)
                        if (parsed.scheme == "https"
                                and parsed.hostname in {"www.pexels.com", "pexels.com"}
                                and match):
                            canonical = f"https://www.pexels.com{parsed.path.rstrip('/')}/"
                            if canonical not in links:
                                links.append(canonical)
                                if len(links) >= count:
                                    return links[:count]

                    height = page.evaluate("document.body.scrollHeight")
                    if height <= previous_height:
                        unchanged_scrolls += 1
                    else:
                        unchanged_scrolls = 0
                    if unchanged_scrolls >= 3:
                        break
                    previous_height = height
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(900)

                if not links:
                    raise PexelsBrowserSearchError(
                        "Không tìm thấy link video công khai trong kết quả Pexels."
                    )
                return links[:count]
            finally:
                browser.close()
    except (PexelsBrowserSearchError, InterruptedError):
        raise
    except Exception as exc:
        raise PexelsBrowserSearchError(
            f"Không thể đọc kết quả tìm kiếm Pexels ({type(exc).__name__})."
        ) from exc


def search_videos(keyword: str, count: int, cancel: Event | None = None) -> list:
    """Search public Pexels results, then resolve each official download URL."""
    links = search_video_links(keyword, min(200, max(count + 12, count * 3)), cancel)
    if cancel and cancel.is_set():
        raise InterruptedError("Đã hủy tìm kiếm Pexels.")

    def resolve(link: str):
        try:
            return resolve_pexels_video(link), None
        except PexelsLinkError as exc:
            return None, str(exc)

    with ThreadPoolExecutor(max_workers=min(3, len(links))) as executor:
        results = list(executor.map(resolve, links))
    videos = [video for video, _ in results if video is not None]
    errors = [error for _, error in results if error]
    if not videos and errors:
        raise PexelsBrowserSearchError(
            f"Found {len(links)} Pexels video links, but none could be resolved. "
            f"First error: {errors[0]}"
        )
    return videos
