from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from main import VideoMergerApp


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = patch('main.SETTINGS', self.root / 'settings.json')
        self.settings.start()
        page = SimpleNamespace(window=SimpleNamespace(), services=[], add=lambda *a: None, update=lambda: None)
        self.app = VideoMergerApp(page)
        self.app.output.value = str(self.root)
        self.videos = [SimpleNamespace(path=self.root / f'{i}.mp4', audio={'index': 1},
                                      duration=1, dimensions=(160, 120)) for i in (2, 10)]
        for video in self.videos:
            video.path.touch()

    async def asyncTearDown(self):
        self.settings.stop()
        self.temp.cleanup()

    async def load(self, single=False):
        self.app.input.value = str(self.videos[0].path if single else self.root)
        with patch('main.probe', AsyncMock(side_effect=lambda p: next(v for v in self.videos if v.path == p))):
            await self.app.refresh()

    async def test_single_file_only_srt(self):
        await self.load(True)
        self.assertTrue(self.app.merge_enabled.disabled)
        self.assertFalse(self.app.merge_enabled.value)
        self.assertTrue(self.app.export_subtitles.value)
        self.assertFalse(self.app.mute_audio.disabled)
        self.assertFalse(self.app.remove_music.disabled)
        self.assertFalse(self.app.filename.disabled)
        self.app.filename.value = 'Viking.mp4'
        with patch('main.probe', AsyncMock(return_value=self.videos[0])), patch('main.check_available'), \
             patch('main.merge', AsyncMock()) as merge, \
             patch('main.export_srt', AsyncMock(return_value=self.root / '2.srt')) as srt:
            await self.app.start_merge(None)
        merge.assert_not_awaited()
        self.assertEqual(srt.await_args.args[1], self.root / 'Viking.srt')

    async def test_nested_folder_enables_run(self):
        nested = self.root / 'nested'
        nested.mkdir()
        for video in self.videos:
            new_path = nested / video.path.name
            video.path.rename(new_path)
            video.path = new_path
        await self.load()
        self.assertEqual(len(self.app.videos), 2)
        self.assertFalse(self.app.merge_button.disabled)
        self.app.recursive.value = False
        await self.load()
        self.assertEqual(self.app.videos, [])
        self.assertTrue(self.app.merge_button.disabled)

    async def test_folder_srt_only_and_both(self):
        await self.load(True)
        await self.load()
        self.assertFalse(self.app.merge_enabled.disabled)
        self.assertEqual([v.path.stem for v in self.app.videos], ['2', '10'])
        self.app.filename.value = 'Viking.srt'
        for merging in (False, True):
            self.app.merge_enabled.value = merging
            self.app.export_subtitles.value = True
            self.app.mute_audio.value = merging
            with patch('main.probe', AsyncMock(side_effect=self.videos)), patch('main.check_available'), \
                 patch('main.merge', AsyncMock(return_value=self.root / 'joined.mp4')) as merge, \
                 patch('main.export_srt', AsyncMock(return_value=self.root / 'result.srt')) as srt:
                await self.app.start_merge(None)
                self.assertEqual(merge.await_count, int(merging))
                self.assertEqual(srt.await_count, 1 if merging else 2)
                if not merging:
                    self.assertEqual([c.args[1].name for c in srt.await_args_list],
                                     ['Viking_001.srt', 'Viking_002.srt'])
                if merging:
                    self.assertTrue(merge.await_args.kwargs['mute_audio'])
                    self.assertEqual(srt.await_args.args[0], self.videos)
                    self.assertEqual(srt.await_args.args[1], self.root / 'joined.srt')

    async def test_merge_only_and_no_selection(self):
        await self.load()
        self.app.export_subtitles.value = False
        with patch('main.probe', AsyncMock(side_effect=self.videos)), \
             patch('main.merge', AsyncMock(return_value=self.root / 'joined.mp4')) as merge, \
             patch('main.export_srt', AsyncMock()) as srt:
            await self.app.start_merge(None)
            merge.assert_awaited_once()
            srt.assert_not_awaited()
        self.app.merge_enabled.value = False
        self.app.update_options()
        self.assertTrue(self.app.merge_button.disabled)

    async def test_remove_music_and_mute_priority(self):
        await self.load()
        self.app.export_subtitles.value = False
        self.app.remove_music.value = True
        for muted in (False, True):
            self.app.mute_audio.value = muted
            self.app.update_options()
            self.assertEqual(self.app.remove_music.disabled, muted)
            with patch('main.probe', AsyncMock(side_effect=self.videos)), \
                 patch('music_removal.find_python', return_value='python'), \
                 patch('main.merge', AsyncMock(return_value=self.root / 'voice.mp4')) as merge:
                await self.app.start_merge(None)
                self.assertEqual(merge.await_args.kwargs['remove_music'], not muted)

    async def test_single_file_audio_without_srt(self):
        await self.load(True)
        self.app.export_subtitles.value = False
        self.app.filename.value = 'voice.mp4'
        for muted in (False, True):
            self.app.mute_audio.value = muted
            self.app.remove_music.value = not muted
            self.app.update_options()
            self.assertFalse(self.app.merge_button.disabled)
            self.assertTrue(self.app.merge_enabled.disabled)
            with patch('main.probe', AsyncMock(return_value=self.videos[0])), \
                 patch('music_removal.find_python', return_value='python'), \
                 patch('main.merge', AsyncMock(return_value=self.root / 'voice.mp4')) as process, \
                 patch('main.export_srt', AsyncMock()) as srt:
                await self.app.start_merge(None)
                process.assert_awaited_once()
                self.assertEqual(process.await_args.args[0], [self.videos[0]])
                self.assertEqual(process.await_args.kwargs['mute_audio'], muted)
                self.assertEqual(process.await_args.kwargs['remove_music'], not muted)
                srt.assert_not_awaited()

    async def test_audio_options_are_mutually_exclusive(self):
        await self.load(True)
        self.app.mute_audio.value = True
        await self.app.audio_changed(None)
        self.assertTrue(self.app.remove_music.disabled)
        self.assertFalse(self.app.mute_audio.disabled)
        self.app.mute_audio.value = False
        await self.app.audio_changed(None)
        self.assertFalse(self.app.remove_music.disabled)
        self.app.remove_music.value = True
        await self.app.audio_changed(None)
        self.assertTrue(self.app.mute_audio.disabled)
        self.assertFalse(self.app.remove_music.disabled)
        self.app.set_busy(True)
        self.app.set_busy(False)
        self.assertTrue(self.app.mute_audio.disabled)
        self.app.remove_music.value = False
        await self.app.audio_changed(None)
        self.assertFalse(self.app.mute_audio.disabled)
        self.assertFalse(self.app.remove_music.disabled)
