"""Find recordings without Spotify timing and record lyric onsets."""
import time
import tkinter as tk
from tkinter import ttk
from tkinter.simpledialog import askstring

from alsong_dialog import AlsongDialog
from artist_names import artist_label
from audio_sync import merge_candidates
from explorer_library import SERIES, artist_key, build_performer_lookup, performers
from lyric_performers import short_performer_name
from sync import MAX_LYRIC_ADVANCE_MS, active_line, edit_manual_time, lyric_advance_ms
from timing_quality import automatic_times, cleared_rows, timing_origins
from ui_theme import ACCENT, BG, MUTED, PANEL, SELECTED, TEXT
from user_annotations import SINGER_KEYS


EDITABLE_COLUMNS = {'lyric': ('original', '가사'), 'reading': ('reading', '후리가나'),
                    'translation': ('korean', '번역')}


def clock(seconds):
    minutes, milliseconds = divmod(round(seconds * 1000), 60000)
    return f'{minutes:02}:{milliseconds / 1000:06.3f}'


class SingerRosterEditor:
    def __init__(self, dialog, people, parent=None):
        self.dialog = dialog
        app = dialog.app
        self.track = app.playback.key
        self.duration = app.playback.duration
        self.album = app.playback.album
        self.initial = app.singer_roster.names
        self.names = list(app.singer_options)
        self.people = people
        self.lookup = build_performer_lookup(people)
        self.brands = {label: key for key, label in SERIES.items()
                       if key and any(person['series'] == key for person in people)}

        window = self.window = tk.Toplevel(parent or dialog.window)
        window.title('곡 참여 가창자 편집')
        window.configure(bg=BG)
        if (parent or dialog.window).winfo_viewable():
            window.transient(parent or dialog.window)
        window.resizable(False, False)
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.bind('<Escape>', lambda _: self.close())
        body = ttk.Frame(window, padding=14)
        body.pack(fill='both', expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text='브랜드').grid(row=0, column=0, sticky='w', padx=(0, 10), pady=(0, 6))
        self.brand = ttk.Combobox(body, values=tuple(self.brands), state='readonly', width=27)
        self.brand.grid(row=0, column=1, sticky='ew', pady=(0, 6))
        self.brand.bind('<<ComboboxSelected>>', self.refresh_choices)
        ttk.Label(body, text='가창자').grid(row=1, column=0, sticky='w', padx=(0, 10))
        self.singer = ttk.Combobox(body, state='disabled', width=27)
        self.singer.grid(row=1, column=1, sticky='ew')
        self.singer.bind('<<ComboboxSelected>>', lambda _: self.update_buttons())
        self.add_button = dialog.button(body, text='추가', command=self.add)
        self.add_button.grid(row=1, column=2, padx=(8, 0))

        ttk.Label(body, text='참여 가창자 · 숫자 키 순서').grid(row=2, column=0, columnspan=3, sticky='w', pady=(12, 4))
        selected = ttk.Frame(body)
        selected.grid(row=3, column=0, columnspan=3, sticky='ew')
        self.selected = tk.Listbox(selected, height=6, selectmode='extended', exportselection=False,
                                   bg=PANEL, fg=TEXT, selectbackground='#345a62', selectforeground=TEXT,
                                   highlightthickness=0, font=('Malgun Gothic', 10))
        self.selected.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(selected, command=self.selected.yview)
        scrollbar.pack(side='right', fill='y')
        self.selected.configure(yscrollcommand=scrollbar.set)
        self.selected.bind('<<ListboxSelect>>', lambda _: self.update_buttons())
        self.selected.bind('<Delete>', lambda _: self.remove())
        self.status = ttk.Label(body, foreground=MUTED, wraplength=330)
        self.status.grid(row=4, column=0, columnspan=3, sticky='w', pady=(8, 0))

        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, columnspan=3, sticky='ew', pady=(8, 0))
        self.remove_button = dialog.button(actions, text='선택 삭제', command=self.remove)
        self.remove_button.pack(side='left')
        self.save_button = dialog.button(actions, text='저장', command=self.save)
        self.save_button.pack(side='right')
        dialog.button(actions, text='취소', command=self.close).pack(side='right', padx=(0, 6))
        self.refresh_selected()
        window.grab_set()
        self.brand.focus_set()
        if parent:
            window.lift()
            self.brand.focus_force()

    def identity(self, name):
        key = artist_key(name)
        person = self.lookup.get(key)
        return artist_key(person['name']) if person else key

    def refresh_choices(self, _event=None):
        brand = self.brands.get(self.brand.get())
        existing = {self.identity(name) for name in self.names}
        self.choices = [person for person in self.people
                        if person['series'] == brand and self.identity(person['name']) not in existing]
        self.singer.configure(values=[person['display'] for person in self.choices],
                              state='readonly' if self.choices else 'disabled')
        self.singer.set('')
        self.update_buttons()

    def refresh_selected(self):
        self.selected.delete(0, 'end')
        for key, name in zip(SINGER_KEYS, self.names):
            self.selected.insert('end', f'{key}   {artist_label((name,), self.album)}')
        self.status.configure(text=f'{len(self.names)} / {len(SINGER_KEYS)}명')
        self.refresh_choices()

    def update_buttons(self):
        count = len(self.names)
        self.add_button.configure(state='normal' if self.singer.current() >= 0 and count < len(SINGER_KEYS)
                                  else 'disabled')
        self.remove_button.configure(state='normal' if self.selected.curselection() else 'disabled')
        changed = tuple(self.names) != self.initial
        self.save_button.configure(state='normal' if changed else 'disabled')

    def add(self):
        choice = self.singer.current()
        if choice >= 0 and len(self.names) < len(SINGER_KEYS):
            name = self.choices[choice]['name']
            key = self.identity(name)
            names = self.dialog.app.singer_options
            self.names.append(next((option for option in names if self.identity(option) == key), name))
            self.refresh_selected()

    def remove(self):
        for index in reversed(self.selected.curselection()):
            del self.names[index]
        self.refresh_selected()

    def save(self):
        dialog = self.dialog
        app = dialog.app
        roster = app.singer_roster
        if (not dialog.can_edit_singers() or self.track != app.playback.key or self.track != roster.key
                or abs(self.duration - app.playback.duration) > 2):
            self.status.configure(text='재생 중인 곡이 바뀌었습니다. 편집창을 다시 열어 주세요.')
            return
        names = tuple(self.names)
        if names == self.initial:
            return
        try:
            roster.save(names)
        except (OSError, ValueError) as error:
            self.status.configure(text=f'가창자 저장 실패. {error}')
            return
        self.close()
        app.render()
        dialog.rows.focus_set()
        dialog.notice('곡 참여 가창자를 저장했습니다. 줄을 선택하고 숫자 키로 가창자를 지정하세요.')

    def close(self):
        self.dialog.singer_editor = None
        self.window.destroy()


class ManualSyncDialog:
    def __init__(self, app):
        self.app = app
        self.song = None
        self.track = None
        self.duration = 0
        self.originals = ()
        self.index = 0
        self.baseline = ([], [])
        self.baseline_checked = 0
        self.playhead = None
        self.alsong_dialog = None
        self.searching = True
        self.follow_current_song = False
        self.skipped = set()
        self.skip_request = None
        self.rewind_request = None
        self.lyric_entry = None
        self.clicked_lyric = None
        self.singer_credits = None
        self.singer_editor = None

        window = self.window = tk.Toplevel(app.root)
        window.title('개발자 모드 · 가사 및 싱크')
        window.geometry('1060x540')
        window.minsize(680, 360)
        window.configure(bg=BG)
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(2, weight=1)
        style = ttk.Style(window)
        style.configure('Developer.TButton', font=('Malgun Gothic', 9), padding=(5, 2))
        style.configure('Developer.TCheckbutton', font=('Malgun Gothic', 9), padding=(2, 0))

        self.title = ttk.Label(window, font=('Malgun Gothic', 12, 'bold'))
        self.title.grid(row=0, column=0, sticky='ew', padx=14, pady=(12, 4))
        details = ttk.Frame(window)
        details.grid(row=1, column=0, sticky='ew', padx=14, pady=(0, 8))
        details.columnconfigure(0, weight=1)
        self.position = ttk.Label(details)
        self.position.grid(row=0, column=0, sticky='ew')
        self.edit_singers_button = self.button(details, text='곡 참여 가창자 편집…', command=self.edit_singers)
        self.edit_singers_button.grid(row=0, column=1, sticky='e', padx=(8, 0))
        self.selected_singers = ttk.Label(details, foreground=ACCENT, wraplength=600, justify='left')
        self.singer_keys = ttk.Label(details, foreground=MUTED, wraplength=600, justify='left')
        table = ttk.Frame(window)
        table.grid(row=2, column=0, sticky='nsew', padx=14)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.rows = ttk.Treeview(table, columns=('line', 'time', 'lyric', 'reading', 'translation', 'singers'),
                                 show='headings', selectmode='browse')
        for name, label, width in (('line', '줄', 58), ('time', '시작 시각', 108),
                                   ('lyric', '가사', 120), ('reading', '후리가나', 105),
                                   ('translation', '한국어 번역', 130), ('singers', '가창자', 105)):
            self.rows.heading(name, text=label)
            self.rows.column(name, width=width, stretch=name not in ('line', 'time'), minwidth=width)
        self.rows.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(table, command=self.rows.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.rows.configure(yscrollcommand=lambda first, last: self.scroll_rows(scrollbar, first, last))
        self.rows.bind('<<TreeviewSelect>>', self.select_row)
        self.rows.bind('<Button-1>', self.remember_lyric_click)
        self.rows.bind('<Double-1>', self.edit_lyric)
        self.rows.bind('<Configure>', lambda _: self.center_row())
        self.rows.tag_configure('missing',foreground=MUTED)
        self.rows.tag_configure('playing',background=SELECTED,foreground=ACCENT)

        controls = ttk.Frame(window)
        controls.grid(row=3, column=0, sticky='ew', padx=14, pady=5)
        self.previous_button = self.button(controls, text='이전', command=self.previous)
        self.previous_button.pack(side='left')
        self.next_button = self.button(controls, text='선택 줄 기록', command=self.record)
        self.next_button.pack(side='left', padx=5)
        self.clear_button = self.button(controls, text='선택 줄 싱크 지우기', command=self.clear_time)
        self.clear_button.pack(side='left')
        self.work_button = self.button(controls, text='다음 작업곡', command=self.next_track)
        self.work_button.pack(side='right')
        self.work_playlist_button = self.button(controls, text='재생목록 변경…', command=self.ask_work_playlist)
        self.work_playlist_button.pack(side='right', padx=5)
        self.follow_playback = tk.BooleanVar(master=window,value=True)
        self.follow_button = ttk.Checkbutton(controls,text='재생 줄 표시',variable=self.follow_playback,
                                            command=self.update_playhead,style='Developer.TCheckbutton')
        self.follow_button.pack(side='left',padx=8)
        playback_controls = ttk.Frame(window)
        playback_controls.grid(row=4, column=0, sticky='ew', padx=14, pady=(0, 6))
        self.rewind_button = self.button(playback_controls, text='곡 처음', command=self.rewind)
        self.rewind_button.pack(side='left')
        self.transport = self.button(playback_controls, text='재생 / 일시정지', command=app.toggle_playback)
        self.transport.pack(side='left', padx=5)
        self.listen_button = self.button(playback_controls, text='선택 줄 듣기', command=self.listen)
        self.listen_button.pack(side='left')
        self.alsong_button = self.button(playback_controls, text='알송 가사 찾기…', command=self.open_alsong)
        self.alsong_button.pack(side='right', padx=5)
        self.alsong_actions = ttk.Frame(window)
        self.confirm_alsong_button = self.button(self.alsong_actions, text='알송 확정', command=self.confirm_alsong)
        self.confirm_alsong_button.pack(side='left', padx=(0, 5))
        self.unlink_alsong_button = self.button(self.alsong_actions, text='알송 임시 연결 해제', command=self.unlink_alsong)
        self.unlink_alsong_button.pack(side='left')
        self.lyric_actions = ttk.Frame(window)
        self.lyric_actions.grid(row=7, column=0, sticky='w', padx=14, pady=(0, 6))
        self.remove_lyric_button = self.button(self.lyric_actions, text='가사 줄 삭제', command=self.remove_lyric_line)
        self.remove_lyric_button.pack(side='left', padx=5)
        self.insert_lyric_button = self.button(self.lyric_actions, text='앞에 가사 추가', command=self.insert_lyric_line)
        self.insert_lyric_button.pack(side='left')
        developer_tools = ttk.Frame(window)
        developer_tools.grid(row=8,column=0,sticky='ew',padx=14,pady=(0,10))
        if hasattr(app,'open_connection'):
            self.connection_button = self.button(developer_tools,text='가사 연결…',
                                                  command=lambda:app.safe(app.open_connection))
            self.connection_button.pack(side='left')
            self.button(developer_tools,text='음원 분석…',
                        command=lambda:app.safe(app.open_audio)).pack(side='left',padx=5)
            self.auto_audio_button = ttk.Checkbutton(developer_tools,text='자동 분석 · Spotify 소리만',variable=app.auto_audio,
                            command=lambda:app.safe(app.toggle_auto_audio),
                            style='Developer.TCheckbutton')
            self.auto_audio_button.pack(side='left',padx=5)
            ttk.Checkbutton(developer_tools,text='재생 중 가사 추적',variable=app.realtime_tracking,
                            command=lambda:app.safe(lambda:app.set_realtime_tracking(app.realtime_tracking.get())),
                            style='Developer.TCheckbutton').pack(side='left',padx=5)
            timing_tools = ttk.Frame(window)
            timing_tools.grid(row=9,column=0,sticky='ew',padx=14,pady=(0,10))
            for label,change in (('싱크 −0.25초',-.25),('싱크 +0.25초',.25)):
                self.button(timing_tools,text=label,command=lambda change=change:self.change_offset(change)).pack(side='left',padx=(0,5))
            ttk.Label(timing_tools,text='표시 선행 (ms)').pack(side='left',padx=(10,4))
            self.lead = ttk.Spinbox(timing_tools,from_=0,to=MAX_LYRIC_ADVANCE_MS,width=5,increment=10)
            self.lead.insert(0,str(app.lyric_advance_ms))
            self.lead.pack(side='left')
            self.button(timing_tools,text='적용',command=self.apply_lead).pack(side='left',padx=5)
            self.singer_input_button = self.button(timing_tools,text='가창자 입력',
                                                    command=lambda:app.safe(app.toggle_singer_input))
            self.singer_input_button.pack(side='right')
            translation_tools = ttk.Menubutton(timing_tools,text='번역 도구')
            translation_menu = tk.Menu(translation_tools,tearoff=False)
            translation_menu.add_command(label='AI 일괄 번역',command=lambda:app.safe(app.open_batch_translation))
            translation_menu.add_command(label='내 자료 관리',command=lambda:app.safe(app.open_translation_resources))
            translation_tools.configure(menu=translation_menu)
            translation_tools.pack(side='right',padx=(0,5))
        self.edit_hint = ttk.Label(window, text='← → 소절 이동   Q/W/E/R −5/−2/+1/+5초   ↑ 앞 줄 끝   ↓ 다음 시작'
                                   '   가사·후리가나·번역 더블클릭')
        self.edit_hint.grid(row=5,column=0,sticky='w',padx=14,pady=(0,5))
        self.status = tk.Label(window, bg=BG, fg=MUTED, anchor='w', justify='left',
                               wraplength=400, font=('Malgun Gothic', 9))
        self.status.grid(row=6, column=0, sticky='ew', padx=14, pady=(0, 10))
        window.bind('<Configure>', self.resize)
        window.bind('<Button-1>', self.apply_lyric_on_click)
        window.bind('<Left>', lambda event:self.step_row(-1,event))
        window.bind('<Right>', lambda event:self.step_row(1,event))
        window.bind('<KeyRelease>',self.key_release)
        window.bind('<Escape>',app.stop_singer_input)
        # The selected row is the recording cursor. Do not also move it on keydown.
        self.rows.bind('<Down>', lambda _: 'break')
        self.rows.bind('<Up>', lambda _: 'break')
        self.rows.bind('<Left>', lambda event:self.step_row(-1,event))
        self.rows.bind('<Right>', lambda event:self.step_row(1,event))
        window.focus_set()
        self.refresh()

    def button(self, parent, **options):
        return ttk.Button(parent, style='Developer.TButton', width=0, **options)

    def change_offset(self, change):
        if not self.matches():
            self.notice('Spotify에서 가사가 연결된 곡을 재생하세요.', error=True)
            return
        try:self.app.offset(change)
        except (OSError,ValueError) as error:
            self.notice(str(error),error=True)
            return
        self.update_times()
        self.notice(f"곡 싱크 보정 {self.app.timing['offset']:+.2f}초")

    def apply_lead(self):
        try:value=int(self.lead.get().strip())
        except ValueError:
            self.notice('표시 선행 값을 밀리초 단위 정수로 입력하세요.', error=True)
            return
        value=lyric_advance_ms(value)
        self.lead.delete(0,'end')
        self.lead.insert(0,str(value))
        self.app.lyric_advance_ms=value
        self.app.safe(self.app.save_settings)
        self.app.render()
        self.notice(f'표시 선행 {value}ms 적용')

    def resize(self, event):
        if event.widget is self.window:
            self.status.configure(wraplength=max(200, event.width - 28))
            self.selected_singers.configure(wraplength=max(200, event.width - 28))
            self.singer_keys.configure(wraplength=max(200, event.width - 28))
            self.edit_hint.configure(wraplength=max(200, event.width - 28))

    def matches(self):
        app = self.app
        return bool(self.song and app.song and app.playback.has_timeline and app.playback.key == self.track
                    and abs(app.playback.duration - self.duration) <= 2 and app.song.id == self.song.id
                    and (app.song is self.song or tuple(line.original for line in app.song.lines) == self.originals))

    def advance(self):
        """Run after the media queue is drained and current source facts are available."""
        app = self.app
        collector = app.missing_audio_collector
        if collector and collector.owns_playback:
            self.refresh()
            return
        playback = app.playback
        if self.alsong_dialog and not self.alsong_dialog.current_track():
            self.alsong_dialog.close()
        if self.alsong_dialog and not self.skip_request and not self.rewind_request:
            self.refresh()
            return
        if self.rewind_request:
            captured, deadline = self.rewind_request
            if not self.matches() or time.monotonic() >= deadline:
                self.rewind_request = None
                if self.matches():
                    self.notice('곡 처음 이동을 확인하지 못했습니다. 재생 위치를 확인해 주세요.', error=True)
            elif not app.pending_seek and playback.captured > captured and playback.position() < 2:
                self.rewind_request = None
                self.notice('첫 줄 시작은 Enter, 다음 줄로 넘어갈 때는 ↓를 누르세요.')
        if self.skip_request:
            key, duration, deadline = self.skip_request
            if playback.has_timeline and (playback.key != key or abs(playback.duration - duration) > .5):
                self.skip_request = None
            elif time.monotonic() >= deadline:
                self.stop_search('다음 곡이 확인되지 않았습니다. Spotify 재생 대기열을 확인해 주세요.')
            else:
                return
        if self.song and not self.matches():
            self.close_lyric_entry()
            self.song = None
            self.searching = not self.follow_current_song
            self.skipped.clear()
            self.rows.delete(*self.rows.get_children())
        if self.follow_current_song and not self.searching and not self.matches() and app.is_bound() and app.display_lines:
            self.load()
        if app.temporary_alsong and not self.matches() and app.is_bound():
            self.load()
        if not self.searching:
            if self.matches() and time.monotonic() - self.baseline_checked >= 2:
                self.update_times()
            self.update_playhead()
            return
        if not playback.has_timeline:
            self.notice('Spotify에서 곡을 재생하면 작업할 곡을 찾습니다.')
        elif app.playlist_lyrics.owns_spotify:
            self.notice('플레이리스트 수집이 끝나면 작업할 곡을 찾습니다.')
        elif not (app.pending_track_change or app.pending_seek or app.pending_playback):
            key = self.playback_identity()
            sync = app.spotify_sync_for_manual_work()
            complete = (app.song and app.is_bound() and app.timing['times'] and app.timing.get('source') != 'LRCLIB'
                        and all(origin == 'manual' for origin in timing_origins(app.timing)))
            if sync == 'synced' or complete:
                if key in self.skipped:
                    self.stop_search('재생 목록을 한 바퀴 확인했습니다. 다른 곡을 재생하거나 다음 작업곡을 누르세요.')
                else:
                    self.skipped.add(key)
                    self.skip('Spotify 싱크가 있어 다음 곡으로 이동합니다.' if sync == 'synced'
                              else '수동 싱크를 저장한 곡을 넘깁니다.')
            elif sync == 'missing' and app.song and app.song.lines and app.is_bound():
                self.load()
                if playback.position() >= 1:
                    self.rewind()
            elif sync == 'missing':
                self.notice('Spotify 싱크 없음 · 기록할 원문 가사 대기 중')
            else:
                service = app.background_lyrics
                detail = service.spotify_status if service.spotify_requested else '가사 연결에서 Spotify 계정 연결을 확인하세요.'
                self.notice(f'Spotify 싱크 확인 중 · {detail}')
        self.refresh()

    def playback_identity(self):
        playback = self.app.playback
        return playback.key, round(playback.duration, 1)

    def stop_search(self, message):
        self.skip_request = None
        self.searching = False
        self.notice(message, error=True)
        self.refresh()

    def skip(self, message):
        app = self.app
        app.change_track('next')
        if app.pending_track_change == 'next':
            self.skip_request = app.playback.key, app.playback.duration, time.monotonic() + 6
            self.notice(message)
            return True
        self.stop_search('Spotify에서 다음 곡으로 이동하지 못했습니다.')
        return False

    def finish_skip(self, direction, success, message):
        if direction == 'next' and self.skip_request and not success:
            self.stop_search(message)

    def next_track(self):
        app = self.app
        if (self.skip_request or not app.playback.has_timeline or app.playlist_lyrics.owns_spotify
                or app.pending_track_change or app.pending_seek or app.pending_playback):
            return
        if self.skip('다음 작업곡을 찾는 중입니다.'):
            self.close_lyric_entry()
            self.song = None
            self.skipped.clear()
            self.rows.delete(*self.rows.get_children())
            self.searching = True
        self.refresh()

    def ask_work_playlist(self):
        playlist_id = self.app.settings.get('developer_work_playlist', '')
        value = askstring('재생목록 변경', 'Spotify 재생목록 공유 링크를 입력하세요.', parent=self.window,
                          initialvalue='https://open.spotify.com/playlist/' + playlist_id if playlist_id else '')
        if value is not None:
            try:self.open_work_playlist(value)
            except (OSError, ValueError) as error:self.notice(str(error), error=True)

    def open_work_playlist(self, value):
        from spotify_desktop import DesktopSpotify
        app = self.app
        collector = app.missing_audio_collector
        if ((collector and collector.owns_playback) or self.skip_request
                or app.pending_track_change or app.pending_seek or app.pending_playback):
            raise ValueError('현재 재생 조작이나 재생목록 분석이 끝난 뒤 재생목록을 바꿔 주세요.')
        playlist_id = DesktopSpotify.open_playlist(value)
        self.searching = False
        self.skipped.clear()
        app.settings['developer_work_playlist'] = playlist_id
        app.save_settings()
        self.refresh()
        self.notice('Spotify에서 열린 재생목록을 재생한 뒤 다음 작업곡을 누르세요.')

    def rewind(self):
        if not self.matches():
            return
        app = self.app
        app.seek_track('first')
        if app.pending_seek == ('first', self.track):
            self.rewind_request = app.playback.captured, time.monotonic() + 5
            self.notice('곡 처음으로 이동 중…')
        else:
            self.notice('곡 처음으로 이동하지 못했습니다. 재생 위치를 확인해 주세요.', error=True)
        self.refresh()

    def load(self, selected=0):
        app = self.app
        self.close_lyric_entry()
        if self.singer_editor:
            self.singer_editor.close()
        self.searching = False
        self.song, self.track, self.duration = app.song, app.playback.key, app.playback.duration
        self.originals = tuple(line.original for line in self.song.lines)
        self.playhead = None
        self.singer_credits = None
        self.rows.delete(*self.rows.get_children())
        for index in range(len(self.originals)):
            self.rows.insert('', 'end', iid=str(index), values=(
                index + 1, '', self.lyric_text(index), app.display_lyric_reading(index),
                app.display_lyric_translation(index), ''))
        self.update_times()
        self.select(selected)
        self.notice('AI 시각을 먼저 듣고, 다음 줄이 시작할 때 ↓를 누르세요.' if 'audio' in self.baseline[1]
                    else '사용 가능한 AI 시각이 아직 없습니다. 분석이 끝난 뒤 확인하거나 직접 기록할 수 있습니다.')

    def update_times(self):
        self.baseline_checked = time.monotonic()
        self.baseline = self.app.manual_sync_baseline()
        times, origins = self.baseline
        for index, (stamp, origin) in enumerate(zip(times, origins)):
            if stamp is None:
                text = '싱크 없음'
            else:
                text = f'{clock(max(0, stamp + self.app.timing["offset"]))} {"AI" if origin == "audio" else "수동"}'
            self.rows.set(str(index), 'time', text)
            self.rows.set(str(index), 'reading', self.app.display_lyric_reading(index))
            self.rows.set(str(index), 'translation', self.app.display_lyric_translation(index))
            self.paint_row(index)

    def lyric_text(self, index):
        line = self.app.display_lines[index]
        return self.app.corrected_lyric_text(index) or line.original or line.korean

    def cell_text(self, index, column):
        if column == 'reading':
            return self.app.display_lyric_reading(index)
        if column == 'translation':
            return self.app.display_lyric_translation(index)
        return self.lyric_text(index)

    def scroll_rows(self, scrollbar, first, last):
        scrollbar.set(first, last)
        if self.lyric_entry:
            self.place_lyric_entry()

    def place_lyric_entry(self):
        row = str(self.lyric_entry_row)
        box = self.rows.bbox(row, self.lyric_entry_column)
        if box:
            x, y, width, height = box
            self.lyric_entry.place(x=x, y=y, width=width, height=height)
        elif self.rows.exists(row):
            self.rows.see(row)

    def remember_lyric_click(self, event):
        # The first click can recenter the selected row before the second click.
        self.clicked_lyric = None
        if self.rows.identify_region(event.x, event.y) == 'cell':
            column = self.rows.column(self.rows.identify_column(event.x), 'id')
            row = self.rows.identify_row(event.y)
            if column in EDITABLE_COLUMNS:
                self.clicked_lyric = self.app.song, row, column

    def edit_lyric(self, _event):
        clicked, self.clicked_lyric = self.clicked_lyric, None
        if not clicked or not self.navigation_available() or self.lyric_entry:
            return
        song, row, column = clicked
        edits = self.app.lyric_text_edits
        layout = edits.key
        if not row or layout is None or song is not self.app.song:
            self.notice('현재 곡의 가사가 연결되어야 수정할 수 있습니다.', error=True)
            return 'break'
        index = int(row)
        self.select_for_edit(index)
        initial = self.cell_text(index, column)
        value = tk.StringVar(master=self.rows, value=initial)
        entry = self.lyric_entry = ttk.Entry(self.rows, textvariable=value)
        self.lyric_entry_value = value
        self.lyric_entry_row = index
        self.lyric_entry_column = column
        self.lyric_entry_layout = layout
        self.lyric_entry_initial = initial
        self.lyric_entry_confirmed = False
        value.trace_add('write', self.reset_lyric_confirmation)
        # Route typing and arrows only to the text field while it has focus.
        entry.bindtags(tuple(tag for tag in entry.bindtags() if tag != str(self.window)))
        entry.selection_range(0, 'end')
        entry.bind('<KeyRelease-Return>', self.confirm_lyric_entry)
        entry.bind('<KeyRelease-KP_Enter>', self.confirm_lyric_entry)
        entry.bind('<FocusOut>', lambda _: self.save_lyric_entry(layout) if self.lyric_entry is entry else None)
        entry.bind('<Escape>', lambda _: self.close_lyric_entry())
        self.place_lyric_entry()
        entry.focus_set()
        self.lyric_edit_notice()
        return 'break'

    def lyric_edit_notice(self):
        message = 'Enter 두 번 또는 바깥 클릭으로 저장 · Esc 취소'
        if self.lyric_entry_column == 'reading':
            message += ' · 비워서 저장하면 자동 독음 복원'
        self.notice(message)

    def reset_lyric_confirmation(self, *_):
        if self.lyric_entry_confirmed:
            self.lyric_entry_confirmed = False
            self.lyric_edit_notice()

    def confirm_lyric_entry(self, _event):
        if self.lyric_entry_confirmed:
            return self.save_lyric_entry(self.lyric_entry_layout)
        self.lyric_entry_confirmed = True
        self.notice('Enter를 한 번 더 누르면 저장 · Esc 취소')
        return 'break'

    def apply_lyric_on_click(self, event):
        if self.lyric_entry and event.widget is not self.lyric_entry:
            self.save_lyric_entry(self.lyric_entry_layout)

    def save_lyric_entry(self, layout):
        if not self.lyric_entry:
            return 'break'
        edits = self.app.lyric_text_edits
        if not self.matches() or layout != edits.key:
            self.close_lyric_entry()
            self.notice('곡이나 가사 줄이 바뀌었습니다. 수정할 줄을 다시 선택하세요.', error=True)
            return 'break'
        index = self.lyric_entry_row
        text = self.lyric_entry.get().strip()
        field, label = EDITABLE_COLUMNS[self.lyric_entry_column]
        if text == self.lyric_entry_initial:
            return self.close_lyric_entry()
        try:
            edits.save(self.app.annotation_row(index), text, field=field)
        except (OSError, ValueError) as error:
            self.notice(f'{label} 저장 실패. {error}', error=True)
            return 'break'
        self.close_lyric_entry()
        self.app.render()
        for column in EDITABLE_COLUMNS:
            self.rows.set(str(index), column, self.cell_text(index, column))
        if self.app.editor:
            self.app.editor_refresh()
        self.notice(f'{index+1}번 {label} 저장')
        return 'break'

    def close_lyric_entry(self):
        if self.lyric_entry:
            entry, self.lyric_entry = self.lyric_entry, None
            if entry.focus_get() is entry:
                self.rows.focus_set()
            entry.destroy()
            self.lyric_entry_value = None
        return 'break'

    def row_stamp(self,index):
        times,_ = self.baseline
        return max(0.,times[index]+self.app.timing['offset']) if times[index] is not None else None

    def paint_row(self,index):
        playing = index == self.playhead
        self.rows.item(str(index),tags=('playing',) if playing else ('missing',) if self.row_stamp(index) is None else ())
        self.rows.set(str(index),'line',f'▶ {index+1}' if playing else str(index+1))

    def selected_index(self):
        return min(self.index, len(self.originals)-1)

    def center_row(self, index=None):
        if not self.matches():
            return
        if index is None:
            index = self.selected_index()
        count = len(self.originals)
        start, end = self.rows.yview()
        visible = max(1, round((end-start)*count))
        top = max(0, min(count-visible, index-visible//2))
        self.rows.yview_moveto(top/count)

    def update_playhead(self):
        following = self.follow_playback.get()
        if not self.matches() or (following and not self.follow_current_song
                                  and (self.app.pending_seek or self.rewind_request)):
            self.refresh()
            return
        current = None
        if following:
            if self.follow_current_song:
                current = self.app.visible_lyric_index()
            else:
                times = [self.row_stamp(index) for index in range(len(self.baseline[0]))]
                current = active_line(times,self.app.playback.position())
        if current != self.playhead:
            old,self.playhead = self.playhead,current
            if old is not None:
                self.paint_row(old)
            if current is not None:
                self.paint_row(current)
        self.refresh()

    def navigation_available(self):
        collector = self.app.missing_audio_collector
        return (self.matches() and not self.skip_request and not self.rewind_request
                and not (collector and collector.owns_playback))

    def step_row(self,delta,event=None):
        if event is not None and (event.state & (0x1|0x4|0x20000) or self.is_text_input(event)):
            return
        if self.navigation_available():
            index = max(0,min(len(self.originals)-1,self.index+delta))
            self.seek_row(index)
        return 'break'

    def seek_row(self,index):
        if not self.navigation_available():return
        self.select_for_edit(index)
        stamp = self.row_stamp(index)
        if stamp is None:
            self.app.queued_lyric_seek = None
            self.notice(f'{index+1}번 줄 · 싱크 없음')
        else:
            self.app.request_lyric_seek(stamp)
            self.notice(f'{index+1}번 줄 · {clock(stamp)}')

    def seek_key(self,event):
        if event.state & (0x1|0x4|0x20000):
            return
        if self.navigation_available() and self.app.seek_by_key(event.keysym):
            return 'break'

    @staticmethod
    def is_text_input(event):
        return event.widget.winfo_class() in ('Entry','TEntry','Spinbox','TSpinbox','TCombobox','Text')

    def key_release(self,event):
        if self.is_text_input(event):
            return
        if event.state & (0x1|0x4|0x20000):
            return
        key = event.keysym.removeprefix('KP_')
        if key == 'Down':
            return self.record_next()
        if key in ('Return', 'Enter'):
            return self.record()
        if key == 'Up':
            return self.record_previous_end()
        if key == 'Delete':
            return self.clear_time()
        if self.seek_key(event):
            return 'break'
        number = len(key) == 1 and key in SINGER_KEYS
        if not number and key not in ('space', 'BackSpace'):
            return
        app = self.app
        if (not self.navigation_available() or app.pending_track_change or app.singer_edits.key is None
                or not self.originals):
            return 'break'
        if number or event.widget.winfo_toplevel() is self.window or app.singer_input_index is not None:
            return app.singer_assignment_key(key, index=self.selected_index())

    def listen(self):
        if not self.navigation_available() or self.index >= len(self.originals):
            return
        stamp = self.row_stamp(self.index)
        if stamp is not None:
            self.app.request_lyric_seek(max(0., stamp - 2.))

    def notice(self, text, error=False):
        self.status.configure(text=text, fg='#9b2940' if error else MUTED)

    def can_edit_singers(self):
        app = self.app
        return (self.navigation_available() and not app.locked and not app.pending_track_change
                and app.singer_edits.key is not None)

    def edit_singers(self, *, parent=None):
        if self.singer_editor:
            self.singer_editor.window.lift()
            return
        if not self.can_edit_singers():
            return
        try:
            people = [person for person in performers(database_path=self.app.store.directory/'imas_db_people.json')
                      if person['kind'] == 'idol']
        except (OSError, ValueError) as error:
            self.notice(f'브랜드별 가창자 명단을 열지 못했습니다. {error}', error=True)
            return
        self.singer_editor = SingerRosterEditor(self, people, parent)

    def refresh_singers(self, valid):
        app = self.app
        album = app.playback.album
        credits = (app.line_singers if valid else (), album)
        if self.singer_credits != credits:
            self.singer_credits = credits
            for row in self.rows.get_children():
                names = app.current_line_singers(int(row)) if valid else ()
                self.rows.set(row, 'singers', ', '.join(short_performer_name(name, album) for name in names))
        index = self.index
        names = app.current_line_singers(index) if valid and index < len(self.originals) else ()
        if names:
            label = '선택 줄 가창자 · ' + artist_label(names, album)
            if app.current_line_singer_partial(index):
                label += ' · 일부 미확인'
            if self.selected_singers.cget('text') != label:
                self.selected_singers.configure(text=label)
                self.selected_singers.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(3, 0))
        elif self.selected_singers.cget('text'):
            self.selected_singers.configure(text='')
            self.selected_singers.grid_remove()
        if valid and app.singer_options:
            keys = '   '.join(f'{key} {short_performer_name(name, album)}'
                             for key, name in zip(SINGER_KEYS, app.singer_options))
            label = f'가창자 키 · {keys}   ·   Space 비우기 / Backspace 자동'
            if self.singer_keys.cget('text') != label:
                self.singer_keys.configure(text=label)
                self.singer_keys.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(3, 0))
        elif self.singer_keys.winfo_manager():
            self.singer_keys.configure(text='')
            self.singer_keys.grid_remove()

    def refresh(self):
        collector = self.app.missing_audio_collector
        collecting = bool(collector and collector.owns_playback)
        valid = self.matches() and not self.skip_request and not collecting
        playback = self.app.playback
        title = playback.title or '현재 곡을 기다리는 중'
        alsong = self.app.temporary_alsong
        if alsong:
            title += ' · 알송 확정' if alsong.confirmed else ' · 알송 임시 연결'
        self.title.configure(text=title)
        if not valid:
            text = 'Spotify 싱크 없는 곡 찾는 중' if self.searching else ''
        else:
            progress = f'{self.index + 1}/{len(self.originals)}줄' if self.index < len(self.originals) else '마지막 줄 기록 완료'
            known = sum(stamp is not None for stamp in self.baseline[0])
            follow = (f'▶ {self.playhead+1}번' if self.playhead is not None else
                      '재생 줄 대기' if known else '싱크 없음') if self.follow_playback.get() else '재생 줄 숨김'
            text = f'{clock(playback.position())} · {follow} · 선택 {progress} · 싱크 {known}/{len(self.originals)}줄'
        self.position.configure(text=text)
        self.refresh_singers(valid)
        singer_editable = self.can_edit_singers()
        self.edit_singers_button.configure(state='normal' if singer_editable else 'disabled')
        if hasattr(self,'singer_input_button'):
            self.singer_input_button.configure(text='가창자 입력 종료' if self.app.singer_input_index is not None else '가창자 입력',
                                               state='normal' if singer_editable else 'disabled')
        if self.singer_editor and not singer_editable:
            self.singer_editor.close()
        busy = bool(self.app.pending_track_change or self.app.pending_seek or self.app.pending_playback
                    or self.skip_request or self.rewind_request)
        self.next_button.configure(state='normal' if valid and not busy and self.index < len(self.originals) else 'disabled')
        self.previous_button.configure(state='normal' if valid and self.index > 0 else 'disabled')
        self.transport.configure(state='normal' if valid and not busy else 'disabled')
        self.rewind_button.configure(state='normal' if valid and not busy else 'disabled')
        times, _ = self.baseline
        listenable = self.index < len(times) and times[self.index] is not None
        self.clear_button.configure(state='normal' if valid and not busy and listenable else 'disabled')
        self.listen_button.configure(state='normal' if valid and not busy and listenable else 'disabled')
        self.work_button.configure(state='normal' if playback.has_timeline and not busy and not collecting else 'disabled')
        self.work_playlist_button.configure(state='disabled' if busy or collecting else 'normal')
        self.alsong_button.configure(state='normal' if playback.has_timeline and not collecting else 'disabled')
        editable = valid and not busy and self.index < len(self.originals)
        self.remove_lyric_button.configure(state='normal' if editable and len(self.originals)>1 else 'disabled')
        self.insert_lyric_button.configure(state='normal' if editable else 'disabled')
        if alsong:
            if not self.alsong_actions.winfo_manager():
                self.alsong_actions.grid(row=8, column=0, sticky='w', padx=14, pady=(0, 10))
            self.confirm_alsong_button.configure(state='normal' if valid and not busy and not alsong.confirmed else 'disabled')
            self.unlink_alsong_button.configure(text='알송 확정 해제' if alsong.confirmed else '알송 임시 연결 해제')
        else:
            if self.alsong_actions.winfo_manager():
                self.alsong_actions.grid_remove()

    def open_alsong(self):
        if self.alsong_dialog:
            self.alsong_dialog.window.lift()
            return
        if self.app.playback.has_timeline:
            parent = self.app.root if self.window.state()=='withdrawn' else self.window
            self.alsong_dialog = AlsongDialog(self,parent)

    def link_alsong(self, result):
        self.close_lyric_entry()
        self.app.link_temporary_alsong(result)
        self.rewind_request = None
        self.load()
        self.notice('알송 가사를 현재 곡에 임시 연결했습니다.')

    def confirm_alsong(self):
        if not self.navigation_available():
            return
        try:
            self.app.confirm_alsong()
        except (OSError, ValueError) as error:
            self.notice(f'알송 가사를 확정하지 못했습니다. {error}', error=True)
            return
        self.refresh()
        self.notice('알송 가사를 확정했습니다. 이 곡을 다시 재생할 때 자동으로 연결됩니다.')

    def unlink_alsong(self):
        self.close_lyric_entry()
        link = self.app.temporary_alsong
        if not link or not self.matches():
            return
        try:
            if link.confirmed:
                self.app.clear_confirmed_alsong()
            else:
                self.app.unlink_temporary_alsong()
                self.app.restore_confirmed_alsong()
        except (OSError, ValueError) as error:
            self.notice(f'알송 연결을 바꾸지 못했습니다. {error}', error=True)
            return
        if self.app.is_bound():
            self.load()
        else:
            self.song = None
            self.searching = True
            self.rows.delete(*self.rows.get_children())
        self.refresh()
        self.notice('알송 확정을 해제했습니다.' if link.confirmed else '알송 임시 연결을 해제했습니다.')

    def insert_lyric_line(self):
        if not self.navigation_available() or self.index >= len(self.originals):
            return
        self.close_lyric_entry()
        index, song = self.index, self.app.song
        text = askstring('앞에 가사 추가', f'{index+1}번 줄 앞에 넣을 가사', parent=self.window)
        if text is None or self.app.song is not song or not self.navigation_available():
            return
        self.change_lyric_rows(index,text)

    def change_lyric_rows(self,index,text=None):
        if (not self.navigation_available() or not 0<=index<len(self.originals)
                or text is None and len(self.originals)<=1):
            return
        self.close_lyric_entry()
        try:
            self.app.edit_developer_lyric_rows(index, text)
        except (OSError, ValueError) as error:
            action = '추가' if text is not None else '삭제'
            self.notice(f'가사 줄을 {action}하지 못했습니다. {error}', error=True)
            return
        self.load(min(index, len(self.app.song.lines)-1))
        self.app.render()
        self.notice(f'{index+1}번 가사 추가 · 시작할 때 Enter로 기록' if text is not None else f'{index+1}번 가사 줄 삭제')

    def remove_lyric_line(self):
        self.change_lyric_rows(self.index)

    def open_collection(self):
        self.app.open_audio(playlist=True)

    def pause_for_playlist_analysis(self):
        self.close_lyric_entry()
        self.searching = False
        self.rewind_request = None
        self.song = None
        self.rows.delete(*self.rows.get_children())
        self.refresh()

    def select_row(self, _event):
        selected = self.rows.selection()
        if selected and int(selected[0]) != self.index:
            self.select_for_edit(int(selected[0]))

    def select_for_edit(self, index):
        self.select(index)
        self.app.render()

    def select(self, index):
        self.index = index
        if index < len(self.originals):
            self.rows.selection_set(str(index))
            self.rows.focus(str(index))
            self.center_row(index)
        else:
            self.rows.selection_remove(*self.rows.selection())
        self.refresh()

    def previous(self, _event=None):
        if self.navigation_available():
            self.select_for_edit(max(0,self.index - 1))
        return 'break'

    def record_next(self, pressed_at=None):
        if not self.navigation_available():
            return 'break'
        index = self.index + 1
        if index >= len(self.originals):
            self.notice('마지막 줄입니다.')
            return 'break'
        return self.record_row(index, index, pressed_at)

    def record_previous_end(self, pressed_at=None):
        if not self.navigation_available() or self.index >= len(self.originals):
            return 'break'
        if self.index == 0:
            self.notice('첫 소절 앞에는 기록할 소절이 없습니다.')
            return 'break'
        return self.record_row(self.index, self.index, pressed_at, previous_end=True)

    def record(self, _event=None):
        if not self.navigation_available() or self.index >= len(self.originals):
            return 'break'
        return self.record_row(self.index, self.index+1)

    def clear_time(self):
        app = self.app
        index = self.index
        if (not self.navigation_available() or not app.is_bound()
                or index >= len(self.originals) or self.row_stamp(index) is None):
            return 'break'
        try:
            updated = edit_manual_time(app.timing, index, None)
            updated['cleared_rows'] = sorted(cleared_rows(updated) | {index})
            app.save_current_timing(updated)
        except (ValueError, OSError) as error:
            self.notice(f'싱크를 지우지 못했습니다. {error}', error=True)
            return 'break'
        self.publish_timing(updated, index)
        self.notice(f'{index+1}번 싱크 삭제 · 시작할 때 Enter로 다시 기록')
        return 'break'

    def record_row(self, index, next_index, pressed_at=None, *, previous_end=False):
        app = self.app
        if not app.is_bound():
            self.notice('현재 재생 중인 곡의 가사가 연결되어야 기록할 수 있습니다.', error=True)
            return 'break'
        timing = app.timing
        try:
            position = round(app.sync_capture_position(pressed_at), 3)
            offset = timing['offset']
            # Rebuild from protected user edits and the reviewed AI candidates.
            # A provider timestamp cannot constrain a user correction.
            times = [stamp if origin == 'manual' and timing.get('source') != 'LRCLIB' else None
                     for stamp, origin in zip(timing['times'], timing_origins(timing))]
            stamp = round(max(0, position - offset), 3)
            conflicting = next((row for row in range(index+1, len(times))
                                if times[row] is not None and times[row] <= stamp), None)
            if conflicting is not None:
                self.notice(f'{conflicting+1}번 줄의 저장 시각과 겹칩니다. 해당 줄을 선택하고 Delete로 지운 뒤 다시 기록하세요.',
                            error=True)
                return 'break'
            updated = {**timing, 'times': times, 'origins': ['manual' if t is not None else None for t in times],
                       'automatic_times': automatic_times(timing)}
            updated = edit_manual_time(updated, index, stamp)
            ai = [stamp + offset if stamp is not None and origin == 'audio' else None
                  for stamp, origin in zip(*self.baseline)]
            updated = merge_candidates(updated, ai)[0]
            updated['source'] = '직접 기록'
            app.save_current_timing(updated)
        except (ValueError, OSError) as error:
            self.notice(f'저장하지 못했습니다. {error}', error=True)
            return 'break'
        self.publish_timing(updated, next_index)
        label = f'{index}번 끝 → {index+1}번 시작' if previous_end else f'{index+1}번 시작'
        self.notice(f'{label} {clock(self.row_stamp(index))} 저장')
        return 'break'

    def publish_timing(self, updated, next_index):
        app = self.app
        app.timing = updated
        self.update_times()
        if app.editor:
            app.editor_refresh()
        self.select_for_edit(next_index)

    def close(self):
        self.close_lyric_entry()
        if self.singer_editor:
            self.singer_editor.close()
        if self.alsong_dialog:
            self.alsong_dialog.close()
        self.app.manual_sync_dialog = None
        self.app.stop_singer_input(redraw=False)
        self.app.hotkeys.developer_input_windows = ()
        self.window.destroy()
        self.app.render()
