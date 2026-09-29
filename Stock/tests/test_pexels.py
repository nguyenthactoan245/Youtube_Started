from __future__ import annotations

import io
import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.pexels import PexelsError, search_page


class PexelsApiTests(unittest.TestCase):
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

    def test_missing_key_fails_before_network_request(self):
        with patch("services.pexels.urlopen") as request:
            with self.assertRaisesRegex(PexelsError, "API key"):
                search_page("", "Alaska")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
