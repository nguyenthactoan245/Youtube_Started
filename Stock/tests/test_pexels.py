from __future__ import annotations

import io
import json
import shutil
import sys
import unittest
import uuid
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.pexels import PexelsError, search_page
from services.pexels_link import _dimensions_from_download_url, resolve_pexels_video


class FakeResponse:
    def __init__(self, url: str, data: bytes = b""):
        self.url = url
        self.data = io.BytesIO(data)
        self.headers = Message()
        self.headers["Content-Type"] = "video/mp4"
        self.headers["Content-Length"] = str(len(data))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self.url

    def read(self, amount=-1):
        return self.data.read(amount)


class PexelsApiTests(unittest.TestCase):
    def test_official_download_filename_supplies_dimensions_without_reading_mp4(self):
        self.assertEqual(_dimensions_from_download_url(
            "https://videos.pexels.com/video-files/77/77-hd_1920_1080_55fps.mp4"), (1920, 1080))
        self.assertEqual(_dimensions_from_download_url(
            "https://videos.pexels.com/video-files/77/77-uhd_2160_3840_30fps.mp4"), (2160, 3840))

    def test_resolver_uses_head_response_and_does_not_parse_or_download_mp4(self):
        direct_url = "https://videos.pexels.com/video-files/77/77-hd_1920_1080_55fps.mp4"
        with patch("services.pexels_link.urlopen", return_value=FakeResponse(direct_url)) as request:
            video = resolve_pexels_video("https://www.pexels.com/video/sample-77/")
        self.assertEqual((video.width, video.height), (1920, 1080))
        self.assertEqual(video.url, direct_url)
        request.assert_called_once()

    def test_resolver_keeps_video_when_filename_has_no_dimensions(self):
        direct_url = "https://videos.pexels.com/video-files/77/77-original.mp4"
        with patch("services.pexels_link.urlopen", return_value=FakeResponse(direct_url)):
            video = resolve_pexels_video("https://www.pexels.com/video/sample-77/")
        self.assertEqual((video.width, video.height), (0, 0))

    def test_search_maps_video_and_selects_requested_resolution(self):
        payload = {"total_results": 1, "videos": [{
            "id": 77, "url": "https://www.pexels.com/video/77/", "image": "https://img.test/77.jpg",
            "duration": 9, "alt": "Alaska glacier aerial", "user": {"name": "Creator"},
            "video_files": [
                {"file_type": "video/mp4", "width": 1280, "height": 720, "link": "https://cdn.test/hd.mp4"},
                {"file_type": "video/mp4", "width": 3840, "height": 2160, "link": "https://cdn.test/4k.mp4"},
                {"file_type": "video/webm", "width": 4096, "height": 2160, "link": "https://cdn.test/other.webm"},
            ],
        }]}
        with patch("services.pexels.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as request:
            videos, total = search_page("test-key", "Alaska", quality="2K", orientation="landscape")
        self.assertEqual(total, 1)
        self.assertEqual(len(videos), 1)
        self.assertEqual((videos[0].source, videos[0].width, videos[0].height), ("pexels", 3840, 2160))
        self.assertEqual(videos[0].url, "https://cdn.test/4k.mp4")
        self.assertEqual(videos[0].author, "Creator")
        self.assertIn("size=medium", request.call_args.args[0].full_url)
        self.assertEqual(request.call_args.args[0].get_header("Authorization"), "test-key")

    def test_search_uses_only_rendition_matching_selected_orientation(self):
        payload = {"total_results": 1, "videos": [{
            "id": 88, "url": "https://www.pexels.com/video/sample-88/",
            "video_files": [
                {"file_type": "video/mp4", "width": 1920, "height": 1080,
                 "link": "https://cdn.test/landscape.mp4"},
                {"file_type": "video/mp4", "width": 1080, "height": 1920,
                 "link": "https://cdn.test/portrait.mp4"},
            ],
        }]}

        def response():
            return io.BytesIO(json.dumps(payload).encode())

        with patch("services.pexels.urlopen", side_effect=lambda *_args, **_kwargs: response()):
            landscape, _ = search_page("test-key", "sample", orientation="landscape")
            portrait, _ = search_page("test-key", "sample", orientation="portrait")
        self.assertEqual([(item.width, item.height) for item in landscape], [(1920, 1080)])
        self.assertEqual([(item.width, item.height) for item in portrait], [(1080, 1920)])

    def test_all_quality_and_orientation_select_highest_pexels_rendition(self):
        payload = {"total_results": 1, "videos": [{
            "id": 89, "url": "https://www.pexels.com/video/sample-89/",
            "video_files": [
                {"file_type": "video/mp4", "width": 1920, "height": 1080,
                 "link": "https://cdn.test/landscape.mp4"},
                {"file_type": "video/mp4", "width": 1080, "height": 1920,
                 "link": "https://cdn.test/portrait.mp4"},
                {"file_type": "video/mp4", "width": 3840, "height": 2160,
                 "link": "https://cdn.test/4k.mp4"},
            ],
        }]}
        with patch("services.pexels.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as request:
            videos, _ = search_page("test-key", "sample", quality="All", orientation="all")
        self.assertEqual([(item.width, item.height) for item in videos], [(3840, 2160)])
        self.assertNotIn("orientation=", request.call_args.args[0].full_url)
        self.assertNotIn("size=", request.call_args.args[0].full_url)

    def test_missing_key_fails_before_network_request(self):
        with patch("services.pexels.urlopen") as request:
            with self.assertRaisesRegex(PexelsError, "API key"):
                search_page("", "Alaska")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
