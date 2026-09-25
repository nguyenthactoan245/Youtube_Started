from __future__ import annotations

import os
import sys
import unittest
import asyncio
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parents[1].as_posix())

import main
from models import Video


class EnvironmentTests(unittest.TestCase):
    def test_dotenv_replaces_an_empty_environment_value(self) -> None:
        with patch.dict(os.environ, {"PIXABAY_API_KEY": ""}, clear=False), \
             patch.object(Path, 'read_text', return_value='PIXABAY_API_KEY=test-key\n'), \
             patch.object(Path, 'exists', return_value=True):
            main.load_env()
            self.assertTrue(os.environ["PIXABAY_API_KEY"])

    def test_ui_builds_with_installed_flet_version(self) -> None:
        class TestPage:
            window = SimpleNamespace()

            def add(self, *controls: object) -> None:
                self.controls = controls

            def update(self) -> None:
                pass

        page = TestPage()
        with patch.object(main, "load_library_entries", return_value=[]), \
             patch.object(main, "load_dashboard_stats", return_value=(7, 4_200_000, 3)):
            asyncio.run(main.main(page))
        self.assertEqual(len(page.controls), 1)
        self.assertTrue(callable(page.on_keyboard_event))
        self.assertTrue(page.window.maximized)
        self.assertEqual(page.window.icon, str(main.WINDOW_ICON))
        root_row = page.controls[0]
        self.assertEqual(root_row.controls[0].width, 72)
        self.assertEqual(root_row.controls[0].content.controls[0].content.src, main.SIDEBAR_LOGO)
        sidebar_buttons = [control for control in root_row.controls[0].content.controls if isinstance(control, main.ft.IconButton)]
        self.assertEqual([button.tooltip for button in sidebar_buttons], ["Dashboard", "Download", "Library", "Project", "Script", "Setup"])
        self.assertFalse(sidebar_buttons[2].disabled)
        self.assertFalse(sidebar_buttons[3].disabled)
        self.assertFalse(sidebar_buttons[4].disabled)
        self.assertTrue(callable(sidebar_buttons[4].on_click))
        panels = root_row.controls[2].content.controls
        download_view = panels[1].content
        download_layout_buttons = download_view.controls[0].controls[1].controls
        download_layout_buttons[1].on_click(None)
        self.assertIsInstance(download_view.controls[-1].content, main.ft.ListView)
        with patch.object(main, "load_dashboard_stats", return_value=(7, 4_200_000, 3)):
            asyncio.run(sidebar_buttons[0].on_click(None))
        self.assertTrue(panels[0].visible)
        dashboard_cards = panels[0].content.controls[-1].controls
        self.assertEqual([card.content.controls[1].controls[0].value for card in dashboard_cards],
                         ["7", "4.2 MB", "3"])
        with patch.object(main, "load_library_entries", return_value=[]):
            asyncio.run(sidebar_buttons[2].on_click(None))
        self.assertTrue(panels[2].visible)
        library_view = panels[2].content
        library_layout_buttons = library_view.controls[0].controls
        self.assertIsInstance(library_view.controls[-1].content, main.ft.GridView)
        library_layout_buttons[1].on_click(None)
        self.assertIsInstance(library_view.controls[-1].content, main.ft.ListView)
        asyncio.run(sidebar_buttons[3].on_click(None))
        self.assertTrue(panels[3].visible)
        self.assertEqual(panels[3].content.controls[0].width, 260)
        asyncio.run(sidebar_buttons[5].on_click(None))
        self.assertTrue(panels[4].visible)

    def test_library_switch_is_cached_and_loading_is_visible(self) -> None:
        class TestPage:
            window = SimpleNamespace()

            def add(self, *controls: object) -> None:
                self.controls = controls

            def update(self) -> None:
                pass

        page = TestPage()
        gate = Event()

        def slow_load(_):
            gate.wait(2)
            return []

        async def run() -> None:
            await main.main(page)
            sidebar = page.controls[0].controls[0].content.controls
            panels = page.controls[0].controls[2].content.controls
            task = asyncio.create_task(sidebar[3].on_click(None))
            await asyncio.sleep(0.05)
            self.assertTrue(panels[2].visible)
            self.assertTrue(panels[2].content.controls[3].visible)
            gate.set()
            await task
            self.assertFalse(panels[2].content.controls[3].visible)
            await sidebar[2].on_click(None)
            await sidebar[3].on_click(None)

        with patch.object(main, "load_library_entries", side_effect=slow_load) as load:
            asyncio.run(run())
        load.assert_called_once()

    def test_library_search_filters_tags_filename_and_visible_select_all(self) -> None:
        class TestPage:
            window = SimpleNamespace()

            def add(self, *controls: object) -> None:
                self.controls = controls

            def update(self) -> None:
                pass

        records = [
            {"id": 1, "file_path": str(Path("library") / "Moscow" / "kremlin.mp4"),
             "details": {"tags": "Moscow Kremlin city"}, "status": "done",
             "exists_on_disk": True, "poster_bytes": None, "size_on_disk": 12},
            {"id": 2, "file_path": str(Path("library") / "Japan" / "tokyo.mp4"),
             "details": {"tags": "Tokyo crossing night"}, "status": "done",
             "exists_on_disk": True, "poster_bytes": None, "size_on_disk": 24},
        ]
        page = TestPage()
        with patch.object(main, "load_library_entries", return_value=records):
            asyncio.run(main.main(page))
            sidebar = page.controls[0].controls[0].content.controls
            panels = page.controls[0].controls[2].content.controls
            asyncio.run(sidebar[3].on_click(None))
            library_view = panels[2].content
            search = library_view.controls[2].controls[1]
            grid = library_view.controls[-1].content
            self.assertEqual([card.data for card in grid.controls], [1, 2])
            search.value = "moscow kremlin"
            search.on_change(None)
            self.assertEqual([card.data for card in grid.controls], [1])
            self.assertEqual(library_view.controls[2].controls[0].value, "1/2 video trong lịch sử")
            library_view.controls[4].controls[0].on_click(None)
            self.assertEqual(library_view.controls[4].controls[1].value, "1 đã chọn")
            search.value = "no-match"
            search.on_change(None)
            self.assertIsInstance(grid.controls[0], main.ft.Text)
            self.assertIn("Không tìm thấy", grid.controls[0].value)

    def test_preview_dialog_has_play_and_close_actions(self) -> None:
        video = Video(1, "Moscow", 5, "author", "https://example.com", "https://example.com/video.mp4", "https://example.com/thumb.jpg", 1280, 720, 42)
        dialog = main.build_preview_dialog(video, lambda _: None)
        self.assertEqual(dialog.content.width, 1280)
        preview_frame = dialog.content.content.controls[0]
        self.assertIsInstance(preview_frame.content, main.ft.Stack)
        poster = preview_frame.content.controls[1]
        self.assertIsInstance(poster.content.controls[1].content, main.ft.IconButton)
        self.assertIsInstance(dialog.data["player"], main.ftv.Video)
        player = dialog.data["player"]
        play_button = poster.content.controls[1].content
        with patch.object(poster, "update"), patch.object(player, "play") as play:
            asyncio.run(play_button.on_click(None))
            self.assertTrue(dialog.data["playback"]["requested"])
            self.assertFalse(play.called)
            asyncio.run(player.on_load(None))
            play.assert_awaited_once()
        self.assertEqual([action.content for action in dialog.actions], ["Chạy video", "Đóng (Esc)"])

    def test_library_lists_completed_mp4_files(self) -> None:
        root = __import__("pathlib").Path(__file__).resolve().parent / ".test-work" / "library"
        nested = root / "Moscow"
        nested.mkdir(parents=True, exist_ok=True)
        video = nested / "sample.mp4"
        video.write_bytes(b"video")
        self.assertEqual(main.list_library_videos(root)[0], video)
        video.with_suffix(".jpg").write_bytes(b"\xff\xd8\xffposter")
        self.assertEqual(main.library_poster(video), b"\xff\xd8\xffposter")

    def test_dashboard_stats_count_real_library_videos_and_projects(self) -> None:
        root = Path(__file__).resolve().parent / ".test-work" / "dashboard-stats"
        if root.exists():
            __import__("shutil").rmtree(root)
        self.addCleanup(__import__("shutil").rmtree, root, True)
        library = root / "library"
        nested = library / "Moscow"
        nested.mkdir(parents=True)
        (nested / "first.mp4").write_bytes(b"first")
        (nested / "second.mp4").write_bytes(b"second video")
        (nested / "ignored.jpg").write_bytes(b"image")
        database = main.VideoDatabase(root / "data" / "stock.db")
        database.create_project("Moscow")
        database.create_project("Tokyo")
        self.assertEqual(main.load_dashboard_stats(database, library), (2, 17, 2))

    def test_player_prefers_downloaded_video(self) -> None:
        root = __import__("pathlib").Path(__file__).resolve().parent / ".test-work" / "playable"
        local = root / "Moscow" / "1_1280x720.mp4"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"video")
        video = Video(1, "Moscow", 5, "author", "https://example.com", "https://example.com/video.mp4", "", 1280, 720, 42)
        self.assertEqual(main.playable_resource(video, root), str(local.resolve()))


if __name__ == "__main__":
    unittest.main()
