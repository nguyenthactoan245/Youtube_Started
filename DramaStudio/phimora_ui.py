#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STUDIO DOWNLOAD — HTML UI BRIDGE & DESKTOP LAUNCHER
===================================================
Tích hợp trực tiếp giao diện HTML (studio_ui.html)
với lõi giải mã, tải đa luồng & xưởng ghép tập dài bằng VGA/GPU trong `phimora_downloader.py`.
"""

import os
import re
import sys
import json
import time
import socket
import threading
import subprocess
import importlib.util
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed


_BOOT_TRACE_PATH = os.environ.get("DRAMASTUDIO_BOOT_TRACE", "")
_DLL_DIRECTORY_HANDLES = []


def _boot_trace(stage: str) -> None:
    if not _BOOT_TRACE_PATH:
        return
    try:
        with open(_BOOT_TRACE_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {stage}\n")
    except OSError:
        pass


def configure_portable_runtime() -> str:
    """Configure paths used by the source checkout and the portable executable."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        runtime_dir = sys._MEIPASS
        app_dir = os.path.dirname(os.path.abspath(sys.executable))
        os.environ["PATH"] = runtime_dir + os.pathsep + os.environ.get("PATH", "")
    else:
        runtime_dir = os.path.dirname(os.path.abspath(__file__))
        app_dir = os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        for folder in (runtime_dir, os.path.join(runtime_dir, "ctranslate2"),
                       os.path.join(runtime_dir, "sentencepiece.libs"),
                       os.path.join(runtime_dir, "numpy.libs")):
            if os.path.isdir(folder):
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(folder))
    cuda_runtime = os.path.join(runtime_dir, "cuda-runtime")
    if os.path.isdir(cuda_runtime):
        os.environ["PATH"] = cuda_runtime + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(cuda_runtime))
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(cuda_runtime)
    return app_dir


APP_DIR = configure_portable_runtime()
if getattr(sys, "frozen", False):
    if sys.platform == "darwin":
        DATA_DIR = os.path.join(os.path.expanduser("~/Library/Application Support"), "DramaStudio")
    else:
        DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "DramaStudio")
else:
    DATA_DIR = APP_DIR
_boot_trace("runtime configured")

from phimora_downloader import (
    parse_series_page,
    expand_shortical_browse_url,
    download_single_episode,
    scan_local_episodes,
    parse_merge_batches,
    merge_episode_range,
    detect_gpu_encoder,
    get_video_duration,
    sanitize_filename,
    http_get,
)
_boot_trace("downloader imported")
from subtitle_translator import TranslationManager, TARGET_LANGUAGES, find_matching_srt, model_is_ready
_boot_trace("translator imported")
from speech_transcriber import FasterWhisperTranscriber
_boot_trace("whisper imported")


def get_resource_path(rel_path: str) -> str:
    """Hỗ trợ đường dẫn file khi chạy trực tiếp hoặc khi đóng gói PyInstaller (.exe)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base_dir = sys._MEIPASS
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(base_dir, rel_path)
    if os.path.exists(candidate):
        return candidate
    return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), rel_path)


# =============================================================================
# GLOBAL SYNC STATE, MERGER STATE & POSTER CACHE
# =============================================================================
POSTER_CACHE = {}
POSTER_LOCK = threading.Lock()
SHORTICAL_RETRY_DELAYS = (2, 4, 8)


def download_episode_with_retry(
    book_id: str,
    serial: int,
    out_dir: str,
    lang: str = "vi",
    mux_subs: bool = True,
    keep_vtt: bool = True,
    status_cb=None,
    byte_cb=None,
    cancel_event: threading.Event = None,
    retry_delays: tuple[float, ...] = SHORTICAL_RETRY_DELAYS,
    download_fn=None,
) -> tuple[str, str]:
    """Retry transient Shortical episode failures before marking the job failed."""
    download_fn = download_fn or download_single_episode
    is_shortical = str(book_id).lower().startswith("shortical:")
    delays = tuple(retry_delays) if is_shortical else ()
    max_attempts = len(delays) + 1

    for attempt in range(1, max_attempts + 1):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Đã hủy tải.")
        try:
            return download_fn(
                book_id,
                serial,
                out_dir,
                lang=lang,
                mux_subs=mux_subs,
                keep_vtt=keep_vtt,
                status_cb=status_cb,
                byte_cb=byte_cb,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Đã hủy tải.") from exc
            if attempt >= max_attempts:
                if is_shortical and max_attempts > 1:
                    raise RuntimeError(
                        f"Shortical thất bại sau {max_attempts} lần thử: {exc}"
                    ) from exc
                raise

            delay = delays[attempt - 1]
            if status_cb:
                status_cb(
                    f"Tập {serial} lỗi: {exc} · thử lại "
                    f"{attempt + 1}/{max_attempts} sau {delay:g}s…"
                )
            if cancel_event:
                if cancel_event.wait(delay):
                    raise RuntimeError("Đã hủy tải.") from exc
            else:
                time.sleep(delay)

    raise RuntimeError("Không thể tải tập phim.")


class SyncManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.cancel_event = threading.Event()
        self.jobs = []
        self.bytes_total = 0
        self.last_bytes = 0
        self.last_tick = time.monotonic()
        self.start_t = 0.0
        self.elapsed = 0.0
        self.speed_ema = 0.0
        self.notice = ""
        self.default_out = os.path.join(DATA_DIR, "downloads")

        # GPU & Standalone Merger Real-Time Progress State
        self.gpu_info = {"label": "Đang dò VGA...", "short_label": "VGA", "is_gpu": True}
        self.merging = False
        self.merge_status = "Sẵn sàng ghép video theo dải tập bằng VGA."
        self.merge_pct = 0
        self.merge_cur_sec = 0.0
        self.merge_tot_sec = 0.0
        self.merge_speed = ""
        self.merge_fps = ""
        self.merge_size_mb = 0.0
        self.merge_engine = ""
        self.merge_batch_idx = 0
        self.merge_batch_total = 0
        self.merge_results = []
        self.translated_results = []
        self.merge_cancel_event = threading.Event()
        self.pipeline = {
            "download": {"state": "idle", "pct": 0, "message": "Chưa bắt đầu tải."},
            "merge": {"state": "idle", "pct": 0, "message": "Chờ tải tập hoàn tất."},
            "translate": {"state": "idle", "pct": 0, "message": "Chờ ghép tập hoàn tất."},
        }
        self.stage_started_at = {name: None for name in self.pipeline}
        self.stage_elapsed = {name: 0.0 for name in self.pipeline}

        threading.Thread(target=self._init_gpu, daemon=True).start()

    def _init_gpu(self):
        info = detect_gpu_encoder()
        with self.lock:
            self.gpu_info = info
            self.merge_engine = info["label"]

    def tick_speed(self):
        with self.lock:
            if not self.running:
                return
            now = time.monotonic()
            dt = now - self.last_tick
            self.elapsed = now - self.start_t
            if dt >= 0.35:
                inst = ((self.bytes_total - self.last_bytes) / (1024 * 1024)) / dt
                self.speed_ema = inst if self.speed_ema <= 0 else (self.speed_ema * 0.55 + inst * 0.45)
                self.last_bytes = self.bytes_total
                self.last_tick = now

    def _set_stage_locked(self, name: str, state: str, pct: float, message: str) -> None:
        now = time.monotonic()
        started = self.stage_started_at[name]
        if started is not None and state != "running":
            self.stage_elapsed[name] += now - started
            self.stage_started_at[name] = None
        elif started is None and state == "running":
            self.stage_started_at[name] = now
        self.pipeline[name] = {"state": state, "pct": round(max(0, min(100, pct)), 1), "message": message}
        if state == "error":
            self.notice = message

    def _set_stage(self, name: str, state: str, pct: float, message: str) -> None:
        with self.lock:
            self._set_stage_locked(name, state, pct, message)

    def _reject_start(self, stage: str, message: str):
        with self.lock:
            self.stage_started_at = {name: None for name in self.pipeline}
            self.stage_elapsed = {name: 0.0 for name in self.pipeline}
            self.pipeline = {
                "download": {"state": "idle", "pct": 0, "message": "Chưa bắt đầu tải."},
                "merge": {"state": "skipped", "pct": 0, "message": "Chưa bắt đầu ghép."},
                "translate": {"state": "skipped", "pct": 0, "message": "Chưa bắt đầu dịch."},
            }
            self.pipeline[stage] = {"state": "error", "pct": 0, "message": message}
            self.notice = message
        return False, message, []

    def get_snapshot(self) -> dict:
        self.tick_speed()
        with self.lock:
            pipeline = {name: dict(info) for name, info in self.pipeline.items()}
            now = time.monotonic()
            for name, info in pipeline.items():
                started = self.stage_started_at[name]
                info["elapsedSeconds"] = round(self.stage_elapsed[name] + (now - started if started is not None else 0), 1)
            if pipeline["download"]["state"] == "running" and self.jobs:
                pipeline["download"]["pct"] = round(sum(j["pct"] for j in self.jobs) / len(self.jobs), 1)
                completed = sum(j["st"] == "ok" for j in self.jobs)
                pipeline["download"]["message"] = f"Đã tải {completed}/{len(self.jobs)} tập."
            completed_videos = []
            for item in self.translated_results:
                if item.get("out_mp4"):
                    completed_videos.append({"title": item.get("title", ""), "type": f"Sub {item.get('language_name', item.get('language', ''))}", "path": item["out_mp4"]})
            for item in self.merge_results:
                if item.get("out_mp4"):
                    completed_videos.append({"title": item.get("title", os.path.basename(item["out_mp4"])), "type": "Tập dài", "path": item["out_mp4"]})
            return {
                "running": self.running,
                "speed": round(self.speed_ema if self.running else 0.0, 2),
                "bytesMb": round(self.bytes_total / (1024 * 1024), 2),
                "elapsed": round(self.elapsed, 1),
                "notice": self.notice,
                "defaultOut": self.default_out,
                "jobs": [dict(j) for j in self.jobs],
                "pipeline": pipeline,
                "completedVideos": completed_videos,
                "gpuLabel": self.gpu_info.get("label", "VGA"),
                "merging": self.merging,
                "mergeStatus": self.merge_status,
                "mergePct": self.merge_pct,
                "mergeCurSec": round(self.merge_cur_sec, 1),
                "mergeTotSec": round(self.merge_tot_sec, 1),
                "mergeSpeed": self.merge_speed,
                "mergeFps": self.merge_fps,
                "mergeSizeMb": round(self.merge_size_mb, 2),
                "mergeEngine": self.merge_engine,
                "mergeBatchIdx": self.merge_batch_idx,
                "mergeBatchTotal": self.merge_batch_total,
                "mergeResults": [dict(r) for r in self.merge_results],
                "whisperModelReady": WHISPER.model_ready if "WHISPER" in globals() else False,
                "whisperModelDir": str(WHISPER.model_dir) if "WHISPER" in globals() else "",
                "whisperEngine": WHISPER.engine if "WHISPER" in globals() else "Chưa nạp model",
                "whisperDownloadingModel": WHISPER.downloading_model if "WHISPER" in globals() else False,
                "whisperDownloadPct": WHISPER.download_pct if "WHISPER" in globals() else 0,
                "whisperDownloadStatus": WHISPER.download_status if "WHISPER" in globals() else "",
            }

    def start(
        self,
        out_dir: str,
        threads: int,
        merge: bool,
        merge_spec: str,
        merge_sub_mode: str,
        target_languages: list[str],
        series_list: list,
    ):
        with self.lock:
            if self.running:
                return False, "Đang có tiến trình đồng bộ chạy.", []
        target_languages = list(dict.fromkeys(str(lang).lower() for lang in target_languages))
        if merge and merge_sub_mode not in ("none", "soft", "translate"):
            return self._reject_start("merge", "Chế độ phụ đề không hợp lệ.")
        if merge and merge_sub_mode == "translate":
            if not target_languages or any(lang not in TARGET_LANGUAGES for lang in target_languages):
                return self._reject_start("translate", "Hãy chọn ít nhất một ngôn ngữ dịch được hỗ trợ.")
            if not model_is_ready(TRANSLATION_MGR.model_dir):
                return self._reject_start("translate", "Chưa có model MADLAD-400. Hãy tải model trong Cấu hình đồng bộ.")
            if TRANSLATION_MGR.snapshot()["translationRunning"]:
                return self._reject_start("translate", "Đang có tác vụ dịch phụ đề khác chạy.")
        if merge and merge_sub_mode != "none" and importlib.util.find_spec("faster_whisper") is None:
            return self._reject_start("merge", (
                "Thiếu faster_whisper để tạo SRT khi nguồn không có sub. "
                "Hãy chạy bằng Python 3.12 và cài: py -3.12 -m pip install -r requirements.txt"
            ))
        if merge and merge_sub_mode != "none" and not WHISPER.model_ready:
            return self._reject_start("merge", "Chưa có model Faster-Whisper. Hãy tải model trong Cấu hình đồng bộ trước khi bắt đầu.")
        with self.lock:
            if self.running:
                return False, "Đang có tiến trình đồng bộ chạy.", []

            out_dir = (out_dir or "").strip() or self.default_out
            os.makedirs(out_dir, exist_ok=True)
            self.default_out = out_dir

            jobs = []
            row_idx = 0
            for s in series_list:
                s_lang = str(s.get("subLang") or "vi").strip().lower()
                for ep in s.get("episodes", []):
                    jobs.append({
                        "row": row_idx,
                        "bookId": s["bookId"],
                        "title": s["title"],
                        "ep": int(ep),
                        "provider": s.get("provider") or "Phimora",
                        "subLang": s_lang,
                        "status": "Chờ xử lý",
                        "size": "—",
                        "st": "wait",
                        "pct": 0,
                    })
                    row_idx += 1

            if not jobs:
                self._set_stage_locked("download", "error", 0, "Chưa chọn tập nào.")
                return False, "Chưa chọn tập nào.", []

            self.running = True
            self.cancel_event = threading.Event()
            self.jobs = jobs
            self.merge_results = []
            self.translated_results = []
            self.bytes_total = 0
            self.last_bytes = 0
            self.start_t = time.monotonic()
            self.last_tick = self.start_t
            self.elapsed = 0.0
            self.speed_ema = 0.0
            self.notice = f"Đang đồng bộ {len(jobs)} tập với {threads} luồng…"
            self.pipeline = {
                "download": {"state": "running", "pct": 0, "message": f"Đang tải {len(jobs)} tập…"},
                "merge": {"state": "idle" if merge else "skipped", "pct": 0, "message": "Chờ tải tập hoàn tất." if merge else "Không chọn ghép tập."},
                "translate": {"state": "idle" if merge_sub_mode == "translate" and merge else "skipped", "pct": 0, "message": "Chờ ghép tập hoàn tất." if merge_sub_mode == "translate" and merge else "Không chọn dịch sub."},
            }
            self.stage_started_at = {name: None for name in self.pipeline}
            self.stage_elapsed = {name: 0.0 for name in self.pipeline}
            self.stage_started_at["download"] = self.start_t

        t = threading.Thread(
            target=self._worker_loop,
            args=(
                out_dir,
                max(1, min(32, int(threads))),
                bool(merge),
                str(merge_spec or "all"),
                str(merge_sub_mode or "soft"),
                target_languages,
            ),
            daemon=True,
        )
        t.start()
        return True, "", jobs

    def stop(self):
        with self.lock:
            if self.running:
                self.cancel_event.set()
                self.notice = "Đang dừng đồng bộ… chờ các luồng kết thúc an toàn."

    def _worker_loop(
        self,
        out_dir: str,
        threads: int,
        merge: bool,
        merge_spec: str,
        merge_sub_mode: str,
        target_languages: list[str],
    ):
        downloaded_by_series = {}
        dl_lock = threading.Lock()

        def run_one(job_idx: int):
            with self.lock:
                job = self.jobs[job_idx]
                if self.cancel_event.is_set():
                    job["st"] = "cancel"
                    job["status"] = "Đã hủy"
                    return
                job["st"] = "run"
                job["status"] = "Đang lấy manifest…"
                job["pct"] = 10
                book_id = job["bookId"]
                ep = job["ep"]
                title = job["title"]
                job_lang = job.get("subLang") or "vi"

            title_clean = sanitize_filename(title)
            safe_book_id = sanitize_filename(str(book_id).replace("shortical:", "SC_"))
            series_dir = os.path.join(out_dir, f"{title_clean}_{safe_book_id}")
            os.makedirs(series_dir, exist_ok=True)

            def on_status(st_text: str):
                with self.lock:
                    j = self.jobs[job_idx]
                    j["status"] = st_text
                    if "nhúng" in st_text.lower() or "mux" in st_text.lower():
                        j["pct"] = 92
                    elif "video" in st_text.lower() or "hls" in st_text.lower():
                        j["pct"] = max(j["pct"], 35)

            def on_bytes(delta: int, cur_b: int, tot_b: int):
                with self.lock:
                    self.bytes_total += int(delta or 0)
                    j = self.jobs[job_idx]
                    cur_mb = cur_b / (1024 * 1024)
                    if tot_b and tot_b > 0:
                        tot_mb = tot_b / (1024 * 1024)
                        j["size"] = f"{cur_mb:.1f} / {tot_mb:.1f} MB"
                        j["pct"] = min(90, max(15, int((cur_b * 85) / tot_b)))
                    else:
                        j["size"] = f"{cur_mb:.1f} MB"
                        j["pct"] = min(88, j["pct"] + 4)

            try:
                saved_path, prov = download_episode_with_retry(
                    book_id,
                    ep,
                    series_dir,
                    lang=job_lang,
                    mux_subs=True,
                    keep_vtt=True,
                    status_cb=on_status,
                    byte_cb=on_bytes,
                    cancel_event=self.cancel_event,
                )
                sz_mb = os.path.getsize(saved_path) / (1024 * 1024) if os.path.exists(saved_path) else 0.0
                with self.lock:
                    j = self.jobs[job_idx]
                    if prov:
                        j["provider"] = prov
                    j["status"] = "Hoàn tất"
                    j["st"] = "ok"
                    j["pct"] = 100
                    j["path"] = saved_path
                    if sz_mb > 0:
                        j["size"] = f"{sz_mb:.2f} MB"
                with dl_lock:
                    downloaded_by_series.setdefault((series_dir, title_clean), []).append((ep, saved_path))
            except Exception as e:
                with self.lock:
                    j = self.jobs[job_idx]
                    if self.cancel_event.is_set():
                        j["st"] = "cancel"
                        j["status"] = "Đã hủy"
                    else:
                        j["st"] = "err"
                        j["status"] = f"Lỗi: {e}"
                        if self.pipeline["download"]["state"] != "error":
                            self._set_stage_locked("download", "error", 0, f"Tải tập {ep} thất bại: {e}")
                            self.notice = self.pipeline["download"]["message"]
                        self.cancel_event.set()

        with ThreadPoolExecutor(max_workers=threads) as pool:
            futs = [pool.submit(run_one, i) for i in range(len(self.jobs))]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    pass

        with self.lock:
            download_failed = self.pipeline["download"]["state"] == "error"
            cancelled = self.cancel_event.is_set() and not download_failed
            if download_failed or cancelled:
                if cancelled:
                    self._set_stage_locked("download", "cancelled", 0, "Đã dừng tải tập.")
                reason = "Đã dừng vì bước tải thất bại." if download_failed else "Đã hủy đồng bộ."
                for stage in ("merge", "translate"):
                    if self.pipeline[stage]["state"] == "idle":
                        self.pipeline[stage] = {"state": "skipped", "pct": 0, "message": reason}
                self.running = False
                self.elapsed = time.monotonic() - self.start_t
                self.speed_ema = 0.0
                self.notice = self.pipeline["download"]["message"]
            else:
                self._set_stage_locked("download", "done", 100, f"Đã tải xong {len(self.jobs)} tập.")
        if download_failed or cancelled:
            return

        merged_count = 0
        translated_count = 0
        merge_error = ""
        if merge and not self.cancel_event.is_set():
            batch_total = sum(
                len([b for b in parse_merge_batches(merge_spec, sorted(ep for ep, _ in items)) if len(b) >= 2])
                for items in downloaded_by_series.values() if len(items) >= 2
            )
            self._set_stage("merge", "running" if batch_total else "error", 0,
                            f"Đang ghép {batch_total} video tập dài…" if batch_total else "Không có dải tập đủ điều kiện để ghép.")
            if not batch_total:
                merge_error = "Không có dải tập đủ điều kiện để ghép."
            for (series_dir, title_clean), items in downloaded_by_series.items():
                if merge_error or self.cancel_event.is_set():
                    break
                if len(items) >= 2:
                    avail_eps = sorted(ep for ep, _ in items)
                    batches = [b for b in parse_merge_batches(merge_spec, avail_eps) if len(b) >= 2]
                    for b_idx, batch in enumerate(batches, 1):
                        if merge_error or self.cancel_event.is_set():
                            break
                        self._set_stage("merge", "running", (merged_count / batch_total) * 100,
                                        f"Đang ghép tập {batch[0]}–{batch[-1]}…")
                        with self.lock:
                            self.merging = True
                            self.merge_batch_idx = b_idx
                            self.merge_batch_total = len(batches)
                            self.merge_pct = 1
                            self.notice = f"Đang ghép tập dài [{b_idx}/{len(batches)}] {title_clean}: Tập {batch[0]}→{batch[-1]}…"

                        def _on_prog(pct, cur_s, tot_s, spd, fps, sz_mb, eng):
                            with self.lock:
                                self.merge_pct = pct
                                self.merge_cur_sec = cur_s
                                self.merge_tot_sec = tot_s
                                if spd:
                                    self.merge_speed = spd
                                if fps:
                                    self.merge_fps = fps
                                self.merge_size_mb = sz_mb
                                self.merge_engine = eng
                                self.pipeline["merge"] = {
                                    "state": "running",
                                    "pct": round(((merged_count + pct / 100) / batch_total) * 100, 1),
                                    "message": self.merge_status,
                                }

                        def _on_st(txt):
                            with self.lock:
                                self.merge_status = f"[{b_idx}/{len(batches)}] {txt}"
                                self.notice = self.merge_status
                                self.pipeline["merge"]["message"] = self.merge_status

                        def _transcribe_missing(mp4_path, duration, ep_num, ep_index, ep_total):
                            _on_st(f"Đang nhận dạng lời thoại tập {ep_num} ({ep_index}/{ep_total})…")

                            def on_asr(pct, _seconds, engine):
                                with self.lock:
                                    self.pipeline["merge"]["pct"] = round(((merged_count + pct / 100) / batch_total) * 100, 1)
                                    self.pipeline["merge"]["message"] = f"Đang tạo SRT tập {ep_num}: {pct}% · {engine}"

                            return WHISPER.transcribe_vtt(
                                mp4_path, duration, progress_cb=on_asr, cancel_event=self.cancel_event
                            )

                        translating = False
                        try:
                            res = merge_episode_range(
                                series_dir=series_dir,
                                ep_numbers=batch,
                                sub_mode="soft" if merge_sub_mode == "translate" else merge_sub_mode,
                                use_gpu=True,
                                keep_sub_files=True,
                                custom_prefix=title_clean,
                                status_cb=_on_st,
                                progress_cb=_on_prog,
                                subtitle_fallback_fn=_transcribe_missing if merge_sub_mode != "none" else None,
                            )
                            if res.get("out_mp4"):
                                merged_count += 1
                                with self.lock:
                                    res["title"] = f"{title_clean} · Tập {batch[0]:02d}–{batch[-1]:02d}"
                                    self.merge_results.insert(0, res)
                                if merge_sub_mode == "translate":
                                    translating = True
                                    self._set_stage("merge", "paused", (merged_count / batch_total) * 100,
                                                    "Đã ghép video; đang chờ dịch sub.")
                                    WHISPER.release_model()
                                    with self.lock:
                                        self.merge_status = f"Đang dịch phụ đề {', '.join(target_languages)} cho {os.path.basename(res['out_mp4'])}…"
                                        self.notice = self.merge_status
                                    self._set_stage("translate", "running", (translated_count / (batch_total * len(target_languages))) * 100, self.merge_status)
                                    ok, error = TRANSLATION_MGR.start_translation(
                                        res["out_mp4"], res.get("out_srt", ""),
                                        target_languages, burn_video=True,
                                    )
                                    if not ok:
                                        raise RuntimeError(error)
                                    while TRANSLATION_MGR.snapshot()["translationRunning"]:
                                        translation_progress = TRANSLATION_MGR.snapshot()
                                        self._set_stage(
                                            "translate", "running",
                                            ((translated_count + translation_progress["translationPct"] * len(target_languages) / 100) / (batch_total * len(target_languages))) * 100,
                                            translation_progress["translationStatus"],
                                        )
                                        if self.cancel_event.wait(0.2):
                                            TRANSLATION_MGR.cancel()
                                    translated = TRANSLATION_MGR.snapshot()
                                    finished_translations = [item for item in translated["translationResults"] if item.get("out_mp4")]
                                    with self.lock:
                                        self.translated_results.extend({**item, "title": res["title"]} for item in finished_translations)
                                    translated_count += len(finished_translations)
                                    if len(translated["translationResults"]) != len(target_languages):
                                        raise RuntimeError(translated["translationStatus"])
                                    self._set_stage("translate", "running", (translated_count / (batch_total * len(target_languages))) * 100,
                                                    f"Đã dịch {translated_count}/{batch_total * len(target_languages)} bản phụ đề.")
                                    self._set_stage("translate", "paused", (translated_count / (batch_total * len(target_languages))) * 100,
                                                    "Đã dịch xong batch này; chờ video tiếp theo.")
                            else:
                                raise RuntimeError("Ghép tập không tạo được video đầu ra.")
                        except Exception as exc:
                            merge_error = str(exc)
                            with self.lock:
                                self.merge_status = f"Lỗi ghép/dịch phụ đề: {merge_error}"
                                self.notice = self.merge_status
                            self._set_stage("translate" if translating else "merge", "error", 0, merge_error)
                            break
            with self.lock:
                self.merging = False
                self.merge_pct = 100 if not merge_error else self.merge_pct
            if not merge_error and not self.cancel_event.is_set():
                self._set_stage("merge", "done", 100, f"Đã ghép {merged_count} video tập dài.")
                if merge_sub_mode == "translate":
                    self._set_stage("translate", "done", 100, f"Đã dịch {translated_count} bản phụ đề.")
            elif self.pipeline["merge"]["state"] in ("running", "paused"):
                if merge_error and self.pipeline["translate"]["state"] == "error":
                    self._set_stage("merge", "cancelled", self.pipeline["merge"]["pct"], f"Đã dừng sau khi ghép {merged_count} video vì dịch sub thất bại.")
                else:
                    self._set_stage("merge", "cancelled", self.pipeline["merge"]["pct"], "Đã dừng ghép tập.")
            if merge_sub_mode == "translate" and self.pipeline["translate"]["state"] in ("idle", "paused"):
                if self.pipeline["translate"]["state"] == "paused":
                    self._set_stage("translate", "cancelled", self.pipeline["translate"]["pct"], "Đã dừng vì bước ghép thất bại.")
                else:
                    self._set_stage("translate", "skipped", 0, "Đã dừng vì bước ghép thất bại.")

        with self.lock:
            self.elapsed = time.monotonic() - self.start_t
            self.running = False
            self.speed_ema = 0.0
            ok_c = sum(1 for j in self.jobs if j["st"] == "ok")
            err_c = sum(1 for j in self.jobs if j["st"] == "err")
            merge_msg = f" · Đã ghép {merged_count} video dài" if merged_count > 0 else ""
            if translated_count:
                merge_msg += f" · Đã dịch {translated_count} bản phụ đề"
            if merge_error:
                merge_msg += f" · Lỗi ghép/dịch: {merge_error}"
            if merge_error:
                self.notice = f"Đã dừng: {merge_error}"
            elif self.cancel_event.is_set():
                self.notice = f"Đã dừng đồng bộ · Hoàn tất {ok_c}/{len(self.jobs)} tập (Lỗi: {err_c}){merge_msg}."
            else:
                self.notice = f"Đồng bộ hoàn tất {ok_c}/{len(self.jobs)} tập{merge_msg} · Lưu tại {out_dir}"

    def start_standalone_merge(self, series_dir: str, range_spec: str, sub_mode: str,
                               keep_sub_files: bool, auto_transcribe: bool = True):
        with self.lock:
            if self.merging:
                return False, "Đang có tiến trình ghép video chạy, vui lòng đợi!"
            if not series_dir or not os.path.isdir(series_dir):
                return False, "Thư mục phim không tồn tại!"

            local_map = scan_local_episodes(series_dir)
            avail_eps = sorted(local_map.keys())
            if len(avail_eps) < 2:
                return False, f"Trong thư mục chỉ có {len(avail_eps)} tập MP4 (cần ít nhất 2 tập để ghép)."

            batches = parse_merge_batches(range_spec, avail_eps)
            valid_batches = [b for b in batches if len(b) >= 2]
            if not valid_batches:
                return False, f"Dải tập '{range_spec}' không khớp với các tập hiện có ({avail_eps[0]}..{avail_eps[-1]})."

            self.merging = True
            self.merge_cancel_event.clear()
            self.merge_pct = 1
            self.merge_cur_sec = 0.0
            self.merge_tot_sec = 0.0
            self.merge_speed = ""
            self.merge_fps = ""
            self.merge_size_mb = 0.0
            self.merge_batch_idx = 1
            self.merge_batch_total = len(valid_batches)
            self.merge_status = f"Chuẩn bị ghép {len(valid_batches)} file tập dài bằng {self.gpu_info.get('label', 'VGA')}…"

        def _merge_worker():
            ok_files = 0
            last_err = ""
            t_start = time.monotonic()
            for idx, batch in enumerate(valid_batches, 1):
                with self.lock:
                    self.merge_batch_idx = idx
                    self.merge_pct = 1

                def on_st(txt, b_i=idx):
                    with self.lock:
                        self.merge_status = f"[{b_i}/{len(valid_batches)}] {txt}"

                def on_prog(pct, cur_s, tot_s, spd, fps, sz_mb, eng):
                    with self.lock:
                        self.merge_pct = pct
                        self.merge_cur_sec = cur_s
                        self.merge_tot_sec = tot_s
                        if spd:
                            self.merge_speed = spd
                        if fps:
                            self.merge_fps = fps
                        self.merge_size_mb = sz_mb
                        self.merge_engine = eng

                def transcribe_missing(mp4_path, duration, ep_num, ep_index, ep_total):
                    on_st(f"Đang nhận dạng lời thoại tập {ep_num} ({ep_index}/{ep_total}) bằng Faster-Whisper…")

                    def on_asr(pct, cur_s, engine):
                        with self.lock:
                            self.merge_pct = pct
                            self.merge_cur_sec = cur_s
                            self.merge_tot_sec = duration
                            self.merge_engine = f"Faster-Whisper distil-large-v3 · {engine}"

                    return WHISPER.transcribe_vtt(
                        mp4_path, duration, progress_cb=on_asr, cancel_event=self.merge_cancel_event
                    )

                try:
                    res = merge_episode_range(
                        series_dir=series_dir,
                        ep_numbers=batch,
                        sub_mode=sub_mode,
                        use_gpu=True,
                        keep_sub_files=keep_sub_files,
                        status_cb=on_st,
                        progress_cb=on_prog,
                        subtitle_fallback_fn=transcribe_missing if auto_transcribe and sub_mode != "none" else None,
                    )
                    ok_files += 1
                    with self.lock:
                        self.merge_results.insert(0, res)
                except Exception as e:
                    last_err = str(e)

            dt = time.monotonic() - t_start
            with self.lock:
                self.merging = False
                self.merge_pct = 100 if ok_files > 0 else 0
                if ok_files > 0 and not last_err:
                    self.merge_status = f"✅ Hoàn tất ghép {ok_files} video tập dài trong {dt:.1f}s tại: {series_dir}"
                elif ok_files > 0:
                    self.merge_status = f"Đã ghép {ok_files} video ({dt:.1f}s) · Có lỗi: {last_err}"
                else:
                    self.merge_status = f"Lỗi khi ghép video: {last_err}"

        threading.Thread(target=_merge_worker, daemon=True).start()
        return True, ""


SYNC_MGR = SyncManager()
_boot_trace("sync manager ready")
WHISPER = FasterWhisperTranscriber(DATA_DIR)
TRANSLATION_MGR = TranslationManager(DATA_DIR, detect_gpu_encoder, get_video_duration)
_boot_trace("translation manager ready")


def list_movie_folders(root_dir: str) -> list[dict]:
    """Quét danh sách các thư mục phim đã tải trong thư mục gốc."""
    out = []
    if not root_dir or not os.path.isdir(root_dir):
        return out
    direct_eps = scan_local_episodes(root_dir)
    if direct_eps:
        ep_nums = sorted(direct_eps.keys())
        sub_cnt = sum(1 for v in direct_eps.values() if v["has_sub"])
        out.append({
            "path": os.path.normpath(root_dir),
            "name": os.path.basename(os.path.normpath(root_dir)) or root_dir,
            "ep_count": len(ep_nums),
            "ep_min": ep_nums[0],
            "ep_max": ep_nums[-1],
            "eps": ep_nums,
            "sub_count": sub_cnt,
        })

    for entry in sorted(os.listdir(root_dir)):
        full_p = os.path.join(root_dir, entry)
        if os.path.isdir(full_p):
            eps_map = scan_local_episodes(full_p)
            if eps_map:
                ep_nums = sorted(eps_map.keys())
                sub_cnt = sum(1 for v in eps_map.values() if v["has_sub"])
                out.append({
                    "path": os.path.normpath(full_p),
                    "name": entry,
                    "ep_count": len(ep_nums),
                    "ep_min": ep_nums[0],
                    "ep_max": ep_nums[-1],
                    "eps": ep_nums,
                    "sub_count": sub_cnt,
                })
    return out


# =============================================================================
# LOCAL HTTP SERVER FOR HTML UI
# =============================================================================
class StudioHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, obj: dict, status: int = 200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/app-icon-v2.png", "/favicon.ico"):
            rel_path = "assets/dramastudio-icon-v2.png" if parsed.path.endswith(".png") else "assets/dramastudio-icon-v2.ico"
            with open(get_resource_path(rel_path), "rb") as icon_file:
                data = icon_file.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if parsed.path.endswith(".png") else "image/x-icon")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path in ("/", "/index.html", "/Main.dc.html", "/studio_ui.html"):
            html_path = get_resource_path("studio_ui.html")
            with open(html_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/status":
            snapshot = SYNC_MGR.get_snapshot()
            snapshot.update(TRANSLATION_MGR.snapshot())
            self._send_json(snapshot)
            return

        if parsed.path == "/api/poster":
            qs = urllib.parse.parse_qs(parsed.query)
            img_url = (qs.get("url") or [""])[0].strip()
            if not img_url:
                self.send_error(404)
                return
            with POSTER_LOCK:
                cached = POSTER_CACHE.get(img_url)
            if cached is None:
                try:
                    cached = http_get(img_url, timeout=15)
                    with POSTER_LOCK:
                        POSTER_CACHE[img_url] = cached
                except Exception:
                    self.send_error(404)
                    return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(cached)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(cached)
            return

        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body_bytes = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            payload = {}

        if self.path == "/api/load-urls":
            raw_urls = str(payload.get("urls") or "")
            raw_list = [u.strip() for u in re.split(r"[\r\n\s,]+", raw_urls) if u.strip() and not u.strip().startswith("#")]
            urls = []
            for u in raw_list:
                for expanded in expand_shortical_browse_url(u):
                    if expanded not in urls:
                        urls.append(expanded)
            loaded = []
            errors = []
            if urls:
                with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
                    futs = {pool.submit(parse_series_page, u): u for u in urls}
                    for fut in as_completed(futs):
                        u = futs[fut]
                        try:
                            loaded.append(fut.result())
                        except Exception as e:
                            errors.append(f"{u}: {e}")
            self._send_json({"loaded": loaded, "errors": errors})
            return

        if self.path == "/api/browse-dir":
            cur = str(payload.get("current") or SYNC_MGR.default_out)
            picked = pick_folder_dialog(cur)
            self._send_json({"path": picked or ""})
            return

        if self.path == "/api/browse-file":
            kind = str(payload.get("kind") or "video").strip().lower()
            current = str(payload.get("current") or SYNC_MGR.default_out).strip()
            picked = pick_file_dialog(current, kind)
            response = {"path": picked or ""}
            if picked and kind == "video":
                response["matchingSrt"] = find_matching_srt(picked)
            self._send_json(response)
            return

        if self.path == "/api/find-matching-srt":
            video_path = str(payload.get("videoPath") or "").strip()
            self._send_json({"path": find_matching_srt(video_path) if video_path else ""})
            return

        if self.path == "/api/open-dir":
            target = str(payload.get("path") or SYNC_MGR.default_out).strip()
            if target:
                if os.path.isfile(target):
                    if sys.platform == "win32":
                        os.startfile(target)
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", target])
                else:
                    os.makedirs(target, exist_ok=True)
                    if sys.platform == "win32":
                        os.startfile(target)
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", target])
            self._send_json({"ok": True})
            return

        if self.path in ("/api/reveal-video", "/api/watch-video"):
            target = os.path.abspath(str(payload.get("path") or ""))
            known = {os.path.abspath(item["path"]) for item in SYNC_MGR.get_snapshot()["completedVideos"]}
            if target not in known or not os.path.isfile(target):
                self._send_json({"ok": False, "error": "Không tìm thấy video đã hoàn tất."}, status=404)
                return
            try:
                if sys.platform == "win32":
                    if self.path == "/api/reveal-video":
                        subprocess.Popen(["explorer.exe", "/select,", target])
                    else:
                        os.startfile(target)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", target] if self.path == "/api/reveal-video" else ["open", target])
            except OSError as exc:
                self._send_json({"ok": False, "error": f"Không mở được video: {exc}"}, status=500)
                return
            self._send_json({"ok": True})
            return

        if self.path == "/api/list-local-folders":
            root_dir = str(payload.get("root") or SYNC_MGR.default_out).strip()
            folders = list_movie_folders(root_dir)
            self._send_json({"folders": folders})
            return

        if self.path == "/api/run-merge":
            ok, err = SYNC_MGR.start_standalone_merge(
                series_dir=str(payload.get("folder") or "").strip(),
                range_spec=str(payload.get("rangeSpec") or "all").strip(),
                sub_mode=str(payload.get("subMode") or "soft").strip(),
                keep_sub_files=bool(payload.get("keepSubFiles", True)),
                auto_transcribe=bool(payload.get("autoTranscribe", True)),
            )
            self._send_json({"ok": ok, "error": err})
            return

        if self.path == "/api/download-translation-model":
            ok, err = TRANSLATION_MGR.start_model_download()
            self._send_json({"ok": ok, "error": err})
            return

        if self.path == "/api/download-whisper-model":
            ok, err = WHISPER.start_model_download()
            self._send_json({"ok": ok, "error": err})
            return

        if self.path == "/api/start-translation":
            ok, err = TRANSLATION_MGR.start_translation(
                video_path=str(payload.get("videoPath") or "").strip(),
                source_srt=str(payload.get("sourceSrt") or "").strip(),
                target_languages=payload.get("targetLanguages") or [],
                burn_video=bool(payload.get("burnVideo", True)),
            )
            self._send_json({"ok": ok, "error": err})
            return

        if self.path == "/api/stop-translation":
            TRANSLATION_MGR.cancel()
            self._send_json({"ok": True})
            return

        if self.path == "/api/start-sync":
            ok, err, jobs = SYNC_MGR.start(
                out_dir=payload.get("out", ""),
                threads=payload.get("threads", 8),
                merge=payload.get("merge", False),
                merge_spec=payload.get("mergeSpec", "all"),
                merge_sub_mode=payload.get("mergeSubMode", "soft"),
                target_languages=payload.get("targetLanguages") or [],
                series_list=payload.get("series", []),
            )
            self._send_json({"ok": ok, "error": err, "jobs": jobs})
            return

        if self.path == "/api/stop-sync":
            SYNC_MGR.stop()
            self._send_json({"ok": True})
            return

        self.send_error(404)


def pick_folder_dialog(initial_dir: str) -> str:
    """Mở hộp thoại chọn thư mục gốc của Windows."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(
            initialdir=initial_dir if (initial_dir and os.path.exists(initial_dir)) else None,
            title="Chọn thư mục phim",
        )
        root.destroy()
        if folder:
            return os.path.normpath(folder)
    except Exception:
        pass
    return ""


def pick_file_dialog(initial_path: str, kind: str) -> str:
    """Open a native file picker for a merged video or source subtitle."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        initial_dir = initial_path
        if initial_dir and os.path.isfile(initial_dir):
            initial_dir = os.path.dirname(initial_dir)
        if not initial_dir or not os.path.isdir(initial_dir):
            initial_dir = SYNC_MGR.default_out
        if kind == "srt":
            filetypes = [("SubRip subtitle", "*.srt"), ("All files", "*.*")]
            title = "Chọn file SRT tiếng Việt"
        else:
            filetypes = [("MP4 video", "*.mp4"), ("Video files", "*.mkv;*.mov;*.avi"), ("All files", "*.*")]
            title = "Chọn video dài cần chèn phụ đề"
        selected = filedialog.askopenfilename(
            initialdir=initial_dir,
            title=title,
            filetypes=filetypes,
        )
        root.destroy()
        return os.path.normpath(selected) if selected else ""
    except Exception:
        return ""


def start_local_server() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = ThreadingHTTPServer(("127.0.0.1", port), StudioHTTPHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port


def launch_gui():
    _boot_trace("launch gui")
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DramaStudio.Source")
    port = start_local_server()
    _boot_trace(f"server listening {port}")
    url = f"http://127.0.0.1:{port}/"

    try:
        import webview
        webview.create_window(
            "Studio Download",
            url,
            width=1440,
            height=960,
            min_size=(1080, 720),
            background_color="#1a1918",
        )
        icon_name = "assets/dramastudio-icon-v2.png" if sys.platform == "darwin" else "assets/dramastudio-icon-v2.ico"
        webview.start(icon=get_resource_path(icon_name))
        return
    except Exception:
        pass

    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for ep in edge_paths:
        if os.path.exists(ep):
            proc = subprocess.Popen([ep, f"--app={url}", "--window-size=1440,960"])
            proc.wait()
            return

    import webbrowser
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    launch_gui()
