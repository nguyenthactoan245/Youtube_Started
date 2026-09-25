from __future__ import annotations

import asyncio
import os
import sys
import shutil
import unittest
import uuid
import zipfile
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import Video
from services.catalog import select_unique
from services.database import VideoDatabase
from services.downloader import download_many
import main


def video(number: int) -> Video:
    return Video(number, f"clip {number}", 5, "author", f"https://pixabay.com/videos/id-{number}/",
                 f"https://example.com/{number}.mp4", "", 1280, 720, 5)


class Response:
    headers = {"Content-Length": "5"}

    def __init__(self):
        self.sent = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, _):
        if self.sent:
            return b""
        self.sent = True
        return b"video"


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parent / '.test-work'
        test_root.mkdir(exist_ok=True)
        self.root = test_root / 'database-tests' / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.assertTrue(self.root.resolve().is_relative_to(test_root.resolve()))
        self.addCleanup(shutil.rmtree, self.root)
        self.db = VideoDatabase(self.root / "data" / "stock.db")
        self.library = self.root / "library"

    def test_pagination_cross_keyword_restart_and_missing_file(self):
        def pages(_, __, page):
            return ([video(i) for i in range(1, 201)] if page == 1 else [video(201)], 201)

        with patch('services.catalog.search_page', side_effect=pages) as search:
            with self.db.batch() as owner:
                selected = select_unique('key', 'Moscow', 200, self.library, self.db, owner, Event())
                self.assertEqual(len(selected), 200)
                with patch('services.downloader.urlopen', side_effect=lambda *a, **k: Response()):
                    download_many(selected, self.library, 'Moscow', Event(), lambda _: None, self.db, owner)
            self.assertTrue((self.library / 'Moscow' / '1_1280x720.mp4').read_bytes() == b'video')
            reopened = VideoDatabase(self.db.path)
            with reopened.batch() as owner:
                new = select_unique('key', 'Russia', 1, self.library, reopened, owner, Event())
                self.assertEqual([item.id for item in new], [201])
            self.assertEqual(search.call_count, 3)
            (self.library / 'Moscow' / '1_1280x720.mp4').unlink()
            with reopened.batch() as owner:
                self.assertFalse(reopened.reserve(video(1), 'Moscow', self.library / '1.mp4', owner))
            self.assertEqual(len(reopened.library()), 200)

    def test_concurrent_reservation_and_retry_after_failure_cancel(self):
        owners = [self.db.start_batch(), self.db.start_batch()]
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                claims = list(pool.map(lambda owner: self.db.reserve(video(7), 'same', self.library / '7.mp4', owner), owners))
            self.assertEqual(sorted(claims), [False, True])
            winner = owners[claims.index(True)]
            loser = owners[claims.index(False)]
            with patch('services.downloader.urlopen', side_effect=OSError('network down')):
                events = []
                download_many([video(7)], self.library, 'same', Event(), events.append, self.db, winner)
            self.assertEqual(events[-1].state, 'error')
            self.assertTrue(self.db.reserve(video(7), 'new query', self.library / '7.mp4', loser))
            cancel = Event()
            cancel.set()
            download_many([video(7)], self.library, 'new query', cancel, events.append, self.db, loser)
            self.assertEqual(events[-1].state, 'cancelled')
            self.assertTrue(self.db.reserve(video(7), 'retry', self.library / '7.mp4', winner))
        finally:
            for owner in owners:
                self.db.finish_batch(owner)

    def test_import_preserves_legacy_files_and_unknown_names(self):
        old = self.root / 'downloads' / 'Moscow'
        old.mkdir(parents=True)
        known = old / '42_1280x720.mp4'
        unknown = old / 'own-name.mp4'
        known.write_bytes(b'video')
        unknown.write_bytes(b'other')
        self.assertEqual(self.db.import_existing(self.library, old.parent), 2)
        self.assertEqual(self.db.import_existing(self.library, old.parent), 0)
        self.assertEqual({item['status'] for item in self.db.library()}, {'completed', 'review'})
        self.assertEqual(known.read_bytes(), b'video')
        with self.db.batch() as owner:
            self.assertFalse(self.db.reserve(video(42), 'other', self.library / '42.mp4', owner))

    def test_stale_claim_can_be_recovered_without_stealing_live_claim(self):
        first = self.db.start_batch()
        second = self.db.start_batch()
        self.assertTrue(self.db.reserve(video(1), 'A', self.library / '1.mp4', first))
        self.assertTrue(self.db.reserve(video(2), 'B', self.library / '2.mp4', second))
        with self.db.connect() as connection:
            connection.execute('UPDATE batches SET heartbeat=0 WHERE id=?', (first,))
        third = self.db.start_batch()
        try:
            self.assertTrue(self.db.reserve(video(1), 'C', self.library / '1.mp4', third))
            self.assertFalse(self.db.reserve(video(2), 'C', self.library / '2.mp4', third))
        finally:
            self.db.finish_batch(second)
            self.db.finish_batch(third)

    def test_download_button_uses_history_and_refreshes_library(self):
        class Page:
            window = SimpleNamespace()

            def add(self, *controls):
                self.controls = controls

            def update(self):
                pass

        page = Page()
        with patch.object(main, 'VideoDatabase', return_value=self.db), \
             patch.object(main, 'ROOT', self.root), patch.object(main, 'LIBRARY_ROOT', self.library), \
             patch.dict(os.environ, {'PIXABAY_API_KEY': 'test-key'}), \
             patch('services.catalog.search_page', return_value=([video(1), video(2)], 2)), \
             patch('services.downloader.urlopen', side_effect=lambda *a, **k: Response()):
            asyncio.run(main.main(page))
            sidebar = page.controls[0].controls[0].content.controls
            content = page.controls[0].controls[2]
            download_view = content.content.controls[1].content
            _, amount, start, _, _ = download_view.controls[2].controls
            download_view.controls[2].controls[0].value = 'Moscow'
            amount.value = '1'
            asyncio.run(start.on_click(None))
            self.assertEqual([r['video_id'] for r in self.db.library()], [1])
            asyncio.run(start.on_click(None))
            self.assertEqual({r['video_id'] for r in self.db.library()}, {1, 2})
            self.assertIn('1/1', download_view.controls[3].value)
            asyncio.run(sidebar[3].on_click(None))
            self.assertEqual(content.content.controls[2].content.controls[2].controls[0].value,
                             '2 video trong lịch sử')

    def test_deleted_download_video_can_be_downloaded_again(self):
        class Page:
            window = SimpleNamespace()

            def add(self, *controls):
                self.controls = controls

            def update(self):
                pass

            def show_dialog(self, dialog):
                self.dialog = dialog

            def pop_dialog(self):
                self.dialog = None

        page = Page()
        with patch.object(main, 'VideoDatabase', return_value=self.db), \
             patch.object(main, 'ROOT', self.root), patch.object(main, 'LIBRARY_ROOT', self.library), \
             patch.dict(os.environ, {'PIXABAY_API_KEY': 'test-key'}), \
             patch('services.catalog.search_page', return_value=([video(1)], 1)), \
             patch('services.downloader.urlopen', side_effect=lambda *a, **k: Response()):
            asyncio.run(main.main(page))
            download_view = page.controls[0].controls[2].content.controls[1].content
            keyword, amount, start, _, _ = download_view.controls[2].controls
            keyword.value = 'Moscow'
            amount.value = '1'
            asyncio.run(start.on_click(None))
            file = self.library / 'Moscow' / '1_1280x720.mp4'
            self.assertTrue(file.is_file())
            select_all, count, delete, _, _ = download_view.controls[-2].controls
            select_all.on_click(None)
            self.assertEqual(count.value, '1 đã chọn')
            asyncio.run(delete.on_click(None))
            self.assertIn('Xóa vĩnh viễn', page.dialog.title)
            asyncio.run(page.dialog.actions[1].on_click(None))
            self.assertFalse(file.exists())
            self.assertEqual(self.db.library(), [])
            self.assertEqual(len(download_view.controls[-1].content.controls), 0)
            asyncio.run(start.on_click(None))
            self.assertEqual([row['video_id'] for row in self.db.library()], [1])
            self.assertEqual(file.read_bytes(), b'video')

    def test_projects_and_chapters_persist_and_stay_isolated(self):
        first = self.db.create_project('  Russia  ')
        second = self.db.create_project('Japan')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda title: self.db.create_chapter(first, title),
                          ['Intro', 'History', 'Places', 'Outro']))
        self.db.create_chapter(second, 'Tokyo')
        reopened = VideoDatabase(self.db.path)
        projects = {row['id']: row for row in reopened.list_projects()}
        self.assertEqual(projects[first]['name'], 'Russia')
        self.assertEqual(projects[first]['chapter_count'], 4)
        self.assertEqual(projects[second]['chapter_count'], 1)
        self.assertEqual([row['position'] for row in reopened.list_chapters(first)], [1, 2, 3, 4])
        self.assertEqual([row['title'] for row in reopened.list_chapters(second)], ['Tokyo'])
        with self.assertRaises(ValueError):
            reopened.create_project('  ')
        with self.assertRaises(ValueError):
            reopened.create_chapter(999999, 'Invalid')

    def test_delete_chapter_and_project_preserve_library_videos(self):
        file = self.library / 'Moscow' / '36_1280x720.mp4'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'video')
        self.db.import_existing(self.library, self.root / 'downloads')
        record_id = self.db.library()[0]['id']
        project_id = self.db.create_project('Moscow')
        chapter_id = self.db.create_chapter(project_id, 'Opening')
        self.db.assign_videos([record_id], project_id, chapter_id)
        with self.assertRaises(ValueError):
            self.db.delete_chapter(project_id, 999999)
        self.assertEqual(self.db.list_project_videos(project_id)[0]['chapter_id'], chapter_id)
        self.assertEqual(self.db.delete_chapter(project_id, chapter_id), 1)
        self.assertEqual(self.db.list_chapters(project_id), [])
        self.assertIsNone(self.db.list_project_videos(project_id)[0]['chapter_id'])
        self.assertTrue(file.is_file())
        self.assertEqual(len(self.db.library()), 1)
        with self.assertRaises(ValueError):
            self.db.delete_project(999999)
        self.assertEqual(self.db.delete_project(project_id), 1)
        self.assertEqual(self.db.list_projects(), [])
        self.assertEqual(self.db.list_project_videos(project_id), [])
        self.assertTrue(file.is_file())
        self.assertEqual([row['id'] for row in self.db.library()], [record_id])

    def test_video_assignment_adds_multiple_targets_without_moving_mp4(self):
        file = self.library / 'Moscow' / '31_1280x720.mp4'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'video')
        self.db.import_existing(self.library, self.root / 'downloads')
        record_id = self.db.library()[0]['id']
        first = self.db.create_project('First')
        second = self.db.create_project('Second')
        chapter = self.db.create_chapter(second, 'Opening')
        self.assertEqual(self.db.assign_videos([record_id], first), 1)
        self.assertIsNone(self.db.list_project_videos(first)[0]['chapter_id'])
        with self.assertRaises(ValueError):
            self.db.assign_videos([record_id], first, chapter)
        with self.assertRaises(ValueError):
            self.db.assign_videos_to_destinations([record_id], [(first, None), (first, 999999)])
        self.assertEqual(len(self.db.list_project_videos(first)), 1)
        self.assertEqual(self.db.assign_videos([record_id], second, chapter), 1)
        self.assertEqual(self.db.assign_videos([record_id], second, chapter), 0)
        self.assertEqual(len(self.db.list_project_videos(first)), 1)
        self.assertEqual(self.db.list_project_videos(second)[0]['chapter_id'], chapter)
        another_chapter = self.db.create_chapter(second, 'Places')
        self.assertEqual(self.db.assign_videos_to_destinations(
            [record_id], [(second, another_chapter), (second, None)]), 2)
        self.assertEqual(self.db.assign_videos_to_destinations(
            [record_id], [(second, None), (second, None)]), 0)
        self.assertEqual({row['chapter_id'] for row in self.db.list_project_videos(second)},
                         {chapter, another_chapter, None})
        self.assertTrue(file.is_file())
        self.assertEqual(len(self.db.library()), 1)
        self.db.delete_video(record_id)
        self.assertEqual(self.db.list_project_videos(first), [])
        self.assertEqual(self.db.list_project_videos(second), [])
        self.assertEqual(self.db.video_records([record_id]), [])

    def test_previously_hidden_pixabay_id_can_be_reserved_again(self):
        file = self.library / '53_1280x720.mp4'
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b'video')
        self.db.import_existing(self.library, self.root / 'downloads')
        with self.db.connect() as connection:
            connection.execute('UPDATE videos SET hidden_at=123 WHERE video_id=53')
        with self.db.batch() as owner:
            self.assertTrue(self.db.reserve(video(53), 'new search', self.library / 'new' / file.name, owner))
        with self.db.connect() as connection:
            row = connection.execute('SELECT hidden_at FROM videos WHERE video_id=53').fetchone()
        self.assertIsNone(row['hidden_at'])

    def test_existing_database_migrates_hidden_at(self):
        old = self.root / 'old.db'
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(old)) as connection:
            with connection:
                connection.execute('''CREATE TABLE videos (
                    id INTEGER PRIMARY KEY, source TEXT, video_id INTEGER,
                    metadata TEXT, keyword TEXT, file_path TEXT, thumbnail_path TEXT,
                    status TEXT, owner TEXT, error TEXT, created_at REAL,
                    updated_at REAL, completed_at REAL)''')
        migrated = VideoDatabase(old)
        with migrated.connect() as connection:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(videos)')}
            placement = connection.execute("SELECT name FROM sqlite_master WHERE name='video_placements'").fetchone()
        self.assertIn('hidden_at', columns)
        self.assertIsNotNone(placement)

    def test_legacy_single_placement_schema_migrates_without_losing_links(self):
        file = self.library / 'Moscow' / '57_1280x720.mp4'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'video')
        self.db.import_existing(self.library, self.root / 'downloads')
        record_id = self.db.library()[0]['id']
        project_id = self.db.create_project('Moscow')
        first = self.db.create_chapter(project_id, 'Opening')
        second = self.db.create_chapter(project_id, 'Places')
        self.db.assign_videos([record_id], project_id, first)
        with self.db.connect() as connection:
            connection.execute('DROP TABLE video_placements')
            connection.execute('''CREATE TABLE video_placements (
                video_record_id INTEGER PRIMARY KEY REFERENCES videos(id),
                project_id INTEGER NOT NULL REFERENCES projects(id),
                chapter_id INTEGER REFERENCES chapters(id),
                assigned_at REAL NOT NULL)''')
            connection.execute('''INSERT INTO video_placements
                (video_record_id, project_id, chapter_id, assigned_at)
                VALUES (?, ?, ?, 123)''', (record_id, project_id, first))
        migrated = VideoDatabase(self.db.path)
        self.assertEqual([row['chapter_id'] for row in migrated.list_project_videos(project_id)], [first])
        self.assertEqual(migrated.assign_videos([record_id], project_id, second), 1)
        self.assertEqual({row['chapter_id'] for row in VideoDatabase(self.db.path).list_project_videos(project_id)},
                         {first, second})

    def test_deleting_one_chapter_keeps_other_placements_and_single_project_root(self):
        file = self.library / 'Moscow' / '58_1280x720.mp4'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'video')
        self.db.import_existing(self.library, self.root / 'downloads')
        record_id = self.db.library()[0]['id']
        first_project = self.db.create_project('First')
        second_project = self.db.create_project('Second')
        chapter = self.db.create_chapter(first_project, 'Opening')
        self.db.assign_videos_to_destinations(
            [record_id], [(first_project, None), (first_project, chapter), (second_project, None)])
        self.assertEqual(self.db.delete_chapter(first_project, chapter), 1)
        self.assertEqual([row['chapter_id'] for row in self.db.list_project_videos(first_project)], [None])
        self.assertEqual(self.db.delete_project(first_project), 1)
        self.assertEqual([row['chapter_id'] for row in self.db.list_project_videos(second_project)], [None])
        self.assertTrue(file.is_file())

    def test_project_ui_creates_project_and_multiple_chapters(self):
        class Page:
            window = SimpleNamespace()

            def add(self, *controls):
                self.controls = controls

            def update(self):
                pass

        page = Page()
        with patch.object(main, 'VideoDatabase', return_value=self.db), \
             patch.object(main, 'ROOT', self.root), patch.object(main, 'LIBRARY_ROOT', self.library):
            asyncio.run(main.main(page))
            sidebar = page.controls[0].controls[0].content.controls
            panels = page.controls[0].controls[2].content.controls
            asyncio.run(sidebar[4].on_click(None))
            project_view = panels[3].content
            side_controls = project_view.controls[0].content.controls
            project_name, create_project = side_controls[1:3]
            project_name.value = 'First project'
            asyncio.run(create_project.on_click(None))
            self.assertEqual(len(self.db.list_projects()), 1)
            detail = project_view.controls[2].content.controls
            chapter_name, add_chapter = detail[2].controls
            self.assertFalse(add_chapter.disabled)
            for title in ('Opening', 'Main story'):
                chapter_name.value = title
                asyncio.run(add_chapter.on_click(None))
            self.assertEqual([row['title'] for row in self.db.list_chapters(self.db.list_projects()[0]['id'])],
                             ['Opening', 'Main story'])
            self.assertEqual(detail[0].value, 'Main story')
            self.assertEqual(detail[1].value, '0 video trong chapter')

    def test_project_sidebar_delete_icons_require_confirmation(self):
        class Page:
            window = SimpleNamespace()

            def __init__(self):
                self.dialog = None

            def add(self, *controls):
                self.controls = controls

            def update(self):
                pass

            def show_dialog(self, dialog):
                self.dialog = dialog

            def pop_dialog(self):
                self.dialog = None

        file = self.library / 'Moscow' / '61_1280x720.mp4'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'video')
        self.db.import_existing(self.library, self.root / 'downloads')
        record_id = self.db.library()[0]['id']
        keep_id = self.db.create_project('Keep')
        project_id = self.db.create_project('Delete me')
        chapter_id = self.db.create_chapter(project_id, 'Opening')
        self.db.assign_videos([record_id], project_id, chapter_id)
        page = Page()

        def entries(database, selected_project):
            return [dict(row, exists_on_disk=True, size_on_disk=5, poster_bytes=None)
                    for row in database.list_project_videos(selected_project)]

        with patch.object(main, 'VideoDatabase', return_value=self.db), \
             patch.object(main, 'ROOT', self.root), patch.object(main, 'LIBRARY_ROOT', self.library), \
             patch.object(main, 'load_project_entries', side_effect=entries):
            asyncio.run(main.main(page))
            sidebar = page.controls[0].controls[0].content.controls
            project_view = page.controls[0].controls[2].content.controls[3].content
            asyncio.run(sidebar[4].on_click(None))
            project_list = project_view.controls[0].content.controls[-1]
            chapter_delete = project_list.controls[0].controls[1].content.controls[1]
            self.assertEqual(chapter_delete.tooltip, 'Xóa chapter')
            chapter_delete.on_click(None)
            self.assertIn('Xác nhận', page.dialog.title)
            self.assertIn('Library', page.dialog.content.value)
            page.dialog.actions[0].on_click(None)
            self.assertIsNone(page.dialog)
            self.assertEqual(len(self.db.list_chapters(project_id)), 1)
            chapter_delete.on_click(None)
            asyncio.run(page.dialog.actions[1].on_click(None))
            self.assertEqual(self.db.list_chapters(project_id), [])
            self.assertIsNone(self.db.list_project_videos(project_id)[0]['chapter_id'])
            self.assertTrue(file.is_file())
            project_delete = project_list.controls[0].controls[0].content.controls[2]
            self.assertEqual(project_delete.tooltip, 'Xóa project')
            project_delete.on_click(None)
            self.assertIn('Xác nhận', page.dialog.title)
            page.dialog.actions[0].on_click(None)
            self.assertEqual({row['id'] for row in self.db.list_projects()}, {keep_id, project_id})
            project_delete.on_click(None)
            asyncio.run(page.dialog.actions[1].on_click(None))
            self.assertEqual([row['id'] for row in self.db.list_projects()], [keep_id])
            self.assertEqual(self.db.list_project_videos(project_id), [])
            self.assertTrue(file.is_file())
            self.assertEqual([row['id'] for row in self.db.library()], [record_id])
            self.assertEqual(project_view.controls[2].content.controls[0].value, 'Keep')

    def test_project_sidebar_folds_and_chapter_filters_video_grid(self):
        class Page:
            window = SimpleNamespace()

            def add(self, *controls):
                self.controls = controls

            def update(self):
                pass

            def show_dialog(self, dialog):
                self.dialog = dialog

        for video_id in (101, 102):
            file = self.library / 'Moscow' / f'{video_id}_1280x720.mp4'
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'video')
        self.db.import_existing(self.library, self.root / 'downloads')
        project_id = self.db.create_project('Moscow')
        first = self.db.create_chapter(project_id, 'Opening')
        second = self.db.create_chapter(project_id, 'Places')
        record_ids = {row['video_id']: row['id'] for row in self.db.library()}
        self.db.assign_videos([record_ids[101]], project_id, first)
        self.db.assign_videos([record_ids[102]], project_id, second)
        self.db.assign_videos([record_ids[101]], project_id, second)
        page = Page()

        def entries(database, selected_project):
            return [dict(row, exists_on_disk=True, size_on_disk=5, poster_bytes=None)
                    for row in database.list_project_videos(selected_project)]

        with patch.object(main, 'VideoDatabase', return_value=self.db), \
             patch.object(main, 'ROOT', self.root), patch.object(main, 'LIBRARY_ROOT', self.library), \
             patch.object(main, 'load_project_entries', side_effect=entries) as load:
            asyncio.run(main.main(page))
            sidebar = page.controls[0].controls[0].content.controls
            project_view = page.controls[0].controls[2].content.controls[3].content
            asyncio.run(sidebar[4].on_click(None))
            project_list = project_view.controls[0].content.controls[-1]
            detail = project_view.controls[2].content.controls
            project_grid = detail[-1]
            self.assertEqual({card.data for card in project_grid.controls}, set(record_ids.values()))
            self.assertEqual(len(project_grid.controls), 2)
            project_item = project_list.controls[0]
            self.assertEqual(len(project_item.controls), 3)
            project_item.controls[0].content.controls[0].on_click(None)
            self.assertEqual(len(project_list.controls[0].controls), 1)
            project_list.controls[0].controls[0].content.controls[0].on_click(None)
            self.assertEqual(len(project_list.controls[0].controls), 3)
            asyncio.run(project_list.controls[0].controls[1].on_click(None))
            self.assertEqual(detail[0].value, 'Opening')
            self.assertEqual([card.data for card in project_grid.controls], [record_ids[101]])
            self.assertEqual(load.call_count, 1)
            asyncio.run(project_list.controls[0].controls[2].on_click(None))
            self.assertEqual({card.data for card in project_grid.controls}, set(record_ids.values()))
            self.assertEqual(load.call_count, 1)
            project_label = project_list.controls[0].controls[0].content.controls[1]
            asyncio.run(project_label.on_click(None))
            self.assertEqual(len(project_grid.controls), 2)
            project_grid.controls[0].on_click(None)
            self.assertIsInstance(page.dialog, main.ft.AlertDialog)

    def test_project_chapter_bulk_icons_export_move_and_delete(self):
        class Page:
            window = SimpleNamespace()

            def __init__(self):
                self.services = []
                self.dialog = None

            def add(self, *controls):
                self.controls = controls

            def update(self):
                pass

            def show_dialog(self, dialog):
                self.dialog = dialog

            def pop_dialog(self):
                self.dialog = None

        files = {}
        for video_id in (111, 112):
            file = self.library / 'Moscow' / f'{video_id}_1280x720.mp4'
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(f'video {video_id}'.encode())
            files[video_id] = file
        self.db.import_existing(self.library, self.root / 'downloads')
        project_id = self.db.create_project('Moscow')
        first = self.db.create_chapter(project_id, 'Opening')
        second = self.db.create_chapter(project_id, 'Places')
        records = {row['video_id']: row['id'] for row in self.db.library()}
        self.db.assign_videos([records[111]], project_id, first)
        self.db.assign_videos([records[112]], project_id, second)
        page = Page()

        def entries(database, selected_project):
            return [dict(row, exists_on_disk=True, size_on_disk=Path(row['file_path']).stat().st_size,
                         poster_bytes=None) for row in database.list_project_videos(selected_project)]

        with patch.object(main, 'VideoDatabase', return_value=self.db), \
             patch.object(main, 'ROOT', self.root), patch.object(main, 'LIBRARY_ROOT', self.library), \
             patch.object(main, 'load_project_entries', side_effect=entries):
            asyncio.run(main.main(page))
            sidebar = page.controls[0].controls[0].content.controls
            project_view = page.controls[0].controls[2].content.controls[3].content
            asyncio.run(sidebar[4].on_click(None))
            detail = project_view.controls[2].content.controls
            select_all, count, delete, export, move = detail[-4].controls
            self.assertEqual(count.value, '0 đã chọn')
            select_all.on_click(None)
            self.assertEqual(count.value, '2 đã chọn')
            archive = self.root / 'selected-videos.zip'
            with patch.object(page.services[0], 'save_file',
                              new=AsyncMock(return_value=str(archive))) as save_file:
                asyncio.run(export.on_click(None))
            save_file.assert_awaited_once()
            with zipfile.ZipFile(archive) as contents:
                self.assertEqual(set(contents.namelist()), {files[111].name, files[112].name})
            project_list = project_view.controls[0].content.controls[-1]
            asyncio.run(project_list.controls[0].controls[1].on_click(None))
            self.assertEqual(count.value, '1 đã chọn')
            self.assertEqual([card.data for card in detail[-1].controls], [records[111]])
            export_folder = self.root / 'export'
            export_folder.mkdir()
            with patch.object(page.services[0], 'get_directory_path',
                              new=AsyncMock(return_value=str(export_folder))):
                asyncio.run(export.on_click(None))
            self.assertEqual((export_folder / files[111].name).read_bytes(), b'video 111')
            self.assertFalse((export_folder / files[112].name).exists())
            asyncio.run(move.on_click(None))
            self.assertIsNotNone(page.dialog)
            fields = page.dialog.content.content.controls
            self.assertIsInstance(fields[0], main.ft.Dropdown)
            self.assertIsInstance(fields[1].controls[0], main.ft.Dropdown)
            fields[1].controls[0].value = str(second)
            asyncio.run(page.dialog.actions[1].on_click(None))
            self.assertEqual([row['chapter_id'] for row in self.db.list_project_videos(project_id)
                              if row['id'] == records[111]], [first, second])
            self.assertTrue(files[111].exists())
            self.assertEqual(count.value, '0 đã chọn')
            project_list = project_view.controls[0].content.controls[-1]
            asyncio.run(project_list.controls[0].controls[2].on_click(None))
            self.assertEqual({card.data for card in detail[-1].controls}, set(records.values()))
            card = next(card for card in detail[-1].controls if card.data == records[111])
            selector = card.content.controls[0].content.controls[1].content
            selector.on_click(None)
            self.assertEqual(count.value, '1 đã chọn')
            asyncio.run(delete.on_click(None))
            self.assertIsNotNone(page.dialog)

            asyncio.run(page.dialog.actions[1].on_click(None))
            self.assertFalse(files[111].exists())
            self.assertEqual([row['video_id'] for row in self.db.library()], [112])
            self.assertEqual([card.data for card in detail[-1].controls], [records[112]])
            self.assertEqual(count.value, '0 đã chọn')

    def test_library_bulk_controls_export_move_and_delete(self):
        class Page:
            window = SimpleNamespace()

            def __init__(self):
                self.services = []
                self.dialog = None

            def add(self, *controls):
                self.controls = controls

            def update(self):
                pass

            def show_dialog(self, dialog):
                self.dialog = dialog

            def pop_dialog(self):
                self.dialog = None

        file = self.library / 'Moscow' / '72_1280x720.mp4'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'video')
        self.db.import_existing(self.library, self.root / 'downloads')
        project_id = self.db.create_project('Russia')
        chapter_id = self.db.create_chapter(project_id, 'Opening')
        other_project = self.db.create_project('Japan')
        other_chapter = self.db.create_chapter(other_project, 'Tokyo')
        page = Page()

        def entries(database):
            return [dict(row, exists_on_disk=True, size_on_disk=file.stat().st_size,
                         poster_bytes=None) for row in database.library()]

        with patch.object(main, 'VideoDatabase', return_value=self.db), \
             patch.object(main, 'ROOT', self.root), patch.object(main, 'LIBRARY_ROOT', self.library), \
             patch.object(main, 'load_library_entries', side_effect=entries):
            asyncio.run(main.main(page))
            sidebar = page.controls[0].controls[0].content.controls
            panels = page.controls[0].controls[2].content.controls
            asyncio.run(sidebar[3].on_click(None))
            library_view = panels[2].content
            select_all, count, delete, export, move = library_view.controls[4].controls
            select_all.on_click(None)
            self.assertEqual(count.value, '1 đã chọn')
            self.assertFalse(move.disabled)
            export_folder = self.root / 'export'
            export_folder.mkdir()
            with patch.object(page.services[0], 'get_directory_path',
                              new=AsyncMock(return_value=str(export_folder))):
                asyncio.run(export.on_click(None))
            self.assertEqual((export_folder / file.name).read_bytes(), b'video')
            asyncio.run(move.on_click(None))
            self.assertIsNotNone(page.dialog)
            fields = page.dialog.content.content.controls
            project_choice = fields[0]
            chapter_choice, add_button = fields[1].controls
            self.assertIsInstance(project_choice, main.ft.Dropdown)
            self.assertIsInstance(chapter_choice, main.ft.Dropdown)
            project_choice.value = str(project_id)
            project_choice.on_select(None)
            add_button.on_click(None)
            chapter_choice.value = str(chapter_id)
            add_button.on_click(None)
            project_choice.value = str(other_project)
            project_choice.on_select(None)
            chapter_choice.value = str(other_chapter)
            add_button.on_click(None)
            self.assertEqual(len(fields[4].controls), 3)
            fields[4].controls[1].controls[1].on_click(None)
            self.assertEqual(len(fields[4].controls), 2)
            project_choice.value = str(project_id)
            project_choice.on_select(None)
            chapter_choice.value = str(chapter_id)
            add_button.on_click(None)
            self.assertEqual(len(fields[4].controls), 3)
            asyncio.run(page.dialog.actions[1].on_click(None))
            self.assertEqual({row['chapter_id'] for row in self.db.list_project_videos(project_id)},
                             {None, chapter_id})
            self.assertEqual([row['chapter_id'] for row in self.db.list_project_videos(other_project)],
                             [other_chapter])
            asyncio.run(move.on_click(None))
            fields = page.dialog.content.content.controls
            fields[1].controls[0].value = str(other_chapter)
            asyncio.run(page.dialog.actions[1].on_click(None))
            self.assertEqual(len(self.db.list_project_videos(other_project)), 1)
            self.assertTrue(file.is_file())
            asyncio.run(delete.on_click(None))
            self.assertIsNotNone(page.dialog)

            asyncio.run(page.dialog.actions[1].on_click(None))
            self.assertEqual(self.db.library(), [])
            self.assertEqual(self.db.list_project_videos(project_id), [])
            self.assertEqual(self.db.list_project_videos(other_project), [])
            self.assertFalse(file.exists())

    def test_download_selection_stays_in_sync_between_grid_and_list(self):
        class Page:
            window = SimpleNamespace()

            def add(self, *controls):
                self.controls = controls

            def update(self):
                pass

        page = Page()
        with patch.object(main, 'VideoDatabase', return_value=self.db), \
             patch.object(main, 'ROOT', self.root), patch.object(main, 'LIBRARY_ROOT', self.library), \
             patch.dict(os.environ, {'PIXABAY_API_KEY': 'test-key'}), \
             patch('services.catalog.search_page', return_value=([video(88)], 1)), \
             patch('services.downloader.urlopen', side_effect=lambda *a, **k: Response()):
            asyncio.run(main.main(page))
            download_view = page.controls[0].controls[2].content.controls[1].content
            search_row = download_view.controls[2].controls
            search_row[0].value = 'Moscow'
            search_row[1].value = '1'
            asyncio.run(search_row[2].on_click(None))
            select_all, count, delete, export, move = download_view.controls[-2].controls
            self.assertEqual(count.value, '0 đã chọn')
            grid = download_view.controls[-1].content
            card = grid.controls[0]
            selector = card.content.controls[0].content.controls[2].content
            selector.on_click(None)
            self.assertEqual(count.value, '1 đã chọn')
            self.assertFalse(export.disabled)
            self.assertFalse(move.disabled)
            layout_buttons = download_view.controls[0].controls[1].controls
            layout_buttons[1].on_click(None)
            list_card = download_view.controls[-1].content.controls[0]
            list_selector = list_card.content.controls[0]
            self.assertEqual(selector.icon, main.ft.Icons.CHECK_BOX)
            self.assertEqual(list_selector.icon, main.ft.Icons.CHECK_BOX)
            list_selector.on_click(None)
            self.assertEqual(count.value, '0 đã chọn')
            select_all.on_click(None)
            self.assertEqual(count.value, '1 đã chọn')
            self.assertFalse(delete.disabled)


if __name__ == '__main__':
    unittest.main()
