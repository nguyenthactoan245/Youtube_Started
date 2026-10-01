from __future__ import annotations

import asyncio
import io
import sys
import shutil
import uuid
from contextlib import contextmanager
import unittest
import zipfile
import time
from pathlib import Path
from types import SimpleNamespace
from threading import Event, Lock
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.scripts import read_tracker, load_tracker, save_tracker
from script_view import ScriptView, ScriptWorkspace
from script_video import ScriptVideo
from services.pixabay import PixabayRateLimitError, rate_limit_delay, _fetch_payload
from urllib.error import HTTPError
from models import Video
from services.database import VideoDatabase
from services.script_downloads import (download_pexels_link, download_scene,
                                       download_scene_direct_pexels)
from services.video_trim import trim_video


@contextmanager
def temporary_directory():
    root = Path(__file__).resolve().parent
    directory = root / ("script-test-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.resolve().parent == root and directory.name.startswith("script-test-"):
            shutil.rmtree(directory)


def make_workbook(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Footage Tracker" r:id="r1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="r1" Target="worksheets/tracker.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Scene ID</t></si><si><t>Voice-over (original)</t></si></sst>')
        z.writestr("xl/worksheets/tracker.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>Notes</t></is></c><c r="C1" t="s"><v>1</v></c></row><row><c r="A2" t="inlineStr"><is><t>S001</t></is></c><c r="C2" t="inlineStr"><is><t>Ká»‹ch báº£n tiáº¿ng Viá»‡t</t></is></c></row></sheetData></worksheet>')


class ScriptTests(unittest.TestCase):
    def test_script_table_hides_visual_direction_and_pixabay_search_columns(self):
        view = ScriptView(SimpleNamespace(update=lambda: None), None, Path("unused"))
        view.data = {
            "filename": "test.xlsx",
            "headers": ["Scene ID", "Visual Direction", "Pixabay Search", "Status",
                        "Primary Stock Keyword", "Beat ID", "Trimed video"],
            "rows": [["1", "Wide establishing shot", "https://pixabay.test/search", "",
                      "Amazon rainforest", "VB-001", ""]],
        }
        view.render()

        header_row = view.table.controls[0].content
        labels = [container.content.value for container in header_row.controls]
        self.assertIn("Scene ID", labels)
        self.assertIn("Status", labels)
        self.assertNotIn("Visual Direction", labels)
        self.assertNotIn("Pixabay Search", labels)
        self.assertEqual(labels[2], "Trimed video")
        self.assertEqual(labels.index("Primary Stock Keyword"), labels.index("Beat ID") + 1)
        self.assertEqual(view.data["rows"][0][1], "Wide establishing shot")
        self.assertEqual(view.data["rows"][0][2], "https://pixabay.test/search")

    def test_script_video_settings_include_pixabay_and_pexels_sources(self):
        operational_preferences = {"quality": "2K", "orientation": "landscape",
                                    "source": "pexels"}
        view = ScriptView(SimpleNamespace(update=lambda: None), None, Path("unused"),
                          video_preferences=operational_preferences)
        dropdown = view.video_settings.source_dropdown
        self.assertEqual(dropdown.value, "pexels")
        self.assertEqual([option.key for option in dropdown.options], ["pixabay", "pexels"])
        self.assertEqual([option.key for option in view.video_settings.controls[1].options],
                         ["All", "HD", "2K", "4K"])
        self.assertEqual([option.key for option in view.video_settings.controls[2].options],
                         ["all", "landscape", "portrait"])
        self.assertEqual([option.key for option in view.video_settings.method_dropdown.options],
                         ["api", "direct"])
        workers = view.video_settings.workers_dropdown
        self.assertEqual(workers.value, "5")
        self.assertEqual([option.key for option in workers.options],
                         [str(value) for value in range(1, 21)])
        workers.value = "20"
        workers.on_change(SimpleNamespace(control=workers))
        self.assertEqual(view.video_settings_preferences["workers"], "20")
        dropdown.value = "pixabay"
        dropdown.on_change(SimpleNamespace(control=dropdown))
        self.assertEqual(view.video_settings_preferences["source"], "pixabay")
        self.assertEqual(operational_preferences["source"], "pexels")

    def test_direct_pexels_download_uses_browser_search_without_api_key(self):
        video = Video(321, "Utah", 8, "Author", "https://www.pexels.com/video/utah-321/",
                      "https://example.test/video.mp4", "", 1920, 1080, 100, "pexels")
        with temporary_directory() as directory:
            database = VideoDatabase(directory / "library.db")
            target = directory / "library" / "Utah" / "pexels_321_1920x1080.mp4"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"video")

            def search(_keyword, _count, _cancel, **kwargs):
                kwargs["on_batch"]([video])
                return []

            with patch("services.script_downloads.search_pexels_videos_direct",
                       side_effect=search) as browser_search, \
                 patch("services.script_downloads.download_matching_pexels_videos",
                       return_value=([video], 1, 0, 0)) as download:
                result = download_scene_direct_pexels(
                    "Utah", directory / "library", database, Event(), orientation="landscape")

            self.assertEqual(Path(result), target.resolve())
            browser_search.assert_called_once()
            self.assertEqual(download.call_args.args[4], "landscape")

    def test_pexels_link_download_resolves_exact_video(self):
        video = Video(654, "Alaska", 9, "Author", "https://www.pexels.com/video/alaska-654/",
                      "https://example.test/video.mp4", "", 1920, 1080, 100, "pexels")
        with temporary_directory() as directory:
            database = VideoDatabase(directory / "library.db")
            target = directory / "library" / "Alaska" / "pexels_654_1920x1080.mp4"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"video")
            with patch("services.script_downloads.resolve_pexels_video",
                       return_value=video) as resolve, \
                 patch("services.script_downloads.download_matching_pexels_videos",
                       return_value=([video], 1, 0, 0)) as download:
                result = download_pexels_link(
                    video.page_url, directory / "library", database, Event(),
                    orientation="landscape")

            self.assertEqual(Path(result), target.resolve())
            resolve.assert_called_once_with(video.page_url)
            self.assertEqual(download.call_args.args[4], "landscape")

    def test_import_rounds_duration_to_one_decimal_place(self):
        with temporary_directory() as directory:
            source = directory / "duration.xlsx"
            with zipfile.ZipFile(source, "w") as z:
                z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Footage Tracker" r:id="r1"/></sheets></workbook>')
                z.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="r1" Target="worksheets/tracker.xml"/></Relationships>')
                z.writestr("xl/worksheets/tracker.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>Scene ID</t></is></c><c r="B1" t="inlineStr"><is><t>Voice-over (original)</t></is></c><c r="C1" t="inlineStr"><is><t>Duration (s)</t></is></c></row><row><c r="A2" t="inlineStr"><is><t>S001</t></is></c><c r="B2" t="inlineStr"><is><t>Test</t></is></c><c r="C2"><v>12.34</v></c></row><row><c r="A3" t="inlineStr"><is><t>S002</t></is></c><c r="B3" t="inlineStr"><is><t>Test</t></is></c><c r="C3"><v>8</v></c></row></sheetData></worksheet>')
            data = read_tracker(source)
            duration_column = data["headers"].index("Duration (s)")
            self.assertEqual([row[duration_column] for row in data["rows"]], ["12.3", "8.0"])

    def test_script_scene_download_uses_pexels_source(self):
        with temporary_directory() as directory:
            database = VideoDatabase(directory / "stock.db")
            video = Video(77, "Utah", 5, "Creator", "https://www.pexels.com/video/77/",
                          "https://example.com/video.mp4", "", 2560, 1440, 5, source="pexels")
            response = io.BytesIO(b"video")
            response.headers = {"Content-Length": "5"}
            with patch("services.script_downloads.select_pexels_unique", return_value=[video]) as search, \
                 patch("services.downloader.urlopen", return_value=response), \
                 patch.object(database, "owns", return_value=True):
                path = download_scene("Utah", "pexels-key", directory / "library", database,
                                      Event(), beat_id="VB-001", visual_direction="Alaska aerial",
                                      source="pexels")
            search.assert_called_once()
            self.assertEqual(Path(path).read_bytes(), b"video")
            self.assertTrue(Path(path).name.startswith("VB-001_Alaska_aerial_77_"))

    def test_script_workspace_adds_and_reopens_independent_scripts(self):
        with temporary_directory() as directory:
            legacy = {"filename": "legacy.xlsx", "headers": ["Scene ID"], "rows": [["S001"]]}
            save_tracker(legacy, directory / "script.json")
            legacy_with_trim = {"filename": "legacy.xlsx", "headers": ["Scene ID", "Thumbnail Video Duration",
                                "Trimed video Duration", "Trimed video"], "rows": [["S001", "", "", ""]]}
            page = SimpleNamespace(update=lambda: None)
            workspace = ScriptWorkspace(page, None, directory)
            asyncio.run(workspace.load())
            self.assertEqual(workspace.entries[0]["name"], "Script")
            self.assertEqual(workspace.editors["default"].data, legacy_with_trim)
            self.assertTrue(asyncio.run(workspace.add_script("Alaska")))
            alaska_id = workspace.active_id
            self.assertNotEqual(workspace.editors[alaska_id].target, directory / "script.json")
            alaska_data = {"filename": "alaska.xlsx", "headers": ["Beat ID"], "rows": [["VB001"]]}
            alaska_with_trim = {"filename": "alaska.xlsx", "headers": ["Beat ID", "Thumbnail Video Duration",
                                "Trimed video Duration", "Trimed video"], "rows": [["VB001", "", "", ""]]}
            save_tracker(alaska_data, workspace.editors[alaska_id].target)
            awaitable_select = workspace.select_script("default")
            asyncio.run(awaitable_select)
            self.assertEqual(workspace.editors["default"].data, legacy_with_trim)
            reopened = ScriptWorkspace(page, None, directory)
            asyncio.run(reopened.load())
            self.assertEqual(reopened.active_id, "default")
            self.assertEqual(reopened.editors["default"].data, legacy_with_trim)
            asyncio.run(reopened.select_script(alaska_id))
            self.assertEqual(reopened.editors[alaska_id].data, alaska_with_trim)

    def test_script_workspace_rename_and_delete(self):
        with temporary_directory() as directory:
            page = SimpleNamespace(update=lambda: None, show_dialog=lambda dialog: None,
                                   pop_dialog=lambda: None)
            workspace = ScriptWorkspace(page, None, directory)
            asyncio.run(workspace.load())
            self.assertTrue(asyncio.run(workspace.rename_script("default", "Alaska footage")))
            self.assertEqual(workspace.entries[0]["name"], "Alaska footage")
            self.assertTrue(asyncio.run(workspace.add_script("Japan")))
            japan_id = workspace.active_id
            japan_target = workspace.editors[japan_id].target
            save_tracker({"filename": "japan.xlsx", "headers": ["Scene ID"], "rows": [["S001"]]},
                         japan_target)
            self.assertTrue(asyncio.run(workspace.delete_script(japan_id)))
            self.assertFalse(japan_target.exists())
            self.assertEqual([item["name"] for item in workspace.entries], ["Alaska footage"])
            self.assertEqual(workspace.active_id, "default")
            self.assertFalse(asyncio.run(workspace.rename_script("default", "  ")))

    def test_rate_limit_headers_and_http_error(self):
        self.assertEqual(rate_limit_delay({}), 60)
        self.assertEqual(rate_limit_delay({"Retry-After": "90", "X-RateLimit-Reset": "20"}), 90)
        self.assertEqual(rate_limit_delay({"Retry-After": "invalid"}), 60)
        with patch("services.pixabay.urlopen", side_effect=HTTPError("https://example.com", 429, "limit",
                    {"Retry-After": "120"}, None)):
            with self.assertRaises(PixabayRateLimitError) as error:
                _fetch_payload("test", "Utah")
        self.assertEqual(error.exception.retry_after, 120)

    def test_rate_limit_retries_same_scene_then_succeeds(self):
        view = ScriptView(SimpleNamespace(update=lambda: None), None, Path("unused"))
        with patch.object(view, "download_with_progress", new_callable=AsyncMock,
                          side_effect=[PixabayRateLimitError(90), "new.mp4"]) as download, \
             patch.object(view, "wait_rate_limit", new_callable=AsyncMock) as wait:
            result = asyncio.run(view.download_with_retry("Utah", "test", "old.mp4"))
        self.assertEqual(result, "new.mp4")
        wait.assert_awaited_once_with(90, 1)
        self.assertEqual(download.await_count, 2)

    def test_rate_limit_retry_cap_and_cancel_wait(self):
        view = ScriptView(SimpleNamespace(update=lambda: None), None, Path("unused"))
        with patch.object(view, "download_with_progress", new_callable=AsyncMock,
                          side_effect=PixabayRateLimitError()) as download, \
             patch.object(view, "wait_rate_limit", new_callable=AsyncMock) as wait:
            with self.assertRaises(PixabayRateLimitError):
                asyncio.run(view.download_with_retry("Utah", "test", ""))
        self.assertEqual(download.await_count, 4)
        self.assertEqual([c.args[0] for c in wait.call_args_list], [60, 120, 240])

        async def cancel_wait():
            task = asyncio.create_task(view.wait_rate_limit(60, 1))
            await asyncio.sleep(0.01)
            view.cancel.set()
            with self.assertRaises(InterruptedError):
                await asyncio.wait_for(task, 1)
        asyncio.run(cancel_wait())

    def test_clear_log_preserves_data_and_new_errors_reappear(self):
        view = ScriptView(SimpleNamespace(update=lambda: None), None, Path("unused"))
        view.data = {"filename": "test.xlsx", "headers": ["Scene ID", "Status"],
                     "rows": [["S001", "Lá»—i: HTTP 429"]]}
        view.render()
        self.assertTrue(view.error_details.visible)
        view.clear_log(None)
        view.render()
        self.assertFalse(view.error_details.visible)
        self.assertEqual(view.data["rows"][0][1], "Lá»—i: HTTP 429")
        view.data["rows"].append(["S002", "Lá»—i: HTTP 429"])
        view.render()
        self.assertTrue(view.error_details.visible)
        self.assertEqual(len(view.error_details.controls), 2)
        self.assertIn("S002", view.error_details.controls[1].value)

    def test_inline_play_stop_and_separate_preview(self):
        with temporary_directory() as directory:
            path = directory / "video.mp4"
            path.write_bytes(b"fixture")
            preview = Mock()
            before = AsyncMock()
            tile = ScriptVideo(path, None, SimpleNamespace(update=lambda: None), preview, before, Mock())

            async def run():
                await tile.toggle(None)
                player = tile.player
                self.assertIsNotNone(player)
                preview.assert_not_called()
                with patch.object(type(player), "stop", new_callable=AsyncMock) as stop:
                    self.assertTrue(player.autoplay)
                    await player.on_complete(SimpleNamespace(data=False))
                    self.assertIs(tile.player, player)
                    stop.assert_not_awaited()
                    await tile.toggle(None)
                    stop.assert_awaited_once()
                    self.assertIsNone(tile.player)
                    await player.on_complete(SimpleNamespace(data=True))
                    self.assertEqual(stop.await_count, 1)
                    await tile.toggle(None)
                    await tile.player.on_complete(SimpleNamespace(data="true"))
                    self.assertIsNone(tile.player)
                await tile.preview(None)
                preview.assert_called_once_with(path)

            asyncio.run(run())

    def test_missing_inline_video_reports_error(self):
        report = Mock()
        preview = Mock()
        with temporary_directory() as directory:
            tile = ScriptVideo(directory / "missing.mp4", None, SimpleNamespace(update=lambda: None),
                               preview, AsyncMock(), report)
            asyncio.run(tile.toggle(None))
            self.assertIsNone(tile.player)
            self.assertIn("KhÃ´ng tÃ¬m tháº¥y", report.call_args.args[0])
            preview.assert_not_called()

    def test_replace_excludes_current_video_even_without_database_record(self):
        with temporary_directory() as directory:
            database = VideoDatabase(directory / "stock.db")
            old = Video(123, "Utah", 5, "author", "", "https://example.com/old.mp4", "", 1280, 720, 5)
            new = Video(456, "Utah", 5, "author", "", "https://example.com/new.mp4", "", 1280, 720, 5)
            response = io.BytesIO(b"video")
            response.headers = {"Content-Length": "5"}
            with patch("services.catalog.search_page", return_value=([old, new], 2)), \
                 patch("services.downloader.urlopen", return_value=response):
                path = download_scene("Utah", "test", directory / "library", database, Event(),
                                      previous_clip=str(directory / "123_1280x720.mp4"))
            self.assertEqual(Path(path).name, "456_1280x720.mp4")

    def test_filter_selection_pagination_and_delete_keep_original_row_indices(self):
        with temporary_directory() as directory:
            view = ScriptView(SimpleNamespace(update=lambda: None), None, directory / "script.json")
            view.data = {"filename": "test.xlsx", "headers": ["Scene ID", "Selected clip URL / file"],
                         "rows": [[str(i), "clip.mp4" if i % 2 else ""] for i in range(45)]}
            view.selected = {0, 1}
            view.set_filter("missing")
            self.assertEqual(view.selected, {0})
            self.assertEqual(view.filtered_indices(), list(range(0, 45, 2)))
            view.next_page(None)
            self.assertEqual(view.offset, 20)
            self.assertEqual(view.table.controls[1].content.controls[0].content.value, "41")
            view.toggle_all(None)
            self.assertEqual(view.selected, set(range(0, 45, 2)))
            asyncio.run(view.delete_rows(set(view.selected)))
            self.assertEqual(view.offset, 0)
            self.assertEqual(view.filtered_indices(), [])
            self.assertTrue(view.select_all.disabled)
            self.assertEqual([row[0] for row in load_tracker(view.target)["rows"]],
                             [str(i) for i in range(1, 45, 2)])
            view.set_filter("all")
            self.assertEqual(len(view.filtered_indices()), 22)

    def test_filter_hides_completed_row_and_clears_its_selection(self):
        view = ScriptView(SimpleNamespace(update=lambda: None), None, Path("unused"))
        view.data = {"filename": "test.xlsx", "headers": ["Scene ID", "Selected clip URL / file"],
                     "rows": [["1", " "], ["2", ""]]}
        view.set_filter("missing")
        view.toggle_all(None)
        view.data["rows"][0][1] = "downloaded.mp4"
        view.render()
        self.assertEqual(view.filtered_indices(), [1])
        self.assertEqual(view.selected, {1})

    def test_scene_download_writes_file_records_library_and_prevents_duplicates(self):
        with temporary_directory() as directory:
            database = VideoDatabase(directory / "stock.db")
            video = Video(123, "Utah", 5, "author", "https://pixabay.com/videos/id-123/",
                          "https://example.com/video.mp4", "", 1280, 720, 5)
            response = io.BytesIO(b"video")
            response.headers = {"Content-Length": "5"}
            with patch("services.catalog.search_page", return_value=([video], 1)), \
                 patch("services.downloader.urlopen", return_value=response):
                path = download_scene("Utah", "test", directory / "library", database, Event(),
                                      beat_id="VB-001", visual_direction="Alaska / North America establishing")
                self.assertEqual(Path(path).read_bytes(), b"video")
                self.assertEqual(Path(path).name,
                                 "VB-001_Alaska_North_America_establishing_123_1280x720.mp4")
                self.assertEqual(Path(database.library()[0]["file_path"]), Path(path))
                with self.assertRaisesRegex(ValueError, "KhÃ´ng cÃ²n video"):
                    download_scene("Utah", "test", directory / "library", database, Event())

    def test_scene_failed_download_can_retry(self):
        with temporary_directory() as directory:
            database = VideoDatabase(directory / "stock.db")
            video = Video(123, "Utah", 5, "author", "", "https://example.com/video.mp4", "", 1280, 720, 5)
            with patch("services.catalog.search_page", return_value=([video], 1)):
                with patch("services.downloader.urlopen", side_effect=OSError("network failed")):
                    with self.assertRaisesRegex(OSError, "network failed"):
                        download_scene("Utah", "test", directory / "library", database, Event())
                self.assertFalse(database.library())
                response = io.BytesIO(b"video")
                response.headers = {"Content-Length": "5"}
                with patch("services.downloader.urlopen", return_value=response):
                    path = download_scene("Utah", "test", directory / "library", database, Event())
                self.assertEqual(Path(path).read_bytes(), b"video")

    def test_sparse_cells_unicode_and_persistence(self):
        with temporary_directory() as directory:
            source = Path(directory) / "tracker.xlsx"
            make_workbook(source)
            data = read_tracker(source)
            self.assertEqual(data["rows"], [["S001", "", "Ká»‹ch báº£n tiáº¿ng Viá»‡t"]])
            target = Path(directory) / "data/script.json"
            save_tracker(data, target)
            self.assertEqual(load_tracker(target), data)

    def test_import_cancel_error_and_reload(self):
        with temporary_directory() as directory:
            source = Path(directory) / "tracker.xlsx"
            make_workbook(source)
            target = Path(directory) / "script.json"
            picker = SimpleNamespace(pick_files=AsyncMock(return_value=[SimpleNamespace(path=str(source))]))
            page = SimpleNamespace(update=lambda: None)
            view = ScriptView(page, picker, target)
            asyncio.run(view.import_file(None))
            self.assertEqual(view.data["rows"][0][0], "S001")
            headers = view.table.controls[0].content.controls
            self.assertEqual([c.content.value for c in headers][:6],
                             ["STT", "Thumbnail video", "Trimed video",
                              "Thumbnail Video Duration", "Trimed video Duration", "Scene ID"])
            self.assertEqual(view.table.controls[1].content.controls[1].controls[0]
                             .content.content.value, "ChÆ°a cÃ³ video")
            original = target.read_bytes()
            picker.pick_files.return_value = []
            asyncio.run(view.import_file(None))
            self.assertEqual(target.read_bytes(), original)
            source.write_text("broken", encoding="utf-8")
            picker.pick_files.return_value = [SimpleNamespace(path=str(source))]
            asyncio.run(view.import_file(None))
            self.assertIn("Import khÃ´ng thÃ nh cÃ´ng", view.message.value)
            self.assertFalse(view.import_button.disabled)
            self.assertEqual(target.read_bytes(), original)
            reopened = ScriptView(page, picker, target)
            asyncio.run(reopened.load())
            self.assertEqual(reopened.data, view.data)

    def test_trim_column_and_action_icon(self):
        page = SimpleNamespace(update=Mock())
        view = ScriptView(page, None, Path("unused"))
        view.data = {"filename": "test.xlsx", "headers": ["Beat ID", "Visual Direction"],
                     "rows": [["VB-001", "Alaska aerial"]]}
        view.ensure_trim_column()
        view.render()
        self.assertIn("Trimed video", view.data["headers"])
        self.assertEqual(view.data["rows"][0][-1], "")
        self.assertEqual(view.table.controls[0].content.controls[2].content.value, "Trimed video")
        self.assertEqual(view.table.controls[0].content.controls[3].content.value, "Thumbnail Video Duration")
        self.assertTrue(asyncio.iscoroutinefunction(view.trim_selected_videos))
        self.assertEqual(view.trim_button.icon, __import__("flet").Icons.CONTENT_CUT)
        self.assertTrue(view.trim_button.disabled)
        view.toggle_row(0, True)
        self.assertFalse(view.trim_button.disabled)
        view.toggle_row(0, False)
        self.assertTrue(view.trim_button.disabled)

    def test_script_time_edit_updates_duration_and_persists(self):
        with temporary_directory() as directory:
            view = ScriptView(SimpleNamespace(update=lambda: None), None, directory / "script.json")
            view.data = {"filename": "test.xlsx", "headers": ["Scene ID", "Start", "End", "Duration (s)"],
                         "rows": [["S001", "00:00:00.000", "00:00:02.000", "2"]]}
            view.toggle_script_edit(None)
            view.edit_values[0]["End"] = "00:00:03.250"
            asyncio.run(view.save_script_edits(None))
            self.assertEqual(view.data["rows"][0][2:], ["00:00:03.250", "3.2"])
            self.assertEqual(load_tracker(view.target), view.data)
            self.assertFalse(view.edit_mode)

    def test_script_time_edit_rejects_overlapping_intervals(self):
        with temporary_directory() as directory:
            page = SimpleNamespace(update=lambda: None, show_dialog=lambda dialog: setattr(page, "dialog", dialog),
                                   pop_dialog=lambda: None)
            view = ScriptView(page, None, directory / "script.json")
            view.data = {"filename": "test.xlsx", "headers": ["Scene ID", "Start", "End", "Duration (s)"],
                         "rows": [["S001", "00:00:00.000", "00:00:02.000", "2"],
                                  ["S002", "00:00:02.000", "00:00:04.000", "2"]]}
            view.toggle_script_edit(None)
            view.edit_values[1]["Start"] = "00:00:01.500"
            asyncio.run(view.save_script_edits(None))
            self.assertTrue(view.edit_mode)
            self.assertIn("bá»‹ trÃ¹ng", view.message.value)
            self.assertIn("KhÃ´ng thá»ƒ lÆ°u chá»‰nh sá»­a", page.dialog.title.value)
            self.assertIn("dÃ²ng 1 vÃ  dÃ²ng 2", page.dialog.content.value)
            self.assertEqual(view.data["rows"][1][1], "00:00:02.000")
            self.assertFalse(view.target.exists())

    def test_video_durations_shorter_than_requested_are_marked_as_errors(self):
        view = ScriptView(SimpleNamespace(update=lambda: None), None, Path("unused"))
        view.data = {"filename": "test.xlsx",
                     "headers": ["Duration (s)", "Thumbnail Video Duration", "Trimed video Duration", "Status"],
                     "rows": [["2.5", "2.0", "3.0", "Downloaded"]]}
        self.assertTrue(view.mark_duration_errors(view.data))
        row = view.data["rows"][0]
        self.assertEqual(view.duration_error_columns(row, view.data["headers"]),
                         {"Thumbnail Video Duration"})
        self.assertIn("Lá»—i duration:", row[view.data["headers"].index("Status")])
        view.render()
        duration_cell = view.table.controls[1].content.controls[2]
        self.assertEqual(duration_cell.bgcolor, "#7f1d1d")
        self.assertEqual(view.table.controls[1].bgcolor, "#4a202b")

    def test_trim_selected_saves_rounded_duration_path(self):
        with temporary_directory() as directory:
            source = directory / "source.mp4"
            trimmed = directory / "source_trimmed_2.3s.mp4"
            source.write_bytes(b"source")
            trimmed.write_bytes(b"trimmed")
            view = ScriptView(SimpleNamespace(update=lambda: None), None,
                directory / "script.json", object(), directory)
            view.data = {"filename": "test.xlsx", "headers": ["Selected clip URL / file", "Duration (s)"],
                         "rows": [[str(source), "2.34"]]}
            view.selected = {0}
            def trim_with_progress(*args):
                self.assertTrue(view.trim_progress_panel.visible)
                self.assertIn("FFmpeg Ä‘ang xá»­ lÃ½", view.trim_progress_label.value)
                return trimmed
            with patch("script_view.trim_video", return_value=trimmed) as trim, \
                 patch("script_view.thumbnail_bytes", return_value=None):
                asyncio.run(view.trim_selected_videos(None))
            self.assertEqual(trim.call_args.args[2], 2.3)
            trim_col = view.data["headers"].index("Trimed video")
            self.assertEqual(view.data["rows"][0][trim_col], str(trimmed.resolve()))
            self.assertEqual(load_tracker(view.target), view.data)
            self.assertEqual(view.trim_progress.value, 1)
            self.assertFalse(view.trim_activity.visible)

    def test_trimmed_video_cell_shows_thumbnail_and_reloads_it(self):
        with temporary_directory() as directory:
            source = directory / "source.mp4"
            trimmed = directory / "source_trimmed_2.3s.mp4"
            source.write_bytes(b"source")
            trimmed.write_bytes(b"trimmed")
            page = SimpleNamespace(update=lambda: None)
            view = ScriptView(page, None, directory / "script.json")
            view.data = {"filename": "test.xlsx",
                         "headers": ["Selected clip URL / file", "Trimed video"],
                         "rows": [[str(source), str(trimmed)]]}
            with patch("script_view.thumbnail_bytes", side_effect=lambda path: b"poster:" + Path(path).name.encode()):
                asyncio.run(view.load_posters())
            self.assertIn(str(trimmed), view.posters)
            view.render()
            trim_cell = view.table.controls[1].content.controls[2]
            trim_player = trim_cell.content
            self.assertEqual(trim_player.width, 180)
            self.assertTrue(any(key[0] == "trim" for key in view.video_tiles))
            tile = next(tile for key, tile in view.video_tiles.items() if key[0] == "trim")
            self.assertEqual(tile.path, trimmed)
            trim_checkbox = trim_player.controls[1].content
            trim_checkbox.on_change(SimpleNamespace(control=SimpleNamespace(value=True)))
            self.assertEqual(view.selected_trimmed, {0})
    def test_pagination(self):
        view = ScriptView(SimpleNamespace(update=lambda: None), None, Path("unused"))
        view.data = {"filename": "test.xlsx", "headers": ["Scene ID"],
                     "rows": [[str(i)] for i in range(41)]}
        view.render()
        view.next_page(None)
        self.assertEqual(view.offset, 20)
        view.next_page(None)
        self.assertTrue(view.next.disabled)
        self.assertEqual(len(view.table.controls), 2)
        view.previous_page(None)
        self.assertEqual(view.offset, 20)

    def test_selection_across_pages_and_delete_persists(self):
        with temporary_directory() as directory:
            view = ScriptView(SimpleNamespace(update=lambda: None), None, directory / "script.json")
            view.data = {"filename": "test.xlsx", "headers": ["Scene ID"],
                         "rows": [[str(i)] for i in range(41)]}
            view.render()
            view.toggle_row(0, True)
            view.next_page(None)
            view.toggle_row(20, True)
            self.assertEqual(view.selected, {0, 20})
            asyncio.run(view.delete_rows(set(view.selected)))
            self.assertEqual(len(load_tracker(view.target)["rows"]), 39)
            self.assertEqual(view.data["rows"][0], ["1"])
            self.assertFalse(view.selected)
            view.toggle_all(SimpleNamespace(control=SimpleNamespace(value=True)))
            self.assertEqual(len(view.selected), 39)
            asyncio.run(view.delete_rows(set(view.selected)))
            self.assertEqual(view.offset, 0)
            self.assertEqual(load_tracker(view.target)["rows"], [])

    def test_trimmed_filter_shows_only_existing_trimmed_files(self):
        with temporary_directory() as directory:
            trimmed = directory / "trimmed.mp4"
            trimmed.write_bytes(b"clip")
            view = ScriptView(SimpleNamespace(update=lambda: None), None, directory / "script.json")
            view.data = {"filename": "test.xlsx",
                         "headers": ["Selected clip URL / file", "Trimed video"],
                         "rows": [["source.mp4", str(trimmed)], ["source2.mp4", ""],
                                  ["source3.mp4", str(directory / "missing.mp4")]]}
            view.set_filter("trimmed")
            self.assertEqual(view.filtered_indices(), [0])
            self.assertEqual(view.filter_count.value, "1 / 3 dÃ²ng")
            view.set_filter("untrimmed")
            self.assertEqual(view.filtered_indices(), [1, 2])
            self.assertEqual(view.filter_count.value, "2 / 3 dÃ²ng")

    def test_error_filter_includes_duration_and_status_errors(self):
        view = ScriptView(SimpleNamespace(update=lambda: None), None, Path("unused"))
        view.data = {"filename": "test.xlsx", "headers": [
            "Duration (s)", "Thumbnail Video Duration", "Trimed video Duration", "Status"],
            "rows": [["4.0", "3.0", "4.0", "Downloaded | Lá»—i duration: source too short"],
                     ["4.0", "", "", "Lá»—i: Pixabay unavailable"],
                     ["4.0", "5.0", "4.5", "Downloaded"]]}
        view.set_filter("errors")
        self.assertEqual(view.filtered_indices(), [0, 1])
        self.assertEqual(view.filter_count.value, "2 / 3 dÃ²ng")

    def test_select_all_trimmed_toggles_only_valid_trimmed_rows(self):
        with temporary_directory() as directory:
            first, second = directory / "one.mp4", directory / "two.mp4"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            view = ScriptView(SimpleNamespace(update=lambda: None), None, directory / "script.json")
            view.data = {"filename": "test.xlsx", "headers": ["Trimed video"],
                         "rows": [[str(first)], [""], [str(second)]]}
            view.selected = {1}
            view.toggle_all_trimmed(None)
            self.assertEqual(view.selected_trimmed, {0, 2})
            self.assertEqual(view.selected, {1})
            self.assertEqual(view.select_all_trimmed.icon, __import__("flet").Icons.VIDEO_LIBRARY)
            view.toggle_all_trimmed(None)
            self.assertFalse(view.selected_trimmed)
            self.assertEqual(view.select_all_trimmed.icon,
                             __import__("flet").Icons.VIDEO_LIBRARY_OUTLINED)

    def test_export_selected_trimmed_videos_without_original_row_selection(self):
        with temporary_directory() as directory:
            original, trimmed = directory / "source_1280x720.mp4", directory / "trimmed.mp4"
            original.write_bytes(b"source")
            trimmed.write_bytes(b"trimmed")
            picker = SimpleNamespace(save_file=AsyncMock(return_value=str(directory / "trimmed.zip")))
            view = ScriptView(SimpleNamespace(update=lambda: None), picker, directory / "script.json")
            view.script_name = "Alaska Project"
            view.data = {"filename": "test.xlsx",
                         "headers": ["Beat ID", "Visual Direction", "Selected clip URL / file", "Trimed video"],
                         "rows": [["VB-001", "Alaska aerial establishing shot", str(original), str(trimmed)]]}
            view.selected_trimmed = {0}
            with patch("script_view.export_videos_zip", return_value=(1, 0)) as export:
                asyncio.run(view.export_selected_videos(None))
            self.assertEqual(export.call_args.args[0], [trimmed])
            exported_name = export.call_args.args[2][0]
            self.assertEqual(exported_name, "VB001_Alaska_aerial_establishing_shot_1280x720.mp4")
            suggested_zip_name = picker.save_file.call_args.kwargs["file_name"]
            self.assertRegex(suggested_zip_name, r"^Alaska_Project_\d{8}_\d{6}\.zip$")

    def test_toggle_row_keeps_existing_checkbox_and_video_tile(self):
        page = SimpleNamespace(update=Mock())
        view = ScriptView(page, None, Path("unused"))
        view.data = {"filename": "test.xlsx", "headers": ["Scene ID"], "rows": [["S001"]]}
        view.render()
        original_row = view.table.controls[1]
        original_stack = original_row.content.controls[1]
        checkbox = original_stack.controls[1].content
        view.toggle_row(0, True, checkbox)
        self.assertIs(view.table.controls[1], original_row)
        self.assertIs(view.table.controls[1].content.controls[1], original_stack)
        self.assertTrue(checkbox.value)
        self.assertEqual(view.selection_count.value, "ÄÃ£ chá»n 1 / 1 dÃ²ng")

    def test_delete_save_error_preserves_rows_and_selection(self):
        with temporary_directory() as directory:
            view = ScriptView(SimpleNamespace(update=lambda: None), None, directory / "script.json")
            view.data = {"filename": "test.xlsx", "headers": ["Scene ID"], "rows": [["1"]]}
            view.selected = {0}
            with patch("script_view.save_tracker", side_effect=OSError("disk full")):
                asyncio.run(view.delete_rows({0}))
            self.assertEqual(view.data["rows"], [["1"]])
            self.assertEqual(view.selected, {0})
            self.assertFalse(view.busy)
            self.assertFalse(view.table.controls[1].content.controls[1].controls[1]
                             .content.disabled)

    def test_download_keywords_partial_failure_replace_and_reload(self):
        with temporary_directory() as directory:
            target = directory / "script.json"
            view = ScriptView(SimpleNamespace(update=lambda: None), None, target, object(), directory)
            view.data = {"filename": "test.xlsx", "headers": ["Scene ID", "Primary search keyword"],
                         "rows": [["1", "Utah"], ["2", "canyon"]]}
            view.selected = {0, 1}
            clip = directory / "test.mp4"
            clip.write_bytes(b"test video")
            with patch.dict("os.environ", {"PIXABAY_API_KEY": "test"}), \
                 patch("script_view.download_scene", side_effect=[str(clip), ValueError("No result")]) as download, \
                 patch("script_view.thumbnail_bytes", return_value=None):
                asyncio.run(view.download_selected(None))
            self.assertEqual([call.args[0] for call in download.call_args_list], ["Utah", "canyon"])
            saved = load_tracker(target)
            self.assertEqual(saved["rows"][0][saved["headers"].index("Status")], "Downloaded")
            self.assertEqual(saved["rows"][0][saved["headers"].index("Selected clip URL / file")], str(clip))
            self.assertEqual(saved["rows"][0][saved["headers"].index("File Name")], clip.name)
            self.assertIn("No result", saved["rows"][1][saved["headers"].index("Status")])
            self.assertEqual(view.progress.value, 1)
            self.assertIn("2/2", view.progress_label.value)
            self.assertTrue(view.error_details.visible)
            self.assertIn("canyon", view.error_details.controls[1].value)
            self.assertIn("No result", view.error_details.controls[1].value)
            self.assertFalse(view.busy)
            view.selected = {0}
            replacement = directory / "new.mp4"
            replacement.write_bytes(b"new video")
            with patch.dict("os.environ", {"PIXABAY_API_KEY": "test"}), \
                 patch("script_view.download_scene", return_value=str(replacement)) as download, \
                 patch("script_view.thumbnail_bytes", return_value=None):
                asyncio.run(view.download_selected(None))
                self.assertEqual(download.call_args.args[6], str(clip))
            saved = load_tracker(target)
            self.assertEqual(saved["rows"][0][saved["headers"].index("Selected clip URL / file")], str(replacement))
            self.assertEqual(saved["rows"][0][saved["headers"].index("File Name")], replacement.name)
            self.assertEqual(clip.read_bytes(), b"test video")
            with patch.dict("os.environ", {"PIXABAY_API_KEY": "test"}), \
                 patch("script_view.download_scene", side_effect=ValueError("No new video")):
                asyncio.run(view.download_selected(None))
            saved = load_tracker(target)
            self.assertEqual(saved["rows"][0][saved["headers"].index("Selected clip URL / file")], str(replacement))

    def test_script_download_runs_up_to_five_rows_concurrently(self):
        with temporary_directory() as directory:
            view = ScriptView(SimpleNamespace(update=lambda: None), None,
                              directory / "script.json", object(), directory)
            view.data = {"filename": "test.xlsx", "headers": ["Scene ID", "Primary search keyword"],
                         "rows": [[str(i), f"keyword-{i}"] for i in range(7)]}
            view.selected = set(range(7))
            active = 0
            max_active = 0
            lock = Lock()

            def download(keyword, *args):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.03)
                with lock:
                    active -= 1
                return str(directory / f"{keyword}.mp4")

            with patch.dict("os.environ", {"PIXABAY_API_KEY": "test"}), \
                 patch("script_view.download_scene", side_effect=download), \
                 patch("script_view.thumbnail_bytes", return_value=None):
                asyncio.run(view.download_selected(None))
            saved = load_tracker(directory / "script.json")
            clip_column = saved["headers"].index("Selected clip URL / file")
            self.assertEqual([row[clip_column] for row in saved["rows"]],
                             [str(directory / f"keyword-{i}.mp4") for i in range(7)])
            self.assertEqual(view.progress.value, 1)
            self.assertEqual(max_active, 5)

    def test_direct_pexels_crawl_uses_worker_count_currently_shown_in_dropdown(self):
        with temporary_directory() as directory:
            view = ScriptView(SimpleNamespace(update=lambda: None), None,
                              directory / "script.json", object(), directory)
            view.video_settings_preferences.update({"method": "direct", "source": "pexels"})
            # Reproduce clicking Crawl before Flet dispatches the dropdown on_change event.
            view.video_settings.workers_dropdown.value = "10"
            view.data = {"filename": "test.xlsx", "headers": ["Scene ID", "Primary search keyword"],
                         "rows": [[str(i), f"keyword-{i}"] for i in range(12)]}
            view.selected = set(range(12))
            active = 0
            max_active = 0
            lock = Lock()

            def download(keyword, *args):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.03)
                with lock:
                    active -= 1
                return str(directory / f"{keyword}.mp4")

            with patch("script_view.download_scene_direct_pexels", side_effect=download) as direct, \
                 patch("script_view.download_scene") as api_download, \
                 patch("script_view.thumbnail_bytes", return_value=None):
                asyncio.run(view.download_selected(None))

            self.assertEqual(direct.call_count, 12)
            api_download.assert_not_called()
            self.assertEqual(max_active, 10)

    def test_cancel_stops_before_next_row(self):
        with temporary_directory() as directory:
            view = ScriptView(SimpleNamespace(update=lambda: None), None, directory / "script.json", object(), directory)
            view.data = {"filename": "test.xlsx", "headers": ["Scene ID", "Primary search keyword"],
                         "rows": [["1", "Utah"], ["2", "canyon"]]}
            view.selected = {0, 1}

            def cancel(*args):
                view.cancel.set()
                raise InterruptedError()

            with patch.dict("os.environ", {"PIXABAY_API_KEY": "test"}), \
                 patch("script_view.download_scene", side_effect=cancel) as download:
                asyncio.run(view.download_selected(None))
                self.assertEqual(download.call_count, 1)
            self.assertFalse(view.busy)
            self.assertIn("ÄÃ£ há»§y", view.message.value)
            self.assertEqual(view.progress.value, 0)
            self.assertFalse(view.file_progress.visible)

    def test_error_details_hide_api_key(self):
        with patch.dict("os.environ", {"PIXABAY_API_KEY": "secret-value"}):
            message = ScriptView.error_text(ValueError("HTTP error ?key=secret-value&q=Utah"))
        self.assertNotIn("secret-value", message)
        self.assertIn("HTTP error", message)
if __name__ == "__main__":
    unittest.main()
