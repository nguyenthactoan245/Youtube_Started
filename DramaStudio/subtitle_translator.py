#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline subtitle translation and hard-sub rendering for DramaStudio."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import requests


MODEL_REPO = "cstr/madlad400-3b-ct2-int8"
MODEL_FILES = (
    "config.json",
    "model.bin",
    "shared_vocabulary.json",
    "spiece.model",
    "README.md",
)
TARGET_LANGUAGES = {
    "vi": {"name": "Tiếng Việt", "font": "Arial"},
    "es": {"name": "Tiếng Tây Ban Nha", "font": "Arial"},
    "ja": {"name": "Tiếng Nhật", "font": "Yu Gothic UI"},
    "ko": {"name": "Tiếng Hàn", "font": "Malgun Gothic"},
}

_TIMESTAMP_RE = re.compile(
    r"^(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})(?:\s+.*)?$"
)


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start_ms: int
    end_ms: int
    text: str


def _timestamp_to_ms(parts: tuple[str, ...]) -> int:
    hours, minutes, seconds, millis = (int(v) for v in parts)
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + millis


def _format_srt_time(value_ms: int) -> str:
    value_ms = max(0, int(value_ms))
    millis = value_ms % 1000
    total_seconds = value_ms // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _format_ass_time(value_ms: int) -> str:
    value_ms = max(0, int(value_ms))
    centis = (value_ms + 5) // 10
    cs = centis % 100
    total_seconds = centis // 100
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def parse_srt(content: str) -> list[SubtitleCue]:
    """Parse SRT while validating timestamps and retaining multiline text."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = re.split(r"\n{2,}", normalized.strip()) if normalized.strip() else []
    cues: list[SubtitleCue] = []
    for block in blocks:
        lines = block.split("\n")
        if not lines:
            continue
        if lines[0].strip().isdigit():
            raw_index = int(lines[0].strip())
            lines = lines[1:]
        else:
            raw_index = len(cues) + 1
        if not lines:
            continue
        match = _TIMESTAMP_RE.match(lines[0].strip())
        if not match:
            raise ValueError(f"Mốc thời gian SRT không hợp lệ ở đoạn {raw_index}.")
        start_ms = _timestamp_to_ms(match.groups()[:4])
        end_ms = _timestamp_to_ms(match.groups()[4:])
        if end_ms <= start_ms:
            raise ValueError(f"Thời gian kết thúc phải lớn hơn thời gian bắt đầu ở đoạn {raw_index}.")
        text = "\n".join(line.strip() for line in lines[1:]).strip()
        if text:
            cues.append(SubtitleCue(raw_index, start_ms, end_ms, text))
    if not cues:
        raise ValueError("File SRT không có đoạn phụ đề hợp lệ.")
    return cues


def read_srt(path: str) -> list[SubtitleCue]:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1258"):
        try:
            return parse_srt(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    raise ValueError("Không đọc được encoding của file SRT.")


def render_srt(cues: Iterable[SubtitleCue]) -> str:
    blocks = []
    for idx, cue in enumerate(cues, 1):
        blocks.append(
            f"{idx}\n{_format_srt_time(cue.start_ms)} --> {_format_srt_time(cue.end_ms)}\n{cue.text.strip()}"
        )
    return "\n\n".join(blocks) + "\n"


def write_text_atomic(path: str, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".part")
    temp.write_text(content, encoding="utf-8-sig", newline="\n")
    os.replace(temp, target)


def render_ass(cues: Iterable[SubtitleCue], language: str) -> str:
    font = TARGET_LANGUAGES.get(language, {}).get("font", "Arial")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: DramaStudio,{font},46,&H00FFFFFF,&H000000FF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,3,1,2,70,70,48,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for cue in cues:
        text = re.sub(r"<[^>]+>", "", cue.text)
        text = text.replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")
        events.append(
            f"Dialogue: 0,{_format_ass_time(cue.start_ms)},{_format_ass_time(cue.end_ms)},"
            f"DramaStudio,,0,0,0,,{text}"
        )
    return header + "\n".join(events) + "\n"


def find_matching_srt(video_path: str) -> str:
    video = Path(video_path)
    candidates = [
        video.with_suffix(".vi.srt"),
        video.with_suffix(".srt"),
    ]
    stem = video.stem
    for suffix in (".es.HardSub", ".ja.HardSub", ".ko.HardSub"):
        if stem.endswith(suffix):
            candidates.insert(0, video.with_name(stem[: -len(suffix)] + ".vi.srt"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    for language in ("vi", "en"):
        nearby = sorted(video.parent.glob(f"{video.stem}*.{language}.srt"))
        if nearby:
            return str(nearby[0])
    nearby = sorted(video.parent.glob(f"{video.stem}*.srt"))
    return str(nearby[0]) if nearby else ""


def model_is_ready(model_dir: str) -> bool:
    root = Path(model_dir)
    return all((root / name).is_file() and (root / name).stat().st_size > 0 for name in MODEL_FILES[:-1])


def download_model(
    model_dir: str,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Download required CTranslate2 files with resumable .part files."""
    root = Path(model_dir)
    root.mkdir(parents=True, exist_ok=True)
    total_files = len(MODEL_FILES)
    for file_index, filename in enumerate(MODEL_FILES):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Đã hủy tải model.")
        target = root / filename
        if target.is_file() and target.stat().st_size > 0:
            if progress_cb:
                progress_cb(((file_index + 1) / total_files) * 100, f"Đã có {filename}")
            continue
        part = target.with_name(target.name + ".part")
        existing = part.stat().st_size if part.exists() else 0
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/main/{filename}?download=true"
        remote_size = 0
        try:
            head = requests.head(url, timeout=(20, 60), allow_redirects=True)
            if head.ok:
                remote_size = int(head.headers.get("Content-Length") or 0)
        except requests.RequestException:
            pass
        if existing and remote_size and existing == remote_size:
            os.replace(part, target)
            if progress_cb:
                progress_cb(((file_index + 1) / total_files) * 100, f"Đã có {filename}")
            continue
        if existing and remote_size and existing > remote_size:
            part.unlink()
            existing = 0
        headers = {"Range": f"bytes={existing}-"} if existing else {}
        with requests.get(url, headers=headers, stream=True, timeout=(20, 120), allow_redirects=True) as response:
            if response.status_code == 416 and part.exists():
                if remote_size and part.stat().st_size == remote_size:
                    os.replace(part, target)
                    continue
                part.unlink()
                return download_model(model_dir, progress_cb, cancel_event)
            response.raise_for_status()
            append = existing > 0 and response.status_code == 206
            if existing and not append:
                existing = 0
            content_length = int(response.headers.get("Content-Length") or 0)
            expected = existing + content_length if content_length else 0
            mode = "ab" if append else "wb"
            downloaded = existing
            with open(part, mode) as output:
                for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                    if cancel_event and cancel_event.is_set():
                        raise RuntimeError("Đã hủy tải model.")
                    if not chunk:
                        continue
                    output.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        within = (downloaded / expected) if expected else 0.0
                        overall = ((file_index + within) / total_files) * 100
                        progress_cb(overall, f"Đang tải {filename} ({downloaded / 1_073_741_824:.2f} GB)")
        if expected and part.stat().st_size != expected:
            raise RuntimeError(f"File model tải chưa đủ: {filename}")
        os.replace(part, target)
    if not model_is_ready(model_dir):
        raise RuntimeError("Model MADLAD-400 chưa đầy đủ sau khi tải.")


class MadladTranslator:
    """Run MADLAD-400 on CUDA when available, with a CPU INT8 fallback."""

    def __init__(self, model_dir: str, device: str = "auto"):
        if not model_is_ready(model_dir):
            raise RuntimeError("Chưa tải model MADLAD-400 INT8.")
        try:
            import ctranslate2
            import sentencepiece
        except ImportError as exc:
            raise RuntimeError(f"Không nạp được CTranslate2/SentencePiece: {exc}") from exc
        self.model_dir = model_dir
        self._ctranslate2 = ctranslate2
        self.engine = "CPU INT8"
        self.cuda_error = ""
        self.translator = None
        if device not in ("auto", "cpu", "cuda"):
            raise ValueError(f"Thiết bị dịch không hợp lệ: {device}")
        if device != "cpu":
            try:
                if ctranslate2.get_cuda_device_count() < 1:
                    raise RuntimeError("Không có GPU CUDA khả dụng")
                supported = ctranslate2.get_supported_compute_types("cuda")
                compute_type = "int8_float16" if "int8_float16" in supported else "int8"
                self.translator = ctranslate2.Translator(model_dir, device="cuda", compute_type=compute_type)
                self.engine = f"CUDA {compute_type.upper()}"
            except Exception as exc:
                self.cuda_error = str(exc)
        if self.translator is None:
            self._use_cpu()
        self.tokenizer = sentencepiece.SentencePieceProcessor(
            model_file=str(Path(model_dir) / "spiece.model")
        )

    def _use_cpu(self) -> None:
        self.translator = None
        import gc
        gc.collect()
        self.translator = self._ctranslate2.Translator(self.model_dir, device="cpu", compute_type="int8")
        self.engine = "CPU INT8"

    @staticmethod
    def _clean_source(text: str) -> str:
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("\n", " ")
        return re.sub(r"\s+", " ", text).strip()

    def translate_texts(self, texts: list[str], target_language: str) -> list[str]:
        if target_language not in TARGET_LANGUAGES:
            raise ValueError(f"Ngôn ngữ không được hỗ trợ: {target_language}")
        encoded = [
            self.tokenizer.encode(f"<2{target_language}> {self._clean_source(text)}", out_type=str)
            for text in texts
        ]
        try:
            results = self.translator.translate_batch(
                encoded, beam_size=2, max_decoding_length=256,
                batch_type="tokens", max_batch_size=2048,
            )
        except Exception as exc:
            if not self.engine.startswith("CUDA"):
                raise
            self.cuda_error = str(exc)
            self._use_cpu()
            results = self.translator.translate_batch(
                encoded, beam_size=2, max_decoding_length=256,
                batch_type="tokens", max_batch_size=2048,
            )
        translated = [self.tokenizer.decode(item.hypotheses[0]).strip() for item in results]
        if len(translated) != len(texts):
            raise RuntimeError("Model trả về thiếu đoạn phụ đề.")
        return translated


def translate_cues(
    cues: list[SubtitleCue],
    target_language: str,
    translator,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
    batch_size: int = 24,
) -> list[SubtitleCue]:
    output: list[SubtitleCue] = []
    cache: dict[str, str] = {}
    total = len(cues)
    for start in range(0, total, batch_size):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Đã hủy dịch phụ đề.")
        batch = cues[start : start + batch_size]
        missing = []
        for cue in batch:
            if cue.text not in cache and cue.text not in missing:
                missing.append(cue.text)
        if missing:
            translated = translator.translate_texts(missing, target_language)
            cache.update(zip(missing, translated))
        output.extend(
            SubtitleCue(cue.index, cue.start_ms, cue.end_ms, cache.get(cue.text, cue.text))
            for cue in batch
        )
        if progress_cb:
            progress_cb(min(start + len(batch), total), total)
    return output


def _run_ffmpeg(
    command: list[str],
    cwd: str,
    duration: float,
    output_path: str,
    progress_cb: Callable[[float, str, str], None] | None,
    cancel_event: threading.Event | None,
) -> tuple[int, str]:
    no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=no_window,
    )
    error_tail: list[str] = []
    speed = ""
    fps = ""
    assert process.stdout is not None
    for raw_line in process.stdout:
        if cancel_event and cancel_event.is_set():
            process.kill()
            process.wait()
            return -1, "Đã hủy render video."
        line = raw_line.strip()
        if "=" not in line:
            if line:
                error_tail.append(line)
                error_tail = error_tail[-20:]
            continue
        key, value = line.split("=", 1)
        if key == "speed":
            speed = value
        elif key == "fps":
            fps = value
        elif key in ("out_time_ms", "out_time_us"):
            current = float(value or 0) / 1_000_000.0
            pct = min(99.0, (current / duration) * 100.0) if duration > 0 else 0.0
            if progress_cb:
                progress_cb(pct, speed, fps)
        elif key == "progress" and value == "end" and progress_cb:
            progress_cb(100.0, speed, fps)
    return process.wait(), "\n".join(error_tail)


def burn_subtitle(
    video_path: str,
    ass_path: str,
    output_path: str,
    gpu_info: dict,
    duration: float,
    progress_cb: Callable[[float, str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("Không tìm thấy FFmpeg.")
    work_dir = str(Path(video_path).resolve().parent)
    temp_path = str(Path(output_path).with_name(Path(output_path).stem + ".part.mp4"))
    ass_name = Path(ass_path).name.replace("'", "\\'").replace(":", "\\:")
    video_name = Path(video_path).name
    temp_name = Path(temp_path).name

    encoders = [
        (gpu_info.get("label", "GPU"), gpu_info.get("hwaccel_args", []), gpu_info.get("encode_args", [])),
    ]
    if gpu_info.get("codec") != "libx264":
        encoders.append(("CPU libx264", [], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]))

    last_error = ""
    try:
        for label, hw_args, encode_args in encoders:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            command = (
                ["ffmpeg", "-y", "-loglevel", "error"]
                + list(hw_args)
                + ["-i", video_name, "-vf", f"ass='{ass_name}'"]
                + list(encode_args)
                + ["-c:a", "copy", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", temp_name]
            )
            rc, last_error = _run_ffmpeg(
                command, work_dir, duration, temp_path, progress_cb, cancel_event
            )
            if rc == 0 and os.path.isfile(temp_path) and os.path.getsize(temp_path) > 0:
                os.replace(temp_path, output_path)
                return label
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Đã hủy render video.")
        raise RuntimeError(f"FFmpeg hard-sub lỗi: {last_error[-300:]}")
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


class TranslationManager:
    def __init__(self, app_dir: str, gpu_detector: Callable[[], dict], duration_reader: Callable[[str], float]):
        self.app_dir = app_dir
        self.model_dir = os.path.join(app_dir, "models", "madlad400-3b-ct2-int8")
        bundled_model = os.path.join(app_dir, "dist", "models", "madlad400-3b-ct2-int8")
        if not model_is_ready(self.model_dir) and model_is_ready(bundled_model):
            self.model_dir = bundled_model
        self.gpu_detector = gpu_detector
        self.duration_reader = duration_reader
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.running = False
        self.downloading_model = False
        self.status = "Sẵn sàng dịch phụ đề offline bằng MADLAD-400."
        self.percent = 0.0
        self.current_language = ""
        self.engine = ""
        self.speed = ""
        self.fps = ""
        self.results: list[dict] = []

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "translationRunning": self.running,
                "translationDownloadingModel": self.downloading_model,
                "translationModelReady": model_is_ready(self.model_dir),
                "translationModelDir": self.model_dir,
                "translationStatus": self.status,
                "translationPct": round(self.percent, 1),
                "translationLanguage": self.current_language,
                "translationEngine": self.engine,
                "translationSpeed": self.speed,
                "translationFps": self.fps,
                "translationResults": [dict(item) for item in self.results],
            }

    def start_model_download(self) -> tuple[bool, str]:
        with self.lock:
            if self.running or self.downloading_model:
                return False, "Đang có tác vụ dịch hoặc tải model chạy."
            if model_is_ready(self.model_dir):
                return False, "Model MADLAD-400 đã sẵn sàng."
            self.downloading_model = True
            self.cancel_event = threading.Event()
            self.percent = 0
            self.status = "Chuẩn bị tải model MADLAD-400 INT8…"

        def worker():
            try:
                def on_progress(pct: float, message: str):
                    with self.lock:
                        self.percent = pct
                        self.status = message

                download_model(self.model_dir, on_progress, self.cancel_event)
                with self.lock:
                    self.percent = 100
                    self.status = "✅ Model MADLAD-400 INT8 đã sẵn sàng."
            except Exception as exc:
                with self.lock:
                    self.percent = 0
                    self.status = f"Lỗi tải model: {exc}"
            finally:
                with self.lock:
                    self.downloading_model = False

        threading.Thread(target=worker, daemon=True).start()
        return True, ""

    def cancel(self) -> None:
        with self.lock:
            self.cancel_event.set()
            self.status = "Đang hủy tác vụ…"

    def start_translation(
        self,
        video_path: str,
        source_srt: str,
        target_languages: list[str],
        burn_video: bool = True,
        translator_factory: Callable[[str], object] | None = None,
    ) -> tuple[bool, str]:
        video_path = os.path.abspath(str(video_path or "").strip())
        source_srt = os.path.abspath(str(source_srt or "").strip())
        languages = list(dict.fromkeys(str(lang).lower() for lang in target_languages))
        invalid = [lang for lang in languages if lang not in TARGET_LANGUAGES]
        if invalid:
            return False, f"Ngôn ngữ không được hỗ trợ: {', '.join(invalid)}"
        if not languages:
            return False, "Hãy chọn ít nhất một ngôn ngữ đích."
        if not os.path.isfile(video_path):
            return False, "Video nguồn không tồn tại."
        if not os.path.isfile(source_srt):
            return False, "File SRT nguồn không tồn tại."
        try:
            read_srt(source_srt)
        except Exception as exc:
            return False, f"SRT nguồn không hợp lệ: {exc}"
        if translator_factory is None and not model_is_ready(self.model_dir):
            return False, "Chưa tải model MADLAD-400. Hãy tải model trước."

        with self.lock:
            if self.running or self.downloading_model:
                return False, "Đang có tác vụ dịch hoặc tải model chạy."
            self.running = True
            self.cancel_event = threading.Event()
            self.status = "Đang nạp model MADLAD-400…"
            self.percent = 0
            self.current_language = ""
            self.engine = ""
            self.speed = ""
            self.fps = ""
            self.results = []

        def worker():
            try:
                cues = read_srt(source_srt)
                runtime = translator_factory(self.model_dir) if translator_factory else MadladTranslator(self.model_dir)
                with self.lock:
                    self.engine = getattr(runtime, "engine", "")
                    self.status = f"Đã nạp MADLAD-400 trên {self.engine}." if self.engine else "Đã nạp MADLAD-400."
                gpu_info = self.gpu_detector() if burn_video else {}
                duration = self.duration_reader(video_path) if burn_video else 0.0
                base = os.path.splitext(video_path)[0]
                total_languages = len(languages)

                for lang_index, language in enumerate(languages):
                    if self.cancel_event.is_set():
                        raise RuntimeError("Đã hủy dịch phụ đề.")
                    language_name = TARGET_LANGUAGES[language]["name"]
                    with self.lock:
                        self.current_language = language
                        self.status = f"Đang dịch {language_name} · {self.engine}…" if self.engine else f"Đang dịch {language_name}…"

                    def on_translate(done: int, total: int):
                        local = (done / total) * (45 if burn_video else 100)
                        with self.lock:
                            self.engine = getattr(runtime, "engine", self.engine)
                            self.percent = ((lang_index + local / 100) / total_languages) * 100
                            self.status = f"Đang dịch {language_name}: {done}/{total} đoạn · {self.engine}" if self.engine else f"Đang dịch {language_name}: {done}/{total} đoạn"

                    translated_cues = translate_cues(
                        cues, language, runtime, on_translate, self.cancel_event
                    )
                    srt_path = f"{base}.{language}.srt"
                    ass_path = f"{base}.{language}.ass"
                    write_text_atomic(srt_path, render_srt(translated_cues))
                    write_text_atomic(ass_path, render_ass(translated_cues, language))
                    result = {
                        "language": language,
                        "language_name": language_name,
                        "translation_engine": self.engine,
                        "out_srt": srt_path,
                        "out_ass": ass_path,
                        "out_mp4": "",
                        "engine": "",
                    }

                    if burn_video:
                        out_mp4 = f"{base}.{language}.HardSub.mp4"
                        with self.lock:
                            self.status = f"Đang chèn phụ đề {language_name} vào video…"

                        def on_render(pct: float, speed: str, fps: str):
                            local = 45 + (pct * 0.55)
                            with self.lock:
                                self.percent = ((lang_index + local / 100) / total_languages) * 100
                                self.speed = speed
                                self.fps = fps
                                self.status = f"Đang render {language_name}: {pct:.0f}%"

                        engine = burn_subtitle(
                            video_path,
                            ass_path,
                            out_mp4,
                            gpu_info,
                            duration,
                            on_render,
                            self.cancel_event,
                        )
                        result["out_mp4"] = out_mp4
                        result["engine"] = engine

                    with self.lock:
                        self.results.append(result)
                        self.percent = ((lang_index + 1) / total_languages) * 100

                with self.lock:
                    self.status = f"✅ Hoàn tất {len(languages)} ngôn ngữ."
                    self.percent = 100
            except Exception as exc:
                with self.lock:
                    self.status = f"Lỗi dịch phụ đề: {exc}"
                    if self.cancel_event.is_set():
                        self.status = "Đã hủy tác vụ dịch phụ đề."
            finally:
                with self.lock:
                    self.running = False
                    self.current_language = ""

        threading.Thread(target=worker, daemon=True).start()
        return True, ""
