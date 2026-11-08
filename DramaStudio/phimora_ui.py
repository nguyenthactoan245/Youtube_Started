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
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed


def configure_portable_runtime() -> str:
    """Configure paths used by the source checkout and the portable executable."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        runtime_dir = sys._MEIPASS
        app_dir = os.path.dirname(os.path.abspath(sys.executable))
        os.environ["PATH"] = runtime_dir + os.pathsep + os.environ.get("PATH", "")
    else:
        app_dir = os.path.dirname(os.path.abspath(__file__))
    return app_dir


APP_DIR = configure_portable_runtime()

from phimora_downloader import (
    parse_series_page,
    expand_shortical_browse_url,
    download_single_episode,
    scan_local_episodes,
    parse_merge_batches,
    merge_episode_range,
    detect_gpu_encoder,
    sanitize_filename,
    http_get,
)


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
        self.default_out = os.path.join(APP_DIR, "downloads")

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

    def get_snapshot(self) -> dict:
        self.tick_speed()
        with self.lock:
            return {
                "running": self.running,
                "speed": round(self.speed_ema if self.running else 0.0, 2),
                "bytesMb": round(self.bytes_total / (1024 * 1024), 2),
                "elapsed": round(self.elapsed, 1),
                "notice": self.notice,
                "defaultOut": self.default_out,
                "jobs": [dict(j) for j in self.jobs],
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
            }

    def start(
        self,
        out_dir: str,
        threads: int,
        mux: bool,
        keep: bool,
        merge: bool,
        merge_spec: str,
        merge_sub_mode: str,
        series_list: list,
    ):
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
                return False, "Chưa chọn tập nào.", []

            self.running = True
            self.cancel_event = threading.Event()
            self.jobs = jobs
            self.bytes_total = 0
            self.last_bytes = 0
            self.start_t = time.monotonic()
            self.last_tick = self.start_t
            self.elapsed = 0.0
            self.speed_ema = 0.0
            self.notice = f"Đang đồng bộ {len(jobs)} tập với {threads} luồng…"

        t = threading.Thread(
            target=self._worker_loop,
            args=(
                out_dir,
                max(1, min(32, int(threads))),
                bool(mux),
                bool(keep),
                bool(merge),
                str(merge_spec or "all"),
                str(merge_sub_mode or "soft"),
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
        mux: bool,
        keep: bool,
        merge: bool,
        merge_spec: str,
        merge_sub_mode: str,
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
                    mux_subs=mux,
                    keep_vtt=keep or (merge and merge_sub_mode != "none"),
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

        with ThreadPoolExecutor(max_workers=threads) as pool:
            futs = [pool.submit(run_one, i) for i in range(len(self.jobs))]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    pass

        merged_count = 0
        if merge and not self.cancel_event.is_set():
            for (series_dir, title_clean), items in downloaded_by_series.items():
                if len(items) >= 2:
                    avail_eps = sorted(ep for ep, _ in items)
                    batches = [b for b in parse_merge_batches(merge_spec, avail_eps) if len(b) >= 2]
                    for b_idx, batch in enumerate(batches, 1):
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

                        def _on_st(txt):
                            with self.lock:
                                self.merge_status = f"[{b_idx}/{len(batches)}] {txt}"
                                self.notice = self.merge_status

                        try:
                            res = merge_episode_range(
                                series_dir=series_dir,
                                ep_numbers=batch,
                                sub_mode=merge_sub_mode,
                                use_gpu=True,
                                keep_sub_files=True,
                                custom_prefix=title_clean,
                                status_cb=_on_st,
                                progress_cb=_on_prog,
                            )
                            if res.get("out_mp4"):
                                merged_count += 1
                                with self.lock:
                                    self.merge_results.insert(0, res)
                        except Exception:
                            pass
            with self.lock:
                self.merging = False
                self.merge_pct = 100

        with self.lock:
            self.elapsed = time.monotonic() - self.start_t
            self.running = False
            self.speed_ema = 0.0
            ok_c = sum(1 for j in self.jobs if j["st"] == "ok")
            err_c = sum(1 for j in self.jobs if j["st"] == "err")
            merge_msg = f" · Đã ghép {merged_count} video dài" if merged_count > 0 else ""
            if self.cancel_event.is_set():
                self.notice = f"Đã dừng đồng bộ · Hoàn tất {ok_c}/{len(self.jobs)} tập (Lỗi: {err_c}){merge_msg}."
            else:
                self.notice = f"Đồng bộ hoàn tất {ok_c}/{len(self.jobs)} tập{merge_msg} · Lưu tại {out_dir}"

    def start_standalone_merge(self, series_dir: str, range_spec: str, sub_mode: str, keep_sub_files: bool):
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

                try:
                    res = merge_episode_range(
                        series_dir=series_dir,
                        ep_numbers=batch,
                        sub_mode=sub_mode,
                        use_gpu=True,
                        keep_sub_files=keep_sub_files,
                        status_cb=on_st,
                        progress_cb=on_prog,
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
            self._send_json(SYNC_MGR.get_snapshot())
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

        if self.path == "/api/open-dir":
            target = str(payload.get("path") or SYNC_MGR.default_out).strip()
            if target:
                if os.path.isfile(target):
                    if sys.platform == "win32":
                        os.startfile(target)
                else:
                    os.makedirs(target, exist_ok=True)
                    if sys.platform == "win32":
                        os.startfile(target)
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
            )
            self._send_json({"ok": ok, "error": err})
            return

        if self.path == "/api/start-sync":
            ok, err, jobs = SYNC_MGR.start(
                out_dir=payload.get("out", ""),
                threads=payload.get("threads", 8),
                mux=payload.get("mux", True),
                keep=payload.get("keep", True),
                merge=payload.get("merge", False),
                merge_spec=payload.get("mergeSpec", "all"),
                merge_sub_mode=payload.get("mergeSubMode", "soft"),
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
    port = start_local_server()
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
        webview.start()
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
