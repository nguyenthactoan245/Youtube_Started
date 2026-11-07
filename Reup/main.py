import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import time

import flet as ft

from merger import ROOT, merge, natural_key, output_path, probe, scan
from subtitles import check_available, export_srt

SETTINGS = ROOT / 'settings.json'
logging.basicConfig(filename=ROOT / 'merge.log', level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')

def duration_text(seconds):
    seconds = round(seconds)
    return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'

class VideoMergerApp:
    def __init__(self, page):
        self.page = page
        self.videos = []
        self.bad_files = []
        self.last_output = None
        self.task = None
        self.busy = False
        self.loaded_folder = None
        self.single_file = False
        self.last_update = 0
        try:
            settings = json.loads(SETTINGS.read_text(encoding='utf-8'))
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}
        page.title = 'Video Merger'
        page.theme_mode = ft.ThemeMode.LIGHT
        page.theme = ft.Theme(color_scheme_seed='#2563EB')
        page.bgcolor = '#F4F7FC'
        page.padding = 24
        page.window.width = 1080
        page.window.height = 800
        page.window.min_width = 850
        page.window.min_height = 650
        page.window.maximized = True
        self.picker = ft.FilePicker()
        page.services.append(self.picker)
        self.input = ft.TextField(label='File hoặc thư mục đầu vào', value=settings.get('input', ''),
                                  expand=True, on_submit=self.refresh, on_change=self.input_changed)
        self.output = ft.TextField(label='Thư mục đầu ra', value=settings.get('output', ''), expand=True)
        self.filename = ft.TextField(label='Tên file xuất', value=settings.get('filename', 'merged_video.mp4'), expand=True)
        self.sort = ft.Dropdown(label='Sắp xếp theo tên', width=220,
            value=settings.get('sort', 'asc'), options=[ft.DropdownOption('asc', 'Tên tăng dần'),
            ft.DropdownOption('desc', 'Tên giảm dần')], on_select=self.sort_changed)
        self.mode = ft.Dropdown(label='Chế độ ghép', width=220,
            value=settings.get('mode', 'auto'), options=[ft.DropdownOption('auto', 'Tự động'),
            ft.DropdownOption('fast', 'Ghép nhanh'), ft.DropdownOption('normalize', 'Chuẩn hóa')])
        device = settings.get('device', 'gpu')
        self.device = ft.Dropdown(label='Thiết bị xử lý', width=220,
            value=device if device in {'cpu', 'gpu'} else 'cpu', options=[
                ft.DropdownOption('cpu', 'CPU'), ft.DropdownOption('gpu', 'GPU (NVIDIA)')])
        self.input_button = ft.Button('Chọn folder', icon=ft.Icons.FOLDER_OPEN, on_click=self.browse_input)
        self.file_button = ft.Button('Chọn file', icon=ft.Icons.VIDEO_FILE, on_click=self.browse_file)
        self.merge_enabled = ft.Checkbox(label='Merge', value=settings.get('merge_enabled', True),
                                         on_change=self.audio_changed)
        self.mute_audio = ft.Checkbox(label='Tắt toàn bộ âm thanh',
            value=settings.get('mute_audio') is True, on_change=self.audio_changed)
        self.remove_music = ft.Checkbox(label='Tắt nhạc nền (giữ lời nói)',
            value=settings.get('remove_music') is True, on_change=self.audio_changed)
        self.music_ranges = ft.TextField(label='Các đoạn tắt nhạc nền (mỗi dòng: bắt đầu - kết thúc)',
            hint_text='00:01:20 - 00:01:28', multiline=True, min_lines=2, max_lines=4,
            value=settings.get('music_ranges', ''), on_change=self.audio_changed, visible=False)
        self.export_subtitles = ft.Checkbox(label='Xuất SRT',
            value=settings.get('export_srt') is True, on_change=self.audio_changed)
        self.subtitle_language = ft.Dropdown(label='Ngôn ngữ lời nói', width=220,
            value=settings.get('subtitle_language', 'auto'), options=[
                ft.DropdownOption('auto', 'Tự nhận dạng'), ft.DropdownOption('vi', 'Tiếng Việt'),
                ft.DropdownOption('en', 'Tiếng Anh'), ft.DropdownOption('zh', 'Tiếng Trung'),
                ft.DropdownOption('ja', 'Tiếng Nhật'), ft.DropdownOption('ko', 'Tiếng Hàn'),
                ft.DropdownOption('th', 'Tiếng Thái')], on_select=self.audio_changed)
        self.subtitle_model = ft.Dropdown(label='Mô hình nhận dạng', width=220,
            value=settings.get('subtitle_model', 'base'), options=[
                ft.DropdownOption('base', 'Base — nhanh'),
                ft.DropdownOption('small', 'Small — chính xác hơn, chậm hơn')], on_select=self.audio_changed)
        self.output_button = ft.Button('Browse', icon=ft.Icons.FOLDER_OPEN, on_click=self.browse_output)
        self.refresh_button = ft.Button('Làm mới', icon=ft.Icons.REFRESH, on_click=self.refresh)
        self.recursive = ft.Checkbox(label='Quét thư mục con', value=settings.get('recursive', True),
                                     on_change=self.refresh)
        self.merge_button = ft.Button('Run', icon=ft.Icons.PLAY_ARROW, on_click=self.start_merge,
                                       bgcolor='#2563EB', color='white', height=48, disabled=True)
        self.cancel_button = ft.Button('Hủy', on_click=self.cancel, disabled=True)
        self.open_button = ft.Button('Mở thư mục đầu ra', icon=ft.Icons.FOLDER_OPEN,
                                      on_click=self.open_output, disabled=True)
        self.summary = ft.Text('Chọn thư mục đầu vào để bắt đầu.', weight=ft.FontWeight.W_500)
        self.status = ft.Text('Sẵn sàng', selectable=True)
        self.progress = ft.ProgressBar(value=0, color='#2563EB', bgcolor='#DBEAFE')
        self.percent = ft.Text('0%')
        self.table = ft.ListView(expand=True, spacing=4)
        self.settings_controls = [self.input, self.output, self.filename, self.sort, self.mode, self.device, self.mute_audio,
                                  self.recursive,
                                  self.remove_music, self.music_ranges,
                                  self.merge_enabled, self.file_button,
                                  self.export_subtitles, self.subtitle_language, self.subtitle_model,
                                  self.input_button, self.output_button, self.refresh_button]
        page.add(ft.Column([
            ft.Text('VIDEO MERGER', size=28, weight=ft.FontWeight.BOLD, color='#172554'),
            ft.Text('Chọn file hoặc folder để xuất SRT, xử lý âm thanh; Merge dành cho folder.', color='#64748B'),
            ft.Row([self.input, self.file_button, self.input_button]), ft.Row([self.output, self.output_button]),
            ft.Row([self.filename, self.mode, self.device]),
            ft.Row([self.export_subtitles, self.merge_enabled, self.mute_audio, self.remove_music],
                   wrap=False, spacing=20, scroll=ft.ScrollMode.AUTO),
            self.music_ranges,
            ft.Row([self.subtitle_language, self.subtitle_model]),
            ft.Row([self.sort, self.recursive, self.refresh_button, self.summary]),
            ft.Container(content=ft.Column([
                ft.Row([ft.Text('STT', width=40), ft.Text('Tên video', expand=True),
                        ft.Text('Thời lượng', width=90), ft.Text('Kích thước', width=120),
                        ft.Text('Âm thanh', width=80)]), ft.Divider(), self.table], expand=True),
                padding=16, bgcolor='white', border_radius=12, expand=True),
            ft.Row([self.merge_button, self.cancel_button, self.open_button]),
            ft.Row([ft.Container(self.progress, expand=True), self.percent]),
            ft.Container(self.status, height=60),
        ], expand=True, spacing=14))
        page.on_disconnect = self.disconnect
        self.update_options()

    def save_settings(self):
        SETTINGS.write_text(json.dumps({'input': self.input.value, 'output': self.output.value,
            'filename': self.filename.value, 'sort': self.sort.value, 'mode': self.mode.value,
            'device': self.device.value,
            'mute_audio': bool(self.mute_audio.value), 'export_srt': bool(self.export_subtitles.value),
            'merge_enabled': bool(self.merge_enabled.value),
            'recursive': bool(self.recursive.value),
            'remove_music': bool(self.remove_music.value),
            'music_ranges': self.music_ranges.value,
            'subtitle_language': self.subtitle_language.value, 'subtitle_model': self.subtitle_model.value},
            ensure_ascii=False, indent=2), encoding='utf-8')

    def set_busy(self, busy, cancellable=False):
        self.busy = busy
        for control in self.settings_controls:
            control.disabled = busy
        self.update_options()
        self.cancel_button.disabled = not cancellable
        self.page.update()

    def update_options(self):
        self.merge_enabled.disabled = self.busy or self.single_file
        merging = bool(self.merge_enabled.value) and not self.single_file
        # Normalize old settings that allowed both options at once.
        if self.mute_audio.value and self.remove_music.value:
            self.remove_music.value = False
        self.mute_audio.disabled = self.busy or bool(self.remove_music.value)
        self.remove_music.disabled = self.busy or bool(self.mute_audio.value)
        self.music_ranges.visible = self.single_file
        self.music_ranges.disabled = self.busy or not self.single_file or not self.remove_music.value
        self.mode.disabled = self.busy or not merging
        self.filename.disabled = self.busy
        self.filename.helper_text = ('Tên video ghép; SRT chung dùng cùng tên.' if merging else
            'Tên kết quả; nhiều video thêm _001, _002… Bỏ trống để giữ tên từng video.')
        self.sort.disabled = self.busy or self.single_file
        self.recursive.disabled = self.busy or self.single_file
        self.subtitle_language.disabled = self.busy or not self.export_subtitles.value
        self.subtitle_model.disabled = self.busy or not self.export_subtitles.value
        self.merge_button.disabled = (self.busy or not self.videos or bool(self.bad_files)
            or self.loaded_folder is None or not (merging or self.export_subtitles.value
                                                  or self.mute_audio.value or self.remove_music.value))

    async def browse_file(self, e):
        try:
            selected = await self.picker.pick_files(dialog_title='Chọn video để xử lý', allow_multiple=False)
            if selected and selected[0].path:
                self.input.value = selected[0].path
                await self.refresh()
        except Exception as exc:
            self.error(exc)
            self.page.update()

    def error(self, exc):
        logging.exception('Operation failed')
        self.status.value = f'Lỗi: {str(exc)[:650]}\nChi tiết: merge.log'
        self.status.color = '#B91C1C'

    async def browse_input(self, e):
        try:
            selected = await self.picker.get_directory_path(dialog_title='Chọn thư mục chứa video')
            if selected:
                self.input.value = selected
                await self.refresh()
        except Exception as exc:
            self.error(exc)
            self.page.update()

    async def browse_output(self, e):
        try:
            selected = await self.picker.get_directory_path(dialog_title='Chọn thư mục lưu kết quả')
            if selected:
                self.output.value = selected
                self.save_settings()
                self.page.update()
        except Exception as exc:
            self.error(exc)
            self.page.update()

    async def input_changed(self, e):
        self.loaded_folder = None
        self.merge_button.disabled = True
        self.status.value = 'Nhấn Làm mới hoặc Enter để đọc đầu vào vừa nhập.'
        self.page.update()

    def render_list(self):
        self.videos.sort(key=lambda v: (natural_key(v.path), v.path.name), reverse=self.sort.value == 'desc')
        self.table.controls = [ft.Container(ft.Row([
            ft.Text(str(i + 1), width=40), ft.Text(v.path.name, expand=True, tooltip=str(v.path)),
            ft.Text(duration_text(v.duration), width=90),
            ft.Text('×'.join(map(str, v.dimensions)), width=120),
            ft.Text('Có' if v.audio else 'Không', width=80)]), padding=8,
            bgcolor='#F8FAFC' if i % 2 == 0 else 'white') for i, v in enumerate(self.videos)]
        self.table.controls.extend(ft.Text(message, color='#B91C1C', selectable=True) for message in self.bad_files)
        self.summary.value = f'{len(self.videos)} video • {duration_text(sum(v.duration for v in self.videos))}'

    async def sort_changed(self, e):
        self.render_list()
        self.page.update()

    async def audio_changed(self, e):
        self.update_options()
        try:
            self.save_settings()
        except OSError as exc:
            self.error(exc)
        self.page.update()

    async def refresh(self, e=None):
        if self.busy:
            return
        self.set_busy(True)
        self.videos, self.bad_files = [], []
        self.loaded_folder = None
        self.status.color = '#334155'
        self.progress.value = None
        try:
            excluded = [self.last_output] if self.last_output else []
            if self.output.value and self.filename.value and self.merge_enabled.value:
                name = self.filename.value.strip()
                if not name.lower().endswith('.mp4'):
                    name += '.mp4'
                excluded.append(Path(self.output.value) / name)
            source = Path(self.input.value.strip())
            if not self.input.value.strip():
                raise ValueError('Hãy chọn file hoặc thư mục đầu vào.')
            self.single_file = source.is_file()
            if self.single_file:
                self.merge_enabled.value = False
                if not (self.export_subtitles.value or self.mute_audio.value or self.remove_music.value):
                    self.export_subtitles.value = True
                paths = [source]
            else:
                if self.output.value.strip():
                    output_folder = Path(self.output.value.strip()).resolve()
                    if output_folder != source.resolve() and output_folder.is_relative_to(source.resolve()):
                        excluded.append(output_folder)
                paths = scan(source, excluded, recursive=bool(self.recursive.value))
            for i, path in enumerate(paths):
                self.status.value = f'Đang đọc {i + 1}/{len(paths)}: {path.name}'
                self.page.update()
                try:
                    self.videos.append(await probe(path))
                except Exception as exc:
                    self.bad_files.append(str(exc))
            self.loaded_folder = Path(self.input.value.strip()).resolve()
            self.save_settings()
            self.status.value = ('Có file lỗi. Di chuyển hoặc sửa các file màu đỏ rồi nhấn Làm mới.'
                if self.bad_files else 'Chọn tác vụ rồi nhấn Run. Chỉ Xuất SRT: mỗi video một file SRT.' if paths else 'Không tìm thấy video trong thư mục.')
        except Exception as exc:
            self.error(exc)
        finally:
            self.progress.value = 0
            self.percent.value = '0%'
            self.render_list()
            self.set_busy(False)

    def report_progress(self, value, message):
        if time.monotonic() - self.last_update < .15 and value < 1:
            return
        self.last_update = time.monotonic()
        self.progress.value = value
        self.percent.value = f'{value:.0%}'
        self.status.value = message
        self.page.update()

    async def start_merge(self, e):
        if self.busy or self.loaded_folder is None or not self.videos or self.bad_files:
            return
        merging = bool(self.merge_enabled.value) and not self.single_file
        exporting = bool(self.export_subtitles.value)
        processing_audio = bool(self.mute_audio.value or self.remove_music.value)
        if not (merging or exporting or processing_audio):
            return
        self.task = asyncio.current_task()
        self.set_busy(True, cancellable=True)
        self.status.color = '#334155'
        self.progress.value = 0
        saved_video = None
        completed = []
        skipped = []
        try:
            folder = Path(self.output.value.strip())
            if not self.output.value.strip() or not folder.is_dir():
                raise ValueError('Hãy chọn thư mục đầu ra tồn tại.')
            destination = output_path(folder, self.filename.value) if merging else None
            subtitle_name = self.filename.value.strip()
            if not merging and subtitle_name:
                if subtitle_name.lower().endswith(('.mp4', '.srt')):
                    subtitle_name = subtitle_name[:-4]
                output_path(folder, subtitle_name, extension='.srt')
            self.save_settings()
            # Re-probe the displayed snapshot, preserving its order.
            fresh = []
            for v in self.videos:
                self.status.value = f'Kiểm tra: {v.path.name}'
                self.page.update()
                fresh.append(await probe(v.path))
            if exporting:
                await asyncio.to_thread(check_available)
                if not any(v.audio for v in fresh):
                    raise ValueError('Không có âm thanh nguồn để tạo SRT. Hãy bỏ chọn Xuất SRT.')
            if self.remove_music.value and not self.mute_audio.value and any(v.audio for v in fresh):
                from music_removal import find_python, parse_music_ranges
                await asyncio.to_thread(find_python, self.device.value)
                music_ranges = (parse_music_ranges(self.music_ranges.value, fresh[0].duration)
                                if self.single_file else None)
            else:
                music_ranges = None
            if merging:
                self.last_output = await merge(fresh, destination, self.mode.value, self.report_progress,
                                               mute_audio=bool(self.mute_audio.value),
                                               remove_music=bool(self.remove_music.value) and not self.mute_audio.value,
                                               device=self.device.value, music_ranges=None)
                saved_video = self.last_output
                completed.append(saved_video)
                self.open_button.disabled = False
            individual_outputs = {}
            if not merging and processing_audio:
                for i, video in enumerate(fresh, 1):
                    name = (subtitle_name + (f'_{i:03}' if len(fresh) > 1 else '')
                            if subtitle_name else video.path.stem)
                    target = output_path(folder, name)
                    result = await merge([video], target, 'auto', self.report_progress,
                        mute_audio=bool(self.mute_audio.value),
                        remove_music=bool(self.remove_music.value) and not self.mute_audio.value,
                        device=self.device.value, music_ranges=music_ranges)
                    individual_outputs[video.path] = result
                    completed.append(result)
                    self.last_output = result
                    self.open_button.disabled = False
            if exporting:
                jobs = [(fresh, saved_video.with_suffix('.srt'))] if merging else []
                if not merging:
                    for i, v in enumerate(fresh, 1):
                        if not v.audio:
                            continue
                        name = (subtitle_name + (f'_{i:03}' if len(fresh) > 1 else '')
                                if subtitle_name else v.path.stem)
                        srt_path = (individual_outputs[v.path].with_suffix('.srt') if v.path in individual_outputs
                                    else output_path(folder, name, extension='.srt'))
                        jobs.append(([v], srt_path))
                if not merging:
                    skipped = [v.path.name for v in fresh if not v.audio]
                for index, (videos, srt_path) in enumerate(jobs):
                    saved_srt = await export_srt(videos, srt_path,
                        self.subtitle_language.value, self.subtitle_model.value,
                        lambda p, message: self.report_progress((index + p) / len(jobs), message),
                        device=self.device.value)
                    completed.append(saved_srt)
                    self.last_output = saved_video or saved_srt
                    self.open_button.disabled = False
            self.status.value = f'Đã lưu {len(completed)} file tại: {folder}'
            if skipped:
                self.status.value += f'\nBỏ qua {len(skipped)} video không có âm thanh.'
            self.status.color = '#15803D'
            self.open_button.disabled = False
        except asyncio.CancelledError:
            self.status.value = f'Đã hủy. Giữ lại {len(completed)} file đã xuất. Video nguồn được giữ nguyên.'
            self.progress.value = 0
            self.percent.value = '0%'
        except Exception as exc:
            self.error(exc)
            if completed:
                self.status.value = f'Đã lưu {len(completed)} file tại {folder}.\nLỗi: {str(exc)[:250]}'
        finally:
            self.task = None
            self.set_busy(False)

    async def cancel(self, e):
        if self.task:
            self.cancel_button.disabled = True
            self.status.value = 'Đang hủy...'
            self.page.update()
            self.task.cancel()

    async def disconnect(self, e):
        if self.task:
            self.task.cancel()

    async def open_output(self, e):
        if self.last_output:
            try:
                os.startfile(self.last_output.parent)
            except OSError as exc:
                self.error(exc)
                self.page.update()

async def main(page: ft.Page):
    app = VideoMergerApp(page)
    if app.input.value:
        await app.refresh()

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--demucs-worker':
        from music_removal import demucs_worker
        demucs_worker(sys.argv[2:])
    else:
        ft.run(main)
