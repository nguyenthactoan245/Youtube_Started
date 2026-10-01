from __future__ import annotations

import sys
from types import SimpleNamespace
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.pexels_browser import PexelsBrowserSearchError, search_video_links, search_videos
from services.pexels_link import PexelsLinkError
from models import Video


class PexelsBrowserSearchTests(unittest.TestCase):
    def test_search_video_links_keeps_scrolling_after_first_rejected_batch(self):
        class FakeLocator:
            def __init__(self, page, selector):
                self.page = page
                self.selector = selector

            def inner_text(self, timeout=None):
                return "video search results"

            def evaluate_all(self, _script):
                links = ["https://www.pexels.com/video/first-1/"]
                if self.page.scrolls >= 1:
                    links.append("https://www.pexels.com/video/second-2/")
                if self.page.scrolls >= 2:
                    links.append("https://www.pexels.com/video/third-3/")
                return links

            @property
            def last(self):
                return self

            def scroll_into_view_if_needed(self, timeout=None):
                self.page.scrolled_last_card = True

            def bounding_box(self):
                return {"x": 100, "y": 200, "width": 300, "height": 100}

        class FakePage:
            scrolls = 0
            viewport_size = {"height": 1000}

            class Mouse:
                def __init__(self, page):
                    self.page = page

                def wheel(self, _x, _y):
                    self.page.scrolls += 1

                def move(self, _x, _y):
                    self.page.mouse_moved_to_card = True

            def __init__(self):
                self.mouse = self.Mouse(self)
                self.scrolled_last_card = False
                self.mouse_moved_to_card = False

            def goto(self, *_args, **_kwargs):
                return SimpleNamespace(status=200)

            def wait_for_timeout(self, _timeout):
                pass

            def locator(self, selector):
                return FakeLocator(self, selector)

        class FakeBrowser:
            def __init__(self):
                self.page = FakePage()

            def new_page(self, **_kwargs):
                return self.page

            def close(self):
                pass

        browser = FakeBrowser()
        playwright = SimpleNamespace(chromium=SimpleNamespace(
            launch=lambda **_kwargs: browser))

        class Manager:
            def __enter__(self):
                return playwright

            def __exit__(self, *_args):
                pass

        batches = []
        with patch("playwright.sync_api.sync_playwright", return_value=Manager()):
            links = search_video_links("Moscow", 2, Event(), max_scrolls=12,
                                       on_batch=lambda batch: batches.append(batch) or False)

        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 2)
        self.assertEqual(batches[1], ["https://www.pexels.com/video/third-3/"])
        self.assertEqual(links, ["https://www.pexels.com/video/third-3/"])
        self.assertTrue(browser.page.scrolled_last_card)
        self.assertTrue(browser.page.mouse_moved_to_card)

    def test_searches_additional_batches_until_consumer_stops(self):
        def resolved(video_id):
            return Video(video_id, "tag", 5, "author", "page", "file", "", 1920, 1080, 1,
                         "pexels")

        consumer_batches = []

        def emit_batches(_keyword, _count, _cancel, *, on_batch, **_kwargs):
            on_batch(["https://www.pexels.com/video/first-1/"])
            on_batch(["https://www.pexels.com/video/second-2/"])

        with patch("services.pexels_browser.search_video_links", side_effect=emit_batches), \
             patch("services.pexels_browser.resolve_pexels_video",
                   side_effect=lambda url: resolved(1 if "first" in url else 2)):
            videos = search_videos("Moscow", 12, Event(), on_batch=lambda batch: (
                consumer_batches.append([video.id for video in batch]) or len(consumer_batches) == 2
            ))

        self.assertEqual(consumer_batches, [[1], [2]])
        self.assertEqual([video.id for video in videos], [1, 2])

    def test_skips_video_ids_already_checked_in_earlier_batch(self):
        def emit_batches(_keyword, _count, _cancel, *, on_batch, **_kwargs):
            on_batch(["https://www.pexels.com/video/duplicate-7/"])
            on_batch(["https://www.pexels.com/video/new-8/"])

        new_video = Video(8, "tag", 5, "author", "page", "file", "", 1920, 1080, 1,
                          "pexels")
        with patch("services.pexels_browser.search_video_links", side_effect=emit_batches), \
             patch("services.pexels_browser.resolve_pexels_video", return_value=new_video) as resolve:
            search_videos("Moscow", 12, Event(), exclude_ids={7})
        resolve.assert_called_once_with("https://www.pexels.com/video/new-8/")

    def test_reports_download_resolution_error_when_every_video_fails(self):
        def emit_batch(_keyword, _count, _cancel, *, on_batch, **_kwargs):
            on_batch([
                "https://www.pexels.com/video/sample-1/",
                "https://www.pexels.com/video/sample-2/",
            ])

        with patch("services.pexels_browser.search_video_links", side_effect=emit_batch), \
             patch("services.pexels_browser.resolve_pexels_video",
                  side_effect=PexelsLinkError("MP4 metadata unavailable")):
            with self.assertRaisesRegex(PexelsBrowserSearchError, "MP4 metadata unavailable"):
                search_videos("Moscow", 1, Event())


if __name__ == "__main__":
    unittest.main()
