import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
TEST_TEMP_DIR = PROJECT_DIR / "tests" / ".tmp"
TEST_TEMP_DIR.mkdir(parents=True, exist_ok=True)
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from subtitle_translator import (  # noqa: E402
    SubtitleCue,
    MadladTranslator,
    TranslationManager,
    find_matching_srt,
    parse_srt,
    render_ass,
    render_srt,
    translate_cues,
)
import phimora_downloader  # noqa: E402
import phimora_ui  # noqa: E402
from speech_transcriber import FasterWhisperTranscriber  # noqa: E402


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:03,250
Xin chào.

2
00:00:04,500 --> 00:00:06,000
Anh khỏe không?
"""


class FakeTranslator:
    def translate_texts(self, texts, target_language):
        return [f"[{target_language}] {text}" for text in texts]


class SubtitleCoreTests(unittest.TestCase):
    def test_cuda_translation_failure_retries_on_cpu(self):
        devices = []

        class FakeCT2Translator:
            def __init__(self, _model_dir, device, compute_type):
                devices.append((device, compute_type))
                self.device = device

            def translate_batch(self, encoded, **_kwargs):
                if self.device == "cuda":
                    raise RuntimeError("CUDA out of memory")
                return [types.SimpleNamespace(hypotheses=[["translated"]]) for _ in encoded]

        fake_ct2 = types.SimpleNamespace(
            get_cuda_device_count=lambda: 1,
            get_supported_compute_types=lambda _device: {"int8_float16"},
            Translator=FakeCT2Translator,
        )
        fake_sp = types.SimpleNamespace(
            SentencePieceProcessor=lambda **_kwargs: types.SimpleNamespace(
                encode=lambda text, **_kwargs: [text],
                decode=lambda _tokens: "Xin chào",
            )
        )
        with (
            mock.patch("subtitle_translator.model_is_ready", return_value=True),
            mock.patch.dict(sys.modules, {"ctranslate2": fake_ct2, "sentencepiece": fake_sp}),
        ):
            runtime = MadladTranslator("fake-model")
            self.assertEqual(runtime.translate_texts(["Hello"], "vi"), ["Xin chào"])
        self.assertEqual(devices, [("cuda", "int8_float16"), ("cpu", "int8")])
        self.assertEqual(runtime.engine, "CPU INT8")
        self.assertIn("out of memory", runtime.cuda_error)

    def test_parse_and_render_srt_preserves_timestamps(self):
        cues = parse_srt(SAMPLE_SRT)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start_ms, 1000)
        self.assertEqual(cues[0].end_ms, 3250)
        reparsed = parse_srt(render_srt(cues))
        self.assertEqual(cues, reparsed)

    def test_invalid_srt_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "thời gian"):
            parse_srt("1\nnot-a-time\nHello\n")

    def test_translate_cues_retains_timing_and_uses_cache(self):
        cues = [
            SubtitleCue(1, 0, 1000, "Lặp lại"),
            SubtitleCue(2, 1000, 2000, "Lặp lại"),
        ]
        translated = translate_cues(cues, "es", FakeTranslator(), batch_size=1)
        self.assertEqual([cue.text for cue in translated], ["[es] Lặp lại", "[es] Lặp lại"])
        self.assertEqual([(cue.start_ms, cue.end_ms) for cue in translated], [(0, 1000), (1000, 2000)])

    def test_vietnamese_is_a_supported_translation_target(self):
        from subtitle_translator import TARGET_LANGUAGES

        self.assertEqual(TARGET_LANGUAGES["vi"]["name"], "Tiếng Việt")
        cues = translate_cues([SubtitleCue(1, 0, 1000, "Hello")], "vi", FakeTranslator())
        self.assertEqual(cues[0].text, "[vi] Hello")

    def test_ass_is_bottom_aligned_and_uses_language_font(self):
        ass = render_ass(parse_srt(SAMPLE_SRT), "ja")
        self.assertIn("Yu Gothic UI", ass)
        self.assertIn(",2,70,70,48,1", ass)
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:03.25", ass)

    def test_find_matching_vietnamese_srt(self):
        temp_dir = TEST_TEMP_DIR / "matching"
        temp_dir.mkdir(parents=True, exist_ok=True)
        video = temp_dir / "movie.mp4"
        source = temp_dir / "movie.vi.srt"
        video.write_bytes(b"video")
        source.write_text(SAMPLE_SRT, encoding="utf-8")
        self.assertEqual(find_matching_srt(str(video)), str(source))


class TranslationManagerTests(unittest.TestCase):
    def test_whisper_model_download_populates_local_model_folder(self):
        from speech_transcriber import MODEL_FILES

        root = TEST_TEMP_DIR / f"whisper-download-{time.time_ns()}"
        root.mkdir(parents=True, exist_ok=True)
        manager = FasterWhisperTranscriber(str(root))

        class FakeResponse:
            status_code = 200
            headers = {"Content-Length": "4"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield b"data"

        with mock.patch("speech_transcriber.requests.get", return_value=FakeResponse()):
            ok, error = manager.start_model_download()
            self.assertTrue(ok, error)
            deadline = time.time() + 5
            while manager.downloading_model and time.time() < deadline:
                time.sleep(0.02)
        self.assertTrue(manager.model_ready)
        self.assertEqual(manager.download_pct, 100)
        self.assertEqual({path.name for path in manager.model_dir.iterdir()}, set(MODEL_FILES))

    def test_whisper_download_recovers_completed_partial_file(self):
        from speech_transcriber import MODEL_FILES

        root = TEST_TEMP_DIR / f"whisper-resume-{time.time_ns()}"
        manager = FasterWhisperTranscriber(str(root))
        manager.model_dir.mkdir(parents=True)
        for filename in MODEL_FILES:
            if filename != "model.bin":
                (manager.model_dir / filename).write_bytes(b"data")
        (manager.model_dir / "model.bin.part").write_bytes(b"data")

        class RangeComplete:
            status_code = 416
            headers = {"Content-Range": "bytes */4"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch("speech_transcriber.requests.get", return_value=RangeComplete()):
            ok, error = manager.start_model_download()
            self.assertTrue(ok, error)
            deadline = time.time() + 5
            while manager.downloading_model and time.time() < deadline:
                time.sleep(0.02)
        self.assertTrue(manager.model_ready)
        self.assertFalse((manager.model_dir / "model.bin.part").exists())

    def test_srt_only_job_runs_in_background_and_writes_result(self):
        temp_dir = TEST_TEMP_DIR / "manager"
        temp_dir.mkdir(parents=True, exist_ok=True)
        video = temp_dir / "movie.mp4"
        source = temp_dir / "movie.vi.srt"
        video.write_bytes(b"video")
        source.write_text(SAMPLE_SRT, encoding="utf-8")
        manager = TranslationManager(str(temp_dir), lambda: {}, lambda _: 10.0)

        ok, error = manager.start_translation(
            str(video),
            str(source),
            ["es", "ja"],
            burn_video=False,
            translator_factory=lambda _: FakeTranslator(),
        )
        self.assertTrue(ok, error)
        deadline = time.time() + 5
        while manager.snapshot()["translationRunning"] and time.time() < deadline:
            time.sleep(0.02)

        snapshot = manager.snapshot()
        self.assertFalse(snapshot["translationRunning"])
        self.assertEqual(snapshot["translationPct"], 100)
        self.assertEqual(len(snapshot["translationResults"]), 2)
        for language in ("es", "ja"):
            output = temp_dir / f"movie.{language}.srt"
            self.assertTrue(output.is_file())
            self.assertIn(f"[{language}]", output.read_text(encoding="utf-8-sig"))

    def test_manager_validates_inputs(self):
        manager = TranslationManager(os.getcwd(), lambda: {}, lambda _: 0)
        ok, error = manager.start_translation("missing.mp4", "missing.srt", ["es"])
        self.assertFalse(ok)
        self.assertIn("không tồn tại", error)


class AutoMergeOptionTests(unittest.TestCase):
    def test_stage_timer_accumulates_only_while_running(self):
        manager = phimora_ui.SyncManager()
        manager._set_stage("merge", "running", 10, "Ghép")
        time.sleep(0.12)
        manager._set_stage("merge", "paused", 50, "Chờ dịch")
        paused = manager.get_snapshot()["pipeline"]["merge"]["elapsedSeconds"]
        time.sleep(0.03)
        self.assertEqual(manager.get_snapshot()["pipeline"]["merge"]["elapsedSeconds"], paused)
        manager._set_stage("merge", "running", 50, "Ghép tiếp")
        time.sleep(0.12)
        manager._set_stage("merge", "done", 100, "Hoàn tất")
        finished = manager.get_snapshot()["pipeline"]["merge"]["elapsedSeconds"]
        self.assertGreater(finished, paused)
        time.sleep(0.03)
        self.assertEqual(manager.get_snapshot()["pipeline"]["merge"]["elapsedSeconds"], finished)

    def test_download_error_stops_before_merge(self):
        manager = phimora_ui.SyncManager()
        with (
            mock.patch.object(phimora_ui, "download_episode_with_retry", side_effect=RuntimeError("network down")),
            mock.patch.object(phimora_ui, "merge_episode_range") as merge_mock,
        ):
            ok, error, _ = manager.start(
                str(TEST_TEMP_DIR), 1, True, "all", "none", [],
                [{"bookId": "sample", "title": "Sample", "episodes": [1, 2]}],
            )
            self.assertTrue(ok, error)
            deadline = time.time() + 5
            while manager.get_snapshot()["running"] and time.time() < deadline:
                time.sleep(0.02)
        pipeline = manager.get_snapshot()["pipeline"]
        self.assertEqual(pipeline["download"]["state"], "error")
        self.assertIn("network down", pipeline["download"]["message"])
        self.assertEqual(pipeline["merge"]["state"], "skipped")
        merge_mock.assert_not_called()

    def test_merge_error_stops_before_translation(self):
        temp_dir = TEST_TEMP_DIR / "sync-merge-error"
        temp_dir.mkdir(parents=True, exist_ok=True)

        def fake_download(book_id, episode, series_dir, **kwargs):
            video = Path(series_dir) / f"Tap_{episode:03d}.mp4"
            video.write_bytes(b"video")
            return str(video), "test"

        with (
            mock.patch.object(phimora_ui, "model_is_ready", return_value=True),
            mock.patch.object(phimora_ui, "download_episode_with_retry", side_effect=fake_download),
            mock.patch.object(phimora_ui, "merge_episode_range", side_effect=RuntimeError("ffmpeg failed")),
            mock.patch.object(phimora_ui.TRANSLATION_MGR, "snapshot", return_value={"translationRunning": False}),
            mock.patch.object(phimora_ui.TRANSLATION_MGR, "start_translation") as translate_mock,
        ):
            manager = phimora_ui.SyncManager()
            ok, error, _ = manager.start(
                str(temp_dir), 1, True, "all", "translate", ["ja"],
                [{"bookId": "sample", "title": "Sample", "episodes": [1, 2]}],
            )
            self.assertTrue(ok, error)
            deadline = time.time() + 5
            while manager.get_snapshot()["running"] and time.time() < deadline:
                time.sleep(0.02)
        pipeline = manager.get_snapshot()["pipeline"]
        self.assertEqual(pipeline["download"]["state"], "done")
        self.assertEqual(pipeline["merge"]["state"], "error")
        self.assertEqual(pipeline["translate"]["state"], "skipped")
        translate_mock.assert_not_called()

    def test_hidden_episode_subtitle_defaults_are_always_enabled(self):
        temp_dir = TEST_TEMP_DIR / "sync-defaults"
        temp_dir.mkdir(parents=True, exist_ok=True)
        captured = {}

        def fake_download(book_id, episode, series_dir, **kwargs):
            captured.update(kwargs)
            video = Path(series_dir) / f"Tap_{episode:03d}.mp4"
            video.write_bytes(b"video")
            return str(video), "test"

        with mock.patch.object(phimora_ui, "download_episode_with_retry", side_effect=fake_download):
            manager = phimora_ui.SyncManager()
            ok, error, _ = manager.start(
                str(temp_dir), 1, False, "all", "soft", [],
                [{"bookId": "sample", "title": "Sample", "episodes": [1]}],
            )
            self.assertTrue(ok, error)
            deadline = time.time() + 5
            while manager.get_snapshot()["running"] and time.time() < deadline:
                time.sleep(0.02)
        self.assertTrue(captured["mux_subs"])
        self.assertTrue(captured["keep_vtt"])
        videos = manager.get_snapshot()["completedVideos"]
        self.assertEqual(videos, [])

    def test_translation_mode_requires_selected_language(self):
        manager = phimora_ui.SyncManager()
        ok, error, _ = manager.start(
            str(TEST_TEMP_DIR), 1, True, "all", "translate", [],
            [{"bookId": "sample", "title": "Sample", "episodes": [1]}],
        )
        self.assertFalse(ok)
        self.assertIn("ngôn ngữ", error)

    def test_auto_merge_translation_passes_merged_srt_and_languages(self):
        temp_dir = TEST_TEMP_DIR / "sync-translate"
        temp_dir.mkdir(parents=True, exist_ok=True)
        captured = {}

        def fake_download(book_id, episode, series_dir, **kwargs):
            video = Path(series_dir) / f"Tap_{episode:03d}.mp4"
            video.write_bytes(b"video")
            return str(video), "test"

        def fake_merge(**kwargs):
            captured["merge_mode"] = kwargs["sub_mode"]
            video = Path(kwargs["series_dir"]) / "merged.mp4"
            srt = Path(kwargs["series_dir"]) / "merged.en.srt"
            video.write_bytes(b"merged")
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            return {"out_mp4": str(video), "out_srt": str(srt)}

        def fake_translate(video, srt, languages, burn_video):
            captured.update(video=video, srt=srt, languages=languages, burn_video=burn_video)
            return True, ""

        fake_snapshot = {
            "translationRunning": False,
            "translationResults": [{"language": "ja", "language_name": "Nhật Bản", "out_mp4": str(temp_dir / "merged.ja.HardSub.mp4")}],
            "translationStatus": "Hoàn tất",
        }
        with (
            mock.patch.object(phimora_ui, "model_is_ready", return_value=True),
            mock.patch.object(phimora_ui, "download_episode_with_retry", side_effect=fake_download),
            mock.patch.object(phimora_ui, "merge_episode_range", side_effect=fake_merge),
            mock.patch.object(phimora_ui.TRANSLATION_MGR, "start_translation", side_effect=fake_translate),
            mock.patch.object(phimora_ui.TRANSLATION_MGR, "snapshot", return_value=fake_snapshot),
        ):
            manager = phimora_ui.SyncManager()
            ok, error, _ = manager.start(
                str(temp_dir), 1, True, "all", "translate", ["ja"],
                [{"bookId": "sample", "title": "Sample", "episodes": [1, 2]}],
            )
            self.assertTrue(ok, error)
            deadline = time.time() + 5
            while manager.get_snapshot()["running"] and time.time() < deadline:
                time.sleep(0.02)
        self.assertEqual(captured["merge_mode"], "soft")
        self.assertEqual(captured["languages"], ["ja"])
        self.assertTrue(captured["srt"].endswith(".en.srt"))
        self.assertTrue(captured["burn_video"])
        self.assertIn("Đã dịch 1", manager.get_snapshot()["notice"])
        videos = manager.get_snapshot()["completedVideos"]
        self.assertEqual([item["type"] for item in videos], ["Sub Nhật Bản", "Tập dài"])


class MergeSubtitleContractTests(unittest.TestCase):
    def test_speech_fallback_creates_english_subtitle_files(self):
        temp_dir = TEST_TEMP_DIR / "merge-asr"
        temp_dir.mkdir(parents=True, exist_ok=True)
        episode = temp_dir / "Tap_001.mp4"
        episode.write_bytes(b"video")
        local_map = {1: {"mp4": str(episode), "has_sub": False}}
        result_file = temp_dir / "result.mp4"

        def fake_ffmpeg(command, *args, **kwargs):
            Path(command[-1]).write_bytes(b"merged")
            return 0, ""

        with (
            mock.patch.object(phimora_downloader, "scan_local_episodes", return_value=local_map),
            mock.patch.object(phimora_downloader, "get_video_duration", return_value=10.0),
            mock.patch.object(phimora_downloader, "extract_or_read_episode_vtt", return_value=""),
            mock.patch.object(phimora_downloader.shutil, "which", return_value="ffmpeg"),
            mock.patch.object(phimora_downloader, "_run_ffmpeg_with_progress", side_effect=fake_ffmpeg),
        ):
            result = phimora_downloader.merge_episode_range(
                str(temp_dir), [1], sub_mode="soft", keep_sub_files=True,
                subtitle_fallback_fn=lambda *_: "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n",
            )
        self.assertEqual(result["sub_language"], "en")
        self.assertTrue(result["out_srt"].endswith(".en.srt"))
        self.assertTrue(Path(result["out_srt"]).is_file())

    def test_merge_with_sub_mode_rejects_missing_source_subtitles(self):
        temp_dir = TEST_TEMP_DIR / "merge-no-sub"
        temp_dir.mkdir(parents=True, exist_ok=True)
        episode = temp_dir / "Tap_001.mp4"
        episode.write_bytes(b"video")
        local_map = {1: {"mp4": str(episode), "has_sub": False}}
        with (
            mock.patch.object(phimora_downloader, "scan_local_episodes", return_value=local_map),
            mock.patch.object(phimora_downloader, "get_video_duration", return_value=10.0),
            mock.patch.object(phimora_downloader, "extract_or_read_episode_vtt", return_value=""),
            mock.patch.object(phimora_downloader.shutil, "which", return_value="ffmpeg"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Không tìm thấy phụ đề"):
                phimora_downloader.merge_episode_range(
                    str(temp_dir), [1], sub_mode="soft", keep_sub_files=True
                )

    def test_shortical_download_saves_published_vtt(self):
        temp_dir = TEST_TEMP_DIR / "shortical-sub"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_video = temp_dir / ".tmp_ep_001_v.ts"

        def fake_download(*args, **kwargs):
            temp_video.write_bytes(b"transport-stream")

        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(b"mp4")
            return mock.Mock(returncode=0, stderr="")

        with (
            mock.patch.object(phimora_downloader, "fetch_shortical_subtitle_vtt", return_value="WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nXin chào\n"),
            mock.patch.object(phimora_downloader, "download_hls_stream", side_effect=fake_download),
            mock.patch.object(phimora_downloader.shutil, "which", return_value="ffmpeg"),
            mock.patch.object(phimora_downloader.subprocess, "run", side_effect=fake_run) as run_mock,
        ):
            output, _ = phimora_downloader.download_shortical_episode(
                "184", 1, str(temp_dir), lang="vi", mux_subs=True, keep_vtt=True
            )
        self.assertTrue(Path(output).is_file())
        self.assertTrue((temp_dir / "Tap_001.vi.vtt").is_file())
        command = run_mock.call_args.args[0]
        self.assertIn("mov_text", command)


if __name__ == "__main__":
    unittest.main()
