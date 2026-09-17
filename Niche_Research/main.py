from __future__ import annotations

import asyncio
import os
import queue
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import flet as ft

from crawler import ROOT, CrawlJob, load_rows, read_json, saved_runs

ORANGE, BG, TEXT, MUTED = '#E87917', '#F3F6FA', '#273244', '#748096'


def number(value):
    return '—' if value is None else f'{int(value):,}'


def posted(row):
    value = row.get('created_time')
    if not value:
        return 'Chưa có ngày đăng'
    try:
        if row.get('created_time_precision') == 'date_only':
            return datetime.fromisoformat(value).strftime('%d/%m/%Y')
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(
            timezone(timedelta(hours=7))).strftime('%d/%m/%Y · %H:%M')
    except ValueError:
        return value


class ResearchApp:
    def __init__(self, page: ft.Page):
        self.page, self.job, self.run_dir = page, None, None
        self.rows, self.logs = [], []
        page.title = 'YouTube Spy | Niche Research'
        page.theme_mode = ft.ThemeMode.LIGHT
        page.theme = ft.Theme(color_scheme_seed=ORANGE, font_family='Segoe UI')
        page.bgcolor, page.padding = BG, 0
        page.window.width, page.window.height = 1380, 900
        page.window.min_width, page.window.min_height = 1100, 760
        page.window.prevent_close = True
        page.window.on_event = self.on_window
        self.channel = ft.TextField(label='Tên kênh hoặc URL YouTube', value='@InfinitePlanet4K',
                                    hint_text='@tenkenh', prefix_icon=ft.Icons.LINK, expand=True,
                                    on_submit=self.start)
        self.limit = ft.TextField(label='Giới hạn / tab', value='0', width=150,
                                  tooltip='0 = tất cả video. Áp dụng riêng cho mỗi tab Videos / Shorts / Streams.')
        self.start_button = ft.FilledButton('Crawl dữ liệu', icon=ft.Icons.DOWNLOAD, on_click=self.start,
                                            style=ft.ButtonStyle(bgcolor=ORANGE, color='white'), height=48)
        self.stop_button = ft.OutlinedButton('Dừng', icon=ft.Icons.STOP_CIRCLE_OUTLINED,
                                             on_click=self.stop, disabled=True, height=48)
        self.status = ft.Text('Sẵn sàng crawl', color=TEXT, weight=ft.FontWeight.W_600)
        self.progress = ft.ProgressBar(value=0, color=ORANGE, bgcolor='#F7DEC2', height=5)
        self.history = ft.Dropdown(label='Dữ liệu đã lưu', expand=True, on_select=self.select_run)
        self.search = ft.TextField(label='Tìm trong tiêu đề', prefix_icon=ft.Icons.SEARCH,
                                   expand=True, dense=True, on_change=self.render_cards)
        self.sort = ft.Dropdown(label='Sắp xếp', value='newest', width=180,
                               options=[ft.dropdown.Option('newest', 'Mới nhất'),
                                        ft.dropdown.Option('views', 'Lượt xem cao nhất'),
                                        ft.dropdown.Option('oldest', 'Cũ nhất')], on_select=self.render_cards)
        self.subtitle = ft.Text('Chọn một lần crawl để xem video.', color=MUTED, size=12)
        self.count = ft.Text('0 video', size=14, weight=ft.FontWeight.W_600, color=TEXT)
        self.stat_videos, self.stat_views, self.stat_images = [ft.Text('—', size=24, weight=ft.FontWeight.W_700, color=TEXT) for _ in range(3)]
        self.grid = ft.GridView(expand=True, max_extent=310, child_aspect_ratio=0.72, spacing=16, run_spacing=16)
        self.empty = ft.Container(content=ft.Column([
            ft.Icon(ft.Icons.VIDEO_LIBRARY_OUTLINED, size=44, color=MUTED),
            ft.Text('Chưa có video phù hợp', size=17, color=TEXT),
            ft.Text('Nhập tên kênh để crawl hoặc chọn dữ liệu đã lưu.', color=MUTED),
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER),
            expand=True, alignment=ft.Alignment.CENTER, visible=False)
        self.log_text = ft.Text('', size=11, font_family='Consolas', selectable=True, color=MUTED)
        self.log_box = ft.Container(content=ft.ListView([self.log_text], auto_scroll=True),
                                     height=115, padding=10, bgcolor='white', border_radius=8, visible=False)
        self.folder_button = ft.OutlinedButton('Thư mục', icon=ft.Icons.FOLDER_OPEN,
                                                on_click=lambda _: self.open_path(self.run_dir), disabled=True)
        self.csv_button = ft.OutlinedButton('Mở CSV', icon=ft.Icons.TABLE_CHART_OUTLINED,
                                             on_click=lambda _: self.open_path(self.run_dir / 'videos.csv' if self.run_dir else None), disabled=True)
        sidebar = ft.Container(width=180, bgcolor='white', padding=20, content=ft.Column([
            ft.Row([ft.Icon(ft.Icons.PLAY_CIRCLE_FILL, color=ORANGE, size=28),
                    ft.Text('YouTube Spy', weight=ft.FontWeight.W_700, color=TEXT, size=16)], spacing=7),
            ft.Text('NICHE RESEARCH', color=MUTED, size=10), ft.Container(height=24),
            ft.Container(content=ft.Row([ft.Icon(ft.Icons.GRID_VIEW, color=ORANGE, size=20),
                                         ft.Text('Khám phá video', color=ORANGE, size=12)]),
                         padding=12, bgcolor='#FFF2E5', border_radius=8),
            ft.TextButton('Mở kho dữ liệu', icon=ft.Icons.FOLDER_OUTLINED, on_click=lambda _: self.open_path(ROOT)),
            ft.Container(expand=True), ft.Divider(),
            ft.Text('Title · Views · Created time\nThumbnail', color=MUTED, size=11),
            ft.Text('Dữ liệu lưu trên máy', color=MUTED, size=11),
        ], spacing=12))
        crawl_panel = ft.Container(bgcolor='white', border_radius=12, padding=18, content=ft.Column([
            ft.Row([self.channel, self.limit, self.start_button, self.stop_button]),
            ft.Row([self.status, ft.Container(expand=True),
                    ft.Text('0 = tất cả video', size=11, color=MUTED)]), self.progress,
        ], spacing=12))
        stats = ft.Row([self.metric('VIDEO', self.stat_videos, ft.Icons.VIDEO_LIBRARY_OUTLINED),
                        self.metric('TỔNG LƯỢT XEM', self.stat_views, ft.Icons.VISIBILITY_OUTLINED),
                        self.metric('THUMBNAIL ĐÃ LƯU', self.stat_images, ft.Icons.IMAGE_OUTLINED)], spacing=14)
        body = ft.Container(expand=True, padding=24, content=ft.Column([
            ft.Row([ft.Column([ft.Text('Khám phá video', size=26, weight=ft.FontWeight.W_700, color=TEXT),
                               ft.Text('Thu thập và nghiên cứu nội dung từ kênh YouTube', size=13, color=MUTED)], spacing=3),
                    ft.Container(expand=True), ft.TextButton('Nhật ký', icon=ft.Icons.TERMINAL, on_click=self.toggle_logs)]),
            crawl_panel, ft.Row([self.history, self.folder_button, self.csv_button,
                                ft.IconButton(ft.Icons.REFRESH, tooltip='Tải lại lịch sử', on_click=self.refresh)]),
            self.subtitle, stats, ft.Row([self.search, self.sort]),
            ft.Row([self.count, ft.Container(expand=True), ft.Text('Giờ hiển thị: Việt Nam (UTC+7)', size=11, color=MUTED)]),
            self.grid, self.empty, self.log_box,
        ], spacing=14))
        page.add(ft.Row([sidebar, body], expand=True, spacing=0))
        self.refresh()

    def metric(self, label, value, icon):
        return ft.Container(expand=True, bgcolor='white', border_radius=10, padding=15,
                            content=ft.Row([ft.Icon(icon, color=ORANGE, size=26),
                                            ft.Column([ft.Text(label, size=10, color=MUTED), value], spacing=2)]))

    def message(self, text, error=False):
        self.status.value, self.status.color = text, '#C23A34' if error else TEXT
        self.page.update()

    def open_path(self, path):
        try:
            if path is None or not path.exists():
                raise FileNotFoundError('Chưa có file hoặc thư mục để mở.')
            os.startfile(str(path))
        except OSError as exc:
            self.message(str(exc), True)

    def refresh(self, _=None, preferred=None):
        runs = saved_runs()
        self.history.options = [ft.dropdown.Option(str(run), f'{run.parent.name}  ·  {self.run_label(run)}') for run in runs]
        default = None
        for run in runs:
            try:
                summary = read_json(run / 'summary.json')
                if summary.get('limit_per_playlist') == 0 and summary.get('video_count', 0) > 0:
                    default = run
                    break
            except (OSError, ValueError):
                continue
        chosen = preferred or (self.run_dir if self.run_dir in runs else default or (runs[0] if runs else None))
        if chosen:
            self.history.value = str(chosen)
            self.load_run(chosen)
        else:
            self.render_cards()

    def select_run(self, _):
        if self.history.value:
            self.load_run(Path(self.history.value))

    @staticmethod
    def run_label(run):
        try:
            return datetime.strptime(run.name[:15], '%Y%m%d_%H%M%S').strftime('%d/%m/%Y %H:%M:%S')
        except ValueError:
            return run.name

    def load_run(self, run):
        try:
            rows = load_rows(run)
            summary = read_json(run / 'summary.json') if (run / 'summary.json').exists() else {}
            self.run_dir, self.rows = run, rows
            label = rows[0].get('channel') if rows else run.parent.name
            status = {'complete': 'Hoàn tất', 'cancelled': 'Đã dừng', 'failed': 'Không lấy được dữ liệu',
                      'partial': 'Có dữ liệu thiếu', 'partial_check_log': 'Cần xem nhật ký',
                      'finished_check_log_for_skipped_videos': 'Đã lưu'}.get(summary.get('status'), 'Đã lưu')
            scope = f"Giới hạn {summary['limit_per_playlist']} video/tab" if summary.get('limit_per_playlist') else 'Không giới hạn'
            self.subtitle.value = f"{label}  ·  {self.run_label(run)}  ·  {scope}  ·  {status}"
            self.stat_videos.value = number(len(rows))
            self.stat_views.value = number(sum(row.get('view_count') or 0 for row in rows))
            self.stat_images.value = number(sum(bool(row.get('thumbnail_file')) and (run / row['thumbnail_file']).is_file() for row in rows))
            self.csv_button.disabled = not (run / 'videos.csv').exists()
            self.folder_button.disabled = False
            if not self.job:
                log = run / 'crawl.log'
                if log.exists():
                    # The original PowerShell crawler wrote UTF-16 logs.
                    data = log.read_bytes()
                    self.logs = data.decode('utf-16' if data.startswith(b'\xff\xfe') else 'utf-8-sig', errors='replace').splitlines()[-120:]
                    self.log_text.value = '\n'.join(self.logs)
            self.render_cards()
        except (OSError, ValueError, TypeError) as exc:
            self.message(f'Không đọc được dữ liệu: {exc}', True)

    def render_cards(self, _=None):
        keyword = (self.search.value or '').casefold().strip()
        rows = [row for row in self.rows if keyword in (row.get('title') or '').casefold()]
        if self.sort.value == 'views':
            rows.sort(key=lambda row: row.get('view_count') if row.get('view_count') is not None else -1, reverse=True)
        else:
            rows.sort(key=lambda row: row.get('created_time') or '', reverse=self.sort.value != 'oldest')
        self.grid.controls = [self.card(row) for row in rows]
        self.count.value = f'{len(rows)} / {len(self.rows)} video'
        self.grid.visible, self.empty.visible = bool(rows), not rows
        self.page.update()

    def card(self, row):
        path = self.run_dir / row['thumbnail_file'] if row.get('thumbnail_file') else None
        placeholder = ft.Container(height=150, bgcolor='#E9EEF5', alignment=ft.Alignment.CENTER,
                                     content=ft.Icon(ft.Icons.IMAGE_OUTLINED, color=MUTED, size=36))
        image = ft.Image(src=str(path), fit=ft.BoxFit.COVER, height=150, width=600,
                         border_radius=ft.BorderRadius(top_left=12, top_right=12, bottom_left=0, bottom_right=0), error_content=placeholder) if path and path.is_file() else placeholder
        async def open_video(_):
            url = row.get('video_url') or ''
            if url.startswith('https://www.youtube.com/'):
                await asyncio.to_thread(webbrowser.open, url)
        return ft.Container(bgcolor='white', border_radius=12, clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            content=ft.Column([image, ft.Container(expand=True, padding=12, content=ft.Column([
                ft.Text(row.get('channel') or '', size=11, color=ORANGE, max_lines=1),
                ft.Text(row.get('title') or 'Không có tiêu đề', size=13, color=TEXT,
                        weight=ft.FontWeight.W_600, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
                        tooltip=row.get('title'), height=38),
                ft.Row([ft.Icon(ft.Icons.VISIBILITY_OUTLINED, size=16, color=ORANGE),
                        ft.Text(number(row.get('view_count')) + ' lượt xem', size=12, color=TEXT)]),
                ft.Text(posted(row), size=11, color=MUTED),
                ft.OutlinedButton('Mở YouTube', icon=ft.Icons.OPEN_IN_NEW, on_click=open_video),
            ], spacing=7))], spacing=0))

    def toggle_logs(self, _):
        self.log_box.visible = not self.log_box.visible
        self.page.update()

    async def start(self, _):
        if self.job:
            return
        try:
            limit = int(self.limit.value or '0')
            job = CrawlJob(self.channel.value or '', limit)
        except ValueError as exc:
            self.message(str(exc) if not str(exc).startswith('invalid literal') else 'Giới hạn phải là số nguyên, 0 = tất cả.', True)
            return
        self.job = job
        self.start_button.disabled = self.channel.disabled = self.limit.disabled = True
        self.stop_button.disabled = False
        self.progress.value = None
        self.logs = []
        self.log_text.value = ''
        self.message('Đang kết nối YouTube…')
        threading.Thread(target=job.run, daemon=True).start()
        while self.job is job:
            await asyncio.sleep(0.15)
            try:
                while True:
                    kind, payload = job.events.get_nowait()
                    if kind == 'log':
                        self.logs.append(payload)
                        self.logs = self.logs[-120:]
                    elif kind == 'progress':
                        current, total = payload
                        self.progress.value = (current - 1) / max(total, 1)
                        self.status.value = f'Đang xử lý video {current}/{total}…'
                    elif kind in ('done', 'error'):
                        self.job = None
                        self.start_button.disabled = self.channel.disabled = self.limit.disabled = False
                        self.stop_button.disabled = True
                        if kind == 'error':
                            self.progress.value = 0
                            self.message(f'Crawl gặp lỗi: {payload}', True)
                        else:
                            run, summary = payload
                            self.progress.value = 1 if summary['status'] == 'complete' else 0
                            self.refresh(preferred=run)
                            count = summary['video_count']
                            labels = {'complete': f'Hoàn tất: đã lưu {count} video và dữ liệu.',
                                      'cancelled': f'Đã dừng. Giữ lại {count} video đã lấy được.',
                                      'partial': f'Đã lưu {count} video; có dữ liệu thiếu hoặc lỗi. Xem nhật ký.',
                                      'failed': 'Không lấy được video. Kiểm tra tên kênh và xem nhật ký.'}
                            self.message(labels[summary['status']], summary['status'] in ('partial', 'failed'))
                        break
            except queue.Empty:
                pass
            self.log_text.value = '\n'.join(self.logs)
            self.page.update()

    def stop(self, _=None):
        if self.job:
            self.job.stop()
            self.stop_button.disabled = True
            self.message('Đang dừng và lưu dữ liệu đã thu thập…')

    async def on_window(self, event):
        if event.type == ft.WindowEventType.CLOSE:
            self.stop()
            while self.job:
                await asyncio.sleep(0.1)
            await self.page.window.destroy()


def main(page: ft.Page):
    ResearchApp(page)


if __name__ == '__main__':
    ft.run(main)
