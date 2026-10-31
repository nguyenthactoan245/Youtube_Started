import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from subtitles import check_available, cues, export_srt, publish, timestamp, worker


class SubtitleTests(unittest.TestCase):
    def test_system_python_falls_back_to_local_environment(self):
        with patch('subtitles.importlib.util.find_spec', return_value=None), \
             patch('subtitles.Path.is_file', return_value=True), \
             patch('subtitles.sys.executable', 'C:/other/python.exe'), \
             patch('subtitles.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            interpreter = check_available()
        self.assertIn('.venv', interpreter)
        self.assertEqual(run.call_args.args[0][0], interpreter)

    def test_timestamps_and_clipping(self):
        self.assertEqual(timestamp(3599.9996), '01:00:00,000')
        segments = [SimpleNamespace(start=-1, end=2, text=' Xin\n chào '),
                    SimpleNamespace(start=3, end=8, text='Kết thúc'),
                    SimpleNamespace(start=8, end=9, text='ngoài clip')]
        self.assertEqual(list(cues(segments, 10, 5)),
                         [(10, 12, 'Xin chào'), (13, 15, 'Kết thúc')])

    def test_publish_preserves_existing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dest = root / 'video.srt'
            dest.write_text('original')
            source = root / 'temp.srt'
            source.write_text('new')
            result = publish(source, dest)
            self.assertEqual(dest.read_text(), 'original')
            self.assertEqual(result.name, 'video_1.srt')
            self.assertEqual(result.read_text(), 'new')

    def test_worker_offsets_preserve_silent_gap_and_unicode(self):
        class Model:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self, path, **kwargs):
                return iter([SimpleNamespace(start=.1, end=.9, text='Xin chào')]), None

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = root / 'result.srt'
            manifest = root / 'job.json'
            manifest.write_text(json.dumps({'model': 'base', 'language': 'vi', 'result': str(result),
                'total': 8, 'entries': [{'path': 'a.wav', 'offset': 0, 'duration': 2},
                                      {'path': 'b.wav', 'offset': 7, 'duration': 1}]}))
            with patch.dict('sys.modules', {'faster_whisper': SimpleNamespace(WhisperModel=Model)}):
                worker(manifest)
            self.assertEqual(result.read_text(encoding='utf-8'),
                '1\n00:00:00,100 --> 00:00:00,900\nXin chào\n\n'
                '2\n00:00:07,100 --> 00:00:07,900\nXin chào\n\n')


class ExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_audio_does_not_publish(self):
        with patch('subtitles.check_available'):
            with self.assertRaisesRegex(ValueError, 'âm thanh'):
                await export_srt([SimpleNamespace(audio=None)], 'unused.srt')

    async def test_cancel_kills_worker_and_cleans_temp_files(self):
        started = asyncio.Event()

        class Process:
            returncode = None
            stdout = None

            async def readline(self):
                started.set()
                await asyncio.Event().wait()

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        process = Process()
        process.stdout = process
        with tempfile.TemporaryDirectory() as folder:
            with patch('subtitles.check_available'), patch('subtitles.run_ffmpeg'), \
                 patch('subtitles.asyncio.create_subprocess_exec', return_value=process):
                task = asyncio.create_task(export_srt([
                    SimpleNamespace(audio={'index': 1}, path='source.mp4', duration=1)],
                    Path(folder) / 'result.srt'))
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(process.returncode, -9)
            self.assertEqual(list(Path(folder).iterdir()), [])
