import asyncio
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from merger import FLAGS, compatible, executable, merge, output_path, probe, scan


class MergeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='video-merger-test-')
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def clip(self, name, color='red', size='160x120', audio=True, fps=24):
        path = self.root / name
        args = [executable('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                f'color=c={color}:s={size}:r={fps}:d=0.5']
        if audio:
            args += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=0.5']
        args += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-t', '0.5', str(path)]
        subprocess.run(args, check=True, capture_output=True, creationflags=FLAGS)
        return path

    async def test_natural_order_fast_merge_unicode_and_collision(self):
        self.clip("đoạn ' 10.mp4", 'blue')
        self.clip("đoạn ' 2.mp4", 'green')
        self.clip("đoạn ' 1.mp4", 'red')
        paths = scan(self.root)
        self.assertEqual([p.name for p in paths], [f"đoạn ' {i}.mp4" for i in (1, 2, 10)])
        videos = [await probe(p) for p in paths]
        self.assertTrue(compatible(videos))
        output = self.root / 'result.mp4'
        output.write_bytes(b'keep me')
        result = await merge(videos, output, 'fast')
        self.assertEqual(output.read_bytes(), b'keep me')
        self.assertEqual(result.name, 'result_1.mp4')
        info = await probe(result)
        self.assertAlmostEqual(info.duration, 1.5, delta=.15)
        # Decode samples at each clip's midpoint to verify actual visual order.
        colors = []
        for offset in (.25, .75, 1.25):
            pixel = subprocess.run([executable('ffmpeg'), '-v', 'error', '-ss', str(offset),
                '-i', str(result), '-frames:v', '1', '-vf', 'scale=1:1', '-f', 'rawvideo',
                '-pix_fmt', 'rgb24', 'pipe:1'], capture_output=True, check=True, creationflags=FLAGS).stdout
            colors.append(max(range(3), key=lambda i: pixel[i]))
        self.assertEqual(colors, [0, 1, 2])

    async def test_normalize_mixed_dimensions_fps_and_missing_audio(self):
        paths = [self.clip('a.mp4', audio=False), self.clip('b.mp4', size='120x160', fps=30)]
        videos = [await probe(p) for p in paths]
        self.assertFalse(compatible(videos))
        with self.assertRaises(ValueError):
            await merge(videos, self.root / 'fast.mp4', 'fast')
        progress = []
        result = await merge(videos, self.root / 'normalized.mp4', progress=lambda p, m: progress.append(p))
        info = await probe(result)
        self.assertEqual(info.dimensions, (160, 120))
        self.assertIsNotNone(info.audio)
        self.assertAlmostEqual(info.duration, 1, delta=.15)
        self.assertEqual(progress[-1], 1)
        self.assertEqual(progress, sorted(progress))

    async def test_silent_normalize_and_cancel_cleanup(self):
        videos = [await probe(self.clip('silent.mp4', audio=False))]
        result = await merge(videos, self.root / 'silent_result.mp4', 'normalize')
        self.assertIsNone((await probe(result)).audio)
        task = None
        def cancel_on_progress(p, message):
            task.cancel()
        task = asyncio.create_task(merge(videos, self.root / 'cancelled.mp4', 'normalize', cancel_on_progress))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse((self.root / 'cancelled.mp4').exists())
        self.assertFalse(list(self.root.glob('.merge-*')))

    async def test_invalid_files_and_paths(self):
        corrupt = self.root / 'broken.mp4'
        corrupt.write_text('not a video')
        with self.assertRaises(ValueError):
            await probe(corrupt)
        for name in ('../bad.mp4', 'CON.mp4', 'bad?.mp4', ''):
            with self.assertRaises(ValueError):
                output_path(self.root, name)
        self.assertEqual(scan(self.root, [corrupt]), [])

    async def test_mute_all_modes_with_mixed_audio(self):
        videos = [await probe(self.clip('voice.mp4')),
                  await probe(self.clip('silent.mp4', audio=False))]
        self.assertFalse(compatible(videos))
        self.assertTrue(compatible(videos, mute_audio=True))
        for mode in ('fast', 'auto', 'normalize'):
            with self.subTest(mode=mode):
                result = await merge(videos, self.root / f'{mode}.mp4', mode, mute_audio=True)
                info = await probe(result)
                self.assertIsNone(info.audio)
                self.assertEqual(info.dimensions, videos[0].dimensions)
                self.assertAlmostEqual(info.duration, 1, delta=.15)
        self.assertIsNotNone((await probe(videos[0].path)).audio)


class UIConstructionTests(unittest.TestCase):
    def test_build_controls(self):
        from main import VideoMergerApp
        page = SimpleNamespace(window=SimpleNamespace(), services=[], add=lambda *args: None)
        app = VideoMergerApp(page)
        self.assertTrue(app.merge_button.disabled)
        self.assertEqual(len(page.services), 1)
        app.render_list()


if __name__ == '__main__':
    unittest.main()
