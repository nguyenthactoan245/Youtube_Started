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
                       *, max_scrolls: int = 80, on_batch=None) -> list[str]:
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
    seen: set[str] = set()
    found_any = False
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

                unchanged_result_scrolls = 0
                for _ in range(max_scrolls + 1):
                    if cancel and cancel.is_set():
                        raise InterruptedError("Đã hủy tìm kiếm Pexels.")
                    new_links = 0
                    batch_processed = False
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
                            if canonical not in seen:
                                links.append(canonical)
                                seen.add(canonical)
                                new_links += 1
                                if len(links) >= count:
                                    batch, links = links, []
                                    if on_batch:
                                        found_any = True
                                        if on_batch(batch):
                                            return []
                                        batch_processed = True
                                        break
                                    else:
                                        return batch[:count]

                    if new_links:
                        unchanged_result_scrolls = 0
                    else:
                        unchanged_result_scrolls += 1
                    if unchanged_result_scrolls >= 6:
                        break
                    # Scroll the results container to its last loaded card first,
                    # then wheel over that card to trigger Pexels' lazy loading.
                    last_video = page.locator('a[href*="/video/"]').last
                    last_video.scroll_into_view_if_needed(timeout=10000)
                    box = last_video.bounding_box()
                    if box:
                        page.mouse.move(box["x"] + box["width"] / 2,
                                        box["y"] + box["height"] / 2)
                    page.mouse.wheel(0, max(700, int(page.viewport_size["height"] * 0.85)))
                    page.wait_for_timeout(1500 if batch_processed else 1100)

                if links and on_batch:
                    found_any = True
                    if on_batch(links):
                        return []
                if not found_any:
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


def search_videos(keyword: str, count: int, cancel: Event | None = None, *,
                  exclude_ids: set[int] | None = None, on_batch=None,
                  max_scrolls: int = 80) -> list:
    """Search results in batches and optionally process each batch before scrolling."""
    def resolve(link: str):
        match = _VIDEO_PATH.fullmatch(urlparse(link).path)
        if exclude_ids and match and int(match.group(1)) in exclude_ids:
            return None, None
        try:
            return resolve_pexels_video(link), None
        except PexelsLinkError as exc:
            return None, str(exc)

    results_all = []
    total_links = 0

    def resolve_batch(links: list[str]):
        nonlocal total_links
        total_links += len(links)
        if cancel and cancel.is_set():
            raise InterruptedError("Đã hủy tìm kiếm Pexels.")
        with ThreadPoolExecutor(max_workers=min(3, len(links))) as executor:
            results = list(executor.map(resolve, links))
        results_all.extend(results)
        videos = [video for video, _ in results if video is not None]
        return bool(videos and on_batch and on_batch(videos))

    search_video_links(keyword, max(1, min(4, count)), cancel,
                       max_scrolls=max_scrolls, on_batch=resolve_batch)
    videos = [video for video, _ in results_all if video is not None]
    errors = [error for _, error in results_all if error]
    if not videos and errors:
        raise PexelsBrowserSearchError(
            f"Found {total_links} Pexels video links, but none could be resolved. "
            f"First error: {errors[0]}"
        )
    return videos
