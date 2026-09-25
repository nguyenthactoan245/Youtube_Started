"""Inline Script player with a separate preview hit target."""
from pathlib import Path
import asyncio

import flet as ft
import flet_video as ftv


class ScriptVideo:
    def __init__(self, path, poster, page, open_preview, before_play, report_error):
        self.path = Path(path)
        self.page = page
        self.open_preview = open_preview
        self.before_play = before_play
        self.report_error = report_error
        self.player = None
        self.lock = asyncio.Lock()
        self.poster = (ft.Image(src=poster, width=160, height=90, fit=ft.BoxFit.COVER) if poster else
                       ft.Container(bgcolor="#10182c", alignment=ft.Alignment(0, 0),
                                    content=ft.Icon(ft.Icons.VIDEO_FILE_OUTLINED)))
        self.surface = ft.Container(content=self.poster, width=160, height=90)
        self.button = ft.IconButton(icon=ft.Icons.PLAY_ARROW, tooltip="Phát tại chỗ", icon_size=28,
                                   icon_color=ft.Colors.WHITE, bgcolor="#B0000000", on_click=self.toggle)
        # Sibling overlay targets ensure the play button does not also open preview.
        self.control = ft.Stack(width=160, height=90, controls=[
            self.surface,
            ft.Container(left=0, right=0, top=0, bottom=0, on_click=self.preview,
                         tooltip="Mở preview"),
            ft.Container(left=60, top=25, width=40, height=40, content=self.button),
        ])

    async def stop(self):
        player, self.player = self.player, None
        if player:
            try:
                await player.stop()
            except Exception:
                pass
        self.surface.content = self.poster
        self.button.icon = ft.Icons.PLAY_ARROW
        self.button.tooltip = "Phát tại chỗ"

    async def toggle(self, _):
        async with self.lock:
            if self.player:
                await self.stop()
            elif not await asyncio.to_thread(self.path.is_file):
                self.report_error("Không tìm thấy file video. Hãy tải lại dòng này.")
            else:
                await self.before_play(self)

                async def complete(event):
                    # The native player also emits completed=false when opening media.
                    if self.player is player and str(event.data).lower() == "true":
                        await self.stop()
                        self.page.update()

                async def error(event):
                    if self.player is player:
                        await self.stop()
                        self.report_error(f"Không thể phát video: {event.data}")
                        self.page.update()

                player = ftv.Video(playlist=[ftv.VideoMedia(resource=str(self.path.resolve()))],
                    autoplay=True, controls=None, width=160, height=90, fit=ft.BoxFit.CONTAIN,
                    playlist_mode=ftv.PlaylistMode.SINGLE, on_complete=complete,
                    on_error=error)
                self.player = player
                self.surface.content = player
                self.button.icon = ft.Icons.STOP
                self.button.tooltip = "Dừng video"
            self.page.update()

    async def preview(self, _):
        await self.stop()
        await self.before_play(self)
        if await asyncio.to_thread(self.path.is_file):
            if self.open_preview:
                self.open_preview(self.path)
        else:
            self.report_error("Không tìm thấy file video. Hãy tải lại dòng này.")
        self.page.update()
