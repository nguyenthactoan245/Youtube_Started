"""Desktop layout/playback regression: python tests/preview_layout_smoke.py clip.mp4 [...]."""
from __future__ import annotations

import argparse
import asyncio
import base64
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import flet as ft
import main as app
from models import Video


def run(media_paths: list[Path]) -> None:
    failures = []

    async def check(page: ft.Page) -> None:
        page.title = "Stock preview layout check"
        page.window.maximized = True
        page.add(ft.Text("Checking preview dimensions and one-click playback"))
        # Local poster avoids depending on Pixabay/network access for layout testing.
        poster = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        try:
            for path in media_paths:
                for kind in ("download-short", "download-long", "library"):
                    title = ("mausoleum, lenin, moscow, red place, art, culture, building, light, "
                             "course of the day, sunrise, sunset, lighting, house, facade, shadows, architecture") if kind == "download-long" else path.name
                    video = Video(-1, title, 4, "test", "", str(path), poster, 0, 0, 1)
                    dialog = (app.build_preview_dialog(video, lambda _: None) if kind.startswith("download")
                              else app.build_library_preview_dialog(path, poster, lambda _: None))
                    player = dialog.data["player"]
                    sizes, errors = {}, []
                    positions = []
                    for key in ("frame", "player", "poster"):
                        def resized(event, key=key):
                            sizes[key] = (event.width, event.height)
                        dialog.data[key].on_size_change = resized
                    dialog.content.on_size_change = lambda event: sizes.__setitem__("content", (event.width, event.height))
                    dialog.title.on_size_change = lambda event: sizes.__setitem__("title", (event.width, event.height))
                    player.on_error = lambda event: errors.append(str(event.data))
                    player.on_position_change = lambda event: positions.append(event.data)
                    page.show_dialog(dialog)
                    try:
                        for _ in range(100):
                            if len(sizes) == 5 and dialog.data["playback"]["loaded"]:
                                break
                            await asyncio.sleep(0.1)
                        assert all(sizes[k] == (1280.0, 720.0) for k in ("frame", "player", "poster")), sizes
                        assert sizes["content"][0] == 1280 and sizes["title"][0] <= 1280, sizes
                        await dialog.data["poster"].content.controls[1].content.on_click(None)
                        await asyncio.sleep(1.2)
                        assert sizes["frame"] == sizes["player"] == (1280.0, 720.0), sizes
                        assert len(set(map(str, positions))) > 1, positions
                        assert not errors, errors
                        print(f"PASS {kind}/{path.name}: {sizes}, position_updates={len(positions)}", flush=True)
                    finally:
                        await player.stop()
                        page.pop_dialog()
                        await asyncio.sleep(0.3)
        except Exception as exc:
            failures.append(str(exc))
            print(f"FAIL: {exc}", flush=True)
        finally:
            await page.window.destroy()

    ft.run(check)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", nargs="+", type=Path)
    args = parser.parse_args()
    run([path.resolve(strict=True) for path in args.media])
