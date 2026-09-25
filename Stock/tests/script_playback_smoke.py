"""Real Flet desktop playback check, using an existing local MP4."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flet as ft
from script_video import ScriptVideo
from main import build_library_preview_dialog


failures = []


async def main(page):
    page.title = "Stock Script playback test"
    errors, previews = [], []

    async def before(_):
        pass

    tile = ScriptVideo(Path(sys.argv[1]), None, page, previews.append, before, errors.append)
    page.add(tile.control)
    try:
        await asyncio.sleep(0.5)
        for attempt in range(2):
            await tile.toggle(None)
            player = tile.player
            positions = []
            player.on_position_change = lambda e: positions.append(str(e.data))
            page.update()
            for _ in range(60):
                await asyncio.sleep(0.1)
                if len(set(positions)) > 2 or errors:
                    break
            assert not errors, errors
            assert len(set(positions)) > 2, f"Playback did not advance: {positions}"
            await tile.toggle(None)
            assert tile.player is None
            assert not previews
            print(f"PASS play/stop {attempt + 1}: {positions[-3:]}", flush=True)
        await tile.preview(None)
        assert previews == [Path(sys.argv[1])]
        print("PASS preview callback", flush=True)
        dialog = build_library_preview_dialog(Path(sys.argv[1]), None, lambda _: None)
        preview_player = dialog.data["player"]
        positions = []
        preview_player.on_position_change = lambda e: positions.append(str(e.data))
        preview_player.on_error = lambda e: errors.append(str(e.data))
        page.show_dialog(dialog)
        for _ in range(60):
            if dialog.data["playback"]["loaded"]:
                break
            await asyncio.sleep(0.1)
        await dialog.actions[0].on_click(None)
        for _ in range(60):
            await asyncio.sleep(0.1)
            if len(set(positions)) > 2 or errors:
                break
        assert len(set(positions)) > 2, positions
        assert not errors, errors
        await preview_player.stop()
        page.pop_dialog()
        print("PASS real preview playback", flush=True)
    except Exception as exc:
        failures.append(repr(exc))
        print(f"FAIL: {exc!r}", flush=True)
    finally:
        await tile.stop()
        await page.window.destroy()


ft.run(main)
if failures:
    raise SystemExit(1)
