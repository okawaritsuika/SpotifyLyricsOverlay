"""A searchable music dictionary alongside the existing lyric overlay."""
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk
from urllib.parse import quote, urlparse
import webbrowser

from artist_names import artist_label, character_name_only, name_key
from artist_colors import _artist_ranges
from explorer_widgets import CatalogList
from explorer_symbols import series_symbol
from explorer_library import (ExplorerLibrary, SERIES, SERIES_COLORS, SONG_SORTS, artists_label,
    artist_key, availability, build_performer_lookup, cached_candidates, group_duplicate_songs,
    matches_query, performers, related_songs, resolve_group_members, search_key, series_label,
    song_release_date, sort_songs)
from local_library import LocalLibrary
from live_catalog import LiveCatalog
from group_credits import is_project_credit, save_group_credits, with_character_voices
from song_catalog import SongCatalog, refresh_catalog
from song_aliases import alias_information, search_aliases, title_readings
from imas_people_sources import refresh_people
from spotify_catalog import SpotifyCatalogError, parse_track_id, rank_candidates
from spotify_desktop import DesktopSpotify
from ui_theme import BG, PANEL, TEXT, MUTED, ACCENT, ControlHint
from windows import work_area


PAGE_SIZE = 100
SONG_KINDS = {'': '전체 유형', 'song': '노래', 'instrumental': '연주 · 반주',
              'spoken': '드라마 · 토크', 'unknown': '유형 미확인'}


def readable_color(value):
    """Keep the character's hue legible on the dictionary's dark panels."""
    if not isinstance(value, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', value):return None
    return '#' + ''.join(f'{round(int(value[i:i+2],16)*.45+255*.55):02x}' for i in (1,3,5))


def voice_label(person):
    if character_name_only(person['name']):return ''
    return ' · '.join(actor.get('display') or actor['name'] for actor in person.get('voice_actors', []))


def duration_label(value):
    try:
        value = int(float(value))
        return f'{value // 60}:{value % 60:02}' if value > 0 else '—'
    except (TypeError, ValueError, OverflowError):return '—'


def release_editions(song):
    editions = []
    for release in song.get('releases') or []:
        if release not in editions:editions.append(release)
    return editions


def open_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('열 수 있는 HTTPS 출처 주소가 없습니다.')
    webbrowser.open(url)


class ExplorerDialog:
    def __init__(self, app):
        self.app, self.directory = app, app.store.directory
        self.catalog = SongCatalog(update_path=self.directory / 'song_catalog.json')
        self.live_catalog = LiveCatalog(update_path=self.directory / 'live_catalog.json')
        self.library = ExplorerLibrary(self.directory)
        self.local = LocalLibrary(self.directory)
        self.spotify = DesktopSpotify(app)
        self.people = performers(external=getattr(self.catalog, 'performers', []),
                                 database_path=self.directory/'imas_db_people.json')
        self.index_people()
        self.duplicate_groups = {}
        self.selected_song = None
        self.selected_person = None
        self.filtered = []
        self.page = 0
        self.performer_filter = None
        self.live_song_filter = None
        self.live_match_cache = {}
        self.song_live_counts = {}
        self.song_live_count_source = None
        self.song_live_count_catalog = None
        self.live_search_after = None
        self.live_update_active = False
        self.music_update_active = False
        self.closed = False
        self.busy = False
        self.auto_links = None
        self.auto_pending = False
        self.auto_cancel = threading.Event()
        self.auto_done = threading.Event();self.auto_done.set()
        self.auto_next = 0
        self.auto_enabled = tk.BooleanVar(value=not getattr(getattr(app,'catalog_sync',None),'paused',False))
        self.auto_status = tk.StringVar(value='Spotify 곡 자동 연결 준비 중')
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.search_after = None
        self.candidate_window = None
        self.release_window = None
        self.window = tk.Toplevel(app.root)
        window = self.window
        window.title('아이마스 음악 사전')
        window.configure(bg=BG)
        window.attributes('-topmost', False)
        left, top, right, bottom = work_area()
        width, height = min(1280, right-left-60), min(860, bottom-top-65)
        window.geometry(f'{width}x{height}+{left+(right-left-width)//2}+{top+20}')
        window.minsize(min(900, width), min(620, height))
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.bind('<Control-f>', self.focus_search)
        window.bind('<Escape>', lambda _: self.close())
        self._styles()
        header = tk.Frame(window, bg=BG)
        header.pack(fill='x', padx=20, pady=(16, 14))
        tk.Label(header, text='아이돌마스터  음악 사전', fg=TEXT, bg=BG,
                 font=('Malgun Gothic', 16, 'bold')).pack(side='left')
        self.count_label = tk.Label(header, text='', fg=MUTED, bg=BG, font=('Malgun Gothic', 9))
        self.count_label.pack(side='right', padx=(20, 0))
        self.notebook = ttk.Notebook(window, style='Explorer.TNotebook')
        self.notebook.pack(fill='both', expand=True, padx=18)
        self.song_tab = ttk.Frame(self.notebook, padding=(4, 12, 4, 4))
        self.person_tab = ttk.Frame(self.notebook, padding=(4, 12, 4, 4))
        self.local_tab = ttk.Frame(self.notebook, padding=(4, 12, 4, 4))
        self.live_tab = ttk.Frame(self.notebook, padding=(4, 12, 4, 4))
        self.settings_tab = ttk.Frame(self.notebook)
        for tab, label in [(self.song_tab, '곡 라이브러리'), (self.person_tab, '유닛 · 아이돌'),
                           (self.live_tab, '라이브'), (self.local_tab, '로컬 파일'),
                           (self.settings_tab, '연결 · 업데이트')]:
            self.notebook.add(tab, text=label)
        self._song_page()
        self._person_page()
        self._local_page()
        self._live_page()
        self._settings_page()
        self.notebook.bind('<<NotebookTabChanged>>', self.tab_changed)
        footer = ttk.Frame(window, padding=(20, 8))
        footer.pack(side='bottom', fill='x', before=self.notebook)
        footer.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value='곡을 선택해 녹음 정보와 재생 경로를 확인하세요.  ·  Ctrl+F 검색')
        self.status_label = ttk.Label(footer, textvariable=self.status, foreground=MUTED, wraplength=920)
        self.status_label.grid(row=0, column=0, sticky='ew')
        self.cancel_button = self.button(footer, '취소', self.cancel_job)
        self.cancel_button.grid(row=0, column=1, padx=(12, 0))
        self.cancel_button.configure(state='disabled')
        self.progress = ttk.Progressbar(footer, mode='indeterminate')
        self.progress.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(6, 0))
        self.progress.grid_remove()
        self.auto_status_label=ttk.Label(footer,textvariable=self.auto_status,foreground=MUTED,wraplength=920)
        self.auto_status_label.grid(row=2,column=0,sticky='w',pady=(4,0))
        ttk.Checkbutton(footer,text='자동 연결',variable=self.auto_enabled,
                        command=self.toggle_auto_links).grid(row=2,column=1,padx=(12,0))
        window.bind('<Configure>', self._resize, add='+')
        self.filter_songs()
        self.filter_people()
        self.refresh_local()
        self.refresh_settings()
        self.filter_lives()
        self.poll_id = window.after(100, self.poll)
        self.auto_id = window.after(400,self.auto_link_tick)
        self.focus_current_song()

    def toggle_auto_links(self):
        sync=getattr(self.app,'catalog_sync',None)
        if sync and sync.enabled:sync.pause(not self.auto_enabled.get())
        if not self.auto_enabled.get():self.auto_cancel.set()
        self.auto_status.set('Spotify 자동 연결 대기' if self.auto_enabled.get() else 'Spotify 자동 연결 일시정지')

    def auto_link_tick(self):
        if self.closed:return
        self.auto_id=self.window.after(1000,self.auto_link_tick)
        sync=getattr(self.app,'catalog_sync',None)
        if sync and sync.enabled:
            self.auto_status.set(sync.status)
            return
        if not self.auto_enabled.get() or self.busy or self.auto_pending or time.monotonic()<self.auto_next:return
        try:
            if self.auto_links is None:
                from catalog_link_queue import CatalogLinkQueue
                self.auto_links=CatalogLinkQueue(self.directory,self.catalog.songs,self.library,self.people)
                linked=self.auto_links.seed()
                if linked:self.filter_songs(preserve_selected=True)
            coverage=self.auto_links.status_text()
            if not self.spotify.connected:
                self.auto_status.set(coverage+' · Spotify 로그인 대기');return
            service=getattr(self.app,'background_lyrics',None)
            if service and (service.spotify_pending or service.metadata_pending):
                self.auto_status.set(coverage+' · 현재 재생곡 가사 확인 중');return
            start=self.page*PAGE_SIZE
            song=self.auto_links.next_song([self.selected_song,*self.filtered[start:start+PAGE_SIZE]])
            if song is None:
                self.auto_status.set(coverage+' · 현재 자동검색 대기 · 미확정 녹음은 개별 확인 가능')
                return
            self.auto_cancel=threading.Event();cancel=self.auto_cancel
            self.auto_pending=True;self.auto_done.clear()
            self.auto_status.set(coverage+' · 검색 중: '+song['title'])
            def work():
                try:
                    tracks=self.spotify.search(song['title'],cancel=cancel)
                    self.events.put(('auto_links',song,tracks,cancel))
                except Exception as error:self.events.put(('auto_link_error',error,cancel))
                finally:self.auto_done.set()
            threading.Thread(target=work,name='dictionary-auto-links',daemon=True).start()
        except (OSError,ValueError) as error:
            self.auto_status.set('자동 연결 자료 확인 실패 · '+str(error));self.auto_next=time.monotonic()+60

    def receive_auto_links(self,event):
        self.auto_pending=False;self.auto_next=time.monotonic()+2
        cancel=event[-1]
        if cancel.is_set():return
        if event[0]=='auto_link_error':
            error=event[1]
            self.auto_next=time.monotonic()+(300 if getattr(error,'code','')=='rate_limited' else 30)
            coverage=self.auto_links.status_text()+' · ' if self.auto_links else ''
            self.auto_status.set(coverage+'Spotify 자동 연결 잠시 대기 · '+str(error));return
        linked=self.auto_links.apply(event[1],event[2])
        self.auto_status.set(self.auto_links.status_text()+' · '+
                             (f'{linked}음원 자동 연결' if linked else '녹음 일치 미확정 · 다음 곡 확인'))
        if linked:self.filter_songs(preserve_selected=True)

    def focus_current_song(self):
        """Navigate on explicit open/action; never follow playback while browsing."""
        playback = getattr(self.app, 'playback', None)
        title = getattr(playback, 'title', '')
        if not title:return False
        track = self.spotify.current_track()
        songs = self.catalog.songs
        linked = [song for song in songs if track and
                  (self.library.linked_track(song['id']) or {}).get('id') == track['id']]
        candidates = linked or [song for song in songs if search_key(title) in
            {search_key(value) for value in [song['title'], *song.get('aliases', [])]}]
        album = search_key(getattr(playback, 'album', ''))
        artists = {artist_key(value) for value in
                   ([getattr(playback, 'artist', '')] + (track.get('artists', []) if track else []))
                   if value}
        album_matches = [song for song in candidates if album and album in
            {search_key(song.get('album', '')),
             *(search_key(release.get('title', '')) for release in song.get('releases', []))}]
        artist_matches = [song for song in candidates if artists.intersection(
            artist_key(value) for value in song.get('artists', []))]
        if album_matches:candidates = album_matches
        if artist_matches:
            narrowed = [song for song in candidates if song in artist_matches]
            if narrowed:candidates = narrowed
            elif not linked:candidates = []
        if not linked and not album_matches and not artist_matches:candidates = []
        if not linked:
            # One matching field must not override another known conflict.
            if album:
                candidates = [song for song in candidates if song in album_matches or not any(
                    search_key(value) for value in [song.get('album', ''),
                    *(release.get('title', '') for release in song.get('releases', []))])]
            if artists:
                candidates = [song for song in candidates if not song.get('artists') or song in artist_matches]
        duration = getattr(playback, 'duration', 0)
        if duration and not linked:
            candidates = [song for song in candidates if not song.get('duration') or
                          abs(song['duration'] - duration) <= 2]
        current = candidates[0] if len(candidates) == 1 else None
        if linked and candidates:current = candidates[0]
        self.performer_filter = None;self.live_song_filter = None
        self.series.set(SERIES['']);self.only_favorites.set(False);self.only_playable.set(False)
        self.song_kind.set(SONG_KINDS[''])
        self.query.set(current['title'] if current else title)
        self.selected_song = current
        self.notebook.select(self.song_tab)
        self.filter_songs(preserve_selected=bool(current))
        if current:
            self.choose_song(current)
            self.status.set('지금 Spotify 곡 · '+title)
        else:
            self.status.set('지금 Spotify 곡 검색 · '+title+' · 일치하는 녹음을 확인해 주세요.')
        return current is not None

    def show_current_song(self):
        if not getattr(getattr(self.app, 'playback', None), 'title', ''):
            self.status.set('현재 Spotify 곡을 확인할 수 없습니다. Spotify에서 곡을 선택해 주세요.')
            return
        self.focus_current_song()

    def _styles(self):
        style = ttk.Style(self.window)
        size = font.Font(self.window, font=('Malgun Gothic', 10)).metrics('linespace')
        style.configure('Explorer.Treeview', rowheight=max(36, size+14), font=('Malgun Gothic', 10))
        style.configure('Explorer.TNotebook', background=BG, borderwidth=0)
        style.configure('Explorer.TNotebook.Tab', padding=(16, 9), background=PANEL, foreground=MUTED)
        style.map('Explorer.TNotebook.Tab', background=[('selected', '#30474c')], foreground=[('selected', ACCENT)])

    def button(self, parent, text, command, accent=False):
        return tk.Button(parent, text=text, command=lambda: self.safe(command), bg=ACCENT if accent else PANEL,
            fg=BG if accent else TEXT, activebackground='#b3eedb' if accent else '#35455a',
            activeforeground=BG if accent else TEXT, disabledforeground='#718297', relief='flat',
            borderwidth=0, highlightthickness=1, highlightbackground=PANEL, highlightcolor=ACCENT,
            font=('Malgun Gothic', 10), padx=12, pady=7, cursor='hand2')

    def menu_button(self, parent, text, choices):
        button = tk.Menubutton(parent, text=text, bg=PANEL, fg=TEXT, activebackground='#35455a',
            activeforeground=TEXT, relief='flat', borderwidth=0, highlightthickness=1,
            highlightbackground='#35455a', highlightcolor=ACCENT, takefocus=True,
            font=('Malgun Gothic', 10), padx=12, pady=7, cursor='hand2', direction='below')
        menu = tk.Menu(button, tearoff=False, bg=PANEL, fg=TEXT, activebackground='#345a62',
                       activeforeground=TEXT, font=('Malgun Gothic', 10))
        for label, command in choices:
            if label is None:menu.add_separator()
            else:menu.add_command(label=label, command=lambda command=command: self.safe(command))
        button.configure(menu=menu)
        button.bind('<Return>', lambda _: menu.tk_popup(button.winfo_rootx(),
            button.winfo_rooty()+button.winfo_height()))
        return button, menu

    def safe(self, command):
        try:command()
        except (ValueError, OSError, SpotifyCatalogError, RuntimeError) as error:
            self.status.set(str(error))
            messagebox.showerror('음악 사전', str(error), parent=self.window)

    def _resize(self, event):
        if event.widget is self.window and hasattr(self, 'status_label'):
            self.status_label.configure(wraplength=max(400, event.width-220))
            self.auto_status_label.configure(wraplength=max(400,event.width-220))

    def wrapping_label(self, parent, **options):
        label = ttk.Label(parent, wraplength=700, justify='left', **options)
        parent.bind('<Configure>', lambda event: label.configure(
            wraplength=max(160, event.width-40)), add='+')
        return label

    def balance_panes(self, panes):
        # Detail text remains readable, while the list gains room on wide screens.
        panes.bind('<Configure>', lambda event: panes.sashpos(0,
            event.width-min(430, max(334, int(event.width*.40)))))

    def tree(self, parent, columns):
        frame = ttk.Frame(parent)
        frame.rowconfigure(0, weight=1);frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=[col[0] for col in columns], show='headings', height=1,
                            selectmode='browse', style='Explorer.Treeview')
        for name, label, width in columns:
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=60, stretch=True)
        tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        scroll.grid(row=0, column=1, sticky='ns');tree.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew');tree.configure(xscrollcommand=horizontal.set)
        tree.tag_configure('even', background=PANEL)
        tree.tag_configure('odd', background='#192231')
        return frame, tree

    def text_panel(self, parent):
        frame = tk.Frame(parent, bg=PANEL)
        frame.rowconfigure(0, weight=1);frame.columnconfigure(0, weight=1)
        text = tk.Text(frame, bg=PANEL, fg=TEXT, font=('Malgun Gothic', 10), relief='flat',
                       wrap='word', padx=18, pady=14, width=28, height=1, cursor='arrow',
                       selectbackground='#345a62', insertbackground=TEXT, takefocus=True)
        text.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(frame, command=text.yview)
        scroll.grid(row=0, column=1, sticky='ns');text.configure(yscrollcommand=scroll.set)
        text.tag_configure('title', font=('Malgun Gothic', 15, 'bold'), spacing3=10)
        text.tag_configure('label', foreground=ACCENT, font=('Malgun Gothic', 9, 'bold'), spacing1=12, spacing3=4)
        text.tag_configure('muted', foreground=MUTED, spacing3=6)
        text.tag_configure('body', spacing3=5)
        text.tag_configure('link', foreground=ACCENT, underline=True, spacing3=6)
        text.tag_configure('active_link', background='#345a62')
        text.bind('<Key-Return>', self.activate_text_link)
        text.bind('<Tab>', lambda event:self.move_text_link(event, 1))
        text.bind('<Shift-Tab>', lambda event:self.move_text_link(event, -1))
        text.bind('<FocusIn>', lambda _:text.mark_set('insert', '1.0'))
        text.bind('<FocusOut>', lambda _:text.tag_remove('active_link', '1.0', 'end'))
        text.tag_bind('link', '<Enter>', lambda _: text.configure(cursor='hand2'))
        text.tag_bind('link', '<Leave>', lambda _: text.configure(cursor='arrow'))
        text.configure(state='disabled')
        return frame, text

    def write_panel(self, text, rows):
        for tag in text.tag_names():
            if tag.startswith('navigate_'):text.tag_delete(tag)
        text.navigation = {}
        text.configure(state='normal');text.delete('1.0', 'end')
        for row in rows:
            value, tag = row[:2]
            start = text.index('end-1c')
            if len(row) > 2:
                link_tag = 'navigate_'+str(len(text.navigation))
                text.navigation[link_tag] = row[2]
                text.tag_bind(link_tag, '<Button-1>',
                    lambda _, command=row[2]: self.safe(command))
                tag = ((tag,) if isinstance(tag, str) else tuple(tag)) + ('link', link_tag)
            if len(row) > 3:
                text.image_create('end', image=series_symbol(text, row[3], 22), align='center')
                text.insert('end', '  ', tag)
            text.insert('end', str(value)+'\n', tag)
            if len(row) > 2:text.tag_add(link_tag, start, 'end-1c')
        text.configure(state='disabled');text.yview_moveto(0)

    def series_rows(self, keys):
        return [(SERIES[key]+' 곡 보기', 'label',
                 lambda key=key:self.show_series_songs(key), key)
                if key in SERIES and key else (str(key), 'label')
                for key in dict.fromkeys(keys)] or [('분류 미확인', 'label')]

    def show_series_songs(self, key):
        self.performer_filter = None;self.live_song_filter = None
        self.selected_song = None
        self.query.set('');self.series.set(SERIES[key])
        self.song_kind.set(SONG_KINDS[''])
        self.only_favorites.set(False);self.only_playable.set(False)
        self.notebook.select(self.song_tab);self.filter_songs()
        self.status.set(SERIES[key]+'의 곡 · 검색어나 유형을 골라 더 좁혀 보세요.')

    def layout_series_buttons(self, event):
        buttons = list(self.series_buttons.values())
        cell = max(button.winfo_reqwidth() for button in buttons)+6
        columns = max(1, min(len(buttons), event.width//cell))
        if columns == getattr(self, '_series_columns', None):return
        self._series_columns = columns
        for column in range(len(buttons)):
            self.series_bar.columnconfigure(column, weight=1 if column < columns else 0,
                                            uniform='series' if column < columns else '')
        for index, button in enumerate(buttons):
            button.grid(row=index//columns, column=index%columns, sticky='ew', padx=3, pady=3)

    def update_series_buttons(self):
        for key, button in self.series_buttons.items():
            selected = self.series.get() == SERIES[key]
            button.configure(bg='#30474c' if selected else PANEL,
                fg=SERIES_COLORS[key] if selected else TEXT,
                relief='sunken' if selected else 'flat',
                highlightbackground=SERIES_COLORS[key] if selected else '#2c3849')

    def activate_text_link(self, event):
        text = event.widget
        for tag in text.tag_names('insert'):
            command = getattr(text, 'navigation', {}).get(tag)
            if command:self.safe(command);return 'break'

    def move_text_link(self, event, direction):
        text = event.widget
        links = [text.tag_ranges(tag) for tag in getattr(text, 'navigation', {})]
        candidates = [ranges for ranges in links if ranges and
            text.compare(ranges[0], '>' if direction > 0 else '<', 'insert')]
        text.tag_remove('active_link', '1.0', 'end')
        if candidates:
            first, last = candidates[0] if direction > 0 else candidates[-1]
            text.mark_set('insert', first);text.tag_add('active_link', first, last);text.see(first)
        else:
            target = text.tk_focusNext() if direction > 0 else text.tk_focusPrev()
            if target:target.focus_set()
        return 'break'

    def color_tags(self, widget, person):
        color = readable_color((person or {}).get('color'))
        if not color:return ()
        tag = 'character_' + color[1:]
        widget.tag_configure(tag, foreground=color)
        return (tag,)

    def character_row(self, widget, person, text, style='body', *, link=False):
        row = (text, (style,) + self.color_tags(widget, person))
        return row + (lambda person=person:self.show_performer_songs(person),) if link else row

    def credit_person(self, name):
        return (self.idol_lookup.get(artist_key(name)) or self.people_lookup.get(artist_key(name))
                or {'name': name, 'display': artist_label([name], 'THE IDOLM@STER'), 'kind': 'unknown'})

    def _song_page(self):
        top = ttk.Frame(self.song_tab);top.pack(fill='x', pady=(0, 10))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text='검색', foreground=MUTED).grid(row=0, column=0, padx=(0, 10))
        self.query = tk.StringVar()
        self.search = ttk.Entry(top, textvariable=self.query)
        self.search.grid(row=0, column=1, sticky='ew', padx=(0, 8))
        self.search.bind('<Return>', lambda _: self.filter_songs())
        self.query.trace_add('write', self.schedule_search)
        self.current_song_button = self.button(top, '현재 곡', self.show_current_song)
        self.current_song_button.grid(row=0, column=2, padx=(0, 8))
        ControlHint(self.current_song_button, '탐색 중인 목록에서 현재 Spotify 곡으로 돌아갑니다. 재생은 바꾸지 않습니다.')
        random, _ = self.menu_button(top, '랜덤 ▾', [('새로운 곡 발견', self.discover),
                                                    ('랜덤으로 재생', self.random_play)])
        random.grid(row=0, column=3)
        ControlHint(self.search, '곡명 · 아이돌 · 유닛 · 앨범 검색  /  Ctrl+F')
        self.series = tk.StringVar(value=SERIES[''])
        self.series_bar = ttk.Frame(self.song_tab)
        self.series_bar.pack(fill='x', pady=(0, 8))
        self.series_buttons = {}
        labels = {'': '전체', '765': '765프로', 'million': '밀리언', 'cinderella': '신데렐라',
                  'sidem': 'SideM', 'shiny': '샤이니', 'gakuen': '학마스', 'other': '기타 · 합동'}
        for index, (key, label) in enumerate(labels.items()):
            button = self.button(self.series_bar, label, lambda key=key:self.show_series_songs(key))
            button.configure(image=series_symbol(button, key, 36), compound='top', padx=8, pady=4,
                             takefocus=True)
            button.bind('<Return>', lambda _, key=key:self.safe(lambda:self.show_series_songs(key)))
            button.grid(row=0, column=index, sticky='ew', padx=3, pady=3)
            ControlHint(button, SERIES[key]+' 곡 보기')
            self.series_buttons[key] = button
        self.series_bar.bind('<Configure>', self.layout_series_buttons)
        filters = ttk.Frame(self.song_tab);filters.pack(fill='x', pady=(0, 10))
        self.song_kind = tk.StringVar(value=SONG_KINDS[''])
        self.kind_filter = ttk.Combobox(filters, values=list(SONG_KINDS.values()),
                                       textvariable=self.song_kind, state='readonly', width=14)
        self.kind_filter.pack(side='left', padx=(8, 0))
        self.kind_filter.bind('<<ComboboxSelected>>', lambda _: self.filter_songs())
        ControlHint(self.kind_filter, '노래, 연주·반주, 드라마·토크 자료를 나누어 볼 수 있습니다.')
        self.only_favorites = tk.BooleanVar()
        ttk.Checkbutton(filters, text='즐겨찾기', variable=self.only_favorites, command=self.filter_songs).pack(side='left', padx=12)
        self.only_playable = tk.BooleanVar()
        ttk.Checkbutton(filters, text='Spotify 연결한 곡', variable=self.only_playable, command=self.filter_songs).pack(side='left')
        self.hide_duplicates = tk.BooleanVar(value=self.library.hide_duplicates)
        self.duplicates_button = ttk.Checkbutton(filters, text='중복곡 제외', variable=self.hide_duplicates,
                                                command=lambda:self.safe(self.toggle_duplicates))
        self.duplicates_button.pack(side='left', padx=12)
        self.button(filters, '초기화', self.reset_filters).pack(side='right')
        results = ttk.Frame(self.song_tab);results.pack(fill='x', pady=(0, 8))
        results.columnconfigure(0, weight=1)
        self.results_label = ttk.Label(results, text='', foreground=MUTED, font=('Malgun Gothic', 9), width=1)
        self.results_label.grid(row=0, column=0, sticky='ew')
        ttk.Label(results, text='정렬', foreground=MUTED).grid(row=0, column=1, padx=(8, 6))
        self.song_sort = tk.StringVar(value=SONG_SORTS[self.library.song_sort])
        self.sort_combo = ttk.Combobox(results, values=list(SONG_SORTS.values()),
            textvariable=self.song_sort, state='readonly', width=25)
        self.sort_combo.grid(row=0, column=2, sticky='e')
        self.sort_combo.bind('<<ComboboxSelected>>', lambda _:self.safe(self.change_song_sort))
        ControlHint(self.sort_combo, '현재 검색 결과 전체를 정렬합니다. 라이브 횟수는 수록 공연 기준이며, 같은 공연은 한 번으로 셉니다.')
        panes = ttk.Panedwindow(self.song_tab, orient='horizontal');panes.pack(fill='both', expand=True)
        listing = ttk.Frame(panes)
        self.song_tree = CatalogList(listing, ('title', 'artists', 'state'),
                                     empty_text='검색 결과가 없습니다. 검색어나 필터를 바꿔 보세요.')
        self.song_tree.pack(fill='both', expand=True)
        self.song_tree.bind('<<TreeviewSelect>>', self.select_song)
        self.song_tree.bind('<Double-1>', lambda _: self.safe(self.play_selected))
        self.song_tree.bind('<<ListActivate>>', lambda _: self.safe(self.play_selected))
        paging = ttk.Frame(listing);paging.pack(side='bottom', fill='x', before=self.song_tree, pady=(8, 0))
        self.previous_page = self.button(paging, '‹ 이전', lambda: self.change_page(-1));self.previous_page.pack(side='left')
        self.page_label = ttk.Label(paging, text='', foreground=MUTED);self.page_label.pack(side='left', padx=10)
        self.next_page = self.button(paging, '다음 ›', lambda: self.change_page(1));self.next_page.pack(side='right')
        detail = ttk.Frame(panes, padding=(14, 0, 0, 0))
        frame, self.song_detail = self.text_panel(detail);frame.pack(fill='both', expand=True)
        actions = ttk.Frame(detail);actions.pack(side='bottom', fill='x', before=frame, pady=(8, 0))
        actions.columnconfigure(0, weight=1);actions.columnconfigure(1, weight=1)
        self.favorite_button = self.button(actions, '☆ 즐겨찾기', self.favorite)
        self.favorite_button.grid(row=0, column=0, sticky='ew', padx=(0, 4), pady=3)
        self.song_more, self.song_menu = self.menu_button(actions, '더보기 ▾', [
            ('Spotify 곡 연결 / 변경', self.search_spotify), ('Spotify에서 열기', self.open_spotify),
            ('로컬 음원 연결', self.show_local), (None, None), ('정보 출처 열기', self.open_song_source)])
        self.song_more.grid(row=0, column=1, sticky='ew', padx=(4, 0), pady=3)
        self.play_button = self.button(actions, 'Spotify 재생', self.play_selected, True)
        self.play_button.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(4, 0))
        panes.add(listing, weight=3);panes.add(detail, weight=2)
        self.balance_panes(panes)
        self.show_song_detail()

    def schedule_search(self, *_):
        if self.search_after:self.window.after_cancel(self.search_after)
        self.search_after = self.window.after(200, self.filter_songs)

    def song_sort_key(self):
        return next((key for key, label in SONG_SORTS.items() if label == self.song_sort.get()), 'default')

    def change_song_sort(self):
        try:self.library.set_song_sort(self.song_sort_key())
        except (OSError, ValueError):
            self.song_sort.set(SONG_SORTS[self.library.song_sort])
            raise
        self.selected_song = None
        self.filter_songs()
        self.status.set(self.song_sort.get()+' · 검색 결과 전체를 정렬했습니다.'+
            (' 수록 공연 기준이며, 기록 없음은 미공연을 뜻하지 않습니다.'
             if self.song_sort_key().startswith('live_') else ''))

    def live_counts_for_sort(self):
        if (self.song_live_count_source is not self.live_catalog.events
                or self.song_live_count_catalog is not self.catalog.songs):
            self.song_live_counts = {song['id']:self.live_catalog.count_for_song(song)
                                     for song in self.catalog.songs}
            self.song_live_count_source = self.live_catalog.events
            self.song_live_count_catalog = self.catalog.songs
        return self.song_live_counts

    def filter_songs(self,preserve_selected=False):
        if self.search_after:self.window.after_cancel(self.search_after)
        self.search_after = None
        selected_id = (self.selected_song or {}).get('id')
        series = next((key for key, label in SERIES.items() if label == self.series.get()), '')
        self.update_series_buttons()
        kind = next((key for key, label in SONG_KINDS.items() if label == self.song_kind.get()), '')
        songs = self.catalog.songs
        if self.performer_filter:songs = related_songs(songs, self.performer_filter,
                                                     known_names=self.known_people_names)
        if self.live_song_filter is not None:
            songs = [song for song in songs if song['id'] in self.live_song_filter['ids']]
        matches = [song for song in songs if
            (not series or series in song.get('series', [])) and matches_query(song, self.query.get())
            and (not kind or song.get('kind', song.get('type', 'unknown')) == kind)
            and (not self.only_favorites.get() or self.library.is_favorite(song['id']))
            and (not self.only_playable.get() or self.library.linked_track(song['id']))]
        self.duplicate_groups = {}
        self.filtered = matches
        if self.hide_duplicates.get():
            self.filtered = []
            for group in group_duplicate_songs(matches):
                def preferred(song):
                    track = self.library.linked_track(song['id'])
                    return (bool(preserve_selected and song['id']!=selected_id),
                            not self.library.is_favorite(song['id']),
                            not bool(track and track.get('is_playable') is not False),
                            not bool(self.local.links.get(song['id'])),
                            song['id'] != (self.selected_song or {}).get('id'))
                representative = min(group, key=preferred)
                self.filtered.append(representative)
                self.duplicate_groups[representative['id']] = group
                if any(song['id'] == selected_id for song in group):
                    selected_id = representative['id']
        self.hidden_duplicate_count = len(matches) - len(self.filtered)
        order = self.song_sort_key()
        counts = self.live_counts_for_sort() if order.startswith('live_') else None
        self.filtered = sort_songs(self.filtered, order, counts)
        selected_index = next((i for i, song in enumerate(self.filtered) if song['id'] == selected_id), None)
        self.selected_song = self.filtered[selected_index] if selected_index is not None else None
        self.page = selected_index // PAGE_SIZE if selected_index is not None else 0
        self.render_page();self.show_song_detail()
        titles=len({search_key(song['title']) for song in self.catalog.songs})
        self.count_label.configure(text=f'제목 {titles:,}  ·  녹음 {len(self.catalog.songs):,}  ·  유닛·아이돌 {len(self.people):,}')

    def reset_filters(self):
        self.query.set('');self.series.set(SERIES['']);self.only_favorites.set(False);self.only_playable.set(False)
        self.song_kind.set(SONG_KINDS[''])
        self.performer_filter = None;self.live_song_filter = None;self.filter_songs()

    def toggle_duplicates(self):
        try:self.library.set_hide_duplicates(self.hide_duplicates.get())
        except (OSError, ValueError):
            self.hide_duplicates.set(self.library.hide_duplicates)
            raise
        self.filter_songs()
        self.status.set('같은 제목 묶기 · 가창자·앨범·길이에 관계없이 제목으로 묶습니다. 상세 정보에서 녹음을 선택하세요.'
                        if self.hide_duplicates.get() else '모든 곡·녹음 자료를 표시합니다.')

    def render_page(self):
        self.song_tree.delete(*self.song_tree.get_children())
        start = self.page * PAGE_SIZE
        for index, song in enumerate(self.filtered[start:start+PAGE_SIZE]):
            state = availability(self.library.linked_track(song['id']))
            if self.local.for_song(song['id']):state += ' · 로컬'
            title = ('★ ' if self.library.is_favorite(song['id']) else '') + song['title']
            versions = self.duplicate_groups.get(song['id'], [])
            if len(versions) > 1:title += f'  · {len(versions)}개 녹음'
            preview, roster = self.song_preview(song)
            kind = song.get('kind', song.get('type', 'unknown'))
            subtitle = (artist_label(self.display_song_credits(song), 'THE IDOLM@STER')
                        or ('가창자 미확인' if kind == 'song' else '아티스트 미확인'))
            if roster:subtitle += ' · 유닛 멤버 미리보기'
            elif song.get('album'):subtitle += ' · '+song['album']
            badge = 'Spotify' if state.startswith('Spotify') else '재생 제한' if state.startswith('재생 제한') else '미연결'
            if self.local.for_song(song['id']):badge += ' · 로컬'
            order = self.song_sort_key()
            if order.startswith('release_'):
                stamp = song_release_date(song)
                badge = ('발매 '+stamp if stamp else '발매일 미확인')+' · '+badge
            elif order.startswith('live_'):
                count = self.song_live_counts.get(song['id'], 0)
                badge = (f'라이브 {count:,}회' if count else '가창 기록 없음')+' · '+badge
            elif order.startswith('duration_'):
                badge = duration_label(song.get('duration'))+' · '+badge
            self.song_tree.insert('', 'end', iid=song['id'],
                values=(title, self.song_artists_label(song), state), subtitle=subtitle,
                preview=preview, badge=badge, series=song.get('series', []))
        total = len(self.filtered)
        self.page_label.configure(text=f'{start+1 if total else 0}–{min(start+PAGE_SIZE,total)} / {total:,}')
        context = (self.performer_filter or {}).get('display') or '음악 자료'
        if self.series.get() != SERIES['']:context += ' · '+self.series.get()
        if self.live_song_filter is not None:context = '라이브 곡목 · '+self.live_song_filter['title']
        duplicates = f' · 같은 제목의 녹음 {self.hidden_duplicate_count:,}개 묶음' if self.hidden_duplicate_count else ''
        note = '수록 공연 기준' if self.song_sort_key().startswith('live_') else '캐릭터 / CV 미리보기'
        self.results_label.configure(text=f'{total:,}개 결과{duplicates}  ·  {context}  ·  {note}')
        self.previous_page.configure(state='normal' if self.page else 'disabled')
        self.next_page.configure(state='normal' if start+PAGE_SIZE < total else 'disabled')
        if self.selected_song and self.song_tree.exists(self.selected_song['id']):
            self.song_tree.selection_set(self.selected_song['id'])
            self.song_tree.see(self.selected_song['id'])
        elif total:
            self.selected_song = self.filtered[start]
            self.song_tree.selection_set(self.filtered[start]['id'])
            self.song_tree.see(self.filtered[start]['id'])
            self.show_song_detail()
        else:
            self.selected_song = None;self.show_song_detail()

    def change_page(self, direction):
        maximum = max(0, (len(self.filtered)-1)//PAGE_SIZE)
        self.page = max(0, min(maximum, self.page+direction));self.render_page()

    def select_song(self, _=None):
        selected = self.song_tree.selection()
        if selected:
            self.selected_song = next((song for song in self.filtered if song['id'] == selected[0]), None)
            self.show_song_detail()

    def display_song_credits(self, song):
        """Fold an explicitly credited character's known CV into its display.

        Compare whole names only; group membership does not establish a song's
        character credit. Original artist values remain playback/search identity.
        """
        credits = tuple(song.get('artists') or ())
        if credits in self.song_display_credits_cache:return self.song_display_credits_cache[credits]
        tokens = [(credit, [credit[start:end].strip() for start, end in
                   _artist_ranges(credit, self.known_people_names)]) for credit in credits]
        voices = set()
        for _, names in tokens:
            for name in names:
                person = self.idol_lookup.get(artist_key(name))
                if not person:continue
                explicit = re.fullmatch(r'(.*?)\s*[（(]\s*CV\s*[.:：]?\s*(.*?)\s*[)）]\s*', name, re.I)
                actors = person.get('voice_actors', [])
                if not explicit and len({name_key(actor['name']) for actor in actors}) > 1:continue
                for actor in actors:
                    aliases = {name_key(value) for value in (actor.get('name'), actor.get('display')) if value}
                    if explicit and name_key(explicit[2]) not in aliases:continue
                    voices.update(aliases)
        result = []
        for credit, names in tokens:
            retained = [name for name in names if name_key(name) not in voices
                        or artist_key(name) in self.idol_lookup or artist_key(name) in self.group_lookup]
            if len(retained)==len(names):result.append(credit)
            elif retained:result.append(', '.join(retained))
        self.song_display_credits_cache[credits] = tuple(result)
        return tuple(result)

    def song_artists_label(self, song):
        artists=with_character_voices(self.display_song_credits(song), 'THE IDOLM@STER',
                                     update_path=self.directory/'group_credits.json')
        return artist_label(artists,'THE IDOLM@STER') or '가창자 미확인'

    def preview_person(self, person, credit=''):
        color = person.get('color') or ''
        readable = readable_color(color)
        name, voice = person.get('display') or person['name'], voice_label(person)
        explicit = re.fullmatch(r'(.*?)\s*[（(]\s*CV\s*[.:：]?\s*(.*?)\s*[)）]\s*', credit, re.I)
        if explicit:
            voice = artist_label([explicit[2]], 'THE IDOLM@STER')
            if person.get('kind') == 'unknown':name = artist_label([explicit[1]], 'THE IDOLM@STER')
        name_only = character_name_only(person['name'])
        return {'name': name, 'voice': '' if name_only else voice, 'hide_voice': name_only,
                'color': color if readable else '', 'text_color': readable or MUTED,
                'kind': person.get('kind', 'unknown'), 'id': person.get('id'),
                'command': lambda person=person:self.safe(lambda:self.show_performer_songs(person))}

    def song_preview(self, song):
        """Resolve display previews separately from unchanged recording credits."""
        key = (song['id'], tuple(song.get('artists') or ()))
        if key in self.song_preview_cache:return self.song_preview_cache[key]
        people, groups, seen, credits = [], [], set(), {}
        for credit in self.display_song_credits(song):
            for start, end in _artist_ranges(credit, self.known_people_names):
                name = credit[start:end].strip()
                if not name:continue
                person = self.idol_lookup.get(artist_key(name)) or self.people_lookup.get(artist_key(name))
                if person and person.get('kind') == 'group' and not is_project_credit(person):
                    if person not in groups:groups.append(person)
                else:
                    person = person or self.credit_person(name)
                    identity = person.get('id') or artist_key(name)
                    if identity not in seen:
                        people.append(person);seen.add(identity);credits[id(person)] = name
        roster = False
        for group in groups:
            members = self.group_members(group)
            # Explicit character credits must not expand to an entire unit.
            if any(member.get('id') in seen for member in members):continue
            roster = True
            for person in members or [group]:
                identity = person.get('id') or artist_key(person['name'])
                if identity not in seen:people.append(person);seen.add(identity)
        result = ([{**self.preview_person(person, credits.get(id(person), '')),
                    'roster':id(person) not in credits} for person in people], roster)
        self.song_preview_cache[key] = result
        return result

    def show_song_detail(self):
        song = self.selected_song
        for button in (self.favorite_button, self.play_button, self.song_more):
            button.configure(state='normal' if song else 'disabled')
        if not song:
            self.write_panel(self.song_detail, [('곡을 찾아보세요', 'title'),
                ('시리즈를 고르거나 곡·아이돌·유닛 이름을 검색하세요.', 'body'),
                ('검색 결과가 없으면 필터를 초기화해 보세요.', 'muted')]);return
        track = self.library.linked_track(song['id'])
        self.favorite_button.configure(text='★ 즐겨찾는 곡' if self.library.is_favorite(song['id']) else '☆ 즐겨찾기')
        self.play_button.configure(state='normal' if not self.busy else 'disabled',
            text='Spotify 재생' if track else 'Spotify에서 찾아 재생')
        rows = self.series_rows(song.get('series', [])) + [(song['title'], 'title')]
        aliases = alias_information(song)
        if aliases['korean_titles']:rows.append(('한국어 제목: '+' · '.join(aliases['korean_titles']),'body'))
        if aliases['readings']:rows.append(('한글 검색 표기: '+aliases['readings'][0],'muted'))
        else:
            readings=title_readings(song['title'])
            if readings and re.search('[가-힣]',readings[0]):rows.append(('한글 읽기 (자동): '+readings[0],'muted'))
        if aliases['search_translations']:
            rows.append(('검색용 번역 (비공식): '+aliases['search_translations'][0],'muted'))
        for url in aliases['source_urls']:
            rows.append(('일본어 원제·버전 출처','muted',lambda url=url:open_url(url)))
        artist_heading=len(rows)
        kind = song.get('kind', song.get('type', 'unknown'))
        rows.append(('가창자' if kind == 'song' else '아티스트 크레딧', 'label'))
        for credit in self.display_song_credits(song):
            for start, end in _artist_ranges(credit, self.known_people_names):
                name = credit[start:end].strip()
                if not name:continue
                display=with_character_voices((name,), 'THE IDOLM@STER',
                                             update_path=self.directory/'group_credits.json')
                rows.append(self.character_row(self.song_detail, self.credit_person(name),
                    artist_label(display, 'THE IDOLM@STER'), link=True))
        if song.get('artist_source_kind')=='official_label':
            previous=song.get('artist_credit_original')
            if not isinstance(previous,str) or not previous.strip():
                originals=song.get('artists_original')
                previous=' · '.join(name for name in originals if isinstance(name,str) and name.strip()) if isinstance(originals,list) else ''
            if previous:rows.append(('이전 자료의 표기: '+previous,'muted'))
            sources=song.get('artist_source_urls')
            for url in sources if isinstance(sources,list) else []:
                try:
                    source=urlparse(url) if isinstance(url,str) else None
                    if not source or source.scheme!='https' or not source.hostname or source.username or source.password:continue
                except ValueError:continue
                rows.append(('가창자 확인 근거 · '+source.hostname,'body',lambda url=url:open_url(url)))
        preview, _ = self.song_preview(song)
        explicit = {person['id'] for person in preview if person['kind']=='idol' and not person['roster']}
        groups, projects = [], []
        for credit in song.get('artists', []):
            for start, end in _artist_ranges(credit, self.known_people_names):
                group = self.people_lookup.get(artist_key(credit[start:end]))
                if not group or group.get('kind') != 'group' or group in groups:continue
                if is_project_credit(group):
                    if group not in projects:projects.append(group)
                    continue
                if any(member.get('id') in explicit for member in self.group_members(group)):continue
                groups.append(group)
        if projects:
            rows[artist_heading] = ('아티스트 크레딧', 'label')
            rows += [(project['display']+' · 프로젝트 크레딧 · 전체 학생의 가창을 뜻하지 않습니다.', 'muted')
                     for project in projects]
        for group in groups:
            rows += [self.character_row(self.song_detail, group,
                group['display']+' · 그룹 멤버', 'label', link=True)]
            rows += self.group_member_rows(self.song_detail, group)
        if groups:
            rows.append(('그룹 명단입니다. 이 녹음의 개별 가창 크레딧은 위 가창자 정보와 구분됩니다.'
                         if kind == 'song' else '그룹 명단입니다. 이 녹음의 개별 참여 크레딧은 위 아티스트 정보와 구분됩니다.', 'muted'))
        if song.get('album'):rows += [('수록 앨범', 'label'), (song['album'], 'body')]
        if song.get('duration'):rows += [('길이  '+duration_label(song['duration']), 'muted')]
        release_date = song.get('first_release_date') or song.get('release_date')
        if release_date:rows += [(('최초 발매  ' if song.get('first_release_date') else '발매  ')+str(release_date), 'muted')]
        releases = release_editions(song)
        if releases:
            rows.append((f'발매 정보 · {len(releases)}개 판본', 'body', lambda song=song:self.show_releases(song)))
        duplicates = self.duplicate_groups.get(song['id'], [])
        if len(duplicates) > 1:
            rows += [(f'같은 제목 · {len(duplicates)}개 녹음', 'label'),
                     ('가창자·앨범을 선택해 재생할 녹음을 바꿀 수 있습니다.', 'muted')]
            for item in duplicates:
                label = ('● ' if item['id'] == song['id'] else '○ ') + self.song_artists_label(item)
                label += ' · ' + (item.get('album') or '앨범 미확인') + ' · ' + duration_label(item.get('duration'))
                if self.library.linked_track(item['id']):label += ' · Spotify 연결'
                rows.append((label, 'body', lambda item=item:self.choose_song(item)))
        kind = song.get('kind', song.get('type'))
        if kind in ('spoken', 'instrumental'):rows += [(SONG_KINDS[kind], 'muted')]
        elif kind=='unknown':rows += [('분류 미확인', 'muted')]
        kind_sources = song.get('kind_source_urls')
        for url in kind_sources if isinstance(kind_sources, list) else []:
            try:
                source = urlparse(url) if isinstance(url, str) else None
                if not source or source.scheme != 'https' or not source.hostname or source.username or source.password:continue
            except ValueError:continue
            rows.append(('유형 분류 근거 · '+source.hostname, 'body', lambda url=url:open_url(url)))
        events = self.live_catalog.for_song(song)
        rows += [(f'라이브 공연 기록 · {len(events):,}회', 'label')]
        if events:
            rows += [('출처의 곡명·시리즈 기준이며 현재 음원과 같은 녹음 버전이라는 뜻은 아닙니다.', 'muted')]
            rows += [(event['date'] + '  ·  ' + event['title'], 'body',
                      lambda event=event:self.show_live(event)) for event in events]
            rows += [('공연명을 선택하면 곡목과 출처를 볼 수 있습니다. 수록 범위 안의 일부 기록입니다.', 'muted')]
        else:rows += [('현재 수록 자료에 공연 기록이 없습니다. 미공연을 뜻하지 않습니다.', 'muted')]
        recordings = song.get('recordings') or []
        if len(recordings) > 1:
            rows += [('수록 / 녹음 자료', 'label')]
            for recording in recordings:
                if isinstance(recording, dict):
                    rows.append((recording.get('title') or recording.get('album') or '제목 미확인', 'body'))
        rows += [('재생 연결', 'label'), (availability(track), 'body')]
        if track:
            rows += [(track.get('title') or '', 'body'), (track.get('album') or '', 'muted')]
            if track.get('checked_at'):rows.append(('확인: '+str(track['checked_at']), 'muted'))
            if track.get('restriction'):rows.append(('제한: '+str(track['restriction']), 'muted'))
        elif kind in ('instrumental', 'spoken'):
            rows.append(('더보기의 Spotify 곡 연결 / 변경에서 이 녹음의 후보를 확인할 수 있습니다.', 'muted'))
        else:rows.append(('Spotify에서 일치하는 녹음을 자동으로 찾습니다. 모호한 후보는 더보기에서 직접 확인할 수 있습니다.', 'muted'))
        linked = self.local.for_song(song['id'])
        if linked:
            rows += [('연결한 로컬 음원', 'label')]
            rows += [(item['title'] + (' · 파일 없음' if not item.get('exists', True) else ''), 'body') for item in linked]
        if not song.get('artists'):
            rows.append(('가창자 자료가 아직 없습니다. 유닛 멤버를 곡의 가창자로 추정하지 않습니다.'
                         if kind == 'song' else '이 녹음의 아티스트 크레딧이 아직 확인되지 않았습니다.', 'muted'))
        rows += [('자료 안내', 'label'), ('동명곡·솔로·게임·라이브 버전은 재생과 가사 싱크가 다를 수 있습니다.', 'muted')]
        self.write_panel(self.song_detail, rows)
        self.song_menu.entryconfigure('정보 출처 열기',
            state='normal' if song.get('source_url') or song.get('url') else 'disabled')

    def favorite(self):
        if self.selected_song:
            self.library.toggle_favorite(self.selected_song['id']);self.filter_songs();self.show_song_detail()

    def show_releases(self, song):
        if self.closed:return
        if self.release_window and self.release_window.winfo_exists():self.release_window.destroy()
        window = tk.Toplevel(self.window);self.release_window = window
        window.title('발매 정보 · '+song['title']);window.configure(bg=BG);window.attributes('-topmost', False)
        window.geometry('720x540');window.minsize(480, 340)
        window.bind('<Escape>', lambda _:window.destroy())
        body = ttk.Frame(window, padding=14);body.pack(fill='both', expand=True)
        frame, text = self.text_panel(body);frame.pack(fill='both', expand=True)
        rows = [(song['title'], 'title'), ('이 녹음에 연결된 발매 자료입니다. 매체는 발매판 전체 구성입니다.', 'muted')]
        for index, release in enumerate(release_editions(song), 1):
            rows.append((f"{index}. {release.get('title') or '제목 미확인'}", 'label'))
            formats = release.get('formats') or []
            labels = ['디지털' if value == 'Digital Media' else value for value in formats]
            rows.append(('발매일  '+(release.get('date') or '미확인')+'  ·  '+(' + '.join(labels) or '매체 미확인'), 'body'))
            if release.get('date_original'):
                rows.append(('날짜 정정 · 이전 자료의 표기 '+release['date_original'], 'muted'))
            sources = {}
            for field, label in (('url', '발매 정보'), ('date_source_url', '날짜 근거'), ('format_source_url', '매체 근거')):
                url = release.get(field)
                if not url:continue
                try:
                    parsed = urlparse(url)
                    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:continue
                except ValueError:continue
                source = sources.setdefault(url, {'host': parsed.hostname, 'labels': []})
                source['labels'].append(label)
            for url, source in sources.items():
                rows.append((' · '.join(source['labels'])+' · '+source['host'], 'body', lambda url=url:open_url(url)))
        self.write_panel(text, rows)
        self.button(body, '닫기', window.destroy).pack(anchor='e', pady=(10, 0))

    def choose_song(self, song):
        if not song:return
        self.selected_song = song
        if (self.hide_duplicates.get() and not any(item['id'] == song['id'] for item in self.filtered)
                and any(any(item['id'] == song['id'] for item in group)
                        for group in self.duplicate_groups.values())):
            self.filter_songs(preserve_selected=True)
        index = next((i for i, item in enumerate(self.filtered) if item['id'] == song['id']), None)
        if index is not None:
            self.page = index // PAGE_SIZE;self.render_page();self.song_tree.see(song['id'])
        self.show_song_detail()

    def discover(self):
        songs = self.discovery_songs()
        song = self.library.choose(songs)
        if not song:self.status.set('조건에 맞는 곡이 없어요. 검색이나 필터를 바꿔 보세요.');return
        self.library.remember(song['id']);self.choose_song(song)
        self.status.set('오늘의 발견 · '+song['title']+'  —  연결한 음원으로 재생하거나 Spotify에서 찾아보세요.')

    def random_play(self):
        songs = self.discovery_songs()
        linked = [song for song in songs if self.library.linked_track(song['id']) and
                  self.library.linked_track(song['id']).get('is_playable') is not False]
        song = self.library.choose(linked or songs)
        if not song:self.status.set('조건에 맞는 곡이 없습니다. 필터를 바꿔 보세요.');return
        self.choose_song(song)
        if linked:self.play_selected()
        else:
            self.status.set('랜덤으로 고른 곡의 Spotify 녹음을 자동으로 찾습니다.')
            self.search_spotify(play_after=True)

    def discovery_songs(self):
        if self.song_kind.get() != SONG_KINDS['']:return self.filtered
        return [song for song in self.filtered
                if song.get('kind', song.get('type')) not in ('spoken', 'instrumental', 'unknown')]

    def play_selected(self):
        song = self.selected_song
        if not song:return
        track = self.library.linked_track(song['id'])
        if not track:self.search_spotify(play_after=True);return
        def work():
            if self.cancel.is_set():return False
            return self.spotify.play(track, cancel=self.cancel)
        def done(result):
            if result:
                if (self.library.linked_track(song['id']) or {}).get('id') == track['id']:
                    from datetime import datetime, timezone
                    self.library.link_spotify(song['id'], {**track, 'is_playable': True,
                        'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
                self.library.remember(song['id']);self.status.set('Spotify에서 재생 중 · '+song['title'])
            else:self.status.set('곡 화면은 열었지만 재생을 확인하지 못했습니다. Spotify에서 이 곡의 ▶를 눌러주세요.')
            self.filter_songs(preserve_selected=True)
        self.run_job('Spotify에서 재생하는 중…', work, done)

    def open_spotify(self):
        if not self.selected_song:return
        track = self.library.linked_track(self.selected_song['id'])
        if track:self.spotify.open(track)
        else:open_url('https://open.spotify.com/search/' + quote(self.selected_song['title'], safe=''))

    def open_song_source(self):
        if self.selected_song:open_url(self.selected_song.get('source_url') or self.selected_song.get('url') or '')

    def search_spotify(self, play_after=False):
        if not self.selected_song:return
        song = self.selected_song.copy()
        def done(tracks):
            if play_after:
                from catalog_autolink import automatic_candidate
                candidate=automatic_candidate(song,tracks,self.people)
                if candidate:
                    # This explicit play request may reconnect a previously unlinked song.
                    self.library.link_spotify(song['id'],candidate)
                    self.choose_song(song);self.play_selected();return
            self.candidates_dialog(song,tracks,play_after=play_after)
        if not self.spotify.connected:
            done(cached_candidates(self.directory,song));return
        def work():
            tracks = self.spotify.search(song['title'], (song.get('artists') or [''])[0], song.get('album') or '', cancel=self.cancel)
            return rank_candidates(song, tracks)
        self.run_job('Spotify에서 같은 곡의 녹음을 찾는 중…', work,
                     done)

    def candidates_dialog(self, song, tracks, play_after=False):
        if self.candidate_window and self.candidate_window.winfo_exists():self.candidate_window.destroy()
        window = tk.Toplevel(self.window);self.candidate_window = window
        window.title('Spotify 곡 연결 · '+song['title']);window.configure(bg=BG);window.attributes('-topmost', False)
        window.geometry('940x560');window.minsize(720, 450)
        window.protocol('WM_DELETE_WINDOW', window.destroy)
        window.bind('<Escape>',lambda _:window.destroy())
        body = ttk.Frame(window, padding=18);body.pack(fill='both', expand=True)
        self.wrapping_label(body, text=song['title'], font=('Malgun Gothic', 14, 'bold')).pack(anchor='w')
        self.wrapping_label(body, text=('녹음을 연결하면 재생합니다. ' if play_after else '')+
                            '가창자 · 앨범 · 버전을 확인하세요. Spotify 곡 링크를 붙여 넣어도 됩니다.',
                            foreground=MUTED).pack(anchor='w', pady=(5, 12))
        frame, tree = self.tree(body, [('title','Spotify 제목',280), ('artist','가창자',220), ('album','앨범',220), ('time','길이',65)])
        frame.pack(fill='both', expand=True)
        for index, track in enumerate(tracks):
            tree.insert('', 'end', iid=str(index), values=(track.get('title',''), artists_label(track), track.get('album',''), duration_label(track.get('duration'))))
        info = tk.StringVar(value='후보가 없습니다. 검색어를 바꾸거나 Spotify 곡 주소를 붙여 넣으세요.' if not tracks else '연결할 녹음을 선택하세요.')
        self.wrapping_label(body, textvariable=info, foreground=MUTED).pack(anchor='w', pady=8)
        def selected(_=None):
            if tree.selection():
                track = tracks[int(tree.selection()[0])]
                info.set(('버전 차이 주의 · ' if track.get('version_conflict') else '') + availability(track) + ' · '+ ' / '.join(track.get('match_reasons') or []))
        tree.bind('<<TreeviewSelect>>', selected)
        url = tk.StringVar()
        ttk.Label(body, text='또는 Spotify 곡 링크 붙여 넣기').pack(anchor='w')
        ttk.Entry(body, textvariable=url).pack(fill='x', pady=5)
        actions = ttk.Frame(body);actions.pack(fill='x', pady=(8,0))
        actions.columnconfigure(0,weight=1);actions.columnconfigure(1,weight=1)
        def save(track):
            if self.closed or not window.winfo_exists():return
            self.library.link_spotify(song['id'], track)
            if window.winfo_exists():window.destroy()
            self.filter_songs();self.status.set('Spotify 녹음을 연결했습니다 · '+song['title'])
            if play_after:self.choose_song(song);self.play_selected()
        def connect():
            if url.get().strip():
                track_id = parse_track_id(url.get().strip())
                if not track_id:raise ValueError('Spotify 곡 링크 또는 22자리 곡 ID를 입력하세요.')
                current=self.spotify.current_track()
                track=current if current and current['id']==track_id else {
                    'id':track_id,'title':'직접 입력한 Spotify 곡','artists':[],'is_playable':None}
                self.confirm_track(window,song,track,save);return
            if not tree.selection():raise ValueError('연결할 녹음을 선택하거나 곡 링크를 입력하세요.')
            track = tracks[int(tree.selection()[0])]
            self.confirm_track(window, song, track, save)
        self.button(actions, '연결하고 재생' if play_after else '선택한 곡 연결', connect,
                    True).grid(row=0,column=1,sticky='ew',padx=(5,0),pady=3)
        self.button(actions, '연결 해제', lambda: self.remove_spotify_link(song, window)).grid(row=1,column=1,sticky='ew',padx=(5,0),pady=3)
        self.button(actions, 'Spotify 검색 열기', lambda: open_url('https://open.spotify.com/search/'+quote(song['title'],safe=''))).grid(row=1,column=0,sticky='ew',padx=(0,5),pady=3)
        self.button(actions, '지금 재생 중인 곡 연결', lambda:self.connect_current(window,song,save)).grid(row=0,column=0,sticky='ew',padx=(0,5),pady=3)
        if not self.spotify.connected:
            self.button(body,'Spotify 로그인',self.connect_spotify).pack(anchor='w',pady=(8,0))

    def connect_current(self,window,song,save):
        track=self.spotify.current_track()
        if not track:raise ValueError('현재 곡의 Spotify 링크를 아직 확인하지 못했습니다. 곡 링크를 직접 붙여 넣거나 잠시 후 다시 시도하세요.')
        self.confirm_track(window,song,track,save)

    def confirm_track(self, window, song, track, save):
        if not window.winfo_exists():return
        text = f"{song['title']}에 아래 녹음을 연결할까요?\n\n{track.get('title','')}\n{artists_label(track)}\n{track.get('album','')}\n{duration_label(track.get('duration'))}"
        if messagebox.askyesno('녹음 버전 확인', text, parent=window) and window.winfo_exists():save(track)

    def remove_spotify_link(self, song, window):
        self.library.unlink_spotify(song['id']);window.destroy();self.filter_songs()

    def _person_page(self):
        top = ttk.Frame(self.person_tab);top.pack(fill='x', pady=(0,12));top.columnconfigure(0,weight=1)
        self.person_query = tk.StringVar()
        self.person_search = ttk.Entry(top, textvariable=self.person_query)
        self.person_search.grid(row=0,column=0,sticky='ew',padx=(0,8))
        self.person_query.trace_add('write', lambda *_: self.filter_people())
        self.person_kind = tk.StringVar(value='유닛')
        combo = ttk.Combobox(top, textvariable=self.person_kind, values=['유닛','아이돌','전체'], state='readonly', width=10)
        combo.grid(row=0,column=1);combo.bind('<<ComboboxSelected>>',lambda _:self.filter_people())
        ControlHint(self.person_search, '아이돌 · 유닛 · 캐릭터 · 성우 이름 검색')
        panes = ttk.Panedwindow(self.person_tab,orient='horizontal');panes.pack(fill='both',expand=True)
        self.person_tree = CatalogList(panes, ('name', 'cv', 'series'),
                                      empty_text='다른 이름이나 별칭을 검색해 보세요.')
        self.person_tree.bind('<<TreeviewSelect>>',self.select_person)
        self.person_tree.bind('<<ListActivate>>', lambda _:self.safe(self.person_songs))
        self.person_tree.bind('<Double-1>', lambda _:self.safe(self.person_songs))
        detail = ttk.Frame(panes,padding=(14,0,0,0))
        panel,self.person_detail = self.text_panel(detail);panel.pack(fill='both',expand=True)
        self.person_songs_button = self.button(detail,'참여한 곡 보기',self.person_songs,True)
        self.person_songs_button.pack(fill='x',pady=(8,0))
        self.person_source_button = self.button(detail,'공식 · 관계 자료 열기',self.person_source)
        self.person_source_button.pack(fill='x',pady=(6,0))
        panes.add(self.person_tree,weight=3);panes.add(detail,weight=2)
        self.balance_panes(panes)

    def index_people(self):
        self.people_lookup = build_performer_lookup(self.people)
        self.idol_lookup = build_performer_lookup(self.people, kind='idol')
        self.group_lookup = build_performer_lookup(self.people, kind='group')
        self.group_rosters = {}
        self.song_preview_cache = {}
        self.song_display_credits_cache = {}
        self.known_people_names = set(self.people_lookup) | set(self.idol_lookup) | set(self.group_lookup)

    def group_members(self, group):
        if group['id'] not in self.group_rosters:
            self.group_rosters[group['id']] = resolve_group_members(group, self.people,
                idol_lookup=self.idol_lookup, group_lookup=self.group_lookup)
        return self.group_rosters[group['id']]

    def group_member_rows(self, widget, group):
        members = self.group_members(group)
        if not members:return [('확인된 멤버 자료가 아직 없습니다.', 'muted')]
        return [self.character_row(widget, member,
                member.get('display') or member['name'], link=True)
                if member.get('kind') != 'idol' or character_name_only(member['name']) else
                self.character_row(widget, member,
                    (member.get('display') or member['name'])+'  · CV: '+(voice_label(member) or '미확인'), link=True)
                for member in members]

    def filter_people(self):
        from explorer_library import search_key
        if not hasattr(self,'person_tree'):return
        query = search_key(self.person_query.get());kind = self.person_kind.get()
        selected = (self.selected_person or {}).get('id')
        self.person_tree.delete(*self.person_tree.get_children())
        for person in self.people:
            if kind=='유닛' and person['kind']!='group' or kind=='아이돌' and person['kind']!='idol':continue
            names = [person['name'],person['display'],SERIES.get(person['series'],'')]+person.get('aliases',[])
            related = [person] + (self.group_members(person) if query and person['kind']=='group' else [])
            for member in related:
                names += [member['name'], member.get('display','')] + member.get('aliases',[])
                names += [name for actor in member.get('voice_actors', []) for name in (actor['name'], actor.get('display', ''))]
            if query and query not in search_key(' '.join(names)):continue
            members = self.group_members(person) if person['kind']=='group' else [person]
            project = person['kind']=='group' and is_project_credit(person)
            self.person_tree.insert('','end',iid=person['id'],
                values=(person['display'], ('소속 아이돌 · 성우 보기' if project else
                        '멤버 · 성우 보기' if person.get('members') else '멤버 미확인')
                        if person['kind']=='group' else voice_label(person) or '—', SERIES.get(person['series'],'기타')),
                subtitle=SERIES.get(person['series'],'기타')+
                    (f' · {len(members)}명 · '+('소속 아이돌' if project else '유닛 명단')
                     if person['kind']=='group' else ' · 캐릭터'),
                preview=[self.preview_person(member) for member in members],
                badge='프로젝트' if project else '유닛' if person['kind']=='group' else '아이돌',
                series=[person['series']],
                title_command=lambda person=person:self.safe(lambda:self.show_performer_songs(person)))
        children = self.person_tree.get_children()
        if children:
            self.person_tree.selection_set(selected if selected in children else children[0]);self.select_person()
        else:
            self.selected_person=None
            self.person_songs_button.configure(state='disabled');self.person_source_button.configure(state='disabled')
            self.write_panel(self.person_detail,[('검색 결과가 없습니다','title'),('다른 이름이나 별칭을 입력해 보세요.','muted')])

    def select_person(self,_=None):
        selected=self.person_tree.selection()
        if not selected:return
        person=self.selected_person=next(item for item in self.people if item['id']==selected[0])
        project = person['kind']=='group' and is_project_credit(person)
        self.person_songs_button.configure(state='normal')
        self.person_source_button.configure(state='normal' if person.get('source_urls') else 'disabled')
        rows=self.series_rows([person['series']])+[
              self.character_row(self.person_detail,person,person['display'],'title',link=True),(person['name'],'muted')]
        if person['kind']=='idol' and not character_name_only(person['name']):
            rows += [('성우','label'), self.character_row(self.person_detail, person,
                voice_label(person) or '확인된 성우 자료가 아직 없습니다.')]
        if person.get('aliases'):rows += [('다른 표기','label'),(' · '.join(person['aliases']),'body')]
        if person['kind']=='group':
            rows += [('소속 아이돌' if project else '멤버','label')]
            rows += self.group_member_rows(self.person_detail, person)
            rows.append(('프로젝트 소속 목록입니다. 곡별 가창 정보와 별개입니다.' if project else
                         '유닛 명단과 개별 곡의 실제 가창자는 다를 수 있습니다.','muted'))
        songs=related_songs(self.catalog.songs,person,known_names=self.known_people_names)
        rows += [(('프로젝트 크레딧이 있는' if project else '참여 기록이 있는')+f' 곡 · 녹음 {len(songs):,}개','label')]
        rows += [(song['title'],'body',lambda song=song:self.show_catalog_song(song)) for song in songs[:20]]
        if not songs:rows.append(('현재 수집 자료에서 가창자 연결을 확인하지 못했습니다.','muted'))
        self.write_panel(self.person_detail,rows)

    def person_songs(self):
        if self.selected_person:self.show_performer_songs(self.selected_person)

    def show_performer_songs(self, person):
        self.performer_filter=person
        self.selected_song = None
        self.live_song_filter = None
        self.query.set('');self.series.set(SERIES['']);self.only_favorites.set(False);self.only_playable.set(False)
        self.song_kind.set(SONG_KINDS[''])
        self.notebook.select(self.song_tab);self.filter_songs()
        label = '프로젝트 크레딧' if is_project_credit(person) else '가창 기록'
        self.status.set((person.get('display') or person['name'])+'의 '+label+' · 전체로 돌아가려면 필터 초기화를 누르세요.')

    def person_source(self):
        if self.selected_person:
            urls=self.selected_person.get('source_urls') or []
            if urls:open_url(urls[0])

    def show_catalog_song(self, song):
        self.reset_filters();self.notebook.select(self.song_tab);self.choose_song(song)

    def _live_page(self):
        self.index_lives()
        top = ttk.Frame(self.live_tab);top.pack(fill='x', pady=(0, 10));top.columnconfigure(0, weight=1)
        self.live_query = tk.StringVar()
        self.live_search = ttk.Entry(top, textvariable=self.live_query)
        self.live_search.grid(row=0, column=0, sticky='ew', padx=(0, 8))
        ControlHint(self.live_search, '공연명 · 곡목 · 시리즈 · 장소 · 출처 검색  /  Ctrl+F')
        self.live_query.trace_add('write', self.schedule_live_search)
        self.live_year = tk.StringVar(value='전체 연도')
        years = sorted({event['date'][:4] for event in self.live_catalog.events}, reverse=True)
        self.live_year_combo = ttk.Combobox(top, textvariable=self.live_year,
            values=['전체 연도']+years, state='readonly', width=11)
        self.live_year_combo.grid(row=0, column=1)
        self.live_year_combo.bind('<<ComboboxSelected>>', lambda _:self.filter_lives())
        self.live_update_button = self.button(top, '라이브 자료 업데이트', self.update_live_catalog)
        self.live_update_button.grid(row=0, column=2, padx=(8,0))
        self.live_scope = tk.StringVar(value=self.live_scope_text())
        self.wrapping_label(self.live_tab, textvariable=self.live_scope,
                            foreground=MUTED).pack(anchor='w', pady=(0, 10))
        self.live_results_label = ttk.Label(self.live_tab, text='', foreground=MUTED, font=('Malgun Gothic',9))
        self.live_results_label.pack(anchor='w', fill='x', pady=(0,8))
        panes = ttk.Panedwindow(self.live_tab, orient='horizontal');panes.pack(fill='both', expand=True)
        listing = ttk.Frame(panes)
        frame, self.live_tree = self.tree(listing,[('date','공연일',105),('title','라이브 / 공연',280),('source','출처',110)])
        frame.pack(fill='both',expand=True)
        self.live_tree.column('date', width=105, minwidth=105, stretch=False)
        self.live_tree.column('title', minwidth=180)
        self.live_tree.column('source', width=110, minwidth=100, stretch=False)
        self.live_tree.bind('<<TreeviewSelect>>', self.select_live)
        paging = ttk.Frame(listing);paging.pack(fill='x', pady=(8,0))
        self.live_previous_page = self.button(paging, '‹ 이전', lambda:self.change_live_page(-1))
        self.live_previous_page.pack(side='left')
        self.live_page_label = ttk.Label(paging,text='',foreground=MUTED);self.live_page_label.pack(side='left',padx=10)
        self.live_next_page = self.button(paging, '다음 ›', lambda:self.change_live_page(1))
        self.live_next_page.pack(side='right')
        detail = ttk.Frame(panes, padding=(14,0,0,0))
        panel, self.live_detail = self.text_panel(detail);panel.pack(fill='both', expand=True)
        actions = ttk.Frame(detail);actions.pack(side='bottom',fill='x',before=panel,pady=(8,0))
        actions.columnconfigure(0,weight=1);actions.columnconfigure(1,weight=1)
        self.live_source_button = self.button(actions, '공연 기록 출처 열기', self.open_live_source)
        self.live_source_button.grid(row=0,column=0,columnspan=2,sticky='ew',pady=(0,4))
        self.live_date_source_button = self.button(actions,'공연일 근거',lambda:self.open_live_source('date_source_url'))
        self.live_date_source_button.grid(row=1,column=0,sticky='ew',padx=(0,4))
        self.live_setlist_source_button = self.button(actions,'곡목 출처',lambda:self.open_live_source('setlist_source_url'))
        self.live_setlist_source_button.grid(row=1,column=1,sticky='ew',padx=(4,0))
        panes.add(listing, weight=3);panes.add(detail, weight=2);self.balance_panes(panes)
        self.selected_live = None

    @staticmethod
    def live_source_kind(event):
        kind = event.get('source_kind')
        if kind == 'fan_database':return '팬 기록'
        if kind == 'official' or event.get('setlist_coverage','').startswith('official_'):return '공식 자료'
        return '출처 자료'

    @staticmethod
    def live_source_name(event):
        return event.get('source_name') or urlparse(event.get('source_url','')).hostname or '출처 미확인'

    def index_lives(self):
        from explorer_library import search_key
        self.live_index_source = self.live_catalog.events
        self.live_order = sorted(self.live_catalog.events, key=lambda event:(event['date'],event['id']), reverse=True)
        self.live_events_by_id = {event['id']:event for event in self.live_order}
        self.live_search_index = {}
        for event in self.live_order:
            fields = [event['title'],event['date'],series_label(event),event.get('venue',''),
                      self.live_source_name(event),self.live_source_kind(event)]
            fields += event.get('series_names',[])
            fields += [value for item in event.get('songs',[]) for value in
                       (item['title'],item.get('performance_title',''),item.get('song_title',''))]
            fields += [alias for item in event.get('songs',[]) for alias in
                       search_aliases({'title':item.get('song_title') or item['title'],
                                       'series':item.get('series') or event.get('series',[])})]
            self.live_search_index[event['id']] = search_key(' '.join(fields))
        self.live_years = sorted({event['date'][:4] for event in self.live_order}, reverse=True)
        self.live_setlist_count = sum(len(event.get('songs',[])) for event in self.live_order)
        if hasattr(self,'live_year_combo'):
            self.live_year_combo.configure(values=['전체 연도']+self.live_years)
            if self.live_year.get() not in ['전체 연도']+self.live_years:self.live_year.set('전체 연도')
        if hasattr(self,'live_scope'):self.live_scope.set(self.live_scope_text())

    def live_scope_text(self):
        dates = [event['date'] for event in self.live_order]
        period = f'{min(dates)} ~ {max(dates)}' if dates else '공연일 미확인'
        stamp = getattr(self.live_catalog,'generated_at','') or getattr(self.live_catalog,'verified_at','') or '미확인'
        text = (f'수록 공연일 {period}  ·  공연 {len(dates):,}회 · 곡목 {self.live_setlist_count:,}개\n'
                f'자료 갱신 {stamp}  ·  출처별 일부 기록이며 누락은 미공연을 뜻하지 않습니다.')
        report = getattr(self.live_catalog,'collection_report',{})
        if report:
            text += (f"\n수집 대상 {len(report.get('index_urls',[])):,}개 주소 · "
                     f"수집 성공 {len(report.get('successful_urls',[])):,}개 · 실패 {len(report.get('failures',[])):,}개")
            excluded=report.get('non_event_pages',len(report.get('skipped_urls',[])))
            if excluded:text += f' · 곡 집계 문서 {excluded:,}개 제외'
        return text

    def schedule_live_search(self,*_):
        if self.live_search_after:self.window.after_cancel(self.live_search_after)
        self.live_search_after = self.window.after(200,self.filter_lives)

    def filter_lives(self,target_id=None):
        from explorer_library import search_key
        if not hasattr(self,'live_tree'):return
        if self.live_search_after:self.window.after_cancel(self.live_search_after)
        self.live_search_after = None
        if self.live_index_source is not self.live_catalog.events:self.index_lives()
        selected = target_id or (getattr(self,'selected_live',None) or {}).get('id')
        words = [search_key(word) for word in self.live_query.get().split()]
        year = self.live_year.get()
        self.live_filtered = [event for event in self.live_order
            if (year=='전체 연도' or event['date'].startswith(year))
            and all(word in self.live_search_index[event['id']] for word in words)]
        selected_index = next((index for index,event in enumerate(self.live_filtered) if event['id']==selected),0)
        self.live_page = selected_index // PAGE_SIZE
        self.render_lives()

    def render_lives(self):
        self.live_tree.delete(*self.live_tree.get_children())
        selected = (getattr(self, 'selected_live', None) or {}).get('id')
        start = self.live_page * PAGE_SIZE
        for event in self.live_filtered[start:start+PAGE_SIZE]:
            self.live_tree.insert('', 'end', iid=event['id'], values=(event['date'],event['title'],self.live_source_kind(event)))
        total = len(self.live_filtered)
        self.live_results_label.configure(text=f'{total:,}회 검색 결과  ·  전체 수록 {len(self.live_order):,}회  ·  최신 공연일부터')
        self.live_page_label.configure(text=f'{start+1 if total else 0}–{min(start+PAGE_SIZE,total)} / {total:,}')
        self.live_previous_page.configure(state='normal' if self.live_page else 'disabled')
        self.live_next_page.configure(state='normal' if start+PAGE_SIZE < total else 'disabled')
        children = self.live_tree.get_children()
        if children:
            self.live_tree.selection_set(selected if selected in children else children[0]);self.select_live()
        else:
            self.selected_live = None
            for button in (self.live_source_button,self.live_date_source_button,self.live_setlist_source_button):
                button.configure(state='disabled')
            self.write_panel(self.live_detail,[('수록 자료에 검색 결과가 없습니다','title'),
                ('다른 검색어나 연도를 선택해 보세요. 기록이 없다는 것은 미공연을 뜻하지 않습니다.','muted')])

    def change_live_page(self,direction):
        maximum = max(0,(len(self.live_filtered)-1)//PAGE_SIZE)
        self.live_page = max(0,min(maximum,self.live_page+direction));self.render_lives()

    def select_live(self, _=None):
        selection = self.live_tree.selection()
        if not selection:return
        event = self.selected_live = self.live_events_by_id[selection[0]]
        rows = [(event['date']+'  ·  '+series_label(event),'label'),(event['title'],'title')]
        if event.get('series_names'):rows.append((' · '.join(event['series_names']),'muted'))
        if event.get('venue'):rows.append((event['venue'],'body'))
        rows += [(self.live_source_kind(event)+'  ·  '+self.live_source_name(event),'muted'),
                 (f"출처의 공연 곡목 · {len(event.get('songs',[])):,}개",'label'),
                 ('곡목을 선택하면 같은 제목·시리즈의 음원 후보를 봅니다. 공연 녹음 버전은 별도 확인이 필요합니다.', 'muted')]
        if event['id'] not in self.live_match_cache:
            self.live_match_cache[event['id']] = self.live_catalog.songs_for_event(event, self.catalog.songs)
        for entry in self.live_match_cache[event['id']]:
            item, matches = entry['performance'], entry['matches']
            label = f"{entry['position']}. {item.get('performance_title') or item['title']}"
            if item.get('performance_kind') == 'dance_without_vocals':label += '  · 댄스 · 가창 없음'
            if matches:
                rows.append((label+f'  · 음원 후보 {len(matches)}개', 'body',
                    lambda item=item, matches=matches:self.show_live_song(event, item, matches)))
            else:rows.append((label+'  · 사전 자료 미확인', 'muted'))
            if item.get('artists'):
                credit_label='공연 출연' if item.get('performance_kind')=='dance_without_vocals' else '공연 가창'
                rows.append((credit_label+' · '+artist_label(item['artists'],'THE IDOLM@STER'),'muted'))
            if item.get('note'):rows.append((item['note'],'muted'))
        rows += [('자료 안내','label'),
                 ('출처가 제공한 공연·영상 수록곡 기준입니다. 전체 공연 구성과 다를 수 있습니다.','muted'),
                 ('수록 범위 안의 일부 기록입니다. 누락은 미공연을 뜻하지 않습니다.', 'muted')]
        if event.get('note'):rows.append((event['note'],'muted'))
        for note in event.get('setlist_notes',[]):rows.append((note,'muted'))
        stamp = event.get('retrieved_at') or getattr(self.live_catalog,'generated_at','') or getattr(self.live_catalog,'verified_at','')
        if stamp:rows.append(('자료 수집·갱신일  '+stamp,'muted'))
        if event.get('source_policy_url'):
            rows.append(('출처의 자료 안내','body',lambda:open_url(event['source_policy_url'])))
        for source in event.get('official_sources',[]):
            url = source.get('url') or source.get('source_url')
            if url:rows.append(('공식 참고: '+(source.get('name') or source.get('title') or urlparse(url).hostname),
                               'body',lambda url=url:open_url(url)))
        self.write_panel(self.live_detail,rows)
        for button,field in ((self.live_source_button,'source_url'),(self.live_date_source_button,'date_source_url'),
                             (self.live_setlist_source_button,'setlist_source_url')):
            button.configure(state='normal' if event.get(field) else 'disabled')

    def show_live(self, event):
        self.live_query.set('');self.live_year.set('전체 연도')
        self.selected_live = event;self.filter_lives(target_id=event['id'])
        self.notebook.select(self.live_tab)
        if self.live_tree.exists(event['id']):
            self.live_tree.selection_set(event['id']);self.live_tree.see(event['id']);self.select_live()

    def show_live_song(self, event, performance, matches):
        self.selected_song = None;self.performer_filter = None
        self.query.set('');self.series.set(SERIES['']);self.only_favorites.set(False);self.only_playable.set(False)
        self.song_kind.set(SONG_KINDS[''])
        self.live_song_filter = {'event':event['id'], 'title':performance.get('song_title') or performance['title'],
                                 'ids':{song['id'] for song in matches}}
        self.notebook.select(self.song_tab);self.filter_songs()
        self.status.set('공연 곡목과 제목·시리즈가 같은 음원 후보입니다. 공연 녹음 버전은 별도로 확인하세요.')

    def open_live_source(self,field='source_url'):
        if self.selected_live and self.selected_live.get(field):open_url(self.selected_live[field])

    def _local_page(self):
        top=ttk.Frame(self.local_tab);top.pack(fill='x',pady=(0,12))
        self.button(top,'음악 폴더 추가 / 다시 읽기',self.scan_folder,True).pack(side='left')
        self.button(top,'Spotify 로컬 파일 안내',self.local_help).pack(side='left',padx=8)
        ttk.Label(self.local_tab,text='선택한 폴더의 제목·가창자·앨범을 읽습니다. 음원 파일은 수정하거나 전송하지 않습니다.',foreground=MUTED,wraplength=1000).pack(anchor='w',pady=(0,10))
        self.local_target=tk.StringVar(value='먼저 곡 탐색에서 연결할 곡을 선택하세요.')
        ttk.Label(self.local_tab,textvariable=self.local_target,foreground=ACCENT,wraplength=1000).pack(anchor='w',pady=(0,8))
        frame,self.local_tree=self.tree(self.local_tab,[('title','로컬 음원',280),('artists','가창자',220),('album','앨범',230),('duration','길이',65),('state','파일 상태',110)])
        frame.pack(fill='both',expand=True)
        self.local_tree.bind('<<TreeviewSelect>>',self.select_local)
        self.local_path=tk.StringVar()
        ttk.Label(self.local_tab,textvariable=self.local_path,foreground=MUTED,wraplength=1000).pack(anchor='w',pady=8)
        actions=ttk.Frame(self.local_tab);actions.pack(fill='x')
        self.button(actions,'선택한 곡에 연결',self.link_local,True).pack(side='left')
        self.button(actions,'이 음원으로 곡 검색',self.search_local_title).pack(side='left',padx=8)
        self.button(actions,'기본 음악 앱으로 열기',self.open_local).pack(side='left')
        self.button(actions,'현재 곡 연결 해제',self.unlink_local).pack(side='right')

    def refresh_local(self):
        self.local_tree.delete(*self.local_tree.get_children())
        candidates={item['id'] for item in self.local.candidates(self.selected_song)} if self.selected_song else set()
        for item in self.local.files:
            exists=Path(item['path']).is_file()
            state='파일 없음' if not exists else '연결 후보' if item['id'] in candidates else '파일 있음'
            self.local_tree.insert('','end',iid=item['id'],values=(item['title'],artists_label(item),item.get('album',''),duration_label(item.get('duration')),state))
        self.local_target.set('연결할 곡: '+self.selected_song['title'] if self.selected_song else '먼저 곡 탐색에서 연결할 곡을 선택하세요.')
        if candidates:
            first=next(iter(candidates))
            if self.local_tree.exists(first):self.local_tree.selection_set(first);self.local_tree.see(first)

    def show_local(self):
        self.notebook.select(self.local_tab);self.refresh_local()

    def tab_changed(self,_=None):
        if self.notebook.select()==str(self.local_tab):self.refresh_local()
        elif self.notebook.select()==str(self.settings_tab):self.refresh_settings()

    def scan_folder(self):
        if self.busy:self.status.set('현재 작업이 끝난 후 폴더를 추가해 주세요.');return
        folder=filedialog.askdirectory(parent=self.window,title='음원이 들어 있는 폴더 선택')
        if not folder:return
        def done(stats):
            self.refresh_local()
            self.status.set('취소했습니다. 기존 목록을 유지합니다.' if stats.get('cancelled') else
                f"로컬 음악 {len(self.local.files):,}개 · 이번 폴더 {stats.get('scanned',0):,}개 검사 · 읽기 오류 {stats.get('errors',0)}개" +
                (' · 일부만 읽었습니다. 더 작은 폴더를 선택해 주세요.' if stats.get('truncated') else ''))
        self.run_job('로컬 음악을 읽는 중…',lambda:self.local.scan(folder,progress=self.report_progress,cancel=self.cancel),done)

    def select_local(self,_=None):
        item=self.local_selection()
        if item:self.local_path.set(item['path'])

    def local_selection(self):
        selected=self.local_tree.selection()
        return next((item for item in self.local.files if selected and item['id']==selected[0]),None)

    def link_local(self):
        item=self.local_selection()
        if not self.selected_song or not item:raise ValueError('곡 탐색의 곡과 연결할 로컬 음원을 모두 선택하세요.')
        self.local.link(self.selected_song['id'],item['id']);self.filter_songs();self.refresh_local()
        self.status.set('로컬 음원을 연결했습니다. Spotify 등록은 로컬 파일 안내를 확인하세요.')

    def unlink_local(self):
        if self.selected_song:
            self.local.unlink(self.selected_song['id']);self.filter_songs();self.refresh_local()
            self.status.set('현재 곡의 로컬 연결을 해제했습니다. 음원 파일은 유지됩니다.')

    def open_local(self):
        item=self.local_selection()
        if item:self.local.open_file(item['id'])

    def search_local_title(self):
        item=self.local_selection()
        if item:
            self.reset_filters();self.query.set(item['title']);self.notebook.select(self.song_tab);self.filter_songs()

    def local_help(self):
        window=tk.Toplevel(self.window);window.title('Spotify에 로컬 음악 연결');window.configure(bg=BG);window.attributes('-topmost',False)
        body=ttk.Frame(window,padding=24);body.pack(fill='both',expand=True)
        ttk.Label(body,text='내 파일을 Spotify에서도 듣기',font=('Malgun Gothic',15,'bold')).pack(anchor='w')
        text=('1. Spotify 데스크톱 앱의 설정을 엽니다.\n\n2. 로컬 파일 표시를 켜고, 음악이 있는 폴더를 소스로 추가합니다.\n\n'
              '3. Spotify의 로컬 파일 목록에서 곡을 선택하고 원하는 플레이리스트에 넣습니다.\n\n'
              '이 사전에서 곡과 파일을 연결하면 경로를 기억합니다. Spotify에 자동 업로드되거나 자동 등록되는 것은 아닙니다.\n'
              '다른 기기에서도 해당 파일을 사용할 수 있어야 합니다.')
        ttk.Label(body,text=text,wraplength=600,justify='left').pack(anchor='w',pady=18)
        self.button(body,'Spotify 공식 안내 열기',lambda:open_url('https://support.spotify.com/article/local-files/'),True).pack(anchor='w')

    def _settings_page(self):
        canvas=tk.Canvas(self.settings_tab,bg=BG,highlightthickness=0)
        scrollbar=ttk.Scrollbar(self.settings_tab,command=canvas.yview);scrollbar.pack(side='right',fill='y')
        canvas.pack(side='left',fill='both',expand=True);canvas.configure(yscrollcommand=scrollbar.set)
        body=ttk.Frame(canvas,padding=(6,16));item=canvas.create_window((0,0),window=body,anchor='nw')
        body.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(item,width=event.width))
        account=ttk.LabelFrame(body,text='Spotify 연결',padding=18);account.pack(fill='x',pady=(0,14))
        self.account_status=tk.StringVar()
        self.wrapping_label(account,textvariable=self.account_status,foreground=ACCENT,font=('Malgun Gothic',11,'bold')).pack(anchor='w')
        self.wrapping_label(account,text='Spotify에 한 번 로그인하면 가사와 곡 찾기에 같은 연결을 사용합니다. 이미 로그인했다면 그대로 이용하세요.',foreground=MUTED).pack(anchor='w',pady=(6,10))
        actions=ttk.Frame(account);actions.pack(fill='x',pady=(10,0))
        self.login_button=self.button(actions,'Spotify 로그인',self.connect_spotify,True)
        self.login_button.pack(side='left')
        self.button(actions,'연결 설정 열기',self.app.open_connection).pack(side='left',padx=8)
        self.button(actions,'상태 새로고침',self.refresh_settings).pack(side='right')
        self.wrapping_label(account,text='재생할 곡을 Spotify에 연결하면 해당 곡을 엽니다. 자동 재생되지 않으면 Spotify의 재생 버튼을 눌러주세요.',foreground=MUTED).pack(anchor='w',pady=(10,0))
        update=ttk.LabelFrame(body,text='음악 · 인물 자료 갱신',padding=18);update.pack(fill='x',pady=(0,14))
        self.update_status=tk.StringVar()
        self.wrapping_label(update,textvariable=self.update_status,foreground=ACCENT).pack(anchor='w')
        self.wrapping_label(update,text='공개 곡·녹음 자료와 캐릭터·성우·색상 자료를 순서대로 확인합니다. 자료량에 따라 몇 분 걸릴 수 있습니다.\n오류·취소 시 현재 저장된 자료를 유지하며, 즐겨찾기와 Spotify·로컬 연결은 별도 보관합니다.',foreground=MUTED).pack(anchor='w',pady=10)
        self.button(update,'음악 · 인물 자료 업데이트',self.update_catalog,True).pack(anchor='w')
        self.source_status=tk.StringVar()
        self.wrapping_label(update,textvariable=self.source_status).pack(anchor='w',pady=(12,0))
        live_update=ttk.LabelFrame(body,text='라이브 공연 자료 갱신',padding=18);live_update.pack(fill='x',pady=(0,14))
        self.live_update_status=tk.StringVar()
        self.wrapping_label(live_update,textvariable=self.live_update_status,foreground=ACCENT).pack(anchor='w')
        self.wrapping_label(live_update,text='공개 공연·곡목 기록을 다시 확인합니다. 공식 자료와 팬 데이터베이스 기록은 출처별로 표시합니다.\n음악 자료 갱신과 별도이며, 오류·취소 시 현재 저장된 자료를 유지합니다.',
                            foreground=MUTED).pack(anchor='w',pady=10)
        self.live_settings_update_button=self.button(live_update,'라이브 자료 업데이트',self.update_live_catalog,True)
        self.live_settings_update_button.pack(anchor='w')
        self.wrapping_label(body,text='수록 범위는 출처의 공개 자료에 따릅니다. 미확인 정보는 빈칸으로 남기며, 전곡·최신 발매 전체 수록을 보장하지 않습니다.',foreground=MUTED).pack(anchor='w')
        self.window.bind('<MouseWheel>',lambda event:canvas.yview_scroll(-int(event.delta/120),'units')
                         if self.notebook.select()==str(self.settings_tab) else None,add='+')

    def show_settings(self,message=''):
        self.notebook.select(self.settings_tab)
        if message:self.status.set(message)

    def refresh_settings(self):
        self.account_status.set('Spotify 연결됨 · 가사와 곡 찾기에 함께 사용 중' if self.spotify.connected else 'Spotify에 로그인해 곡 후보를 찾아보세요.')
        self.login_button.configure(text='연결 상태 보기' if self.spotify.connected else 'Spotify 로그인')
        self.update_status.set(f"{len(self.catalog.songs):,}개 곡·녹음 자료  ·  자료 생성: {getattr(self.catalog,'generated_at','') or '미확인'}")
        sources=getattr(self.catalog,'sources',[]) or []
        lines=[]
        for source in sources:
            if isinstance(source,dict):lines.append(str(source.get('name') or source.get('title') or source.get('url') or source.get('id') or '공개 자료'))
        self.source_status.set('자료 출처: '+' · '.join(lines))
        self.live_update_status.set(self.live_scope_text())

    def connect_spotify(self):
        if self.busy:self.status.set('현재 작업을 마치거나 취소한 뒤 Spotify를 연결해 주세요.');return
        if self.spotify.connected:
            self.app.open_connection()
            return
        self.app.connect_spotify()
        self.status.set('열린 Spotify 창에서 한 번 로그인하세요. 기존 가사 연결에도 함께 적용됩니다.')
        self.refresh_settings()

    def update_catalog(self):
        if self.busy:self.status.set('현재 작업이 끝난 뒤 다시 시도해 주세요.');return
        def work():
            try:stats=refresh_catalog(self.directory/'song_catalog.json',progress=self.report_progress,cancel=self.cancel)
            except (OSError,ValueError) as error:stats={'updated':False,'errors':[str(error)],'message':str(error)}
            if self.cancel.is_set() or stats.get('cancelled'):return stats
            try:people=refresh_people(self.directory/'imas_db_people.json',progress=self.report_progress,cancel=self.cancel)
            except (OSError,ValueError) as error:people={'updated':False,'errors':[str(error)],'message':str(error)}
            return {**stats,'people':people}
        def done(stats):
            self.music_update_active=False
            self.catalog.reload()
            if self.auto_links:self.auto_links.update(self.catalog.songs)
            self.live_match_cache.clear()
            self.people = performers(external=getattr(self.catalog, 'performers', []),
                                     database_path=self.directory/'imas_db_people.json')
            self.index_people()
            if self.auto_links:self.auto_links.people=self.people
            self.filter_songs();self.filter_people();self.refresh_settings()
            if stats.get('cancelled'):self.status.set('음악 자료 업데이트를 취소했습니다.')
            elif not stats.get('updated'):self.status.set('음악 자료 갱신 실패 · 현재 저장된 자료 유지'+
                (' · '+stats['message'] if stats.get('message') else ''))
            else:self.status.set(f'음악 자료를 갱신했습니다 · {len(self.catalog.songs):,}개' + (' · 일부 출처 실패, 해당 자료는 이전 내용 유지' if stats.get('errors') else ''))
            people=stats.get('people')
            if people:
                if people.get('updated'):message=f"인물 자료 갱신 · 캐릭터 {people.get('characters',len(self.people)):,}명"
                elif people.get('cancelled'):message='인물 자료 갱신을 취소했습니다.'
                else:message='인물 자료 갱신 실패 · 현재 저장된 자료 유지'+(' · '+people['message'] if people.get('message') else '')
                self.status.set(self.status.get()+'  /  '+message)
            if stats.get('updated') or (people and people.get('updated')):
                try:
                    save_group_credits(self.people, self.directory/'group_credits.json')
                    if hasattr(self.app,'update_track_title'):self.app.update_track_title()
                except (OSError,ValueError):
                    self.status.set(self.status.get()+' · 가사창 멤버 자료 저장 실패, 이전 자료 유지')
        self.music_update_active=True
        self.run_job('음악 · 인물 자료를 업데이트하는 중…',work,done)

    def reload_live_records(self):
        self.live_catalog.reload()
        self.live_match_cache.clear()
        self.song_live_count_source = None
        self.index_lives();self.filter_lives();self.refresh_settings()
        if self.song_sort_key().startswith('live_'):self.filter_songs(preserve_selected=True)
        else:self.show_song_detail()

    def update_live_catalog(self):
        if self.busy:self.status.set('현재 작업이 끝난 뒤 다시 시도해 주세요.');return
        def work():
            from live_catalog_update import refresh_live_catalog
            return refresh_live_catalog(self.directory/'live_catalog.json',
                                        progress=self.report_progress,cancel=self.cancel)
        def done(stats):
            self.live_update_active=False;self.reload_live_records()
            if stats.get('cancelled'):
                self.status.set('라이브 자료 업데이트를 취소했습니다. 현재 저장된 자료를 표시합니다.')
            elif not stats.get('updated'):
                self.status.set(stats.get('message') or '라이브 자료를 갱신하지 못해 현재 저장된 자료를 유지합니다.')
            else:
                self.status.set(f'라이브 자료를 갱신했습니다 · 공연 {len(self.live_order):,}회 · 곡목 {self.live_setlist_count:,}개'+
                    (f" · 일부 기록 수집 실패 {len(stats['errors']):,}건, 수록 범위를 확인하세요." if stats.get('errors') else ''))
        self.live_update_active=True
        self.run_job('라이브 공연 자료를 업데이트하는 중…',work,done)

    def report_progress(self,value):
        self.events.put(('progress',value))

    def run_job(self,label,work,done):
        if self.busy:self.status.set('현재 작업이 끝난 뒤 다시 시도해 주세요.');return
        self.busy=True;self.cancel=threading.Event()
        self.auto_cancel.set()
        self.status.set(label);self.progress.grid();self.progress.start(16)
        self.cancel_button.configure(state='normal');self.show_song_detail()
        def run():
            try:
                self.auto_done.wait(2)
                self.events.put(('done',done,work()))
            except Exception as error:self.events.put(('error',error))
        threading.Thread(target=run,name='music-explorer',daemon=True).start()

    def poll(self):
        if self.closed:return
        try:
            while True:
                event=self.events.get_nowait()
                if event[0] in ('auto_links','auto_link_error'):
                    self.safe(lambda:self.receive_auto_links(event));continue
                if event[0]=='progress':
                    value=event[1]
                    if isinstance(value,dict):
                        value=value.get('message') or f"자료 확인 중 · {value.get('scanned', value.get('count',''))}"
                    self.status.set(str(value));continue
                self.busy=False;self.progress.stop();self.progress.grid_remove();self.cancel_button.configure(state='disabled')
                if self.cancel.is_set():
                    self.catalog.reload();self.live_match_cache.clear();self.refresh_local();self.refresh_settings()
                    if self.live_update_active:self.reload_live_records()
                    self.live_update_active=False
                    if self.music_update_active:
                        self.people=performers(external=getattr(self.catalog,'performers',[]),
                                               database_path=self.directory/'imas_db_people.json')
                        if self.auto_links:
                            self.auto_links.update(self.catalog.songs);self.auto_links.people=self.people
                        self.index_people();self.filter_people();self.filter_songs()
                    self.music_update_active=False
                    self.status.set('작업을 취소했습니다.')
                elif event[0]=='error':
                    self.live_update_active=False;self.music_update_active=False;self.status.set(str(event[1]))
                else:self.safe(lambda:event[1](event[2]))
                self.show_song_detail()
        except queue.Empty:pass
        self.poll_id=self.window.after(100,self.poll)

    def cancel_job(self):
        self.cancel.set();self.status.set('작업을 취소하는 중입니다. 진행 중인 요청이 끝나면 멈춥니다.')

    def focus_search(self,_=None):
        current = self.notebook.select()
        search = self.person_search if current == str(self.person_tab) else self.live_search if current == str(self.live_tab) else self.search
        if search is self.search:self.notebook.select(self.song_tab)
        search.focus_set();search.selection_range(0,'end');return 'break'

    def close(self):
        if self.closed:return
        self.closed=True;self.cancel.set()
        self.auto_cancel.set()
        if hasattr(self,'auto_id'):self.window.after_cancel(self.auto_id)
        self.spotify.close()
        self.progress.stop()
        if self.search_after:self.window.after_cancel(self.search_after)
        if self.live_search_after:self.window.after_cancel(self.live_search_after)
        if hasattr(self,'poll_id'):self.window.after_cancel(self.poll_id)
        self.app.explorer_dialog=None
        self.window.destroy()
