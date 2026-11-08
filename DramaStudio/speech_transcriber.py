#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local speech recognition for DramaStudio using Faster-Whisper."""

from __future__ import annotations

import threading
import subprocess
from pathlib import Path

import requests

MODEL_NAME = "distil-large-v3"
MODEL_REPO_DIR = "distil-large-v3"
MODEL_REPO = "Systran/faster-distil-whisper-large-v3"
MODEL_FILES = ("config.json", "model.bin", "preprocessor_config.json", "tokenizer.json", "vocabulary.json")


def _vtt_time(seconds: float) -> str:
    millis = max(0, round(float(seconds) * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


class FasterWhisperTranscriber:
    """Prefer CUDA FP16 and transparently retry on CPU INT8."""

    def __init__(self, app_dir: str):
        self.model_root = Path(app_dir) / "models"
        bundled_model = Path(app_dir) / "dist" / "models" / MODEL_REPO_DIR
        if not (self.model_root / MODEL_REPO_DIR / "model.bin").is_file() and (bundled_model / "model.bin").is_file():
            self.model_root = bundled_model.parent
        self.model_dir = self.model_root / MODEL_REPO_DIR
        self._model = None
        self._engine = "Chưa nạp model"
        self._lock = threading.RLock()
        self.downloading_model = False
        self.download_pct = 0.0
        self.download_status = ""

    @property
    def engine(self) -> str:
        return self._engine

    @property
    def model_ready(self) -> bool:
        return all((self.model_dir / name).is_file() and (self.model_dir / name).stat().st_size > 0
                   for name in MODEL_FILES)

    def start_model_download(self) -> tuple[bool, str]:
        with self._lock:
            if self.model_ready:
                return False, "Model Faster-Whisper đã sẵn sàng."
            if self.downloading_model:
                return False, "Model Faster-Whisper đang tải."
            self.downloading_model = True
            self.download_pct = 0.0
            self.download_status = "Đang chuẩn bị tải Faster-Whisper…"

        def worker():
            try:
                self.model_dir.mkdir(parents=True, exist_ok=True)
                for index, filename in enumerate(MODEL_FILES):
                    target = self.model_dir / filename
                    if target.is_file() and target.stat().st_size > 0:
                        with self._lock:
                            self.download_pct = (index + 1) * 100 / len(MODEL_FILES)
                        continue
                    part = target.with_name(filename + ".part")
                    url = f"https://huggingface.co/{MODEL_REPO}/resolve/main/{filename}?download=true"
                    existing = part.stat().st_size if part.exists() else 0
                    with requests.get(url, headers={"Range": f"bytes={existing}-"} if existing else {},
                                      stream=True, timeout=(20, 120)) as response:
                        if response.status_code == 416 and existing:
                            remote_size = int((response.headers.get("Content-Range") or "0").split("/")[-1])
                            if remote_size and existing == remote_size:
                                part.replace(target)
                                with self._lock:
                                    self.download_pct = (index + 1) * 100 / len(MODEL_FILES)
                                continue
                            part.unlink()
                            raise RuntimeError(f"File tạm {filename} không hợp lệ; hãy thử tải lại.")
                        response.raise_for_status()
                        append = existing > 0 and response.status_code == 206
                        length = int(response.headers.get("Content-Length") or 0)
                        expected = (existing if append else 0) + length
                        downloaded = existing if append else 0
                        with open(part, "ab" if append else "wb") as output:
                            for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                                if not chunk:
                                    continue
                                output.write(chunk)
                                downloaded += len(chunk)
                                with self._lock:
                                    within = downloaded / expected if expected else 0
                                    self.download_pct = ((index + within) / len(MODEL_FILES)) * 100
                                    self.download_status = f"Đang tải {filename}: {downloaded / (1024 ** 3):.2f} GB"
                        if expected and downloaded != expected:
                            raise RuntimeError(f"File {filename} tải chưa đủ.")
                    part.replace(target)
                if not self.model_ready:
                    raise RuntimeError("Model Faster-Whisper chưa đầy đủ sau khi tải.")
                with self._lock:
                    self.download_pct = 100
                    self.download_status = "Model Faster-Whisper đã sẵn sàng."
            except Exception as exc:
                with self._lock:
                    self.download_status = f"Lỗi tải Faster-Whisper: {exc}"
            finally:
                with self._lock:
                    self.downloading_model = False

        threading.Thread(target=worker, daemon=True).start()
        return True, ""

    def release_model(self) -> None:
        """Free cached Whisper weights before another large CUDA model is loaded."""
        with self._lock:
            self._model = None
            self._engine = "Chưa nạp model"
        import gc
        gc.collect()

    def _create_model(self, device: str, compute_type: str):
        from faster_whisper import WhisperModel
        self.model_root.mkdir(parents=True, exist_ok=True)
        if not self.model_ready:
            raise RuntimeError("Chưa tải model Faster-Whisper distil-large-v3.")
        return WhisperModel(str(self.model_dir), device=device, compute_type=compute_type,
                            local_files_only=True)

    def _load(self, force_cpu: bool = False):
        with self._lock:
            if self._model is not None and (not force_cpu or self._engine == "CPU INT8"):
                return self._model
            if not force_cpu:
                try:
                    self._model = self._create_model("cuda", "float16")
                    self._engine = "CUDA FP16"
                    return self._model
                except Exception:
                    self._model = None
            self._model = self._create_model("cpu", "int8")
            self._engine = "CPU INT8"
            return self._model

    def _run(self, media_path, duration, progress_cb, cancel_event, force_cpu=False):
        model = self._load(force_cpu)
        import numpy as np
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", media_path, "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if decoded.returncode != 0:
            detail = decoded.stderr.decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"Không thể tách âm thanh bằng FFmpeg: {detail}")
        audio = np.frombuffer(decoded.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = model.transcribe(audio, language="en", task="transcribe",
                                       beam_size=5, vad_filter=True,
                                       condition_on_previous_text=False)
        cues = []
        for segment in segments:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Đã dừng nhận dạng phụ đề.")
            text = str(segment.text or "").strip()
            if text:
                cues.append(f"{_vtt_time(segment.start)} --> {_vtt_time(segment.end)}\n{text}")
            if progress_cb and duration > 0:
                progress_cb(min(99, max(1, int(segment.end * 100 / duration))), segment.end, self._engine)
        return "WEBVTT\n\n" + "\n\n".join(cues) + ("\n" if cues else "")

    def transcribe_vtt(self, media_path, duration, progress_cb=None, cancel_event=None):
        try:
            return self._run(media_path, duration, progress_cb, cancel_event)
        except Exception:
            if cancel_event and cancel_event.is_set():
                raise
            if self._engine != "CUDA FP16":
                raise
            with self._lock:
                self._model = None
            return self._run(media_path, duration, progress_cb, cancel_event, force_cpu=True)
