"""Video scanning and cancellable FFmpeg jobs; no UI dependency."""
import asyncio
from dataclasses import dataclass
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
EXTENSIONS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.mts', '.ts'}
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

def executable(name):
    local = ROOT / 'bin' / (name + ('.exe' if os.name == 'nt' else ''))
    result = str(local) if local.is_file() else shutil.which(name)
    if not result:
        raise RuntimeError('Thiếu FFmpeg/FFprobe. Hãy chạy install.bat trước.')
    return result

def natural_key(path):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r'(\d+)', Path(path).name)]

def scan(folder, excluded=(), recursive=False):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError('Thư mục đầu vào không tồn tại.')
    excluded = {Path(p).resolve() for p in excluded}
    paths = folder.rglob('*') if recursive else folder.iterdir()
    return sorted((p for p in paths if p.is_file() and p.suffix.lower() in EXTENSIONS
                   and not any(part.startswith(('.merge-', '.srt-')) for part in p.relative_to(folder).parts)
                   and p.resolve() not in excluded
                   and not any(parent in excluded for parent in p.resolve().parents)),
                  key=lambda p: (natural_key(p), str(p)))

@dataclass
class Video:
    path: Path
    duration: float
    video: dict
    audio: dict | None

    @property
    def dimensions(self):
        w, h = self.video['width'], self.video['height']
        rotation = next((s.get('rotation', 0) for s in self.video.get('side_data_list', [])
                         if 'rotation' in s), 0)
        return (h, w) if abs(rotation) % 180 == 90 else (w, h)

async def probe(path):
    process = await asyncio.create_subprocess_exec(executable('ffprobe'), '-v', 'error',
        '-show_streams', '-show_format', '-show_data_hash', 'sha256', '-of', 'json', str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=FLAGS)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise ValueError(f'Không đọc được {Path(path).name}: {stderr.decode(errors="replace")[-800:]}')
    data = json.loads(stdout)
    video = next((s for s in data['streams'] if s['codec_type'] == 'video'
                  and not s.get('disposition', {}).get('attached_pic')), None)
    audio = next((s for s in data['streams'] if s['codec_type'] == 'audio'), None)
    if not video:
        raise ValueError(f'{Path(path).name}: không có luồng video.')
    duration = float(video.get('duration') or data['format'].get('duration') or 0)
    if duration <= 0:
        raise ValueError(f'{Path(path).name}: thời lượng không hợp lệ.')
    return Video(Path(path), duration, video, audio)

def compatible(videos, mute_audio=False):
    def signature(item):
        keys = ('codec_name', 'profile', 'width', 'height', 'pix_fmt', 'time_base',
                'r_frame_rate', 'avg_frame_rate', 'sample_aspect_ratio', 'extradata_hash',
                'color_space', 'color_transfer', 'color_primaries', 'side_data_list')
        akeys = ('codec_name', 'sample_rate', 'channels', 'channel_layout', 'time_base', 'extradata_hash')
        return ([item.video.get(k) for k in keys],
                [item.audio.get(k) for k in akeys] if item.audio and not mute_audio else None)
    first = videos[0]
    return (first.video.get('codec_name') == 'h264'
            and (mute_audio or first.audio is None or first.audio.get('codec_name') == 'aac')
            and all(signature(v) == signature(first) for v in videos))

def output_path(folder, name, extension='.mp4'):
    name = name.strip()
    if not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith(('.', ' ')):
        raise ValueError('Tên file không hợp lệ. Chỉ nhập tên, không nhập đường dẫn.')
    if name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
            *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        raise ValueError('Tên file này được Windows dành riêng.')
    if not name.lower().endswith(extension):
        name += extension
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError('Thư mục đầu ra không tồn tại.')
    candidate = folder / name
    number = 1
    while candidate.exists():
        candidate = folder / f'{Path(name).stem}_{number}{extension}'
        number += 1
    return candidate

async def run_ffmpeg(args, duration, progress):
    with tempfile.TemporaryFile() as errors:
        process = await asyncio.create_subprocess_exec(executable('ffmpeg'), '-hide_banner',
            '-nostdin', '-y', '-loglevel', 'error', '-progress', 'pipe:1', '-nostats', *map(str, args),
            stdout=asyncio.subprocess.PIPE, stderr=errors, creationflags=FLAGS)
        try:
            while line := await process.stdout.readline():
                text = line.decode(errors='replace').strip()
                if text.startswith('out_time_us='):
                    try:
                        progress(max(0, min(.99, int(text.split('=')[1]) / 1e6 / duration)))
                    except ValueError:
                        pass
            await process.wait()
            if process.returncode:
                errors.seek(0)
                raise RuntimeError(errors.read().decode(errors='replace')[-6000:] or 'FFmpeg thất bại.')
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    progress(1)

async def merge(videos, destination, mode='auto', progress=lambda value, message: None, *, mute_audio=False, remove_music=False):
    if not videos:
        raise ValueError('Chưa có video để ghép.')
    destination = Path(destination)
    if destination.resolve() in {v.path.resolve() for v in videos}:
        raise ValueError('File đầu ra trùng với video nguồn.')
    fast = compatible(videos, mute_audio=mute_audio)
    if mode == 'fast' and not fast:
        raise ValueError('Video không tương thích để ghép nhanh. Hãy chọn Tự động hoặc Chuẩn hóa.')
    normalize = mode == 'normalize' or not fast
    total = sum(v.duration for v in videos)
    with tempfile.TemporaryDirectory(prefix='.merge-', dir=destination.parent) as temp:
        temp = Path(temp)
        paths = [v.path.resolve() for v in videos]
        if normalize:
            width, height = videos[0].dimensions
            width, height = width + width % 2, height + height % 2
            rate = Fraction(videos[0].video.get('avg_frame_rate', '0/1'))
            if rate <= 0:
                rate = Fraction(30)
            with_audio = not mute_audio and any(v.audio for v in videos)
            paths = []
            elapsed = 0
            for index, item in enumerate(videos):
                out = temp / f'clip_{index:06}.mp4'
                args = ['-i', item.path]
                if with_audio and not item.audio:
                    args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
                args += ['-map', f'0:{item.video["index"]}', '-vf',
                    f'setpts=PTS-STARTPTS,scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={rate}',
                    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
                    '-video_track_timescale', '90000']
                if with_audio:
                    args += ['-map', f'0:{item.audio["index"]}' if item.audio else '1:a:0',
                             '-af', 'asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,apad',
                             '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2']
                args += ['-map_metadata', '-1', '-t', str(item.duration), out]
                await run_ffmpeg(args, item.duration, lambda p: progress(
                    .9 * (elapsed + p * item.duration) / total,
                    f'Chuẩn hóa {index + 1}/{len(videos)}: {item.path.name}'))
                elapsed += item.duration
                paths.append(out)
        elif mute_audio:
            # Strip audio before concat so differing stream layouts and audio
            # durations cannot affect video stream matching or clip boundaries.
            paths = []
            elapsed = 0
            for index, item in enumerate(videos):
                out = temp / f'video_{index:06}.mp4'
                await run_ffmpeg(['-i', item.path, '-map', f'0:{item.video["index"]}',
                    '-c:v', 'copy', '-an', out], item.duration,
                    lambda p: progress(.1 * (elapsed + p * item.duration) / total,
                        f'Bỏ âm thanh {index + 1}/{len(videos)}: {item.path.name}'))
                elapsed += item.duration
                paths.append(out)
        listing = temp / 'inputs.txt'
        def quote(path):
            value = path.as_posix()
            if '\n' in value or '\r' in value:
                raise ValueError('Tên file chứa ký tự xuống dòng không được hỗ trợ.')
            return "'" + value.replace("'", "'\\''") + "'"
        listing.write_text(''.join(f'file {quote(p)}\n' for p in paths), encoding='utf-8')
        output = temp / 'result.mp4'
        audio_args = ['-an'] if mute_audio else ['-map', '0:a:0?']
        base = .9 if normalize else .1 if mute_audio else 0
        await run_ffmpeg(['-f', 'concat', '-safe', '0', '-i', listing,
            '-map', '0:v:0', *audio_args, '-c', 'copy', '-movflags', '+faststart', output],
            total, lambda p: progress(base + (1 - base) * p, 'Đang ghép và hoàn thiện file...'))
        info = await probe(output)
        if remove_music and not mute_audio and info.audio:
            from music_removal import remove_background
            filtered = temp / 'voice_only.mp4'
            await remove_background(output, filtered, info.duration, progress)
            await probe(filtered)
            output = filtered
        # Exclusive publication prevents overwriting a file created while the job ran.
        destination = output_path(destination.parent, destination.name)
        if os.name == 'nt':
            output.rename(destination)
        else:
            os.link(output, destination)
        progress(1, f'Hoàn tất: {destination.name}')
        return destination
