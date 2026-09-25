"""Shared video download preferences and controls."""
from __future__ import annotations

import flet as ft


def build_video_settings(preferences: dict[str, str]) -> ft.Row:
    quality = ft.Dropdown(
        label="Chất lượng",
        value=preferences.get("quality", "2K"),
        width=170,
        options=[ft.DropdownOption(key=value, text=value) for value in ("HD", "2K", "4K")],
    )
    orientation = ft.Dropdown(
        label="Kích thước",
        value=preferences.get("orientation", "landscape"),
        width=190,
        options=[
            ft.DropdownOption(key="landscape", text="Ngang"),
            ft.DropdownOption(key="portrait", text="Dọc"),
        ],
    )

    def update_quality(event):
        preferences["quality"] = event.control.value or "2K"

    def update_orientation(event):
        preferences["orientation"] = event.control.value or "landscape"

    quality.on_change = update_quality
    orientation.on_change = update_orientation
    return ft.Row(
        controls=[ft.Text("Thiết lập tải video", color=ft.Colors.BLUE_GREY_300), quality, orientation],
        spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        wrap=True,
    )
