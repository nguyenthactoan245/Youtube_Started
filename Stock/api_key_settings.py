"""Setup controls for editing API credentials without displaying them by default."""
import asyncio

import flet as ft

from app_config import (configured_api_keys, configured_pexels_api_keys, save_api_keys,
                       save_pexels_api_keys)
from services.api_keys import get_api_key_pool


class ApiKeySettings(ft.Column):
    def __init__(self, page, root):
        super().__init__(spacing=10)
        self.host_page = page
        self.root = root
        self.fields = []
        self.rows = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, height=220)
        self.pexels_rows = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, height=160)
        self.pexels_fields = []
        self.status = ft.Text()
        self.key_status = ft.Text()
        self.pexels_status = ft.Text()
        self.save_pexels_button = ft.OutlinedButton("Lưu danh sách Pexels key", icon=ft.Icons.SAVE,
                                                    on_click=self.save_pexels)
        self.save_button = ft.FilledButton("Lưu danh sách key", icon=ft.Icons.SAVE, on_click=self.save)
        self.save_pexels_button = ft.OutlinedButton("Lưu Pexels key", icon=ft.Icons.SAVE,
                                                    on_click=self.save_pexels)
        self.controls = [
            ft.Text("Pixabay API keys", weight=ft.FontWeight.BOLD),
            ft.Text("Thêm hoặc xóa key rồi bấm Lưu. Key được dùng luân phiên khi tìm video."),
            self.rows,
            ft.Row([
                ft.OutlinedButton("Thêm key", icon=ft.Icons.ADD, on_click=self.add),
                self.save_button,
                ft.OutlinedButton("Cập nhật trạng thái", icon=ft.Icons.REFRESH, on_click=self.refresh),
            ], wrap=True),
            self.status, self.key_status,
            ft.Divider(),
            ft.Text("Pexels API keys", weight=ft.FontWeight.BOLD),
            ft.Text("Có thể lưu nhiều key để quản lý; app dùng key đầu tiên trong danh sách."),
            self.pexels_rows,
            ft.Row([
                ft.OutlinedButton("Thêm key Pexels", icon=ft.Icons.ADD, on_click=self.add_pexels),
                self.save_pexels_button,
            ], wrap=True),
            self.pexels_status,
        ]
        for key in configured_api_keys() or [""]:
            self.append_field(key)
        for key in configured_pexels_api_keys() or [""]:
            self.append_pexels_field(key)
        self.refresh_status()

    def append_pexels_field(self, value=""):
        field = ft.TextField(value=value, password=True, can_reveal_password=True, expand=True)
        self.pexels_fields.append(field)

        def remove(_):
            self.pexels_fields.remove(field)
            self.rebuild_pexels()
            self.host_page.update()

        field.data = remove
        self.rebuild_pexels()

    def rebuild_pexels(self):
        self.pexels_rows.controls = []
        for i, field in enumerate(self.pexels_fields, 1):
            field.label = f"Pexels Key {i}"
            self.pexels_rows.controls.append(ft.Row([
                field, ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip=f"Xóa Pexels Key {i}",
                                     on_click=field.data),
            ]))

    def add_pexels(self, _):
        self.append_pexels_field()
        self.host_page.update()

    def append_field(self, value=""):
        field = ft.TextField(value=value, password=True, can_reveal_password=True, expand=True)
        self.fields.append(field)

        def remove(_):
            self.fields.remove(field)
            self.rebuild()
            self.host_page.update()

        field.data = remove
        self.rebuild()

    def rebuild(self):
        self.rows.controls = []
        for i, field in enumerate(self.fields, 1):
            field.label = f"Key {i}"
            self.rows.controls.append(ft.Row([
                field, ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, tooltip=f"Xóa Key {i}",
                                    on_click=field.data),
            ]))

    def add(self, _):
        self.append_field()
        self.host_page.update()

    def refresh_status(self):
        self.key_status.value = "\n".join(get_api_key_pool().statuses()) or "Chưa lưu API key."

    def refresh(self, _):
        self.refresh_status()
        self.host_page.update()

    async def save(self, _):
        if self.save_button.disabled:
            return
        value = "\n".join(field.value or "" for field in self.fields)
        self.disabled = True
        self.save_button.disabled = True
        self.host_page.update()
        try:
            keys = await asyncio.to_thread(save_api_keys, value, self.root)
            self.status.value = f"Đã lưu {len(keys)} key. Áp dụng từ yêu cầu tiếp theo."
            self.status.color = ft.Colors.GREEN_300
            self.fields.clear()
            self.rebuild()
            for key in keys:
                self.append_field(key)
            self.refresh_status()
        except (OSError, ValueError) as exc:
            self.status.value = (str(exc) if isinstance(exc, ValueError)
                                 else "Không thể lưu cấu hình. Hãy kiểm tra quyền ghi thư mục dữ liệu.")
            self.status.color = ft.Colors.RED_300
        finally:
            self.disabled = False
            self.save_button.disabled = False
            self.host_page.update()

    async def save_pexels(self, _):
        if self.save_pexels_button.disabled:
            return
        self.disabled = True
        self.save_pexels_button.disabled = True
        self.host_page.update()
        try:
            value = "\n".join(field.value or "" for field in self.pexels_fields)
            keys = await asyncio.to_thread(save_pexels_api_keys, value, self.root)
            self.pexels_status.value = (f"Đã lưu {len(keys)} Pexels key. Áp dụng từ yêu cầu tiếp theo."
                                        if keys else "Đã xóa toàn bộ Pexels API key.")
            self.pexels_status.color = ft.Colors.GREEN_300
            self.pexels_fields.clear()
            self.rebuild_pexels()
            for key in keys:
                self.append_pexels_field(key)
        except (OSError, ValueError) as exc:
            self.pexels_status.value = (str(exc) if isinstance(exc, ValueError)
                                        else "Không thể lưu Pexels API key. Hãy kiểm tra quyền ghi thư mục dữ liệu.")
            self.pexels_status.color = ft.Colors.RED_300
        finally:
            self.disabled = False
            self.save_pexels_button.disabled = False
            self.host_page.update()
