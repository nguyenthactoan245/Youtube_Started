"""Shared video download preferences and controls."""
from __future__ import annotations

import flet as ft


def build_video_settings(preferences: dict[str, str], include_source: bool = False,
                         include_method: bool = False, include_workers: bool = False) -> ft.Row:
    quality = ft.Dropdown(
        label="Chất lượng",
        value=preferences.get("quality", "2K"),
        width=170,
        options=[ft.DropdownOption(key=value, text=value) for value in ("All", "HD", "2K", "4K")],
    )
    orientation = ft.Dropdown(
        label="Kích thước",
        value=preferences.get("orientation", "landscape"),
        width=190,
        options=[
            ft.DropdownOption(key="all", text="All"),
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
    controls = [ft.Text("Thiết lập tải video", color=ft.Colors.BLUE_GREY_300), quality, orientation]
    source = None
    if include_source:
        source = ft.Dropdown(
            label="Nguồn",
            value=preferences.get("source", "pixabay"),
            width=160,
            options=[ft.DropdownOption(key="pixabay", text="Pixabay"),
                     ft.DropdownOption(key="pexels", text="Pexels")],
        )
        source.on_change = lambda event: preferences.__setitem__(
            "source", event.control.value or "pixabay")
        controls.append(source)
    row = ft.Row(controls=controls, spacing=12,
                 vertical_alignment=ft.CrossAxisAlignment.CENTER, wrap=True)
    row.source_dropdown = source
    method = None
    if include_method:
        method = ft.Dropdown(
            label="Phương thức",
            value=preferences.get("method", "direct"),
            width=190,
            options=[
                ft.DropdownOption(key="api", text="Crawl by API"),
                ft.DropdownOption(key="direct", text="Crawl trực tiếp"),
            ],
        )
        method.on_change = lambda event: preferences.__setitem__(
            "method", event.control.value or "direct")
        row.controls.insert(len(row.controls) - (1 if source is not None else 0), method)
    row.method_dropdown = method
    workers = None
    if include_workers:
        worker_value = str(preferences.get("workers", "5"))
        if worker_value not in {str(value) for value in range(1, 21)}:
            worker_value = "5"
        preferences["workers"] = worker_value
        workers = ft.Dropdown(
            label="Số luồng",
            value=worker_value,
            width=125,
            options=[ft.DropdownOption(key=str(value), text=str(value))
                     for value in range(1, 21)],
        )
        workers.on_change = lambda event: preferences.__setitem__(
            "workers", event.control.value or "5")
        row.controls.append(workers)
    row.workers_dropdown = workers
    return row
