"""Spotify playlist controls inside the shared audio analysis window."""
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk

from app_paths import resource_dir
from missing_audio import CollectionPlaylist, MissingAudioCollector
from spotify_catalog import parse_playlist_id
from storage import atomic_json, read_json


class PlaylistAnalysisPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        if app.missing_audio_collector is None:
            app.missing_audio_collector = MissingAudioCollector(app, resource_dir())
        self.collector = app.missing_audio_collector
        self.items = []
        self.track_ids = set()
        self.loading = False
        self.pending = None
        self.selected = '기본 재생목록'

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        source = ttk.Frame(self)
        source.grid(row=0, column=0, columnspan=2, sticky='ew', padx=8, pady=(8, 0))
        source.columnconfigure(1, weight=1)
        self.playlist_link = tk.StringVar(master=self)
        ttk.Label(source, text='Spotify 재생목록 링크').grid(row=0, column=0, padx=(0, 8))
        self.link_input = ttk.Entry(source, textvariable=self.playlist_link)
        self.link_input.grid(row=0, column=1, sticky='ew')
        self.load_button = ttk.Button(source, text='불러오기', command=self.load_playlist)
        self.load_button.grid(row=0, column=2, padx=6)
        self.reset_button = ttk.Button(source, text='기본 목록', command=lambda: self.load_inventory(None, save=True))
        self.reset_button.grid(row=0, column=3)
        self.link_input.bind('<Return>', lambda _: self.load_playlist())

        self.label = ttk.Label(self, wraplength=485, justify='left')
        self.label.grid(row=1, column=0, columnspan=2, sticky='ew', padx=8, pady=6)
        self.table = ttk.Treeview(self, columns=('track', 'state'), show='headings', height=4)
        self.table.heading('track', text='곡')
        self.table.heading('state', text='상태')
        self.table.column('track', width=340, minwidth=140)
        self.table.column('state', width=150, minwidth=100, stretch=False)
        self.table.grid(row=2, column=0, sticky='nsew', padx=(8, 0))
        scroll = ttk.Scrollbar(self, command=self.table.yview)
        scroll.grid(row=2, column=1, sticky='ns', padx=(0, 8))
        self.table.configure(yscrollcommand=scroll.set)

        controls = ttk.Frame(self)
        controls.grid(row=3, column=0, columnspan=2, sticky='ew', padx=8, pady=6)
        self.start_button = ttk.Button(controls, text='재생목록 분석 시작', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(controls, text='분석 중지', command=self.collector.stop)
        self.stop_button.pack(side='left', padx=6)
        self.restore_button = ttk.Button(controls, text='소리 복원', command=self.restore)
        self.restore_button.pack(side='right')
        self.status = ttk.Label(self, wraplength=485, justify='left')
        self.status.grid(row=4, column=0, columnspan=2, sticky='ew', padx=8, pady=(0, 8))
        self.bind('<Configure>', self.resize)

        try:
            manifest = read_json(self.collector.selection_path, {})
            if not isinstance(manifest, dict) or (manifest and (
                    parse_playlist_id(manifest.get('playlist_id')) != manifest.get('playlist_id')
                    or not isinstance(manifest.get('entries'), list))):
                raise ValueError('저장한 재생목록을 읽을 수 없습니다. 기본 목록을 눌러 주세요.')
        except (OSError, ValueError) as error:
            self.fail(error)
        else:
            self.load_inventory(manifest or None)

    def resize(self, event):
        for label in (self.label, self.status):
            label.configure(wraplength=max(280, event.width - 18))

    def start(self):
        if self.loading:
            return
        app, collector = self.app, self.collector
        dialog = app.manual_sync_dialog
        try:
            if ((dialog and dialog.skip_request) or app.pending_track_change or app.pending_seek or app.pending_playback):
                raise ValueError('Spotify 재생 위치 이동이 끝난 뒤 시작하세요.')
            done = collector.collected | collector.review_needed
            collector.start([item for item in self.items if item['track']['id'] not in done])
            if collector.active and dialog:
                dialog.pause_for_playlist_analysis()
        except (OSError, ValueError) as error:
            collector.status = str(error)
        self.refresh()

    def restore(self):
        try:
            self.collector.restore()
        except (OSError, ValueError) as error:
            self.collector.status = str(error)
        self.refresh()

    def refresh(self):
        collector = self.collector
        self.status.configure(text=collector.summary() if collector.total or collector.status else '재생목록 분석 대기')
        can_select = not (self.loading or collector.owns_playback or collector.jobs)
        selectable = 'normal' if can_select else 'disabled'
        for control in (self.link_input, self.load_button, self.reset_button):
            control.configure(state=selectable)
        remaining = self.track_ids - collector.collected - collector.review_needed
        self.start_button.configure(state=selectable if remaining and not self.app.audio_job else 'disabled')
        self.stop_button.configure(state='normal' if collector.active else 'disabled')
        self.restore_button.configure(state='normal' if collector.quiet.path.exists() and not collector.owns_playback else 'disabled')
        for track_id in self.track_ids & (collector.collected | collector.review_needed | collector.failures.keys()):
            if track_id in collector.collected:
                state = '분석 완료'
            elif track_id in collector.review_needed:
                state = '싱크 검토 필요'
            else:
                state = collector.failures[track_id]
            if self.table.set(track_id, 'state') != state:
                self.table.set(track_id, 'state', state)

    def fail(self, error):
        self.loading = False
        self.label.configure(text=f'{self.selected} · {error}')
        self.refresh()

    def load_inventory(self, manifest, save=False):
        self.loading = True
        self.label.configure(text='분석할 곡 확인 중…')
        self.refresh()
        executor = ThreadPoolExecutor(max_workers=1)
        inventory = executor.submit(self.collector.inventory, manifest)
        executor.shutdown(wait=False)

        def populate():
            self.pending = None
            if not inventory.done():
                self.pending = self.after(120, populate)
                return
            try:
                items = inventory.result()
                if save:
                    atomic_json(self.collector.selection_path, manifest or {})
            except (OSError, ValueError) as error:
                self.fail(error)
                return
            playlist_id = manifest['playlist_id'] if manifest is not None else ''
            self.selected = '선택한 재생목록' if playlist_id else '기본 재생목록'
            self.playlist_link.set('https://open.spotify.com/playlist/' + playlist_id if playlist_id else '')
            self.items = items
            self.loading = False
            self.table.delete(*self.table.get_children())
            self.track_ids = {item['track']['id'] for item in items}
            for item in items:
                track = item['track']
                self.table.insert('', 'end', iid=track['id'],
                                  values=(f"{item['position']:04d}  {track['title']} · {track['artists'][0]}", item['reason']))
            self.label.configure(text=f'{self.selected} · 분석 대상 {len(items)}곡')
            self.refresh()

        self.pending = self.after(120, populate)

    def load_playlist(self):
        app, collector = self.app, self.collector
        if self.loading or collector.owns_playback or collector.jobs:
            return
        playlist_id = parse_playlist_id(self.playlist_link.get())
        if not playlist_id:
            self.fail('Spotify 재생목록 공유 링크를 입력해 주세요.')
            self.link_input.focus_set()
            return
        if app.playlist_lyrics.owns_spotify or app.catalog_sync.active:
            self.fail('다른 Spotify 조회가 끝난 뒤 다시 불러와 주세요.')
            return
        try:
            request = collector.playlist_request = CollectionPlaylist(collector, playlist_id)
        except (OSError, ValueError) as error:
            self.fail(error)
            return
        self.loading = True
        self.refresh()

        def poll():
            self.pending = None
            try:
                manifest = request.poll()
            except (OSError, ValueError) as error:
                collector.cancel_playlist()
                self.fail(error)
                return
            if manifest is not None:
                collector.playlist_request = None
                self.load_inventory(manifest, save=True)
                return
            self.label.configure(text=request.progress())
            self.pending = self.after(120, poll)

        poll()

    def close(self):
        if self.pending:
            self.after_cancel(self.pending)
            self.pending = None
        self.collector.cancel_playlist()
        if self.collector.active:
            self.collector.stop()
