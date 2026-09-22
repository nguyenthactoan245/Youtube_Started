from __future__ import annotations

import sys
import unittest
from email.message import Message
from urllib.error import HTTPError
from unittest.mock import patch
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import Video
from services.downloader import safe_folder_name
from services.pixabay import PixabayError, _cache_file, search_page, search_videos


class ServiceTests(unittest.TestCase):
    def test_safe_folder_name(self) -> None:
        self.assertEqual(safe_folder_name("Moscow / 2026"), "Moscow_2026")
        self.assertEqual(safe_folder_name("..."), "untitled")

    def test_missing_key_is_a_friendly_error(self) -> None:
        with self.assertRaisesRegex(PixabayError, "PIXABAY_API_KEY"):
            search_videos("", "Moscow", 3)

    def test_download_skips_existing_file_without_network(self) -> None:
        # The downloader's idempotency rule is checked without contacting Pixabay.
        from services.downloader import _download_one

        video = Video(7, "test", 1, "author", "https://example.com", "https://invalid.example/video.mp4", "", 1280, 720, 1)
        events = []
        folder = Path(__file__).resolve().parent / ".test-work"
        folder.mkdir(exist_ok=True)
        target = folder / "7_1280x720.mp4"
        target.write_bytes(b"already downloaded")
        _download_one(video, folder, Event(), events.append)
        self.assertEqual(events[-1].state, "done")
        self.assertEqual(events[-1].message, "Đã có sẵn")

    def test_search_reuses_24_hour_cache(self) -> None:
        payload = {
            "hits": [{
                "id": 1, "tags": "Moscow", "duration": 5, "user": "author", "pageURL": "https://example.com/1",
                "videos": {"medium": {"url": "https://example.com/1.mp4", "thumbnail": "https://example.com/1.jpg", "width": 1280, "height": 720, "size": 42}},
            }]
        }
        cache_dir = Path(__file__).resolve().parent / ".test-work" / "cache"
        _cache_file(cache_dir, "Moscow cache test").unlink(missing_ok=True)
        with patch("services.pixabay._fetch_payload", return_value=payload) as fetch:
            first = search_videos("key", "Moscow cache test", 1, cache_dir)
            second = search_videos("key", "Moscow cache test", 1, cache_dir)
        self.assertEqual([video.id for video in first], [1])
        self.assertEqual([video.id for video in second], [1])
        self.assertEqual(fetch.call_count, 1)

    def test_rate_limit_error_includes_retry_guidance(self) -> None:
        headers = Message()
        headers["X-RateLimit-Reset"] = "42"
        error = HTTPError("https://pixabay.com/api/videos/", 429, "Too Many Requests", headers, None)
        cache_dir = Path(__file__).resolve().parent / ".test-work" / "rate-limit-cache"
        with patch("services.pixabay.urlopen", side_effect=error):
            with self.assertRaisesRegex(PixabayError, "42 giây"):
                search_videos("key", "unique rate limit keyword", 1, cache_dir)

    def test_each_search_page_is_cached_independently(self) -> None:
        cache_dir = Path(__file__).resolve().parent / ".test-work" / "paged-cache"
        payload = {"totalHits": 201, "hits": [{
            "id": 201, "pageURL": "https://pixabay.com/videos/id-201/",
            "videos": {"medium": {"url": "https://example.com/201.mp4", "width": 1280, "height": 720}},
        }]}
        _cache_file(cache_dir, "page test", 2).unlink(missing_ok=True)
        with patch("services.pixabay._fetch_payload", return_value=payload) as fetch:
            first, total = search_page("key", "page test", 2, cache_dir)
            second, _ = search_page("key", "page test", 2, cache_dir)
        self.assertEqual(([item.id for item in first], total), ([201], 201))
        self.assertEqual([item.id for item in second], [201])
        fetch.assert_called_once_with("key", "page test", 2)


if __name__ == "__main__":
    unittest.main()
