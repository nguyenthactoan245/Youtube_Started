"""Cancellable local vocal isolation using Demucs."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from merger import ROOT, FLAGS, executable, run_ffmpeg


def find_python():
    candidates = [ROOT / '.venv/Scripts/python.exe',
                  ROOT.parent / 'YTDownloader/.venv/Scripts/python.exe', Path(sys.executable)]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            result = subprocess.run([str(candidate), '-c', 'import demucs.separate, torch'],
                                    capture_output=True, timeout=30, creationflags=FLAGS)
            if result.returncode == 0:
                return str(candidate)
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError('Thiếu thư viện tách nhạc. Hãy chạy install_music.bat trong Reup.')


async def remove_background(source, destination, duration, progress):
    interpreter = await asyncio.to_thread(find_python)
    with tempfile.TemporaryDirectory(prefix='.audio-', dir=Path(destination).parent) as folder:
        folder = Path(folder)
        audio = folder / 'input.wav'
        await run_ffmpeg(['-i', source, '-vn', '-ac', '2', '-ar', '44100', audio], duration,
                         lambda p: progress(.05 * p, 'Chuẩn bị tách nhạc nền...'))
        env = os.environ.copy()
        env['PATH'] = str(Path(executable('ffmpeg')).parent) + os.pathsep + env.get('PATH', '')
        cache = ROOT.parent / 'YTDownloader/models/torch'
        env['TORCH_HOME'] = str(cache if cache.is_dir() else ROOT / 'models/torch')
        env['PYTHONIOENCODING'] = 'utf-8'
        progress(.05, 'Đang tách nhạc nền bằng AI; lần đầu có thể cần tải mô hình...')
        with tempfile.TemporaryFile() as log:
            process = await asyncio.create_subprocess_exec(interpreter, '-m', 'demucs',
                '--two-stems', 'vocals', '-n', 'htdemucs', '-d', 'cpu', '--shifts', '0',
                '-o', str(folder / 'stems'), str(audio), env=env,
                stdout=log, stderr=log, creationflags=FLAGS)
            try:
                await process.wait()
                if process.returncode:
                    log.seek(0)
                    raise RuntimeError('Tách nhạc thất bại: ' + log.read().decode('utf-8', errors='replace')[-2000:])
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        vocals = folder / 'stems/htdemucs/input/vocals.wav'
        if not vocals.is_file():
            raise RuntimeError('Không tìm thấy âm thanh lời nói sau khi tách nhạc.')
        await run_ffmpeg(['-i', source, '-i', vocals, '-map', '0:v:0', '-map', '1:a:0',
            '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-af', 'apad', '-t', str(duration),
            '-movflags', '+faststart', destination], duration,
            lambda p: progress(.9 + .1 * p, 'Lưu video đã tắt nhạc nền...'))
