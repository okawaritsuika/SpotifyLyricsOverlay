"""Optional release downloads; worker threads never call Tk."""
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

import app_updates
from app_paths import frozen
from app_version import VERSION
from data_updates import RECEIPTS, stage_package
from storage import atomic_json, read_json


class UpdateController:
    def __init__(self, app):
        self.app = app
        self.directory = Path(app.store.directory)
        self.receipt = self.directory / 'update-check.json'
        self.messages = queue.Queue()
        self.release = None
        self.status = '최신 배포를 확인하면 프로그램과 자료를 따로 받을 수 있습니다.'
        self.busy = False
        self.closed = False
        self.checked = False
        self.progress = (0, 0)
        self.program_path = None
        self.program_release = None
        self.data_staged = False
        self.after = app.root.after(100, self.poll)
        self.auto_after = None

    def schedule_automatic(self):
        if self.app.services_started and not self.closed and self.auto_after is None:
            self.auto_after = self.app.root.after(5000, self.automatic_check)

    def automatic_check(self):
        self.auto_after = None
        if self.closed or self.checked:
            return
        if self.busy:
            self.auto_after = self.app.root.after(1000, self.automatic_check)
        else:
            self.check(quiet=True)

    def notify_discoveries(self, release):
        if not release:
            return
        try:
            receipt = read_json(self.receipt, {})
        except (OSError, ValueError):
            receipt = {}
        if not isinstance(receipt, dict):
            receipt = {}
        programs = receipt.get('program_notified', [])
        data_versions = receipt.get('data_notified', [])
        programs = programs if isinstance(programs, list) else []
        data_versions = data_versions if isinstance(data_versions, list) else []
        program = (release.version if app_updates.is_newer(release.version)
                   and app_updates.EXE_ASSET_NAME in release.assets else None)
        asset = release.assets.get(app_updates.DATA_ASSET_NAME)
        data_key = None
        if isinstance(asset, dict):
            digest = asset.get('digest')
            if isinstance(digest, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
                data_key = digest.lower()
                try:
                    applied = read_json(self.directory / RECEIPTS, {})
                    if (isinstance(applied, dict) and isinstance(applied.get('applied'), dict)
                            and data_key[7:] in applied['applied']):
                        data_key = None
                except (OSError, ValueError):
                    pass
            else:
                asset_id = asset.get('id')
                data_key = 'asset:' + release.tag + ':' + (str(asset_id) if type(asset_id) is int else app_updates.DATA_ASSET_NAME)
        fresh_program = program and program not in programs
        fresh_data = data_key and data_key not in data_versions
        if not fresh_program and not fresh_data:
            return
        lines = []
        if fresh_program:
            lines.append('새 프로그램 버전 ' + program + '을 받을 수 있습니다.')
        if fresh_data:
            lines.append('새 사전·가사 자료를 받을 수 있습니다.')
        self.show_notification('\n'.join(lines))
        if fresh_program:
            programs.append(program)
        if fresh_data:
            data_versions.append(data_key)
        receipt['program_notified'] = programs
        receipt['data_notified'] = data_versions
        try:
            atomic_json(self.receipt, receipt)
        except OSError:
            pass

    def show_notification(self, text):
        window = tk.Toplevel(self.app.root)
        window.title('업데이트 알림')
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=text + '\n업데이트 화면에서 원하는 항목을 선택할 수 있습니다.',
                  justify='left', wraplength=400).pack(anchor='w')
        buttons = ttk.Frame(frame)
        buttons.pack(anchor='e', pady=(14, 0))
        def open_updates():
            window.destroy()
            self.app.open_updates()
        ttk.Button(buttons, text='업데이트 확인', command=open_updates).pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='닫기', command=window.destroy).pack(side='left')
        window.update_idletasks()
        x = max(0, (window.winfo_screenwidth() - window.winfo_reqwidth()) // 2)
        y = max(0, (window.winfo_screenheight() - window.winfo_reqheight()) // 2)
        window.geometry(f'+{x}+{y}')
        window.attributes('-topmost', True)
        window.after(1000, lambda: window.attributes('-topmost', False) if window.winfo_exists() else None)

    def refresh(self):
        dialog = getattr(self.app, 'update_dialog', None)
        if dialog:
            dialog.refresh()

    def start(self, operation, worker, quiet=False):
        if self.busy or self.closed:
            return False
        self.busy = True
        self.progress = (0, 0)
        self.status = operation
        self.refresh()

        def run():
            try:
                result = worker()
            except Exception as error:
                self.messages.put(('error', str(error), quiet))
            else:
                self.messages.put(('done', result, quiet))

        threading.Thread(target=run, name='release-update', daemon=True).start()
        return True

    def check(self, quiet=False):
        def worker():
            return 'checked', app_updates.check_release()

        return self.start('GitHub의 최신 배포를 확인하는 중…', worker, quiet)

    def download(self, kind):
        release = self.release
        if not release or kind not in ('program', 'data'):
            return False
        if kind == 'program' and (not frozen() or not app_updates.is_newer(release.version)):
            return False
        name = app_updates.EXE_ASSET_NAME if kind == 'program' else app_updates.DATA_ASSET_NAME
        if name not in release.assets:
            self.status = '이 배포에는 선택한 다운로드 파일이 없습니다.'
            self.refresh()
            return False

        def worker():
            last = 0

            def progress(received, total):
                nonlocal last
                now = time.monotonic()
                if received == total or now - last >= .1:
                    self.messages.put(('progress', (received, total), False))
                    last = now

            path = app_updates.download_asset(release, name,
                self.directory / 'updates' / 'downloads' / name, progress=progress)
            if kind == 'data':
                return 'data', stage_package(path, self.directory)
            return 'program', (path, release)

        return self.start(('프로그램' if kind == 'program' else '사전·가사 자료') + ' 다운로드·검증 중…', worker)

    def restart(self):
        if self.busy or self.closed or not (self.program_path or self.data_staged):
            return False
        parent = self.app.update_dialog.window if self.app.update_dialog else self.app.root
        if not messagebox.askyesno('업데이트 적용',
                '준비한 업데이트를 적용하고 앱을 다시 시작할까요?\n'
                '기존 싱크와 직접 입력한 가사·번역, 설정은 보존됩니다.', parent=parent):
            return False

        def worker():
            if self.program_path:
                app_updates.prepare_executable_update(self.program_path, self.program_release, self.directory)
            else:
                app_updates.restart_app(self.directory)
            return 'restart', None

        return self.start('업데이트 적용과 재시작을 준비하는 중…', worker)

    def poll(self):
        self.after = None
        if self.closed:
            return
        while True:
            try:event, value, quiet = self.messages.get_nowait()
            except queue.Empty:break
            if event == 'progress':
                self.progress = value
                continue
            self.busy = False
            if event == 'error':
                self.status = '업데이트를 완료하지 못했습니다: ' + value
                if not quiet:
                    dialog = getattr(self.app, 'update_dialog', None)
                    if dialog:
                        messagebox.showerror('업데이트 실패', value, parent=dialog.window)
                    else:self.app.notify(self.status, 12)
                continue
            kind, result = value
            if kind == 'checked':
                self.checked = True
                self.release = result
                if not result:
                    self.status = '아직 공개된 배포가 없습니다.'
                elif app_updates.is_newer(result.version) and app_updates.EXE_ASSET_NAME in result.assets:
                    self.status = '새 프로그램 버전이 있습니다. 필요한 항목을 선택해 다운로드하세요.'
                else:
                    self.status = '현재 프로그램이 최신입니다. 사전·가사 자료는 별도로 받을 수 있습니다.'
                self.notify_discoveries(result)
            elif kind == 'program':
                self.program_path, self.program_release = result
                self.status = '프로그램 검증 완료. 아래 재시작 버튼을 누르면 적용합니다.'
            elif kind == 'data':
                self.data_staged = result['status'] == 'staged'
                self.status = ('자료 검증 완료. 아래 재시작 버튼을 누르면 새 자료를 적용합니다.'
                               if self.data_staged else '이 자료는 이미 적용되어 있습니다.')
            elif kind == 'restart':
                self.app.close()
                return
        self.refresh()
        if not self.closed:
            self.after = self.app.root.after(100, self.poll)

    def close(self):
        self.closed = True
        for pending in (self.after, self.auto_after):
            if pending:
                try:self.app.root.after_cancel(pending)
                except tk.TclError:pass
        self.after = self.auto_after = None


class UpdateDialog:
    def __init__(self, app):
        self.app = app
        self.controller = app.update_controller
        self.window = window = tk.Toplevel(app.root)
        window.title('프로그램·자료 업데이트')
        window.geometry('680x550')
        window.minsize(600, 490)
        window.protocol('WM_DELETE_WINDOW', self.close)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)
        self.version = ttk.Label(frame, text='현재 버전 ' + VERSION)
        self.version.grid(row=0, column=0, sticky='w')
        self.check_button = ttk.Button(frame, text='최신 배포 확인', command=self.controller.check)
        self.check_button.grid(row=0, column=1, padx=(10, 0))
        ttk.Label(frame, text='공개 사전 자료를 갱신하고 비어 있는 가사·번역·싱크를 보완합니다.\n'
                  '직접 추가한 곡, 개인 연결, 기존 가사·싱크와 설정은 보존합니다.',
                  wraplength=625, justify='left').grid(row=1, column=0, columnspan=2, sticky='w', pady=(14, 12))
        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, columnspan=2, sticky='ew')
        self.program_button = ttk.Button(actions, text='프로그램 다운로드',
                                        command=lambda:self.controller.download('program'))
        self.program_button.pack(side='left')
        self.data_button = ttk.Button(actions, text='사전·가사 자료 다운로드',
                                     command=lambda:self.controller.download('data'))
        self.data_button.pack(side='left', padx=8)
        self.link_button = ttk.Button(actions, text='배포 페이지', command=self.open_release)
        self.link_button.pack(side='right')
        ttk.Label(frame, text='릴리스 안내').grid(row=3, column=0, columnspan=2, sticky='w', pady=(16, 5))
        self.notes = tk.Text(frame, height=10, wrap='word', state='disabled', font=('Malgun Gothic', 10))
        self.notes.grid(row=4, column=0, columnspan=2, sticky='nsew')
        self.status = ttk.Label(frame, wraplength=625, justify='left')
        self.status.grid(row=5, column=0, columnspan=2, sticky='w', pady=(12, 5))
        self.progress = ttk.Progressbar(frame, mode='determinate', maximum=100)
        self.progress.grid(row=6, column=0, columnspan=2, sticky='ew')
        footer = ttk.Frame(frame)
        footer.grid(row=7, column=0, columnspan=2, sticky='ew', pady=(12, 0))
        self.restart_button = ttk.Button(footer, text='준비한 업데이트 적용 · 재시작', command=self.controller.restart)
        self.restart_button.pack(side='left')
        ttk.Button(footer, text='닫기', command=self.close).pack(side='right')
        self.notes_text = None
        self.refresh()
        if not self.controller.checked and not self.controller.busy:
            self.controller.check()

    def refresh(self):
        controller = self.controller
        release = controller.release
        self.version.configure(text='현재 버전 ' + VERSION + (' · 최신 배포 ' + release.version if release else '')
                               + (' · 소스 실행' if not frozen() else ''))
        self.status.configure(text=controller.status)
        received, total = controller.progress
        self.progress.configure(value=(100 * received / total if total else 0))
        self.check_button.configure(state='disabled' if controller.busy else 'normal')
        available = bool(release and not controller.busy)
        program = bool(available and frozen() and app_updates.is_newer(release.version)
                       and app_updates.EXE_ASSET_NAME in release.assets)
        data = bool(available and app_updates.DATA_ASSET_NAME in release.assets)
        self.program_button.configure(state='normal' if program else 'disabled')
        self.data_button.configure(state='normal' if data else 'disabled')
        self.link_button.configure(state='normal' if release else 'disabled')
        self.restart_button.configure(state='normal' if not controller.busy and
                                      (controller.program_path or controller.data_staged) else 'disabled')
        text = release.body if release else '최신 배포를 확인하면 변경 사항이 표시됩니다.'
        if text != self.notes_text:
            self.notes.configure(state='normal')
            self.notes.delete('1.0', 'end')
            self.notes.insert('1.0', text or '이 배포에는 별도 안내가 없습니다.')
            self.notes.configure(state='disabled')
            self.notes_text = text

    def open_release(self):
        if self.controller.release:
            webbrowser.open(self.controller.release.html_url)

    def close(self):
        self.app.update_dialog = None
        self.window.destroy()
