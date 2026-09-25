from __future__ import annotations

import asyncio
import io
import sys
import shutil
import uuid
from contextlib import contextmanager
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.scripts import read_tracker, load_tracker, save_tracker
from script_view import ScriptView
from script_video import ScriptVideo
from services.pixabay import PixabayRateLimitError, rate_limit_delay, _fetch_payload
from urllib.error import HTTPError
from models import Video
from services.database import VideoDatabase
from services.script_downloads import download_scene


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
        z.writestr("xl/worksheets/tracker.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>Notes</t></is></c><c r="C1" t="s"><v>1</v></c></row><row><c r="A2" t="inlineStr"><is><t>S001</t></is></c><c r="C2" t="inlineStr"><is><t>Kịch bản tiếng Việt</t></is></c></row></sheetData></worksheet>')


class ScriptTests(unittest.TestCase):
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
                     "rows": [["S001", "Lỗi: HTTP 429"]]}
        view.render()
        self.assertTrue(view.error_details.visible)
        view.clear_log(None)
        view.render()
        self.assertFalse(view.error_details.visible)
        self.assertEqual(view.data["rows"][0][1], "Lỗi: HTTP 429")
        view.data["rows"].append(["S002", "Lỗi: HTTP 429"])
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
            self.assertIn("Không tìm thấy", report.call_args.args[0])
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
            self.assertEqual(view.table.controls[1].content.controls[1].content.value, "41")
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
                path = download_scene("Utah", "test", directory / "library", database, Event())
                self.assertEqual(Path(path).read_bytes(), b"video")
                self.assertEqual(database.library()[0]["file_path"], path)
                with self.assertRaisesRegex(ValueError, "Không còn video"):
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
            self.assertEqual(data["rows"], [["S001", "", "Kịch bản tiếng Việt"]])
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
            self.assertEqual([c.content.value for c in headers][:4], ["", "STT", "Thumbnail video", "Scene ID"])
            self.assertEqual(view.table.controls[1].content.controls[2].content.content.value, "Chưa có video")
            original = target.read_bytes()
            picker.pick_files.return_value = []
            asyncio.run(view.import_file(None))
            self.assertEqual(target.read_bytes(), original)
            source.write_text("broken", encoding="utf-8")
            picker.pick_files.return_value = [SimpleNamespace(path=str(source))]
            asyncio.run(view.import_file(None))
            self.assertIn("Import không thành công", view.message.value)
            self.assertFalse(view.import_button.disabled)
            self.assertEqual(target.read_bytes(), original)
            reopened = ScriptView(page, picker, target)
            asyncio.run(reopened.load())
            self.assertEqual(reopened.data, view.data)

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
            self.assertFalse(view.table.controls[1].content.controls[0].content.disabled)

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
            self.assertEqual(saved["rows"][0][2:], ["Downloaded", str(clip)])
            self.assertIn("No result", saved["rows"][1][2])
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
                self.assertEqual(download.call_args.args[-1], str(clip))
            self.assertEqual(load_tracker(target)["rows"][0][-1], str(replacement))
            self.assertEqual(clip.read_bytes(), b"test video")
            with patch.dict("os.environ", {"PIXABAY_API_KEY": "test"}), \
                 patch("script_view.download_scene", side_effect=ValueError("No new video")):
                asyncio.run(view.download_selected(None))
            self.assertEqual(load_tracker(target)["rows"][0][-1], str(replacement))

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
            self.assertIn("Đã hủy", view.message.value)
            self.assertEqual(view.progress.value, 0)
            self.assertFalse(view.file_progress.visible)

    def test_error_details_hide_api_key(self):
        with patch.dict("os.environ", {"PIXABAY_API_KEY": "secret-value"}):
            message = ScriptView.error_text(ValueError("HTTP error ?key=secret-value&q=Utah"))
        self.assertNotIn("secret-value", message)
        self.assertIn("HTTP error", message)
if __name__ == "__main__":
    unittest.main()
