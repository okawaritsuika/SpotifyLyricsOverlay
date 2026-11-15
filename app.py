"""Japanese / reading / Korean desktop lyrics with user-controlled timing."""
from pathlib import Path
import sys
from app_paths import resource_dir, default_data_dir, seed_data, frozen, webview2_available

BASE = resource_dir()
if (BASE / '.deps').is_dir():
    sys.path.insert(0, str(BASE / '.deps'))

import argparse
from base64 import b64encode
from copy import copy
from dataclasses import dataclass, replace
from functools import partial
import hashlib
from io import BytesIO
import math
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace
import webbrowser

from lyrics import Line, Song, import_folder, match_song, match_recording, normalized
from lyric_match import match_lyrics,is_short_recording
from media import Playback, SpotifyMonitor
from spotify_lifecycle import (SpotifyLifecycleMonitor, configure_startup, ensure_watcher,
                               mark_spotify_exit, clear_spotify_exit)
from storage import Store, atomic_json, read_json, validate_times
from audio_job import AudioJob, ready as audio_ready
from audio_capture_cache import CAPTURE_NAME, discard_private_capture, find_capture
from audio_install import AudioInstaller, AudioInstallDialog
from audio_progress import AudioProgress, TERMINAL
from audio_sync import merge_candidates
from timing_quality import cleared_rows,timing_origins,saved_rows
from native_timing import NativeTiming
from auto_audio import AutoAnalyzer
from recording_context import RecordingContexts
from appearance import AppearanceDialog, Backdrop, DEFAULT_BACKGROUND, DEFAULT_BACKGROUND_OPACITY, DEFAULT_TRANSPARENT
from appearance import styles_from_settings, valid_color, compact_from_settings
from web_dialog import WebLyricsDialog
from manual_dialog import ManualLyricsDialog
from manual_sync_dialog import ManualSyncDialog
from batch_translation import BatchTranslationStore, is_latin_only, preserves_latin_phrases
from web_lyrics import JA, fetch_lyrics, make_song, source_id
import imas_lyrics
from extension_inbox import ExtensionInbox
from lyric_annotations import LyricAnnotations,LyricTextEdits,match_annotations,match_verified_annotations,readings_for_display
from user_annotations import SINGER_KEYS, SingerEdits, SingerRoster, UserAnnotations
from wiki_part_store import save_wiki_parts
from saved_annotations import match_saved_annotations
from reader_bridge import ReaderBridge
from internal_reader import InternalReader
from connection_dialog import ConnectionDialog
from live_lyrics import LiveLyrics
from live_timing import LiveTimingStore
from lyric_cache import LyricCache
from lyric_follow import nearby_line,native_line_mapping,is_vocalise,limit_vocalise
from screen_lyrics import ScreenLyricsMonitor
from background_lyrics import BackgroundLyrics,PlaybackLyricMerge,mapped_times,prefer_spotify_timing,previous_row_mapping
from lyric_merge import make_snapshot,restore_cached_translations,snapshot_from_direct
from lyricsplus_job import ProviderSyncJob
from lyricsplus_provider import fetch_lyricsplus
from alsong_provider import fetch_alsong
from sync import (active_line, align_lines, estimate_times, get_public_timing, parse_lrc,
                  edit_manual_time, restore_auto_time, blend_live_index, lyric_advance_ms)
from subtitle import (SubtitleLine, outline_padding, place_side_preview, scaled_outline,
                      side_preview_width, text_style_options)
from artist_names import artist_label
from explorer_library import artist_key
from group_credits import group_member_lines, performer_options, with_character_voices
from artist_colors import character_color_spans
from artist_header import ArtistHeader
from lyric_performers import LyricPerformers,LYRIC_GUTTER,MARKER_HEIGHT,short_performer_name
from windows import (GlobalHotkeys, SingleInstance, WindowActivation, monitor_work_area,
                     set_click_through, window_position, work_area)
from shortcuts import ACTIONS,shortcut_label,validate_bindings
from shortcut_dialog import ShortcutDialog
from update_dialog import UpdateController, UpdateDialog
from ui_theme import BG, PANEL, TEXT, MUTED, ACCENT, BORDER, HOVER, SELECTED
from ui_theme import configure_theme, ControlHint, transport_icon
from tray_icon import WindowsTray
from desktop_workspace import DesktopWorkspace
from overlay_fonts import register_bundled_fonts

APP_ICON = BASE / 'resources' / 'app.ico'
OVERLAY_BG, OVERLAY_PANEL, OVERLAY_TEXT, OVERLAY_MUTED, OVERLAY_ACCENT = (
    '#10151f', '#1d2635', '#f4f7fc', '#afbbcf', '#99e3c8')
TRANSPARENT = '#010203'
MP3_SYNC = 'MP3 음원 싱크'
PLAYBACK_STEP_KEYS = {'q': -5, 'w': -2, 'e': 1, 'r': 5}
LYRIC_PADDING = {'reading': (0, 2), 'original': (2, 2), 'korean': (2, 3)}


@dataclass
class TemporaryAlsong:
    duration: float
    previous: tuple
    source: Song
    rows: tuple
    provider_times: tuple
    confirmed: bool = False
    additions: tuple = ()

    @property
    def lines(self):
        return self.source.lines + tuple(Line(text,'','') for _,text in self.additions)


def visible_overlay_rect(rect=None, *, fully_visible=False):
    area = monitor_work_area(rect) if rect is not None else None
    if area is not None:
        width, height, x, y = rect
        if fully_visible:
            left, top, right, bottom = area
            width, height = min(width, right - left), min(height, bottom - top)
            x, y = max(left, min(x, right - width)), max(top, min(y, bottom - height))
        return width, height, x, y
    left, top, right, bottom = work_area()
    width, height = rect[:2] if rect is not None else (1040, 260)
    width = min(width, right - left - 40)
    height = min(height, bottom - top - 60)
    return width, height, left + (right - left - width) // 2, bottom - height - 30


def display_translation(line, original_visible=False, *, user_override=False):
    if user_override and line.korean.strip():
        return '' if original_visible and line.korean == line.original else line.korean
    if is_latin_only(line.original) or not preserves_latin_phrases(line.original, line.korean):
        return '' if original_visible else line.original
    return line.korean if line.korean.strip() else ''


def format_time(seconds):
    if seconds is None:
        return '—'
    return f'{int(seconds) // 60:02}:{seconds % 60:05.2f}'


def parse_time(text):
    value = text.strip()
    if not value:
        return None
    try:
        parts = value.split(':')
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2 and 0 <= float(parts[1]) < 60:
            return int(parts[0]) * 60 + float(parts[1])
    except ValueError:
        pass
    raise ValueError('시간은 12.50 또는 01:12.50 형태로 입력하세요.')


class LyricsApp:
    def __init__(self, root, store, start_services=True, spotify_auto_start=False, activation=None):
        register_bundled_fonts()
        self.root, self.store = root, store
        self.settings = store.settings()
        self.songs = store.library()
        self.batch_translation_error = ''
        try:self.batch_translations = BatchTranslationStore(store.directory)
        except (OSError,ValueError) as error:
            self.batch_translations=None;self.batch_translation_error=str(error)
        self.translation_dialog = None
        self.translation_resource_dialog = None
        self.batch_translation_source = None
        self.song, self.timing = None, None
        self.playback = Playback()
        self.messages = queue.Queue()
        self.extension_inbox=ExtensionInbox(store,self.messages,self.settings.get('extension_inbox'))
        self.lyric_annotations=LyricAnnotations(store)
        self.lyric_text_edits=LyricTextEdits(store)
        self.user_annotations=UserAnnotations(store.directory)
        self.user_annotation_signature=None;self.user_annotation_rows=();self.line_singers=()
        self.singer_edits=SingerEdits(store.directory)
        self.singer_roster=SingerRoster(store.directory)
        self.singer_options=()
        self.singer_input_index=None
        self.singer_input_live=False
        self.singer_input_changed_row=None
        self.queued_lyric_seek=None
        self.line_readings=()
        self.line_reading_request=None
        self.line_reading_worker=None
        self.line_reading_queue=queue.Queue(maxsize=1)
        self.reader_bridge=ReaderBridge(messages=self.messages,document_urls=read_json(store.directory/'wiki_documents.json',{}),
                                        preserve_sources=True)
        self.internal_reader=InternalReader(BASE,self.reader_bridge,self.messages,profile=store.directory/'browser_profile')
        self.reader_mode=tk.StringVar(value='internal')
        self.reader_bridge.set_backend(self.reader_mode.get())
        self.reader_observed=0;self.services_started=start_services
        self.connection_dialog=None
        self.explorer_dialog=None
        self.update_dialog=None;self.update_controller=None
        self.screen_lyrics=ScreenLyricsMonitor(self.messages)
        self.screen_song=None
        self.background_lyrics=BackgroundLyrics(BASE,store.directory,self.messages,defer_catalog=start_services)
        self.initial_playback_paint_pending=start_services
        self.initial_playback_paint_callback=None
        self.background_lyrics.artist_catalog_held=start_services
        from catalog_sync import CatalogSync
        self.catalog_sync=CatalogSync(self)
        self.lyricsplus_job=ProviderSyncJob(self.messages,partial(fetch_lyricsplus,merge_partial=True),reports_status=True)
        self.alsong_job=ProviderSyncJob(self.messages,partial(fetch_alsong,merge_partial=True),name='알송',reports_status=True)
        self.direct_result=None;self.direct_map_cache=None
        self.lyric_merge_job=PlaybackLyricMerge(self.messages)
        self.current_lyric_merge=None
        self.merge_reader_receipts=[]
        self.sync_lookup_started=0
        self.lyric_cache=LyricCache(store.directory/'lyrics_cache')
        from playlist_lyrics_job import PlaylistLyricsJob
        self.playlist_lyrics=PlaylistLyricsJob(self)
        from spotify_translation_sources import SpotifyTranslationSources
        self.spotify_translation_sources=SpotifyTranslationSources(self)
        from personal_resources import PersonalResources
        self.personal_resources=PersonalResources(self)
        self.restoring_cache=False;self.cache_hit=False;self.cache_load_ms=None;self.cache_reuse=None;self.screen_cache_tainted=False
        self.bundled_manual_key=None
        self.related_cache_request=None
        self.related_cache_running=False
        self.related_cache_pending=None
        self.cache_tracking=None
        self.spotify_current=None;self.spotify_observed_at=0
        self.spotify_source_index=None;self.spotify_anchor=None
        self.spotify_sample_after=0
        self.realtime_job=None;self.realtime_index=None;self.realtime_state=''
        self.realtime_matched_at=0;self.realtime_detail='';self.realtime_runtime={}
        self.screen_status='Spotify 가사 화면에서 원문 대조 대기'
        self.live_lyrics=LiveLyrics(LiveTimingStore(store.directory))
        self.temporary_alsong=None
        self.native_timing=NativeTiming(store.directory)
        self.display_lines=()
        self.vocalise_rows=();self.vocalise_waiting=False;self.translated_count=0
        self.manual_index = None
        self.cursor = 0
        self.editor = None
        self.audio_window = None
        self.audio_playlist_panel = None
        self.audio_installer=AudioInstaller(BASE);self.audio_install_dialog=None
        self.audio_job = None
        self.audio_song = None
        self.audio_draft = None
        self.audio_draft_readonly = False
        self.audio_manual_progress = None
        self.audio_progress_updated = 0
        self.audio_status = tk.StringVar(value='분석 대기를 누른 뒤 Spotify에서 선택한 곡을 처음부터 재생하세요.')
        self.appearance_dialog=None
        self.volume_popup=None
        self.shortcut_dialog=None
        self.web_dialog=None
        self.manual_dialog=None
        self.manual_sync_dialog=None
        self.missing_audio_collector=None
        self.close_after_collection=False
        self.web_queried=set()
        self.web_running=set()
        self.web_auto=tk.BooleanVar(value=self.settings.get('web_auto',False))
        self.auto_audio=tk.BooleanVar(value=self.settings.get('auto_audio',False))
        self.realtime_tracking=tk.BooleanVar(value=self.settings.get('realtime_tracking',True) is not False)
        self.realtime_tracking_since=0.
        self.spotify_lifecycle=tk.BooleanVar(value=self.settings.get('spotify_lifecycle',False))
        self.auto_analyzer=AutoAnalyzer(BASE,store,self.messages,realtime_device=self.settings.get('audio_device','auto'),
                                       alignment=self.settings.get('audio_alignment'),realtime_enabled=self.realtime_tracking.get())
        self.recording_contexts=RecordingContexts()
        self.locked = False
        self.lock_available = False
        self.chrome_available = False
        self.borderless = tk.BooleanVar(value=False)
        self.transparent = tk.BooleanVar(value=self.settings.get('transparent', DEFAULT_TRANSPARENT))
        self.use_estimate = tk.BooleanVar(value=self.settings.get('use_estimate', True))
        self.lyric_advance_ms = lyric_advance_ms(self.settings.get('lyric_advance_ms'))
        self.time_cache = None
        self.drag_origin = None
        self.lyric_click = False
        self.input_status={'clicks':0,'arrows':0,'modifiers':0}
        self.notice_until = 0
        self.auto_queried = set()
        self.query_running = {}
        self.online = tk.BooleanVar(value=self.settings.get('online', True))
        self.public_background=tk.BooleanVar(value=self.settings.get('public_background',False))
        self.lyricsplus_background=tk.BooleanVar(value=self.settings.get('lyricsplus_background',False))
        self.alsong_background=tk.BooleanVar(value=self.settings.get('alsong_background',False))
        self.follow = tk.BooleanVar(value=True)
        self.show_reading = tk.BooleanVar(value=self.settings.get('show_reading', True))
        self.show_original = tk.BooleanVar(value=self.settings.get('show_original', True))
        self.opacity = tk.DoubleVar(value=self.settings.get('opacity', .94))
        self.background_opacity=tk.DoubleVar(value=self.settings.get('background_opacity',DEFAULT_BACKGROUND_OPACITY))
        self.background_color=valid_color(self.settings.get('background_color'),DEFAULT_BACKGROUND)
        self.text_styles=styles_from_settings(self.settings)
        self.font_size = tk.IntVar(value=self.settings.get('font_size', 24))
        self.last_render = None
        self.last_width = 0
        self.suppress_preview_id = None
        self.closed = False
        self.closing = False
        self.pending_height_fit = None
        self.manual_window_size=bool(self.settings.get('manual_window_size',False))
        self.compact_layout=compact_from_settings(self.settings)
        self.compact_hidden=set()
        self.compact_controls=False
        self.compact_metadata=False
        self.compact_reading=False
        self.lyric_scale=1.0
        self.resize_origin=None
        self.last_height=0
        self.pending_track_change=None
        self.pending_seek=None
        self.pending_playback=None
        self.track_notice_until=0
        self.track_title_tip=None
        self.footer_height=24
        self.tray = None
        self.electron_overlay_active = False
        self.electron_overlay_fonts = {}
        self.electron_overlay_images = {}
        self.electron_overlay_icons = None
        self.electron_overlay_publish = None
        self.electron_overlay_position = 0
        self.workspace = DesktopWorkspace(self,BASE)
        self.lyric_measure_context=None
        self.lyric_measurements={}
        configure_theme(root)
        self._build_overlay()
        self.backdrop=Backdrop(root)
        self.backdrop.window.bind('<ButtonPress-1>',self.start_drag)
        self.backdrop.window.bind('<B1-Motion>',self.drag_window)
        self.backdrop.window.bind('<ButtonRelease-1>',self.finish_drag)
        self.backdrop.window.bind('<Button-3>',self.show_menu)
        self.backdrop.window.bind('<Left>',self.lyric_arrow)
        self.backdrop.window.bind('<Right>',self.lyric_arrow)
        self.backdrop.window.bind('<KeyRelease>',self.overlay_key_release)
        self.monitor, self.hotkeys = SpotifyMonitor(self.messages), GlobalHotkeys(self.messages,self.settings.get('hotkeys'))
        self.lifecycle=SpotifyLifecycleMonitor(self.messages,enabled=self.spotify_lifecycle.get(),
                                               observed_running=spotify_auto_start)
        self.hotkeys.register_window(root)
        self.hotkeys.register_window(self.backdrop.window)
        if start_services:
            if self.reader_mode.get()=='internal':
                try:self.internal_reader.set_enabled(True)
                except ValueError as error:
                    self.notify(str(error),12)
            self.extension_inbox.start()
            try:self.reader_bridge.start()
            except OSError:self.notify('Spotify 읽기 모드 연결 포트가 사용 중입니다. 다른 가사 앱을 종료한 뒤 다시 실행하세요.',12)
            if self.auto_audio.get():
                try:self.auto_analyzer.set_enabled(True)
                except ValueError as error:
                    self.auto_audio.set(False)
                    self.notify(str(error),12)
            self.screen_lyrics.start()
            if self.online.get() and self.settings.get('spotify_connected') and self.settings.get('spotify_authenticated',True):
                self.safe(partial(self.connect_spotify,visible=False),interactive=False)
            self.monitor.start()
            self.hotkeys.start()
            self.lifecycle.start()
            if self.spotify_lifecycle.get():
                self.safe(lambda:self.apply_spotify_lifecycle(persist=False),interactive=False)
        self.root.after(80, self.tick)
        self.root.after(500, self.watch_overlay_position, activation)
        self.apply_appearance()
        if start_services and sys.platform == 'win32':
            self.tray = WindowsTray(root, APP_ICON, self.toggle_overlay_visibility, self.show_tray_menu)
            try:self.tray.start()
            except OSError:
                self.tray = None
                self.show_track_notice('Windows 알림 영역에 아이콘을 등록하지 못했습니다.')
        self.update_controller=UpdateController(self)
        self.update_controller.schedule_automatic()
        if start_services and sys.platform == 'win32':
            self.root.after_idle(self.workspace.start_overlay)

    def button(self, parent, text, command, **options):
        defaults = dict(bg=PANEL, fg=TEXT, activebackground=HOVER, activeforeground=TEXT,
                        disabledforeground='#737c90', relief='flat', borderwidth=0,
                        highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                        padx=9, pady=6, cursor='hand2', font=('Malgun Gothic', 10))
        return tk.Button(parent, text=text, command=lambda: self.safe(command), **{**defaults, **options})

    def overlay_button(self, parent, text, command, **options):
        colors = dict(bg=OVERLAY_PANEL, fg=OVERLAY_TEXT, activebackground='#35455a',
                      activeforeground=OVERLAY_TEXT, highlightbackground=OVERLAY_PANEL,
                      highlightcolor=OVERLAY_ACCENT)
        return self.button(parent,text,command,**{**colors,**options})

    def safe(self, command, interactive=True):
        try:
            command()
        except (ValueError, OSError) as error:
            self.notify(str(error))
            if interactive:messagebox.showerror('가사 작업을 완료하지 못했습니다', str(error), parent=self.editor or self.root)
            else:self.error_notice=(str(error),time.monotonic()+6)

    def _build_overlay(self):
        BG, PANEL, TEXT, MUTED, ACCENT = OVERLAY_BG, OVERLAY_PANEL, OVERLAY_TEXT, OVERLAY_MUTED, OVERLAY_ACCENT
        root = self.root
        root.title('아이마스 가사 · Spotify 오버레이')
        if sys.platform == 'win32' and APP_ICON.is_file():
            root.iconbitmap(default=str(APP_ICON))
        root.configure(bg=BG)
        root.attributes('-topmost', True)
        root.attributes('-alpha', self.opacity.get())
        root.minsize(240,100) if self.manual_window_size else root.minsize(560,210)
        rect = self.settings.get('window_rect')
        if not (isinstance(rect,list) and len(rect)==4 and all(isinstance(n,int) for n in rect)
                and rect[0]>=240 and rect[1]>=90):
            rect = None
        width, height, x, y = visible_overlay_rect(rect)
        root.geometry(f'{width}x{height}{window_position(x, y)}')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.columnconfigure(0, weight=1)
        self.tools = tk.Frame(root, bg=BG)
        self.tools.grid(row=0, column=0, sticky='ew', padx=12, pady=(7, 0))
        self.primary_tools = tk.Frame(self.tools, bg=BG)
        self.primary_tools.pack(fill='x')
        self.primary_buttons = [
            self.overlay_button(self.primary_tools, '가사 표시 설정', self.show_appearance_settings),
            self.overlay_button(self.primary_tools, '클릭 통과', self.toggle_lock),
            self.overlay_button(self.primary_tools, '헤더 숨기기', self.toggle_chrome),
            self.overlay_button(self.primary_tools, '더 보기 ▾', self.show_more),
        ]
        self.lock_button = self.primary_buttons[1]
        self.more_button = self.primary_buttons[-1]
        self.tool_buttons = self.primary_buttons
        self.control_hints = [
            ControlHint(self.primary_buttons[2], '조작줄을 접습니다. 우클릭 메뉴에서 복원할 수 있습니다.',
                        background=PANEL,foreground=TEXT),
        ]
        self.lyric_frame = tk.Frame(root, bg=BG)
        self.lyric_frame.grid(row=1, column=0, sticky='nsew', padx=22, pady=(6, 4))
        root.rowconfigure(1, weight=1)
        self.lyric_frame.columnconfigure(0, weight=1)
        self.lyric_frame.rowconfigure(1, weight=1)
        self.track_metadata=tk.Frame(self.lyric_frame,bg=BG)
        self.track_metadata.columnconfigure(0,weight=1)
        self.lyric_content=tk.Frame(self.lyric_frame,bg=BG)
        self.lyric_content.grid(row=1,column=0,sticky='ew',padx=LYRIC_GUTTER)
        # Keep grid propagation active when only the 1px-wide lyric canvases are mapped.
        self.lyric_content.columnconfigure(0,weight=1,minsize=2)
        self.current_singers=LyricPerformers(self.lyric_frame,
            update_path=self.store.directory/'group_credits.json',bg=BG,fg=MUTED,
            font=('Malgun Gothic',11,'bold'),justify='center',max_lines=2)
        self.current_singers.attach_to(self.lyric_content)
        self.control_hints.extend(column.hint for column in self.current_singers.columns)
        self.reading_label = SubtitleLine(self.lyric_content, bg=BG, fg=ACCENT, justify='center',
                                     font=('Yu Gothic UI', 14))
        self.original_label = SubtitleLine(self.lyric_content, bg=BG, fg=TEXT, justify='center',
                                      font=('Yu Gothic UI', 24, 'bold'))
        self.korean_label = SubtitleLine(self.lyric_content, bg=BG, fg='#f1d6a5', justify='center',
                                    font=('Malgun Gothic', 20))
        self.lyric_labels={'reading':self.reading_label,'original':self.original_label,'korean':self.korean_label}
        for row,(key,label) in enumerate(self.lyric_labels.items(),1):
            label.grid(row=row,column=0,sticky='nsew',pady=LYRIC_PADDING[key])
        self.mode_label = SubtitleLine(self.lyric_content, bg=BG, fg=MUTED, font=('Malgun Gothic', 9))
        self.mode_label.grid(row=4, column=0, sticky='ew', pady=(2, 0))
        self.track_title_label=ArtistHeader(self.track_metadata,bg=BG,fg=TEXT,
                                           font=('Malgun Gothic',11,'bold'),justify='left',max_lines=1)
        self.track_title_label.grid(row=0,column=0,sticky='new')
        self.track_artist_label=ArtistHeader(self.track_metadata,bg=BG,fg='#e5eaf2',
                                            font=('Malgun Gothic',11,'bold'),justify='left',max_lines=2)
        self.track_artist_label.grid(row=1,column=0,sticky='new',pady=(3,0))
        self.side_lyrics = {
            side: SubtitleLine(self.lyric_frame, bg=BG, fg=MUTED,
                               justify='right' if side=='previous' else 'left',
                               ellipsis='start' if side=='previous' else 'end', font=('Yu Gothic UI', 12))
            for side in ('previous', 'next')
        }
        self.lyric_content.bind('<Configure>', lambda _: self.layout_side_lyrics(
            self.root.winfo_width(), self.lyric_scale, self.compact_hidden), add='+')
        for label in (self.track_title_label,self.track_artist_label):
            label.bind('<Enter>',lambda event:self.show_track_title_tip())
            label.bind('<Leave>',lambda event:self.hide_track_title_tip())
        self.transport=tk.Frame(root,bg=BG,height=self.footer_height)
        self.transport.grid(row=2,column=0,sticky='ew',padx=12,pady=(0,10))
        self.transport_images={kind:transport_icon(root,kind,ACCENT if kind in ('play','pause') else TEXT)
                               for kind in ('previous','first','play','pause','volume','last','next')}
        controls=(
            ('previous_track_button','previous','이전 곡',lambda:self.change_track('previous')),
            ('track_start_button','first','현재 곡 처음으로 이동 · 재생 상태 유지',lambda:self.seek_track('first')),
            ('play_pause_button','play','재생',self.toggle_playback),
            ('volume_button','volume','음량 조절 / 음소거',self.open_volume),
            ('track_end_button','last','현재 곡 끝 1초 전으로 이동 · 재생 상태 유지',lambda:self.seek_track('last')),
            ('next_track_button','next','다음 곡',lambda:self.change_track('next')),
        )
        for index,(attribute,kind,hint,command) in enumerate(controls):
            button=self.overlay_button(self.transport,hint,command,image=self.transport_images[kind],compound='none',
                               default='disabled',
                               padx=0,pady=0,highlightthickness=0)
            setattr(self,attribute,button)
            if index<3:button.place(x=index*24,width=22,relheight=1)
            else:button.place(relx=1,x=(index-5)*24,anchor='ne',width=22,relheight=1)
            control_hint=ControlHint(button,hint,background=PANEL,foreground=TEXT)
            self.control_hints.append(control_hint)
            if kind=='play':self.play_pause_hint=control_hint
        self.playback_badge = SubtitleLine(self.transport,bg=BG,fg=TEXT,font=('Consolas',-9))
        self.playback_badge.configure(wraplength=0)
        self.playback_badge.place(relx=.5,rely=.5,anchor='center',width=76,height=20)
        self.transport_notice=tk.Label(root,bg=BG,fg=MUTED,font=('Malgun Gothic',9))
        self.status = tk.Label(root, bg=BG, fg=MUTED, anchor='w', justify='left',
                               font=('Malgun Gothic', 9))
        self.status.grid(row=3, column=0, sticky='ew', padx=14, pady=(0, 8))
        self.status.grid_remove()
        root.bind('<Configure>', self.on_resize,add='+')
        root.bind('<ButtonRelease-1>', self.finish_drag,add='+')
        root.bind('<Left>', self.lyric_arrow)
        root.bind('<Right>', self.lyric_arrow)
        root.bind('<KeyRelease>', self.overlay_key_release)
        root.bind('<Escape>', self.stop_singer_input)
        root.bind('<Button-3>', self.show_menu)
        self.context_menu = self.build_context_menu()
        for widget in (self.lyric_frame,self.lyric_content,self.track_metadata,self.reading_label,self.original_label,
                       self.korean_label,self.mode_label,self.track_title_label,self.track_artist_label,
                       *self.side_lyrics.values(),*self.current_singers.columns):
            widget.bind('<ButtonPress-1>', self.start_drag)
            widget.bind('<B1-Motion>', self.drag_window)
        self.build_resize_handles()
        self.render()

    def menu_items(self, *, tray=False):
        def command(label, action):
            return {'label':label, 'command':lambda:self.safe(action)}

        lifecycle = command('Spotify와 함께 시작 / 종료', self.toggle_spotify_lifecycle)
        lifecycle['checked'] = self.spotify_lifecycle.get()
        items = [
            command('헤더 숨기기 / 복원', self.toggle_chrome),
            command('클릭 통과 / 해제', self.toggle_lock),
            command('배경 투명 / 복원', self.toggle_transparent),
            None,
            command('가사 표시 설정…', self.show_appearance_settings),
            command('단축키 설정…', self.open_shortcuts),
            lifecycle,
            None,
            command('개발자 모드…', self.show_developer_workspace),
            command('아이마스 음악 사전…', self.show_music_dictionary),
            command('프로그램·자료 업데이트…', self.open_updates),
            None,
            command('종료', self.close),
        ]
        if tray:
            items[:0] = [command('가사창 숨기기 / 복원', self.toggle_overlay_visibility), None]
        return items

    def build_context_menu(self):
        menu = tk.Menu(self.root, tearoff=False, font=('Malgun Gothic', 10))
        for item in self.menu_items():
            if item is None:
                menu.add_separator()
            elif 'checked' in item:
                menu.add_checkbutton(label=item['label'], variable=self.spotify_lifecycle, selectcolor=ACCENT,
                                     command=lambda:self.safe(self.apply_spotify_lifecycle))
            else:
                menu.add_command(**item)
        return menu

    def build_resize_handles(self):
        # Reveal a hit-testable edge on hover, including over a transparent backdrop.
        placements={
            'n':dict(relwidth=1,height=4),
            's':dict(rely=1,relwidth=1,height=4,anchor='sw'),
            'w':dict(width=4,relheight=1),
            'e':dict(relx=1,width=4,relheight=1,anchor='ne'),
            'nw':dict(width=10,height=10),
            'ne':dict(relx=1,width=10,height=10,anchor='ne'),
            'sw':dict(rely=1,width=10,height=10,anchor='sw'),
            'se':dict(relx=1,rely=1,width=10,height=10,anchor='se'),
        }
        self.resize_handles={}
        for edge,placement in placements.items():
            cursor='size_ns' if edge in ('n','s') else 'size_we' if edge in ('w','e') else (
                'size_nw_se' if edge in ('nw','se') else 'size_ne_sw')
            handle=tk.Frame(self.root,bg=TRANSPARENT,cursor=cursor,borderwidth=0)
            handle.place(**placement)
            handle.bind('<Enter>',lambda event:event.widget.configure(bg=ACCENT))
            handle.bind('<Leave>',lambda event:event.widget.configure(bg=TRANSPARENT))
            handle.bind('<ButtonPress-1>',lambda event,edge=edge:self.start_resize(event,edge))
            handle.bind('<B1-Motion>',self.resize_window)
            handle.bind('<ButtonRelease-1>',self.finish_resize)
            handle.bind('<Button-3>',self.show_menu)
            self.resize_handles[edge]=handle
        self.root.after(40,self.poll_resize_handles)

    def update_resize_handles(self):
        active=''
        if not self.locked and self.root.winfo_viewable():
            if self.resize_origin is not None:
                active=self.resize_origin[0]
            else:
                # Color-keyed pixels pass mouse events through, so Enter alone
                # cannot reveal a hidden handle. Check only the overlay's edges.
                x,y=self.root.winfo_pointerxy()
                x-=self.root.winfo_rootx();y-=self.root.winfo_rooty()
                width,height=self.root.winfo_width(),self.root.winfo_height()
                if 0<=x<width and 0<=y<height:
                    vertical='n' if y<10 else 's' if y>=height-10 else ''
                    horizontal='w' if x<10 else 'e' if x>=width-10 else ''
                    if vertical and horizontal:active=vertical+horizontal
                    elif y<4 or y>=height-4:active=vertical
                    elif x<4 or x>=width-4:active=horizontal
        for edge,handle in self.resize_handles.items():
            color=ACCENT if edge==active else TRANSPARENT
            if handle.cget('bg')!=color:handle.configure(bg=color)

    def poll_resize_handles(self):
        if self.closed or self.closing:return
        if not self.electron_overlay_active:self.update_resize_handles()
        self.root.after(40,self.poll_resize_handles)

    def start_resize(self,event,edge):
        if self.locked:return 'break'
        self.hide_track_title_tip()
        self.drag_origin=None
        self.root.focus_force()
        self.resize_origin=(edge,event.x_root,event.y_root,self.root.winfo_x(),self.root.winfo_y(),
                            self.root.winfo_width(),self.root.winfo_height())
        event.widget.grab_set()
        return 'break'

    def resize_window(self,event):
        if self.locked or self.resize_origin is None:return 'break'
        edge,x0,y0,x,y,width,height=self.resize_origin
        dx,dy=event.x_root-x0,event.y_root-y0
        if not dx and not dy and not self.manual_window_size:return 'break'
        self.manual_window_size=True
        self.root.minsize(240,100)
        new_width=max(240,width+(dx if 'e' in edge else -dx if 'w' in edge else 0))
        new_height=max(100,height+(dy if 's' in edge else -dy if 'n' in edge else 0))
        if 'w' in edge:x+=width-new_width
        if 'n' in edge:y+=height-new_height
        self.root.geometry(f'{new_width}x{new_height}{window_position(x, y)}')
        self.request_fit_height()
        return 'break'

    def finish_resize(self,event):
        if self.resize_origin is None:return 'break'
        self.resize_origin=None
        event.widget.grab_release()
        self.root.update_idletasks()
        self.safe(self.save_settings)
        return 'break'

    def on_resize(self, event):
        if self.closing or event.widget is not self.root:
            return
        width = event.width
        if width < 240:
            return
        if width == self.last_width and event.height == self.last_height:
            return
        self.last_width = width
        self.last_height = event.height
        button_width = max(button.winfo_reqwidth() for button in self.tool_buttons) + 4
        columns = 4 if width >= button_width*4+24 else 2
        for index, button in enumerate(self.primary_buttons):
            button.grid(row=index // columns, column=index % columns, sticky='ew', padx=2, pady=2)
        for column in range(4):
            self.primary_tools.columnconfigure(column, weight=1 if column < columns else 0,
                                               uniform='controls' if column < columns else '')
        self.status.configure(wraplength=max(200, width - 30))
        self.playback_badge.configure(font=('Consolas',-9 if width<280 else -12))
        self.playback_badge.place_configure(width=76 if width<280 else 110)
        self.update_track_title()
        self.request_fit_height()

    def displayed_artists(self):
        names=self.background_lyrics.display_artists(self.playback)
        return names or ((self.playback.artist,) if self.playback.artist else ())

    def track_credit_height(self,label,font,outline=None):
        linespace=int(self.root.tk.call('font','metrics',font,'-linespace'))
        return linespace*label.max_lines+2*outline_padding(label.cget('outline') if outline is None else outline)

    def update_track_title(self):
        if self.closed:return
        title=' '.join(self.playback.title.split())
        error,expires=getattr(self,'error_notice',('',0))
        width=max(240,self.root.winfo_width())
        artists=self.displayed_artists()
        display_credits=with_character_voices(artists,self.playback.album,
                                             update_path=self.store.directory/'group_credits.json')
        artist=artist_label(display_credits,self.playback.album)
        spans=list(character_color_spans(artist,self.playback.album)) if title else []
        rosters=group_member_lines(artists,self.playback.album,
                                  update_path=self.store.directory/'group_credits.json') if title else ()
        for members in rosters:
            group,separator,credits=members.partition(' 멤버: ')
            prefix=group+': ' if separator and len(rosters)>1 else ''
            members=prefix+credits if separator else members
            start=len(artist)+1
            artist+='\n'+members
            # Only multi-group credits need a group label on the extra line.
            offset=len(prefix)
            spans.extend((start+offset+begin,start+offset+end,color) for begin,end,color in
                         character_color_spans(members[offset:],self.playback.album))
        changed=(self.track_title_label.cget('text'),self.track_artist_label.cget('text'))!=(title,artist)
        self.track_title_label.configure(text=title,wraplength=max(80,width-66))
        self.track_artist_label.configure(text=artist if title else '',wraplength=max(80,width-66),
                                          outline_spans=tuple(spans))
        title_visible=bool(title) and 'title' not in self.compact_hidden and self.text_styles['title'].get('visible',True)
        artist_slot=bool(title) and 'artists' not in self.compact_hidden and self.text_styles['artists'].get('visible',True)
        artist_gap=round(3*self.lyric_scale) if title_visible and artist_slot else 0
        if title_visible or artist_slot:
            self.track_metadata.grid(row=0,column=0,sticky='new')
        else:
            self.track_metadata.grid_remove()
            self.hide_track_title_tip()
        if title_visible:self.track_title_label.grid()
        else:self.track_title_label.grid_remove()
        if artist_slot and artist:self.track_artist_label.grid(pady=(artist_gap,0))
        else:self.track_artist_label.grid_remove()
        title_height=self.track_credit_height(self.track_title_label,self.track_title_label.cget('font')) if title_visible else 0
        artist_height=self.track_credit_height(self.track_artist_label,self.track_artist_label.cget('font')) if artist_slot else 0
        self.track_metadata.rowconfigure(0,minsize=title_height)
        self.track_metadata.rowconfigure(1,minsize=artist_height+artist_gap)
        if self.singer_input_index is not None or (not self.compact_controls and
                (not title or (error and time.monotonic()<expires))):self.mode_label.grid()
        else:self.mode_label.grid_remove()
        gap=round(8*self.lyric_scale) if title_visible or artist_slot else 0
        self.lyric_content.grid_configure(pady=(gap,0))
        self.layout_side_lyrics(width,self.lyric_scale,self.compact_hidden)
        for label in (self.reading_label,self.original_label,self.korean_label,self.mode_label):
            label.configure(wraplength=max(200,width-52))
        if self.transport_notice.cget('text'):self.show_track_notice(self.transport_notice.cget('text'))
        if changed:self.request_fit_height()

    def show_track_title_tip(self):
        self.hide_track_title_tip()
        if not self.playback.title:return
        # Large credits can be scrolled in place; a full tooltip would cover them.
        if self.track_title_label.overflow or self.track_artist_label.overflow:return
        tip=tk.Toplevel(self.root);self.track_title_tip=tip
        tip.withdraw();tip.overrideredirect(True);tip.attributes('-topmost',True)
        names=self.displayed_artists();original=', '.join(names)
        translated=artist_label(names,self.playback.album)
        text='\n'.join(value for value in (self.playback.title,translated,
                       original if original!=translated else '',self.mode_label.cget('text')) if value)
        left,top,right,bottom=work_area()
        tk.Label(tip,text=text,bg=OVERLAY_PANEL,fg=OVERLAY_TEXT,font=('Malgun Gothic',9),justify='left',
                 wraplength=min(560,right-left-40),padx=10,pady=7).pack()
        tip.update_idletasks()
        x=max(left,min(self.track_title_label.winfo_rootx(),right-tip.winfo_reqwidth()))
        y=self.track_title_label.winfo_rooty()-tip.winfo_reqheight()-5
        if y<top:y=min(bottom-tip.winfo_reqheight(),self.track_title_label.winfo_rooty()+self.track_title_label.winfo_height()+5)
        y=max(top,y)
        tip.geometry(f'{x:+d}{y:+d}');tip.deiconify()

    def hide_track_title_tip(self):
        if self.track_title_tip:self.track_title_tip.destroy();self.track_title_tip=None

    def show_track_notice(self,message):
        if message!=self.transport_notice.cget('text'):
            self.track_notice_until=time.monotonic()+6 if message else 0
        self.transport_notice.configure(text=message)
        if message:
            area=self.lyric_content if self.playback.title else self.mode_label
            self.transport_notice.configure(wraplength=max(200,area.winfo_width()-8))
            self.transport_notice.place(in_=area,relx=0,rely=0,anchor='nw',relwidth=1,
                                        height=max(22,self.transport_notice.winfo_reqheight()))
            self.transport_notice.lift()
        else:self.transport_notice.place_forget()

    def open_volume(self):
        if self.closed:return
        if self.volume_popup is not None and not self.volume_popup.closed:
            if self.volume_popup.visible:self.volume_popup.hide()
            else:self.volume_popup.show()
            return
        from volume_popup import VolumePopup
        self.volume_popup=VolumePopup(self.root,self.volume_button)

    def change_track(self,direction):
        if self.closed or self.pending_track_change or self.pending_seek or self.pending_playback:return
        if not self.monitor.request_skip(direction):
            self.show_track_notice('곡 이동 요청을 처리할 수 없습니다. 잠시 후 다시 눌러 주세요.')
            return
        self.pending_track_change=direction
        self.update_transport_buttons()
        self.show_track_notice('이전 곡으로 이동 중…' if direction=='previous' else '다음 곡으로 이동 중…')

    def finish_track_change(self,direction,success,message):
        if direction!=self.pending_track_change:return
        self.pending_track_change=None
        self.update_transport_buttons()
        self.show_track_notice('' if success else message)
        if self.manual_sync_dialog:
            self.manual_sync_dialog.finish_skip(direction,success,message)

    def update_transport_buttons(self):
        busy=bool(self.pending_track_change or self.pending_seek or self.pending_playback)
        for button in (self.previous_track_button,self.next_track_button):
            button.configure(state='disabled' if busy else 'normal')
        for button in (self.track_start_button,self.track_end_button):
            button.configure(state='normal' if not busy and self.playback.has_timeline else 'disabled')
        label,icon=('일시정지','pause') if self.playback.playing else ('재생','play')
        self.play_pause_hint.text=label
        self.play_pause_button.configure(text=label,image=self.transport_images[icon],
            state='normal' if not busy and self.playback.title and not self.playback.error else 'disabled')

    def toggle_playback(self):
        if self.closed or self.pending_track_change or self.pending_seek or self.pending_playback:return
        if not self.playback.title or self.playback.error:return
        action='pause' if self.playback.playing else 'play'
        if not self.monitor.request_playback(action):
            self.show_track_notice('재생 요청을 처리할 수 없습니다. 잠시 후 다시 눌러 주세요.')
            return
        self.pending_playback=action
        self.update_transport_buttons()
        self.show_track_notice('일시정지 요청 중…' if action=='pause' else '재생 요청 중…')

    def finish_playback(self,action,success,message):
        if action!=self.pending_playback:return
        self.pending_playback=None
        self.update_transport_buttons()
        self.show_track_notice('' if success else message)

    def seek_track(self,edge):
        if self.closed or self.pending_track_change or self.pending_seek or self.pending_playback:return
        if not self.playback.has_timeline:return
        key=self.playback.key
        if not self.monitor.request_seek(edge,key):
            self.show_track_notice('재생 위치를 이동할 수 없습니다. 잠시 후 다시 눌러 주세요.')
            return
        self.pending_seek=(edge,key)
        self.update_transport_buttons()
        self.show_track_notice('곡 처음으로 이동 중…' if edge=='first' else '곡 끝부분으로 이동 중…')

    def finish_seek(self,edge,success,message):
        if self.pending_seek is None or edge!=self.pending_seek[0]:return
        key=self.pending_seek[1];self.pending_seek=None
        self.update_transport_buttons()
        same_track=key==self.playback.key
        if same_track and success and isinstance(edge,str) and self.singer_input_index is None:self.resume_follow()
        self.show_track_notice('' if success or not same_track else message)
        if self.queued_lyric_seek is not None:
            target,context=self.queued_lyric_seek
            self.queued_lyric_seek=None
            if same_track and context==self.lyric_seek_context():self.request_lyric_seek(target)

    def lyric_seek_context(self):
        return self.playback.key,self.singer_edits.key

    def request_lyric_seek(self, target):
        if not self.playback.has_timeline:return
        target=max(0.,min(float(target),self.playback.duration-.001))
        if self.pending_seek:
            self.queued_lyric_seek=(target,self.lyric_seek_context())
            return
        key=self.playback.key
        if self.pending_track_change or self.pending_playback or not self.monitor.request_seek(target,key):
            self.show_track_notice('재생 위치를 이동할 수 없습니다. 잠시 후 다시 눌러 주세요.')
            return
        self.pending_seek=(target,key)
        self.update_transport_buttons()

    def seek_by_key(self,key):
        seconds=PLAYBACK_STEP_KEYS.get(key.lower())
        if seconds is None:return False
        if self.playback.has_timeline:
            position=self.playback.position()
            if self.queued_lyric_seek and self.queued_lyric_seek[1]==self.lyric_seek_context():
                position=self.queued_lyric_seek[0]
            elif self.pending_seek and self.pending_seek[1]==self.playback.key:
                edge=self.pending_seek[0]
                position=0. if edge=='first' else max(0.,self.playback.duration-1) if edge=='last' else edge
            self.request_lyric_seek(position+seconds)
        return True

    def seek_lyric_row(self, index, *, allow_estimate=True):
        times,_=self.effective_times()
        stamp=times[index] if allow_estimate else self.time_cache[0][1][index]
        if stamp is None:
            self.queued_lyric_seek=None
            self.show_track_notice('이 구절의 시작 시간이 없습니다.')
            return
        target=max(0.,min(stamp+self.timing['offset'],self.playback.duration-.001))
        self.request_lyric_seek(target)

    def open_shortcuts(self):
        if self.shortcut_dialog and not self.shortcut_dialog.closed:
            self.shortcut_dialog.window.lift();return
        self.shortcut_dialog=ShortcutDialog(self.editor or self.root,self.hotkeys,self.save_shortcuts)

    def apply_spotify_lifecycle(self,persist=True):
        enabled=self.spotify_lifecycle.get()
        try:
            configure_startup(self.store.directory,enabled)
        except OSError:
            self.spotify_lifecycle.set(self.settings.get('spotify_lifecycle',False))
            raise
        if persist:self.save_settings()
        if enabled!=self.lifecycle.enabled:self.lifecycle.configure(enabled)
        if enabled:ensure_watcher(self.store.directory)
        self.notify('Spotify 실행·종료에 맞춰 가사창을 열고 닫습니다.' if enabled else
                    'Spotify 동시 실행·종료를 해제했습니다.')

    def toggle_spotify_lifecycle(self):
        self.spotify_lifecycle.set(not self.spotify_lifecycle.get())
        self.apply_spotify_lifecycle()

    def shortcut_text(self,action):
        return shortcut_label(validate_bindings(self.settings.get('hotkeys'))[action])

    def save_shortcuts(self,bindings):
        bindings=validate_bindings(bindings)
        updated={**self.settings,'hotkeys':bindings}
        self.store.save_settings(updated);self.settings=updated;self.hotkeys.configure(bindings)
        if self.locked and bindings['lock'] is None:self.toggle_lock()
        if self.borderless.get() and bindings['chrome'] is None:self.toggle_chrome()
        self.last_render=None;self.render()

    def request_fit_height(self):
        if self.closed or self.closing:
            return
        if not self.manual_window_size:
            width=max(240,self.root.winfo_width())
            self.reserve_lyric_rows(width,1.0,set())
        if self.pending_height_fit is not None:
            self.root.after_cancel(self.pending_height_fit)

        def after_layout():
            # The lyric content and its parent each propagate size at idle.
            self.pending_height_fit = self.root.after_idle(self.fit_height)

        self.pending_height_fit = self.root.after_idle(after_layout)

    def fit_height(self):
        self.pending_height_fit = None
        if self.closed or not self.root.winfo_viewable():
            return
        if self.manual_window_size:
            self.fit_compact_layout()
            return
        requested = self.lyric_frame.winfo_reqheight() + self.footer_height + 20
        if not self.borderless.get():
            requested += self.tools.winfo_reqheight() + 7
            if self.status.winfo_manager():
                requested += self.status.winfo_reqheight() + 8
        if self.root.winfo_height() < requested or self.borderless.get():
            width, current_height, x, current_y = self.overlay_rect()
            area = monitor_work_area((width, current_height, x, current_y))
            if area is None:
                return
            _, top, _, bottom = area
            height = max(self.root.minsize()[1], min(requested, bottom-top-70))
            y = max(top+30, min(current_y, bottom-height-35))
            if height != current_height or y != current_y:
                self.root.geometry(f'{width}x{height}{window_position(x, y)}')

    def lyric_row_heights(self,width,scale,hidden):
        gutter=self.lyric_gutter(scale,hidden)
        user_override=self.user_lyric_override()
        context=(width,self.display_lines,self.line_readings,self.lyric_text_edits.rows,user_override,
                 float(self.root.tk.call('tk','scaling')))
        if context!=self.lyric_measure_context:
            self.lyric_measure_context=context
            self.lyric_measurements.clear()
        heights={}
        for key,label in self.lyric_labels.items():
            if (key in hidden or key=='reading' and not self.show_reading.get()
                    or key=='original' and not self.show_original.get()
                    or key=='korean' and not self.text_styles[key].get('visible',True)):
                heights[key]=0
                continue
            style=self.text_styles[key]
            font=(style['family'],max(1,round(style['size']*scale)),
                  'bold' if style['bold'] else 'normal')
            padding=outline_padding(scaled_outline(style,scale))
            wrap=max(80,width-44-2*gutter-2*padding)
            fallback='' if self.display_lines else label.cget('text')
            cache_key=(key,font,wrap,padding,fallback,self.show_original.get() if key=='korean' else None)
            if cache_key not in self.lyric_measurements:
                if key=='reading':
                    values=(self.display_lyric_reading(i) for i in range(len(self.display_lines)))
                elif key=='korean':
                    values=(self.display_lyric_translation(i, self.show_original.get())
                            for i in range(len(self.display_lines)))
                else:
                    values=(getattr(line,key) for line in self.display_lines)
                height=0
                probe=label.create_text(0,0,font=font,width=wrap,anchor='nw')
                for value in dict.fromkeys(values if self.display_lines else (fallback,)):
                    if not value:continue
                    label.itemconfigure(probe,text=value)
                    box=label.bbox(probe)
                    if box:height=max(height,box[3]+2*padding)
                label.delete(probe)
                self.lyric_measurements[cache_key]=height
            heights[key]=self.lyric_measurements[cache_key]
        return heights

    def reserve_lyric_rows(self,width,scale,hidden):
        heights=self.lyric_row_heights(width,scale,hidden)
        reserved=0
        used=0
        for key,label in self.lyric_labels.items():
            padding=tuple(round(value*scale) for value in LYRIC_PADDING[key])
            if heights[key]:
                reserved+=heights[key]+sum(padding)
            if heights[key] and label.cget('text'):
                label.grid(pady=padding)
                used+=label.winfo_reqheight()+sum(padding)
            else:
                label.grid_remove()
        # Keep wrapped-line space outside the displayed lyric group.
        spare=max(0,reserved-used)
        singer_only=(not reserved and 'singers' not in hidden and self.text_styles['singers'].get('visible',True)
                     and self.current_singers.cget('text'))
        self.lyric_content.rowconfigure(0,minsize=max(1,round(MARKER_HEIGHT*scale)) if singer_only else spare//2)
        self.lyric_content.rowconfigure(5,minsize=spare-spare//2)

    def fit_compact_layout(self):
        width,height=self.root.winfo_width(),self.root.winfo_height()
        labels={'title':self.track_title_label,'artists':self.track_artist_label,
                'reading':self.reading_label,'original':self.original_label,'korean':self.korean_label}
        styles=self.text_styles
        policy=self.compact_layout
        hidden=set()
        available=height-self.footer_height-20
        error,expires=getattr(self,'error_notice',('',0))
        mode_visible=not self.playback.title or (error and time.monotonic()<expires)

        def content_height(scale,hidden,controls):
            total=0
            for key,row_height in self.lyric_row_heights(width,scale,hidden).items():
                if row_height:
                    total+=row_height+sum(round(value*scale) for value in LYRIC_PADDING[key])
            metadata=[key for key in ('title','artists')
                      if self.playback.title.strip() and key not in hidden and styles[key].get('visible',True)]
            for key in metadata:
                options=text_style_options(styles[key],scale)
                total+=self.track_credit_height(labels[key],options['font'],options['outline'])
            if len(metadata)==2:total+=round(3*scale)
            if metadata:total+=round(8*scale)
            if self.singer_input_index is not None or (mode_visible and not controls):
                total+=self.mode_label.winfo_reqheight()+2
            if not controls:
                if not self.borderless.get():
                    total+=self.tools.winfo_reqheight()+7
                    if self.status.cget('text'):total+=self.status.winfo_reqheight()+8
            return total

        controls=width<760 or content_height(1,hidden,False)>available
        priorities=policy['priorities']
        stages=sorted(set(priority for priority in priorities.values() if priority))
        if policy['mode']=='auto_hide':
            for threshold,priority in zip((760,540,440,360,300,260),stages):
                if width>=threshold and content_height(1,hidden,controls)<=available:break
                hidden.update(key for key,value in priorities.items() if value==priority)
        shrink_only=policy['mode']=='shrink_only' or not stages
        scale=min(1.0,width/(760 if shrink_only else 440))
        while scale>.025 and content_height(scale,hidden,controls)>available:
            scale=max(.025,round(scale-.025,3))
        self.compact_hidden=hidden
        self.compact_controls=controls
        self.compact_metadata={'title','artists'}.issubset(hidden)
        self.compact_reading='reading' in hidden
        self.lyric_scale=scale
        self.current_singers.set_compact('singers' in hidden or not styles['singers'].get('visible',True))
        self.current_singers.set_scale(scale)
        for key,label in labels.items():
            label.configure(**text_style_options(styles[key],scale))
        self.reserve_lyric_rows(width,scale,hidden)
        if self.borderless.get() or controls:
            self.tools.grid_remove()
        else:
            self.tools.grid()
        self.update_notice_visibility()
        self.update_track_title()

    def lyric_gutter(self, scale, hidden):
        singers = self.text_styles['singers'].get('visible',True) and 'singers' not in hidden
        return round(LYRIC_GUTTER*scale) if singers else 0

    def layout_side_lyrics(self, width, scale, hidden):
        gutter = self.lyric_gutter(scale,hidden)
        side_width = side_preview_width(width,self.lyric_labels.values())
        self.lyric_content.grid_configure(padx=gutter)
        for key,label in self.side_lyrics.items():
            if not side_width or not label.cget('text') or not self.text_styles[key].get('visible',False):
                label.place_forget()
                continue
            label.configure(**text_style_options(self.text_styles[key],scale))
            place_side_preview(label,self.lyric_frame,self.lyric_content,key,side_width)

    def notify(self, text, seconds=7):
        self.status.configure(text=text)
        self.notice_until = time.monotonic() + seconds
        self.update_notice_visibility()
        self.request_fit_height()

    def update_notice_visibility(self):
        if self.status.cget('text') and not (self.borderless.get() or self.compact_controls):
            self.status.grid()
        else:
            self.status.grid_remove()

    def overlay_rect(self):
        return (self.root.winfo_width(), self.root.winfo_height(), self.root.winfo_x(), self.root.winfo_y())

    def overlay_artwork_widgets(self):
        return (self.track_title_label,self.track_artist_label,*self.lyric_labels.values(),
                self.mode_label,self.playback_badge,*self.side_lyrics.values(),*self.current_singers.columns)

    def set_electron_overlay_active(self, active, hwnd=None):
        if self.closed or self.closing:
            return
        process = self.workspace.process
        self.hotkeys.set_external_overlay(hwnd if active else None, process.pid if process else None)
        if self.electron_overlay_active == active:
            return
        self.electron_overlay_active = self.root.electron_overlay_active = active
        # Keep a mapped Tk layout and dialog owner behind the Electron window.
        self.root.overrideredirect(active or self.borderless.get())
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', 0.0 if active else max(.15,min(1,self.opacity.get())))
        set_click_through(self.root, active or self.locked)
        self.sync_overlay_backdrop()
        for label in self.overlay_artwork_widgets():
            if active:
                label._hide_layer()
            else:
                label._schedule_layer()

    def sync_overlay_backdrop(self):
        self.backdrop.color = self.background_color
        self.backdrop.opacity = (0 if self.electron_overlay_active or self.transparent.get()
                                 else max(0,min(1,self.background_opacity.get())))
        self.backdrop.sync()

    def electron_overlay_font(self, widget):
        font = widget.cget('font')
        scaling = float(self.root.tk.call('tk','scaling'))
        key = (str(font), scaling)
        if key not in self.electron_overlay_fonts:
            actual = self.root.tk.splitlist(widget.tk.call('font','actual',font))
            options = dict(zip(actual[::2], actual[1::2]))
            size = int(font[1] if isinstance(font,tuple) else options['-size'])
            pixels = size * scaling if size > 0 else -size
            line_height = int(widget.tk.call('font','metrics',font,'-linespace'))
            self.electron_overlay_fonts[key] = (dict(family=options['-family'],size=pixels,
                                                      bold=options['-weight']=='bold'), line_height)
        return self.electron_overlay_fonts[key]

    def electron_overlay_image(self, widget):
        image = widget._artist_image
        if image is None:
            self.electron_overlay_images.pop(widget,None)
            return {}
        old, encoded = self.electron_overlay_images.get(widget,(None,None))
        if image is not old:
            with BytesIO() as stream:
                image.save(stream,format='PNG')
                encoded = b64encode(stream.getvalue()).decode('ascii')
            self.electron_overlay_images[widget] = (image,encoded)
        return dict(png=encoded,imageWidth=image.width,imageHeight=image.height)

    def electron_overlay_snapshot(self, _position):
        width,height,x,y = self.overlay_rect()
        visible = bool(self.root.winfo_viewable())
        result = dict(bounds=dict(x=x,y=y,width=width,height=height),visible=visible,
                      locked=self.locked,borderless=self.borderless.get(),minSize=list(self.root.minsize()),
                      current=self.visible_lyric_index() if self.display_lines else None,
                      backgroundColor=self.background_color,
                      backgroundOpacity=0 if self.transparent.get() else max(0,min(1,self.background_opacity.get())),
                      textOpacity=max(.15,min(1,self.opacity.get())))
        if not visible:
            return result

        left,top = self.root.winfo_rootx(),self.root.winfo_rooty()

        def rect(widget):
            return [widget.winfo_rootx()-left,widget.winfo_rooty()-top,
                    widget.winfo_width(),widget.winfo_height()]

        labels = {}
        for name,widget in {'title':self.track_title_label,'artists':self.track_artist_label,
                            **self.lyric_labels,**self.side_lyrics,'mode':self.mode_label,
                            'clock':self.playback_badge,'status':self.status,'notice':self.transport_notice}.items():
            if not widget.winfo_ismapped() or not (text := widget.cget('text')):
                continue
            font,line_height = self.electron_overlay_font(widget)
            label = dict(rect=rect(widget),text=text,font=font,lineHeight=line_height,color=widget.cget('fg'))
            if isinstance(widget, SubtitleLine):
                outline = widget.cget('outline')
                align = widget.cget('justify')
                edge = outline_padding(outline)
                label.update(outline=list(outline),align=align,padding=[3,edge,0,edge],
                             **self.electron_overlay_image(widget))
                if isinstance(widget,ArtistHeader):
                    label.update(scrollable=widget.overflow,spans=widget.cget('outline_spans'))
                if ellipsis := widget.cget('ellipsis'):
                    items = widget.find_withtag('lyric')
                    if items:
                        label['displayText'] = widget.itemcget(items[-1],'text')
                    label['ellipsis'] = ellipsis
            else:
                label.update(align=widget.cget('justify'),outline=[0,0,None],padding=[1,2,0,2])
                if name == 'status':
                    label['background'] = widget.cget('bg')
            labels[name] = label

        buttons = {}
        tools = dict(appearance=self.primary_buttons[0],lock=self.lock_button,
                     chrome=self.primary_buttons[2],menu=self.more_button)
        transport = dict(previous=self.previous_track_button,first=self.track_start_button,
                         play=self.play_pause_button,volume=self.volume_button,
                         last=self.track_end_button,next=self.next_track_button)
        for name,widget in {**tools,**transport}.items():
            if widget.winfo_ismapped():
                button = dict(rect=rect(widget),text=widget.cget('text'),disabled=str(widget.cget('state'))=='disabled')
                if name in transport:
                    button['kind'] = 'pause' if name == 'play' and self.playback.playing else name
                else:
                    button['font'] = self.electron_overlay_font(widget)[0]
                buttons[name] = button

        singers = []
        for widget in self.current_singers.columns:
            if widget.winfo_ismapped() and (image := self.electron_overlay_image(widget)):
                singers.append(dict(rect=rect(widget),hint=widget.hint.text,**image))
        if self.electron_overlay_icons is None:
            self.electron_overlay_icons = {kind:b64encode(image.tk.call(str(image),'data','-format','png')).decode('ascii')
                                           for kind,image in self.transport_images.items()}
        result.update(labels=labels,buttons=buttons,singers=singers,icons=self.electron_overlay_icons,
                      toolsRect=rect(self.tools) if self.tools.winfo_ismapped() else None)
        return result

    def publish_electron_overlay(self):
        self.electron_overlay_publish = None
        if self.closed or self.closing:
            return
        # Grid changes queued by render must land before taking the snapshot.
        if self.pending_height_fit is not None:
            self.electron_overlay_publish = self.root.after_idle(self.publish_electron_overlay)
            return
        self.workspace.publish_developer_playback(self.electron_overlay_position)

    def electron_overlay_action(self, action, payload):
        if self.closed or self.closing or not self.electron_overlay_active:
            return False
        if action == 'bounds':
            bounds = payload.get('bounds')
            if not isinstance(bounds,dict):
                raise ValueError('가사창 크기와 위치를 확인하세요.')
            width,height,x,y = (bounds.get(key) for key in ('width','height','x','y'))
            if (any(type(value) is not int or abs(value)>32768 for value in (width,height,x,y))
                    or width<240 or height<100):
                raise ValueError('가사창 크기와 위치를 확인하세요.')
            if self.locked:
                return False
            resized = payload.get('resize') is True and not self.manual_window_size
            if resized:
                self.manual_window_size = True
                self.root.minsize(240,100)
            min_width,min_height = self.root.minsize()
            bounds = visible_overlay_rect((max(width,min_width),max(height,min_height),x,y))
            if resized or bounds != self.overlay_rect():
                width,height,x,y = bounds
                self.root.geometry(f'{width}x{height}{window_position(x,y)}')
                self.root.update_idletasks()
                self.root.after_idle(lambda:self.safe(self.save_settings,interactive=False))
            width,height,x,y = self.overlay_rect()
            return dict(x=x,y=y,width=width,height=height)
        elif action == 'menu':
            if self.locked:
                return False
            return [None if item is None else {key:value for key,value in item.items() if key!='command'}
                    for item in self.menu_items()]
        elif action == 'menuSelect':
            index = payload.get('index')
            items = self.menu_items()
            if type(index) is not int or not 0<=index<len(items) or items[index] is None:
                raise ValueError('지원하지 않는 가사창 메뉴입니다.')
            if self.locked:
                return False
            items[index]['command']()
        elif action == 'key':
            self.electron_overlay_key(payload.get('key'),payload.get('phase'))
        else:
            actions = {'appearance':self.show_appearance_settings,'lock':self.toggle_lock,'chrome':self.toggle_chrome,
                       'previous':lambda:self.change_track('previous'),'next':lambda:self.change_track('next'),
                       'first':lambda:self.seek_track('first'),'last':lambda:self.seek_track('last'),
                       'play':self.toggle_playback,'volume':self.open_volume,'hide':self.toggle_overlay_visibility,
                       'close':lambda:self.root.after_idle(self.close)}
            if action not in actions:
                raise ValueError('지원하지 않는 가사창 동작입니다.')
            if self.locked:
                return False
            actions[action]()
        return True

    def electron_overlay_key(self, key, phase):
        aliases = {'ArrowLeft':'Left','ArrowRight':'Right','ArrowUp':'Up','ArrowDown':'Down',
                   'Enter':'Return',' ':'space','Space':'space'}
        if self.locked or not isinstance(key,str):
            return
        event = SimpleNamespace(widget=self.root,keysym=aliases.get(key,key),state=0)
        if phase == 'down':
            if key in ('ArrowLeft','ArrowRight'):
                self.lyric_arrow(event)
            elif key == 'Escape':
                self.stop_singer_input()
        elif phase == 'up' and (key in ('ArrowUp','ArrowDown','Enter','Delete','Backspace',' ','Space')
                                or len(key)==1 and key in SINGER_KEYS + ''.join(PLAYBACK_STEP_KEYS)):
            self.overlay_key_release(event)

    def watch_overlay_position(self, activation):
        if self.closing or self.closed:
            return
        requested = activation is not None and activation.consume()
        if requested:
            self.root.deiconify()
            if self.electron_overlay_active:
                set_click_through(self.root,True)
            else:
                self.root.lift()
                self.root.update_idletasks()
        dragging = self.drag_origin is not None or self.resize_origin is not None
        if self.root.winfo_viewable() and (requested or not dragging):
            current = self.overlay_rect()
            rect = visible_overlay_rect(current, fully_visible=requested)
            if rect != current:
                width, height, x, y = rect
                self.root.geometry(f'{width}x{height}{window_position(x, y)}')
                self.root.after_idle(lambda: self.safe(self.save_settings, interactive=False))
        if requested and self.electron_overlay_active:
            self.workspace.send({'activateOverlay':True})
        self.root.after(500, self.watch_overlay_position, activation)

    def save_settings(self):
        self.settings.update(online=self.online.get(), opacity=self.opacity.get(),
                             font_size=self.font_size.get(), show_reading=self.show_reading.get(),
                             show_original=self.show_original.get(), borderless=self.borderless.get(),
                             transparent=self.transparent.get(), use_estimate=self.use_estimate.get(),
                             lyric_advance_ms=self.lyric_advance_ms)
        self.settings.update(background_opacity=self.background_opacity.get(),background_color=self.background_color,
                             text_styles=self.text_styles,auto_audio=self.auto_audio.get(),realtime_tracking=self.realtime_tracking.get(),
                             web_auto=self.web_auto.get(),reader_mode=self.reader_mode.get(),
                             public_background=self.public_background.get(),lyricsplus_background=self.lyricsplus_background.get(),
                             alsong_background=self.alsong_background.get(),spotify_lifecycle=self.spotify_lifecycle.get())
        self.settings['manual_window_size']=self.manual_window_size
        self.settings['compact_layout']=self.compact_layout
        if self.root.winfo_viewable():
            self.settings['window_rect'] = list(self.overlay_rect())
        self.store.save_settings(self.settings)

    def apply_appearance(self, *_):
        self.root.attributes('-alpha', 0.0 if self.electron_overlay_active else max(.15,min(1,self.opacity.get())))
        color = TRANSPARENT
        self.root.attributes('-transparentcolor', TRANSPARENT)
        for widget in (self.root,self.lyric_frame,self.lyric_content,self.track_metadata,self.transport,self.reading_label,self.original_label,
                       self.korean_label,self.mode_label,self.track_title_label,self.track_artist_label,self.transport_notice,self.playback_badge,
                       *self.side_lyrics.values(),*self.current_singers.columns):
            widget.configure(bg=color)
        self.text_styles['original']['size']=self.font_size.get()
        for key,widget in {**self.lyric_labels,'title':self.track_title_label,
                           'artists':self.track_artist_label,**self.side_lyrics}.items():
            widget.configure(**text_style_options(self.text_styles[key],self.lyric_scale))
        self.current_singers.set_style(self.text_styles['singers'])
        self.current_singers.set_compact('singers' in self.compact_hidden or not self.text_styles['singers'].get('visible',True))
        self.sync_overlay_backdrop()
        self.last_render = None
        self.render()

    def toggle_chrome(self):
        self.borderless.set(not self.borderless.get())
        self.root.overrideredirect(self.electron_overlay_active or self.borderless.get())
        if self.borderless.get():
            self.tools.grid_remove()
            self.root.minsize(240,100)
        else:
            self.tools.grid()
            self.root.minsize(240,100) if self.manual_window_size else self.root.minsize(560,210)
        self.update_notice_visibility()
        self.root.attributes('-topmost', True)
        if self.electron_overlay_active or self.locked:
            set_click_through(self.root, True)
        self.apply_appearance()
        self.save_settings()
        self.request_fit_height()

    def toggle_transparent(self):
        self.transparent.set(not self.transparent.get())
        self.apply_appearance()
        self.save_settings()

    def start_drag(self, event):
        self.input_status['clicks']+=1
        self.lyric_click=False
        if not self.locked:
            self.root.focus_force()
            self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())
            self.lyric_click=True

    def finish_drag(self, event):
        self.drag_origin = None
        self.lyric_click=False

    def toggle_singer_input(self):
        if self.singer_input_index is not None:
            self.stop_singer_input()
        elif self.manual_sync_dialog and self.singer_edits.key and self.display_lines:
            self.singer_input_index=self.visible_lyric_index()
            self.singer_input_changed_row=None
            self.hotkeys.lyric_input_window=self.root
            self.render()

    def visible_lyric_index(self):
        index=self.last_render[1] if self.last_render else self.displayed_index()
        return 0 if index is None else index

    def stop_singer_input(self, event=None, *, redraw=True):
        if self.singer_input_index is None:return
        self.singer_input_index=None
        self.singer_input_live=False
        self.hotkeys.lyric_input_window=None
        if redraw:self.render()
        return 'break'

    def singer_key(self, event):
        if self.singer_input_index is None or self.locked or event.state & (0x1|0x4|0x20000):return
        key=event.keysym.removeprefix('KP_')
        if self.seek_by_key(key):return 'break'
        if key in ('Return','Enter'):
            if self.singer_input_live:
                self.singer_input_index=self.visible_lyric_index()
            else:
                self.follow.set(True)
                self.manual_index=None
            self.singer_input_live=not self.singer_input_live
            self.render()
            return 'break'
        return self.singer_assignment_key(key)

    def singer_assignment_key(self, key, *, index=None):
        if self.locked or (index is None and self.singer_input_index is None):return
        if index is None:index=self.visible_lyric_index()
        source_index=self.annotation_row(index)
        if key=='space':
            names=()
        elif key=='BackSpace':
            names=None
        elif len(key)==1 and key in SINGER_KEYS:
            slot=SINGER_KEYS.index(key)
            if slot>=len(self.singer_options):return 'break'
            name=self.singer_options[slot]
            previous=self.singer_edits.rows.get(source_index,()) if self.singer_input_changed_row==index else ()
            selected=set(previous)
            selected.symmetric_difference_update((name,))
            order=dict.fromkeys((*self.singer_options,*previous))
            names=tuple(candidate for candidate in order if candidate in selected)
        else:return

        def save():
            self.singer_edits.save(source_index,names,keep_carry=key=='space')
            self.singer_input_changed_row=index if names else None
            self.render()
        self.safe(save)
        return 'break'

    def overlay_key_release(self, event):
        if self.locked or event.state & (0x1|0x4|0x20000):return
        if self.manual_sync_dialog:
            return self.manual_sync_dialog.key_release(event)

    def lyric_arrow(self, event):
        self.input_status['arrows']+=1;self.input_status['modifiers']=event.state
        # Plain arrows belong to the focused overlay, not editor inputs or
        # configurable global shortcuts containing Ctrl, Alt or Shift.
        # Windows Tk reports NumLock as 0x8; Alt is 0x20000. Extended
        # arrow keys also carry 0x40000, which is not a held modifier.
        if self.locked or event.state & (0x1|0x4|0x20000):return
        key=0x25 if event.keysym=='Left' else 0x27
        if not self.manual_sync_dialog and self.singer_input_index is None and any(
               binding and binding['key']==key and binding['modifiers']==0
               for binding in self.hotkeys.bindings.values()):return 'break'
        self.safe(lambda:self.move(-1 if event.keysym=='Left' else 1))
        return 'break'

    def drag_window(self, event):
        if self.drag_origin and not self.locked:
            x0,y0,wx,wy = self.drag_origin
            if self.lyric_click and max(abs(event.x_root-x0),abs(event.y_root-y0))<4:return
            self.lyric_click=False
            self.root.geometry(window_position(wx + event.x_root - x0, wy + event.y_root - y0))

    def popup_menu(self, menu, x, y):
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def show_menu(self, event):
        self.popup_menu(self.context_menu, event.x_root, event.y_root)

    def show_more(self):
        self.popup_menu(self.context_menu, self.more_button.winfo_rootx(),
                        self.more_button.winfo_rooty()+self.more_button.winfo_height())

    def show_tray_menu(self, x, y):
        if self.tray and not self.closing and not self.closed:
            self.tray.popup(x, y, self.menu_items(tray=True))

    def toggle_overlay_visibility(self):
        if self.closed or self.closing:
            return
        if self.root.winfo_viewable():
            self.safe(self.save_settings, interactive=False)
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.lift()
            if self.electron_overlay_active or self.locked:
                set_click_through(self.root, True)
        self.backdrop.sync()

    def effective_times(self):
        if not self.song:
            self.time_sources=[]
            return [], False
        duration = self.playback.duration if self.is_bound() else 0
        known = self.timing['times']
        local_job = None
        if duration and (self.store.directory / 'sync_reanalysis.sqlite3').is_file():
            from sync_reanalysis import applied_song, apply_job_times
            from recording_context import recording_key
            from sqlite3 import Error

            now = time.monotonic()
            key = (self.song.id, self.song.fingerprint, self.playback.key, duration, id(self.timing),
                   self.timing.get('offset', 0), tuple(known))
            cached, checked, stamps = getattr(self, 'reanalysis_playback_cache', (None, 0, None))
            if key != cached or now - checked >= 1:
                try:
                    recording = recording_key(self.playback.title, self.playback.artist, self.playback.album)
                    stamps = applied_song(self.store.directory, self.song, self.timing,
                                          (recording, self.playback.key), duration)
                except (Error, OSError, ValueError):
                    stamps = known
                self.reanalysis_playback_cache = key, now, stamps
            known = stamps
            local_job = self.current_local_analysis(verified_playlist=True)
            if local_job and local_job['result'].get('alignment', {}).get('reanalysis_rows'):
                known = apply_job_times(local_job, self.timing, known, duration)
        origins=timing_origins(self.timing)
        # A completed analysis is still inferred. Only edits made by the user
        # can displace an observed/provider onset for this recording.
        protected=[origin=='manual' and self.timing.get('source')!='LRCLIB' for origin in origins]
        direct=self.direct_times() if duration else []
        observed=[]
        if any(t is not None for t in direct):
            base={'offset':0,'times':[t if keep else None for t,keep in zip(known,protected)]}
            # Provider times use the recording clock; the user's offset still delays them.
            base=merge_candidates(base,direct)[0]
            bounds=self.direct_map_cache[5]
            candidates=[stamp if stamp is not None and all(stamp>value if row<i else stamp<value
                         for row,value in bounds) else None for i,stamp in enumerate(known)]
            known=merge_candidates(base,candidates)[0]['times']
        elif duration:
            try:observed=self.native_timing.select(self.song,duration)
            except (ValueError,OSError) as error:
                observed=[]
                self.error_notice=(str(error),time.monotonic()+6)
            if any(t is not None for t in observed):
                base={'times':[t if keep else None for t,keep in zip(known,protected)],'offset':self.timing.get('offset',0)}
                base=merge_candidates(base,observed)[0]
                known=merge_candidates({**base,'offset':0},known)[0]['times']
        mp3_times=[]
        if duration and any(stamp is None for stamp in known):
            job=local_job or self.current_local_analysis(verified_playlist=True)
            if job:
                from local_analysis import playlist_auto_times
                raw=playlist_auto_times(job)
                if len(raw)==len(known):
                    bounds=self.direct_map_cache[5] if any(stamp is not None for stamp in direct) else ()
                    candidates=[stamp if stamp is not None and stamp<duration and all(
                        stamp>value if row<i else stamp<value for row,value in bounds) else None
                        for i,stamp in enumerate(raw)]
                    before=known
                    known=merge_candidates({'times':known,'offset':0},candidates)[0]['times']
                    mp3_times=[stamp if old is None else None for old,stamp in zip(before,known)]
        reference=[]
        tracking=self.current_reference()
        if duration and tracking:
            bounds=self.direct_map_cache[5] if any(t is not None for t in direct) else ()
            reference=[stamp if stamp is not None and all(stamp>value if row<i else stamp<value
                       for row,value in bounds) else None for i,stamp in enumerate(tracking[2])]
            before=list(known)
            known=merge_candidates({'times':known,'offset':0},reference)[0]['times']
            reference=[stamp if old is None else None for old,stamp in zip(before,reference)]
        bounded=any(t is not None for t in direct)
        eligible=self.direct_map_cache[3] if bounded else ()
        key = (self.song.fingerprint, tuple(known), duration, self.use_estimate.get(), eligible,
               self.direct_map_cache[5] if bounded else (),
               self.timing.get('estimate_intro',12), self.timing.get('estimate_outro',8))
        if self.time_cache is None or self.time_cache[0] != key:
            times = estimate_times(self.song.lines, duration, known, key[-2], key[-1]) if self.use_estimate.get() else list(known)
            if bounded:
                anchors=[i for i,t in enumerate(known) if t is not None]
                # Only known provider text between actual onsets may be timed
                # by interpolation. Added backing lyrics and open ends wait.
                times=[t if known[i] is not None or (t is not None and eligible[i] and anchors[0]<i<anchors[-1]
                       and all(t>value if row<i else t<value for row,value in self.direct_map_cache[5]))
                       else None for i,t in enumerate(times)]
            estimated = any(a is None and b is not None for a,b in zip(known,times))
            self.time_cache = key, times, estimated
        self.time_sources=[]
        for i,stamp in enumerate(self.time_cache[1]):
            source='자동 대기'
            if stamp is not None:
                if protected[i] and stamp==self.timing['times'][i]:source='수동'
                elif direct and direct[i] is not None and stamp==direct[i]:source=self.direct_map_cache[4][i]
                elif known[i] is None:source='추정'
                elif observed and observed[i] is not None and stamp==round(observed[i]-self.timing.get('offset',0),3):source='Spotify 관찰'
                elif mp3_times and mp3_times[i] is not None:source=MP3_SYNC
                elif reference and reference[i] is not None and stamp==reference[i]:source='참고 싱크'
                else:source='자동'
            self.time_sources.append(source)
        return self.time_cache[1], self.time_cache[2]

    def align_now(self):
        if not self.is_bound():
            self.notify('현재 Spotify 곡과 연결된 가사를 선택하세요.')
            return
        index = self.visible_lyric_index() if self.singer_input_index is not None else self.displayed_index()
        if index is None:
            self.notify('위/아래 키나 목록으로 현재 들리는 구절을 먼저 선택하세요.')
            return
        position=self.sync_capture_position()
        updated=edit_manual_time(self.timing,index,round(max(0,position-self.timing['offset']),3))
        updated['source']='직접 편집 / 자동 싱크'
        self.save_current_timing(updated)
        self.timing = updated
        if self.editor:self.editor_refresh()
        if self.singer_input_index is None:
            self.resume_follow()
        else:
            self.render()
        self.notify(f'{index+1}번만 지금에 맞췄습니다. 나머지 구절은 자동 싱크를 이어갑니다.')

    def update_estimate(self):
        if self.song:
            intro, outro = float(self.intro_entry.get()), float(self.outro_entry.get())
            if not all(math.isfinite(x) and x >= 0 for x in (intro,outro)):
                raise ValueError('전주·후주 시간은 0 이상의 숫자여야 합니다.')
            updated = {**self.timing, 'estimate_intro':intro, 'estimate_outro':outro}
            self.save_current_timing(updated)
            self.timing = updated
        self.save_settings()
        self.resume_follow()
        if self.editor:
            self.editor_refresh()

    def select_song(self, song):
        # Load first: a failed read must not pair a new song with old timestamps.
        temporary=self.live_lyrics.is_temporary(song)
        timing=self.live_lyrics.timing if temporary else (self.store.timing(song) if song else None)
        display_lines=song.lines if temporary else (self.lyric_annotations.display_lines(song) if song else ())
        same=bool(self.song and song and self.song.id==song.id and tuple(x.original for x in self.song.lines)==tuple(x.original for x in song.lines))
        if not same:
            self.clear_realtime()
            self.spotify_current=None;self.spotify_observed_at=0
            self.spotify_source_index=None;self.spotify_anchor=None
            self.spotify_sample_after=time.monotonic()
            self.native_timing.reset_observation()
        self.spotify_mapping=None
        self.song = song
        self.display_lines=self.live_lyrics.display(song,display_lines)
        self.vocalise_rows=tuple(i for i,line in enumerate(self.display_lines) if is_vocalise(line.original))
        self.translated_count=sum(bool(line.korean) for line in self.display_lines)
        self.timing = timing
        if not same:
            self.cursor = 0
            self.manual_index = None
            self.follow.set(True)
        self.last_render = None
        self.render()
        if self.editor:
            self.editor_refresh()

    def is_bound(self):
        if self.live_lyrics.is_temporary(self.song):
            return self.live_lyrics.track==self.playback.key and self.playback.has_timeline
        linked = self.settings.get('links', {}).get(self.playback.key)
        matched = match_recording(self.songs, self.playback.title, self.playback.artist, self.playback.album, linked)
        return self.song and matched and self.song.id == matched.id and self.playback.has_timeline

    def screen_lyrics_allowed(self):
        if self.temporary_alsong or self.lyrics_deleted():return False
        # A minimized desktop can retain the previous song's lyric DOM after its
        # window title changes. Background providers must never trust that text.
        return not (self.online.get() and (self.settings.get('spotify_connected') or self.public_background.get()
                                         or self.lyricsplus_background.get() or self.alsong_background.get()))

    def stop_screen_lyrics(self):
        self.screen_lyrics.observe(self.playback,needed=False,allow_ocr=False)
        self.spotify_current=None;self.spotify_observed_at=0
        self.spotify_source_index=None;self.spotify_anchor=None
        self.native_timing.reset_observation()
        if self.song is not None and self.song is self.screen_song:
            self.live_lyrics.clear();self.select_song(None)
            self.screen_cache_tainted=False
        self.screen_song=None
        self.reader_bridge.clear_reference(self.playback.key)
        if self.song:self.reader_bridge.set_reference(self.playback.key,[line.original for line in self.song.lines],complete=True)
        # A running document search may still hold the discarded screen references.
        if self.internal_reader.job:self.internal_reader.job.cancel();self.internal_reader.job=None
        self.internal_reader.last_key=''

    def set_playback(self, snapshot):
        self.recording_contexts.observe(snapshot)
        job=self.audio_job
        context=getattr(job,'recording_context',None)
        if job and context and not job.cancelled.is_set() and not self.recording_contexts.valid(context,job.song):
            job.cancelled.set()
            if self.audio_manual_progress:self.audio_manual_progress.update({'kind':'progress'},cancelled=True)
            threading.Thread(target=job.cancel,daemon=True,name='manual-audio-duration-cancel').start()
        changed = snapshot.key != self.playback.key
        cache_changed=changed or abs(snapshot.duration-self.playback.duration)>.5 or (snapshot.has_timeline and not self.playback.has_timeline)
        seeked=not changed and snapshot.has_timeline and self.playback.has_timeline and abs(snapshot.position()-self.playback.position())>2
        if seeked:
            self.clear_realtime()
            self.spotify_current=None;self.spotify_observed_at=0;self.spotify_source_index=None;self.spotify_anchor=None
            self.spotify_sample_after=time.monotonic()
            self.native_timing.reset_observation()
        self.playback = snapshot
        if self.temporary_alsong:
            if changed:self.temporary_alsong=None
            elif snapshot.has_timeline and abs(snapshot.duration-self.temporary_alsong.duration)>2:
                self.unlink_temporary_alsong()
        if cache_changed:
            if self.initial_playback_paint_callback is not None:
                self.root.after_cancel(self.initial_playback_paint_callback)
                self.initial_playback_paint_callback=None
            self.sync_lookup_started=time.monotonic()
            self.bundled_manual_key=None
            self.related_cache_request=None
            self.lyric_merge_job.reset()
            self.current_lyric_merge=None
            self.merge_reader_receipts.clear()
            self.web_queried.clear()
        self.reader_bridge.observe(snapshot)
        if changed:
            self.hide_track_title_tip()
            self.direct_result=None;self.direct_map_cache=None
            self.cache_hit=False;self.cache_load_ms=None;self.cache_reuse=None;self.screen_cache_tainted=False
            self.cache_tracking=None
            self.screen_song=None
            self.spotify_current=None;self.spotify_observed_at=0
            self.screen_status='Spotify 가사 화면에서 원문 대조 대기'
            self.live_lyrics.clear()
            linked = self.settings.get('links', {}).get(snapshot.key)
            try:self.select_song(None if self.lyrics_deleted() else match_recording(self.songs, snapshot.title, snapshot.artist, snapshot.album, linked))
            except (ValueError,OSError):
                self.select_song(None)
                raise
            if self.song:self.reader_bridge.set_reference(snapshot.key,[line.original for line in self.song.lines],complete=True)
        if cache_changed and snapshot.has_timeline:
            if changed:
                # Paint the new track before the local lyric search can block Tk.
                self.root.update_idletasks()
                if self.electron_overlay_active:self.workspace.publish_developer_playback(snapshot.position())
            self.restore_cached_lyrics()
            if not self.temporary_alsong and not self.lyrics_deleted():
                try:self.restore_confirmed_alsong()
                except (ValueError,OSError) as error:
                    self.show_track_notice(f'확정한 알송 가사를 불러오지 못했습니다. {error}')
        screen_allowed=self.screen_lyrics_allowed()
        self.screen_lyrics.observe(snapshot,needed=screen_allowed and not (self.is_bound() and (self.has_saved_sync() or self.has_direct_sync() or self.has_spotify_clock())),allow_ocr=screen_allowed and not bool(self.is_bound()))
        if self.services_started:self.background_lyrics.observe(snapshot,enabled=self.online.get(),public_enabled=self.public_background.get(),cached_sync=self.reuse_complete_sync(),spotify_sync_present=self.has_spotify_timing())
        if self.services_started:self.observe_lyricsplus()
        self.observe_auto_audio()
        if (self.reader_mode.get()!='internal' and not self.lyrics_deleted() and self.web_auto.get()
                and snapshot.has_timeline and not self.lyric_merge_job.has_source('namu')):
            identity=(snapshot.key,snapshot.duration)
            if identity not in self.web_queried:
                self.web_queried.add(identity)
                self.fetch_web_lyrics(snapshot.title,snapshot.artist,snapshot.album,playback=snapshot)
        if (self.web_auto.get() and snapshot.has_timeline and not self.lyrics_deleted()
                and not self.display_lines and imas_lyrics.series_for(snapshot.album,snapshot.artist)):
            identity=('imas',snapshot.key,snapshot.duration)
            if identity not in self.web_queried:
                self.web_queried.add(identity)
                self.fetch_web_lyrics(snapshot.title,snapshot.artist,snapshot.album,playback=snapshot,imas=True)
        # Cache restore can consume the reader's freshness window on a slow
        # track change. Publish the accepted observation after that work too.
        if cache_changed:
            self.reader_bridge.observe(snapshot)
            if snapshot.has_timeline and self.initial_playback_paint_pending:
                self.initial_playback_paint_callback=self.root.after_idle(self.finish_initial_playback_paint)

    def finish_initial_playback_paint(self):
        self.initial_playback_paint_callback=None
        if self.closed or self.closing or not self.playback.has_timeline:return
        if self.pending_height_fit is not None or self.electron_overlay_publish is not None:
            self.initial_playback_paint_callback=self.root.after(10,self.finish_initial_playback_paint)
            return
        # The first exact-cache result, including an empty result, can now paint.
        self.initial_playback_paint_pending=False
        self.background_lyrics.artist_catalog_held=False
        self.displayed_artists()
        pending=self.related_cache_pending
        self.related_cache_pending=None
        if pending and pending[0] is self.related_cache_request:self.run_related_lyrics(*pending)

    def restore_cached_lyrics(self, *, related_lookup=None):
        if self.temporary_alsong or self.lyrics_deleted():return
        if self.user_lyric_override():
            cached=self.lyric_cache.load(self.playback)
            if cached:
                self.restore_merge_sources(cached)
                self.restoring_cache=True
                try:
                    if cached.get('merge') and tuple(line.original for line in cached['lines'])==tuple(
                            line.original for line in self.song.lines):
                        self.apply_merged_lyrics({**cached['merge'],'lines':self.display_lines,
                                                  'direct':cached['direct']},self.song,locked=True)
                    elif cached.get('direct'):
                        self.receive_direct_lyrics(self.playback.key,cached['direct'])
                finally:self.restoring_cache=False
            return
        started=time.perf_counter()
        self.cache_hit=False;self.cache_reuse=None;self.cache_tracking=None
        self.batch_translation_source=None
        cached=self.lyric_cache.load(self.playback)
        bundled_data=resource_dir()/'resources'/'seed_data'
        local_clock=self.timing if self.song else None
        if not local_clock or not (local_clock.get('offset') or cleared_rows(local_clock)
                or any(origin=='manual' for origin in timing_origins(local_clock))):
            cached=self.lyric_cache.prefer_bundled_manual(self.playback,cached,
                bundled_data=bundled_data,timing_store=self.live_lyrics.timing_store)
        direct=cached.get('direct') if cached else None
        if (not cached or not direct or not any(stamp is not None for stamp in direct['times'])
                or any(not line.korean.strip() for line in cached['lines'])):
            immutable=LyricCache(bundled_data/'lyrics_cache').load(self.playback)
            cached=self.lyric_cache.supplement_exact(cached,immutable)
        manual_timing=cached.get('manual_timing') if cached else None
        saved_playlist=self.playlist_lyrics.store.find(self.playback)
        self.local_playlist_track=((self.playback.key,self.playback.duration),
                                   saved_playlist['track']['id'] if saved_playlist else None)
        if saved_playlist:
            if saved_playlist.get('snapshots') and saved_playlist.get('merge_rows'):
                saved_playlist={**saved_playlist,'merge':{'snapshots':saved_playlist['snapshots'],
                                                          'rows':saved_playlist['merge_rows']}}
            track=saved_playlist['track']
            if not self.background_lyrics.artist_metadata.get(self.playback):
                self.background_lyrics.artist_metadata.put(self.playback,track)
            source=(track['title'],track['artists'][0],track['album'])
            self.batch_translation_source=(self.playback.key,source)
            saved_playlist={**saved_playlist,'lines':self.fill_batch_annotations(*source,saved_playlist['lines'])}
            if cached and not manual_timing and len(cached['lines'])==1 and len(saved_playlist['lines'])>1:
                direct=cached.get('direct')
                if (direct and direct['source']=='LyricsPlus' and direct['times']==[0.0]
                        and normalized(cached['lines'][0].original)==''.join(
                            normalized(line.original) for line in saved_playlist['lines'])):
                    # Recover the saved line breaks before restoring cached provider snapshots.
                    cached=None
            if cached:
                from lyric_annotations import merge_verified_annotations
                lines,_=merge_verified_annotations(cached['lines'],[saved_playlist['lines']])
                cached={**cached,'lines':lines}
                supplied=saved_playlist.get('direct')
                if supplied and not cached.get('merge'):
                    existing=cached.get('direct')
                    spotify=prefer_spotify_timing(existing,supplied)
                    if spotify:
                        cached['direct']=spotify
                    else:
                        old_count=sum(t is not None for t in mapped_times(existing,lines)) if existing else 0
                        new_count=sum(t is not None for t in mapped_times(supplied,lines))
                        if new_count>old_count:cached['direct']=supplied
            else:cached=saved_playlist
        from work_reuse import anniversary_work
        if (not cached and self.auto_analyzer.enabled and not anniversary_work(self.playback)
                and not (self.missing_audio_collector and self.missing_audio_collector.owns_playback)):
            # Even the bounded local fallback search must not consume the
            # opening audio while a new lyric document is still unavailable.
            bound=self.is_bound()
            self.auto_analyzer.observe(self.song if bound else None,self.playback,
                memory_only=self.live_lyrics.is_temporary(self.song),
                external_sync=bool(bound and self.has_saved_sync()),awaiting_sync=True)
        related=[]
        linked_reference=None
        clock=mapped_times(cached['direct'],cached['lines']) if cached and cached.get('direct') else []
        if not manual_timing and (not cached or not clock or any(stamp is None for stamp in clock)):
            if related_lookup is not None:
                related,linked_reference=related_lookup
            elif self.services_started:
                self.start_related_lyrics(saved_playlist)
            else:
                related,linked_reference=self.load_related_lyrics(self.playback,saved_playlist)
            if not cached and related:
                from recording_reuse import related_quality
                cached=max(related,key=related_quality)
            elif cached and not any(stamp is not None for stamp in clock):
                from recording_reuse import related_quality,related_reference
                compatible=[value for value in related if value.get('reuse',{}).get('timing_compatible')
                            and (value.get('direct') or value.get('timing'))
                            and ''.join(normalized(line.original) for line in value['lines'])
                                ==''.join(normalized(line.original) for line in cached['lines'])]
                if compatible and related_reference(cached['lines'],related) is not None:
                    best=max(compatible,key=related_quality)
                    cached={**cached,'direct':best.get('direct'),'reuse':best['reuse']}
                    if best.get('timing'):
                        source_times=[stamp+best['timing'].get('offset',0) if stamp is not None else None
                                      for stamp in best['timing']['times']]
                        cached['direct']={'source':'저장 캐시','lines':best['lines'],
                                          'times':source_times,'provider_id':None}
        if not cached:return
        # Legacy cache entries remain readable and seed the same collection
        # when a new provider supplies additional material for this recording.
        self.restore_merge_sources(cached)
        playlist_added=False
        if saved_playlist and saved_playlist is not cached and saved_playlist.get('merge'):
            playlist_added=self.restore_merge_sources(saved_playlist)
        self.cache_reuse=cached.get('reuse')
        from recording_reuse import anniversary_reference, related_reference
        clocks=tuple(clock for value in related for clock in value.get('anniversary_clocks',()))
        reference=anniversary_reference(cached['lines'],clocks)
        if reference is None:
            clocks=()
            if linked_reference:reference=related_reference(cached['lines'],[linked_reference])
            if reference is None:
                linked_reference=None
                reference=related_reference(cached['lines'],related or [cached])
        if reference is not None:
            self.cache_tracking=(self.playback.key,tuple(normalized(line.original) for line in cached['lines']),
                                 reference,clocks,linked_reference)
        self.restoring_cache=True
        try:
            linked=self.settings.get('links',{}).get(self.playback.key)
            base=match_recording(self.songs,self.playback.title,self.playback.artist,self.playback.album,linked)
            lines=cached['lines']
            if base:
                # User-saved annotations take precedence over an older cache.
                matches=match_annotations(lines,self.lyric_annotations.display_lines(base))
                lines=tuple(Line(old.original,current.reading or old.reading,current.korean or old.korean) if current else old
                            for old,current in zip(lines,matches))
            if manual_timing:
                self.bundled_manual_key=self.playback.key
                self.select_song(self.live_lyrics.receive_merged(self.playback,None,lines,manual_timing))
                if cached['direct']:self.receive_direct_lyrics(self.playback.key,cached['direct'])
            elif cached.get('merge') and not cached.get('reuse'):
                self.apply_merged_lyrics({**cached['merge'],'lines':lines,'direct':cached['direct']},base)
            else:
                self.select_song(self.live_lyrics.receive(self.playback,base,lines))
                if base:self.complete_saved_lyrics(lines)
                if cached['direct']:self.receive_direct_lyrics(self.playback.key,cached['direct'])
            # Borrow a verified clock as a cache provider, not as this
            # recording's own manual/audio timestamps. Its original recording
            # and duration remain attached when the cache is saved again.
            timing=cached.get('timing')
            if timing and tuple(normalized(line.original) for line in self.display_lines)==tuple(normalized(line.original) for line in cached['lines']):
                times=[round(stamp+timing.get('offset',0),3) if fixed else None
                       for stamp,fixed in zip(timing['times'],saved_rows(timing))]
                if any(stamp is not None for stamp in times):
                    self.receive_direct_lyrics(self.playback.key,{'source':'저장 캐시','lines':cached['lines'],
                                                                 'times':times,'provider_id':None})
            self.reader_bridge.set_reference(self.playback.key,[line.original for line in self.song.lines],complete=True)
            self.cache_hit=True
        except ValueError:
            # A user's edited library can supersede text from an older cache.
            self.bundled_manual_key=None
            self.cache_reuse=None
            self.cache_tracking=None
            self.live_lyrics.clear();self.direct_result=None;self.direct_map_cache=None
            self.current_lyric_merge=None
            self.select_song(base)
        finally:
            self.restoring_cache=False
            self.cache_load_ms=round((time.perf_counter()-started)*1000,3)
        if self.cache_hit:self.capture_translation_lyrics()
        if playlist_added:self.queue_lyric_merge()

    def load_related_lyrics(self,playback,saved_playlist):
        from playlist_lyrics_store import PlaylistLyricsStore
        # Separate stores keep a previous track's background scan from holding
        # the locks used by exact lookups and saves on the Tk thread.
        cache=LyricCache(self.lyric_cache.directory)
        playlist=PlaylistLyricsStore(self.store.directory)
        timing_store=LiveTimingStore(self.store.directory)
        reference=(playlist.find_linked_reference(playback,saved_playlist,timing_store,cache)
                   if saved_playlist else None)
        related=[value for value in (cache.load_related(playback,timing_store=timing_store),
                                     playlist.find_related(playback,timing_store=timing_store)) if value]
        return related,reference

    def start_related_lyrics(self,saved_playlist):
        if self.related_cache_request is not None:return
        request=object()
        self.related_cache_request=request
        work=(request,self.playback,saved_playlist)
        if self.related_cache_running or self.initial_playback_paint_pending:
            self.related_cache_pending=work
            return
        self.run_related_lyrics(*work)

    def run_related_lyrics(self,request,playback,saved_playlist):
        self.related_cache_running=True
        def run():
            try:result=self.load_related_lyrics(playback,saved_playlist)
            except (OSError,ValueError):result=([],None)
            self.messages.put(('related_lyrics',(request,playback,result)))
        threading.Thread(target=run,daemon=True,name='saved-lyric-supplements').start()

    def receive_related_lyrics(self,request,playback,result):
        self.related_cache_running=False
        pending=self.related_cache_pending
        self.related_cache_pending=None
        if pending and not self.closed and not self.closing:self.run_related_lyrics(*pending)
        if (request is not self.related_cache_request or playback.key!=self.playback.key
                or abs(playback.duration-self.playback.duration)>.5 or self.has_complete_sync()
                or self.timing and (cleared_rows(self.timing) or self.timing.get('offset')
                    or 'manual' in timing_origins(self.timing))):return
        if not result[0] and result[1] is None:return
        self.restore_cached_lyrics(related_lookup=result)
        self.last_render=None

    def cache_current_lyrics(self):
        if (self.temporary_alsong or self.restoring_cache or self.screen_cache_tainted
                or not self.song or self.song is self.screen_song or not self.is_bound()):return
        self.capture_translation_lyrics()
        direct=self.direct_result[2] if self.direct_result and self.direct_result[0]==self.playback.key and abs(self.direct_result[1]-self.playback.duration)<=2 else None
        options={'reuse':self.cache_reuse} if self.cache_reuse else {}
        merged=self.current_lyric_merge
        if merged and not self.cache_reuse and tuple(line.original for line in merged['lines'])==tuple(
                line.original for line in self.display_lines):
            options['merge']=merged
        if self.lyric_cache.save(self.playback,self.display_lines,direct,**options) and self.live_lyrics.is_temporary(self.song):
            # Preserve mapped manual corrections when a provider changes grouping.
            try:self.save_current_timing(self.timing)
            except (OSError,ValueError):pass

    def connect_spotify(self, *, visible=True):
        if frozen() and not webview2_available():
            self.background_lyrics.spotify_state='error'
            self.background_lyrics.spotify_status='Spotify 연결에 Microsoft Edge WebView2 Runtime이 필요합니다.'
            if visible:
                if not self.settings.get('connection_setup_seen'):
                    self.settings['connection_setup_seen']=True
                    self.save_settings()
                if messagebox.askyesno('Microsoft Edge WebView2 설치',
                        'Spotify 연결에 Microsoft Edge WebView2 Runtime이 필요합니다.\n'
                        '설치한 뒤 Spotify 연결을 다시 누르세요.\n\nMicrosoft 설치 페이지를 여시겠어요?',
                        parent=self.root):
                    webbrowser.open('https://developer.microsoft.com/microsoft-edge/webview2/#download')
            return
        if not visible:
            self.background_lyrics.connect(visible=False)
            return
        if not self.settings.get('spotify_connected'):
            self.settings.setdefault('spotify_authenticated',False)
        self.online.set(True)
        self.stop_screen_lyrics()
        self.background_lyrics.connect(visible=True)
        self.background_lyrics.observe(self.playback,enabled=True,public_enabled=self.public_background.get(),spotify_sync_present=self.has_spotify_timing())
        self.settings.update(spotify_connected=True,connection_setup_seen=True);self.save_settings()
        self.notify('저장된 Spotify 연결로 현재 곡을 다시 확인합니다.' if self.settings.get('spotify_authenticated')
                    else '열린 Spotify 창에서 한 번 로그인하세요. 다음 실행부터 연결을 기억합니다.',15)

    def start_public_lyrics(self):
        # Only the disclosed, explicit button opts into sending metadata to LRCLIB.
        self.public_background.set(True);self.online.set(True)
        self.stop_screen_lyrics()
        self.settings.update(connection_setup_seen=True);self.save_settings()
        self.background_lyrics.observe(self.playback,enabled=True,public_enabled=True,cached_sync=self.reuse_complete_sync(),spotify_sync_present=self.has_spotify_timing())
        self.notify('공개 가사 보조 조회를 켰습니다. LRCLIB에서 누락된 가사와 싱크를 확인합니다.',12)

    def stop_public_lyrics(self):
        self.public_background.set(False);self.save_settings()
        self.background_lyrics.observe(self.playback,enabled=self.online.get(),public_enabled=False,cached_sync=self.reuse_complete_sync(),spotify_sync_present=self.has_spotify_timing())
        self.notify('공개 싱크 보조 조회를 껐습니다. 저장한 가사와 싱크는 계속 사용합니다.',8)

    def observe_lyricsplus(self):
        sources=self.lyric_merge_job
        # Use a provider's original reference through the lookup. Newly added
        # lines should not cancel a request already verified against that text.
        anchor=next((value['lines'] for name in ('spotify','lrclib')
                     for (source,_),value in sources.sources.items() if source==name),None)
        lines=anchor or (self.display_lines if self.is_bound() and not self.screen_cache_tainted else ())
        reference=tuple(line.original for line in lines)
        has_clock=self.has_spotify_timing() or self.has_complete_sync()
        translated=bool(self.display_lines) and all(line.korean.strip() for line in self.display_lines)
        service=self.background_lyrics
        waiting=service.key==self.playback.key and service.awaiting_spotify
        for job, preference, cached_sync, waiting_for_primary in (
                (self.lyricsplus_job,self.lyricsplus_background,has_clock,waiting),
                (self.alsong_job,self.alsong_background,has_clock and translated,False)):
            enabled=self.online.get() and preference.get()
            if (enabled and job.pending and self.playback.has_timeline and job.key==self.playback.key
                    and job.playback.duration==self.playback.duration):
                continue
            job.observe(self.playback,enabled=enabled,cached_sync=cached_sync,reference=reference,
                        waiting_for_primary=waiting_for_primary)

    def start_lyricsplus(self):
        self.lyricsplus_background.set(True);self.online.set(True)
        self.stop_screen_lyrics()
        self.settings.update(connection_setup_seen=True);self.save_settings()
        self.sync_lookup_started=time.monotonic();self.observe_lyricsplus();self.observe_auto_audio()
        self.notify('LyricsPlus 싱크 조회를 켰습니다. 받은 싱크는 저장해 다음 재생에 재사용합니다.',10)

    def stop_lyricsplus(self):
        self.lyricsplus_background.set(False);self.save_settings();self.observe_lyricsplus()
        self.notify('LyricsPlus 조회를 껐습니다. 저장한 가사와 싱크는 계속 사용합니다.',8)

    def start_alsong(self):
        self.alsong_background.set(True);self.online.set(True)
        self.stop_screen_lyrics()
        self.settings.update(connection_setup_seen=True);self.save_settings()
        self.sync_lookup_started=time.monotonic();self.observe_lyricsplus();self.observe_auto_audio()
        self.notify('알송 싱크 조회를 켰습니다. 받은 싱크는 저장해 다음 재생에 재사용합니다.',10)

    def stop_alsong(self):
        self.alsong_background.set(False);self.save_settings();self.observe_lyricsplus()
        self.notify('알송 조회를 껐습니다. 저장한 가사와 싱크는 계속 사용합니다.',8)

    def receive_lyricsplus(self,event):
        service=self.alsong_job if event.get('provider')=='알송' else self.lyricsplus_job
        received=service.receive(event)
        if received:self.queue_provider_lyrics(*received)
        self.observe_auto_audio();self.last_render=None

    def open_connection(self):
        if self.connection_dialog:self.connection_dialog.window.deiconify();self.connection_dialog.window.lift()
        else:self.connection_dialog=ConnectionDialog(self)

    def show_music_dictionary(self):
        self.workspace.show('catalog', self.open_explorer)

    def show_appearance_settings(self):
        self.workspace.show('appearance', self.open_appearance)

    def show_developer_workspace(self):
        self.workspace.show('developer', self.open_developer_mode)

    def open_explorer(self):
        if self.explorer_dialog:
            self.explorer_dialog.window.deiconify();self.explorer_dialog.window.lift()
            self.explorer_dialog.focus_current_song()
        else:
            from explorer_dialog import ExplorerDialog
            self.explorer_dialog=ExplorerDialog(self)

    def open_local_analysis(self, path=None):
        if not self.song or not self.is_bound() or not self.playback.has_timeline:
            raise ValueError('Spotify에서 곡을 재생하고 가사 원문을 연결하세요.')
        dialog = getattr(self, 'local_analysis_dialog', None)
        if dialog and not dialog.current_recording():
            if dialog.worker:
                raise ValueError('앞 곡의 로컬 음원 분석이 진행 중입니다.')
            dialog.close()
            dialog = None
        if not dialog:
            from local_analysis_dialog import LocalAnalysisDialog
            dialog = self.local_analysis_dialog = LocalAnalysisDialog(self, BASE)
        dialog.window.deiconify()
        dialog.window.lift()
        if path:
            dialog.choose(path)

    def open_sync_reanalysis(self):
        from sync_reanalysis import SyncReanalysis
        from sync_reanalysis_dialog import SyncReanalysisDialog

        service = getattr(self, 'sync_reanalysis', None)
        if service is None:
            service = self.sync_reanalysis = SyncReanalysis(self.store.directory, BASE)
        dialog = getattr(self, 'sync_reanalysis_dialog', None)
        if dialog is None:
            service.refresh_async()
            dialog = self.sync_reanalysis_dialog = SyncReanalysisDialog(self, service)
        dialog.show()

    def sync_reanalysis_changed(self):
        self.reanalysis_playback_cache = self.local_analysis_cache = self.local_playlist_sync_cache = (None, 0, None)
        self.time_cache = None
        self.render()
        if self.editor:
            self.editor_refresh()

    def current_local_analysis(self, *, verified_playlist=False):
        if not self.song or not self.is_bound() or not self.playback.has_timeline:
            return None
        now = time.monotonic()
        key = self.playback.key, self.song.fingerprint, self.playback.duration
        cache = 'local_playlist_sync_cache' if verified_playlist else 'local_analysis_cache'
        previous, checked, result = getattr(self, cache, (None, 0, None))
        if previous != key or now - checked >= 1:
            result = None
            if (self.store.directory / 'local_analysis.sqlite3').is_file():
                from sqlite3 import Error
                from local_analysis import AnalysisStore, lyric_digest
                try:
                    database = getattr(self, 'local_analysis_store', None)
                    if database is None:
                        database = self.local_analysis_store = AnalysisStore(self.store.directory)
                    originals = [line.original for line in self.song.lines]
                    from sync_reanalysis import applied_job, has_applied_job_times
                    linked = database.linked(self.playback.key, lyric_digest(originals), self.playback.duration)
                    linked = applied_job(self.store.directory, linked)
                    if not verified_playlist or has_applied_job_times(linked):
                        result = linked
                    if result is None:
                        identity = self.playback.key, self.playback.duration
                        cached, track_id = getattr(self, 'local_playlist_track', (None, None))
                        if cached != identity:
                            record = self.playlist_lyrics.store.find(self.playback)
                            track_id = record['track']['id'] if record else None
                            self.local_playlist_track = identity, track_id
                        if track_id:
                            result = database.playlist_job(track_id, originals, self.playback.duration,
                                                           verified_only=verified_playlist)
                except (Error, OSError, ValueError):
                    pass
            setattr(self, cache, (key, now, result))
        return result

    def show_startup(self,connect=False):
        if connect or not (self.settings.get('spotify_connected') or self.settings.get('connection_setup_seen')):
            self.safe(self.connect_spotify)

    def connection_status(self):
        service=self.background_lyrics
        connected=self.online.get() and self.settings.get('spotify_connected')
        if self.song and self.is_bound():
            headline='원문 가사를 표시하고 있어요' if not self.translated_count else '가사를 표시하고 있어요'
            if connected and service.spotify_state in ('error','reconnecting'):
                prefix='로그인은 완료됐어요. ' if service.spotify_authenticated else ''
                return headline,prefix+service.spotify_status
            if self.has_direct_sync() or self.has_saved_sync():
                return headline,'Spotify를 최소화하거나 가사 화면을 닫아도 자동으로 진행합니다.'
            return headline,'정확한 줄별 시간이 없는 구절은 추정 진행이나 직접 보정을 사용합니다.'
        if connected and service.spotify_state=='reconnecting':
            return 'Spotify 연결을 복구하고 있어요',service.spotify_status
        if not self.online.get():return '가사 연결이 꺼져 있어요','아래에서 사용할 연결 방식을 선택하세요.'
        if service.spotify_state=='connecting':
            if service.spotify_authenticated or self.settings.get('spotify_authenticated'):
                return 'Spotify 가사를 찾고 있어요','저장된 계정 연결로 현재 곡을 확인하고 있습니다.'
            return 'Spotify에 연결하고 있어요','열린 Spotify 창에서 로그인을 마쳐 주세요.'
        if self.settings.get('spotify_connected') and service.spotify_state=='login_required' and not service.spotify_authenticated:
            return 'Spotify 연결이 필요해요','Spotify 연결을 눌러 로그인해 주세요.'
        if not self.playback.has_timeline:return 'Spotify에서 노래를 틀어 주세요','재생 중인 곡을 자동으로 찾아 연결합니다.'
        if service.awaiting_spotify:
            return 'Spotify 가사를 찾고 있어요','현재 곡의 Spotify 가사를 먼저 확인하고 있습니다.'
        if self.lyric_merge_job.running:
            return '가사를 연결하고 있어요','가져온 가사를 현재 곡에 맞추고 있습니다.'
        if self.public_background.get():
            if service.public_running:
                return '공개 가사를 찾고 있어요','공개 출처에서 현재 곡을 확인하고 있습니다.'
            if service.public_retry:
                if not service.public_done:return '공개 가사를 다시 확인하고 있어요',service.status
                return '공개 가사 조회에 실패했어요',service.status
        if connected and service.spotify_state=='error':
            if service.spotify_authenticated:
                return 'Spotify 가사를 가져오지 못했어요','로그인은 완료됐어요. '+service.spotify_status
            return '연결을 다시 확인해 주세요',service.spotify_status
        if service.spotify_authenticated:
            if service.spotify_state=='not_found':
                if self.public_background.get():
                    if service.public_done:return '확인한 출처에서 가사를 찾지 못했어요',service.status
                    return '공개 가사를 확인할 예정이에요','Spotify에서 가사를 찾지 못해 공개 출처를 확인합니다.'
                return '이 곡의 Spotify 가사를 찾지 못했어요','다른 곡은 자동으로 다시 확인합니다. 공개 싱크 켜기로 보조 가사를 찾아볼 수도 있어요.'
            if service.spotify_state=='unsynced':return '가사는 있지만 줄별 시간이 없어요','원문을 표시하고 추정 진행이나 직접 보정을 사용할 수 있습니다.'
            return 'Spotify 가사를 찾고 있어요','연결이 끝나면 창을 닫아도 됩니다.'
        if self.public_background.get():
            if service.public_done:return '이번 공개 가사 조회를 마쳤어요',service.status
            return '로그인 없이 사용할 준비가 됐어요','곡이 바뀌면 공개 가사를 자동으로 찾습니다.'
        if self.lyricsplus_background.get():return 'LyricsPlus 싱크를 확인하고 있어요',self.lyricsplus_job.status
        if self.alsong_background.get():return '알송 싱크를 확인하고 있어요',self.alsong_job.status
        return '가사 연결 방식을 선택해 주세요','추가 설치 없이 아래에서 연결 방법을 선택할 수 있습니다.'

    def direct_times(self):
        if not self.song or not self.direct_result:return []
        key,duration,result=self.direct_result
        if key!=self.playback.key or abs(duration-self.playback.duration)>2:return []
        signature=(id(result),tuple(line.original for line in self.song.lines))
        if self.direct_map_cache is None or self.direct_map_cache[0]!=signature:
            merged=self.current_lyric_merge
            if merged and result is merged['direct'] and signature[1]==tuple(line.original for line in merged['lines']):
                times=result['times']
                rows=merged['rows']
                reliable=sum(t is not None for t in times)/len(times)>=.8
                eligible=tuple(row.get('kind')!='dialogue' and (row.get('original_source') or '').casefold()
                               in ('spotify','local','manual') for row in rows)
                sources=tuple(row.get('selected_source') or '' for row in rows)
                bounds=tuple((index,stamp) for index,stamp in enumerate(times) if stamp is not None)
                self.direct_map_cache=signature,times,reliable,eligible,sources,bounds
                return times
            times=mapped_times(result,self.song.lines)
            reliable=bool(times and sum(t is not None for t in times)/len(times)>=.8)
            source=result['lines']
            if (not reliable and any(t is not None for t in times) and source
                    and sum(t is not None for t in result['times'])/len(source)>=.8):
                forward,_=native_line_mapping([line.original for line in source],self.song.lines)
                reverse,_=native_line_mapping([line.original for line in self.song.lines],source)
                # Compare the finer row grouping: merged translation rows and
                # added backing vocals must not discard a verified song clock.
                # The same 80% rule still rejects short excerpts of long lyrics.
                reliable=max(len(forward),len(reverse))/max(len(source),len(self.song.lines))>=.8
            primary={**result,'fallback':None}
            primary_times=mapped_times(primary,self.song.lines)
            text_onsets=mapped_times({**primary,'times':list(range(len(source)))},self.song.lines)
            eligible=tuple(t is not None for t in text_onsets)
            fallback=result.get('fallback') or {}
            sources=tuple(result['source'] if primary_times[i] is not None else fallback.get('source','')
                          for i in range(len(times)))
            containing,_=native_line_mapping([line.original for line in source],self.song.lines)
            bounds=tuple((target,result['times'][i]) for i,target in containing.items() if result['times'][i] is not None)
            self.direct_map_cache=signature,times,reliable,eligible,sources,bounds
        return self.direct_map_cache[1]

    def has_direct_sync(self):
        times=self.direct_times()
        return bool(times and self.direct_map_cache[2])

    def has_complete_sync(self):
        if not self.is_bound() or self.screen_cache_tainted:return False
        times=self.direct_times()
        return self.has_saved_sync() or bool(times and all(t is not None for t in times))

    def has_spotify_timing(self):
        # Different display line breaks do not mean Spotify's clock is missing.
        return bool(self.direct_result and self.direct_result[0]==self.playback.key
                    and abs(self.direct_result[1]-self.playback.duration)<=2
                    and ((self.direct_result[2].get('source')=='Spotify'
                          and any(stamp is not None for stamp in self.direct_result[2]['times']))
                         or (self.current_lyric_merge and any(
                             (row.get('selected_source') or '').casefold()=='spotify'
                             for row in self.current_lyric_merge['rows']))))

    def has_spotify_clock(self):
        """A complete recording clock need not match every document row."""
        if not self.has_spotify_timing():return False
        if self.current_lyric_merge:
            spotify=next((snapshot for (source,_),snapshot in self.lyric_merge_job.sources.items()
                          if source=='spotify'),None)
            if spotify:return sum(stamp is not None for stamp in spotify['times'])/len(spotify['times'])>=.8
        result=self.direct_result[2]
        return bool(result['lines'] and sum(stamp is not None for stamp in result['times'])/len(result['lines'])>=.8)

    def spotify_sync_for_manual_work(self):
        """Use current recording source evidence before looking at merged display rows."""
        spotify = [snapshot['times'] for (source, _), snapshot in self.lyric_merge_job.sources.items()
                   if source=='spotify' and snapshot['recording_verified'] and not snapshot['annotation_only']]
        direct=self.direct_result
        if (direct and not self.cache_reuse and direct[0]==self.playback.key
                and abs(direct[1]-self.playback.duration)<=.5 and direct[2]['source']=='Spotify'):
            spotify.append(direct[2]['times'])
        if spotify:
            return 'synced' if any(stamp is not None for times in spotify for stamp in times) else 'missing'
        service=self.background_lyrics
        if (service.enabled and service.playback and service.key==self.playback.key
                and abs(service.playback.duration-self.playback.duration)<=.5
                and service.request_key and service.spotify_missing_lyrics==service.request_key):
            return 'missing'
        return 'unknown'

    def reuse_complete_sync(self):
        service=self.background_lyrics
        # Legacy complete clocks can still lack alternate originals. Upgrade
        # those cache entries once using the providers enabled for this track.
        sources=self.lyric_merge_job
        return (self.has_complete_sync()
                and (not service.spotify_requested or sources.has_source('Spotify'))
                and (not self.public_background.get() or sources.has_source('LRCLIB')
                     or service.key==self.playback.key and service.spotify_has_complete_sync)
                and not (service.key==self.playback.key and (service.spotify_pending or service.public_running)))

    def current_reference(self):
        tracking=self.cache_tracking
        if (tracking and self.is_bound() and tracking[0]==self.playback.key
                and tracking[1]==tuple(normalized(line.original) for line in self.song.lines)):
            return tracking
        return None

    def anniversary_sync(self):
        tracking=self.current_reference()
        return tracking[2] if tracking and tracking[3] else None

    def observe_auto_audio(self):
        if self.temporary_alsong:return
        if self.missing_audio_collector and self.missing_audio_collector.owns_playback:
            return
        if self.lyrics_deleted():return
        if self.services_started:self.observe_lyricsplus()
        bound=self.is_bound()
        external=bool(bound and (self.has_spotify_clock() or self.has_direct_sync() or self.has_saved_sync()))
        temporary=self.live_lyrics.is_temporary(self.song)
        service=self.background_lyrics
        pending=bool(self.settings.get('spotify_connected') and service.awaiting_spotify)
        pending=pending or (self.public_background.get() and not service.cached_sync and not service.public_done
                            and not (service.key==self.playback.key and service.spotify_has_complete_sync))
        pending=pending or (self.lyricsplus_background.get() and self.lyricsplus_job.pending)
        pending=pending or (self.alsong_background.get() and self.alsong_job.pending)
        # Capture can begin before lyrics arrive. Only model work waits for
        # the bounded provider lookup; the opening audio is retained meanwhile.
        position=self.playback.position()
        awaiting=bool(self.services_started and self.online.get() and pending and
                      not self.cache_hit and (position<2 or (temporary and position>3)) and
                      time.monotonic()-self.sync_lookup_started<12)
        tracking=self.current_reference()
        options={'tracking_times':tracking[2]} if tracking else {}
        merged=self.current_lyric_merge
        merged=merged if (bound and merged and tuple(line.original for line in merged['lines'])
                          ==tuple(line.original for line in self.song.lines)) else None
        reference=self.anniversary_sync() if not external else None
        if not external and tracking and tracking[4] and all(stamp is not None for stamp in tracking[2]):
            reference=tracking[2]
        if self.auto_analyzer.enabled and (merged or reference):
            direct=self.direct_times()
            origins=timing_origins(self.timing)
            anchors=[stamp if origin=='manual' and self.timing.get('source')!='LRCLIB' else None
                     for stamp,origin in zip(self.timing['times'],origins)]
            base={'times':anchors,'offset':0}
            if direct:base=merge_candidates(base,direct)[0]
            known=merge_candidates(base,[stamp if origin=='audio' else None
                                         for stamp,origin in zip(self.timing['times'],origins)])[0]['times']
            if reference:known=merge_candidates({'times':known,'offset':0},reference)[0]['times']
            offset=self.timing['offset']
            anchors=[round(stamp+offset,3) if stamp is not None and 0<=stamp+offset<=self.playback.duration else None
                     for stamp in known]
            if any(stamp is not None for stamp in anchors):
                kinds=[row['kind'] for row in merged['rows']] if merged else ['unknown']*len(anchors)
                options['merged_timing']={'times':anchors,'row_kinds':kinds,'reference':bool(reference)}
                external=True
        self.auto_analyzer.observe(self.song if bound else None,self.playback,memory_only=temporary,
                                   external_sync=external,awaiting_sync=awaiting,**options)
        if external:self.clear_realtime()

    def receive_background_lyrics(self,sender,event):
        collector=getattr(self,'missing_audio_collector',None)
        if collector and collector.playlist_request and collector.playlist_request.receive(sender,event):return
        if self.playlist_lyrics.receive(sender,event):return
        if self.spotify_translation_sources.receive(sender,event):return
        if self.catalog_sync.receive(sender,event):return
        current_worker=sender is self.background_lyrics.job
        received=self.background_lyrics.receive(sender,event)
        if current_worker and event.get('kind') in ('status','metadata_status'):
            authenticated=self.background_lyrics.spotify_authenticated
            if event.get('login_cancelled') is True and not self.settings.get('spotify_authenticated'):
                self.settings.update(spotify_connected=False,spotify_authenticated=False)
                self.save_settings()
            elif (authenticated or event.get('state')=='login_required') and self.settings.get('spotify_authenticated') is not authenticated:
                self.settings['spotify_authenticated']=authenticated;self.save_settings()
        if received:self.queue_provider_lyrics(*received)
        self.last_render=None

    def restore_merge_sources(self,cached):
        job=self.lyric_merge_job
        if cached.get('merge') and not cached.get('reuse'):
            added=False
            for snapshot in cached['merge']['snapshots']:
                if snapshot['source'].casefold() not in ('manual','local'):
                    added=job.remember(snapshot) or added
            return added
        direct=cached.get('direct')
        if direct and not cached.get('reuse'):
            job.remember(snapshot_from_direct(direct))
            fallback=direct.get('fallback')
            if fallback:
                # Legacy fallback clocks have no recording-level verification.
                job.remember(snapshot_from_direct({**fallback,'annotation_only':True}))
        # Preserve annotations from old entries independently of their clock.
        return job.remember(make_snapshot('cache',cached['lines']))

    def merge_inputs(self):
        linked=self.settings.get('links',{}).get(self.playback.key)
        base=match_recording(self.songs,self.playback.title,self.playback.artist,self.playback.album,linked)
        old=self.song if self.is_bound() and not self.screen_cache_tainted else None
        bundled_layout=bool(old and self.live_lyrics.is_temporary(old) and self.bundled_manual_key==self.playback.key)
        if bundled_layout:base=None
        locked=bundled_layout or bool(base and self.user_lyric_override(base))
        snapshots=[]
        base_lines=self.lyric_annotations.display_lines(base) if base else ()
        if base:
            snapshots.append(make_snapshot('manual' if locked else 'local',base_lines,recording_verified=True))
        times=()
        if old and self.timing:
            times=tuple(stamp if origin=='manual' and self.timing.get('source')!='LRCLIB' else None
                        for stamp,origin in zip(self.timing['times'],timing_origins(self.timing)))
            if any(stamp is not None for stamp in times):
                manual_lines=(base_lines if old==base else
                              tuple(Line(line.original,'','') for line in old.lines))
                if locked and old==base:snapshots.clear()
                snapshots.append(make_snapshot('manual',manual_lines,times,recording_verified=True))
        return (base,old,times,locked,base_lines),snapshots

    def queue_provider_lyrics(self,key,result):
        if key!=self.playback.key or not self.playback.has_timeline or self.lyrics_deleted():return
        if not result['lines']:return
        if self.lyric_merge_job.remember(snapshot_from_direct(result)) and not self.temporary_alsong:
            self.queue_lyric_merge()

    def queue_lyric_merge(self):
        context,snapshots=self.merge_inputs()
        reader_snapshots=(snapshot for *_,documents in self.merge_reader_receipts for snapshot in documents)
        self.lyric_merge_job.submit(self.playback,context,snapshots,reader_snapshots)

    def receive_merged_lyrics(self,serial,playback,context,result,error):
        if (self.temporary_alsong or serial!=self.lyric_merge_job.serial or playback.key!=self.playback.key
                or not self.playback.has_timeline or abs(playback.duration-self.playback.duration)>.5
                or self.lyrics_deleted()):return
        if error is not None:
            self.finish_merge_reader_receipts(False)
            raise error
        current,_=self.merge_inputs()
        if context!=current:
            self.queue_lyric_merge()
            return
        try:
            if result['lines']:
                self.finish_merge_reader_receipts(bool(self.apply_merged_lyrics(result,current[0],locked=current[3])))
            else:
                # The reader already validated its work-level document for
                # provisional display. Keep that existing behavior until a
                # recording-scoped original arrives and can verify the rows.
                self.finish_merge_reader_receipts(False,provisional=True)
        except Exception:
            self.finish_merge_reader_receipts(False)
            raise

    def apply_merged_lyrics(self,result,base,locked=False):
        lines=result['lines']
        if self.restoring_cache and not locked:
            lines,rows=restore_cached_translations(lines,result['rows'],result['snapshots'])
            result={**result,'lines':lines,'rows':rows}
        old=self.song if self.is_bound() and not self.screen_cache_tainted else base
        timing=self.timing if old is self.song else self.store.timing(old) if old else None
        mapping=result.get('previous_rows')
        if mapping is None:mapping=previous_row_mapping(old.lines,lines) if old else {}
        if locked and old:
            if any(index not in mapping for index in range(len(old.lines))):return
            selected=[mapping[index] for index in range(len(old.lines))]
            lines=self.display_lines if old is self.song else self.lyric_annotations.display_lines(old)
            rows=[result['rows'][index] for index in selected]
            direct=result.get('direct')
            if direct:direct={**direct,'lines':lines,'times':[direct['times'][index] for index in selected]}
            result={**result,'lines':lines,'times':[row['selected_time'] for row in rows],
                    'rows':rows,'direct':direct}
            mapping={index:index for index in range(len(old.lines))}
        if timing and old and tuple(line.original for line in old.lines)!=tuple(line.original for line in lines):
            from lyric_completion import expand_timing
            indices=[None]*len(lines)
            for before,after in mapping.items():indices[after]=before
            if any(origin=='manual' and index not in indices
                   for index,origin in enumerate(timing_origins(timing))):
                # Keep the displayed rows and user's clock if an old manual
                # correction cannot be mapped to the replacement document.
                return
            timing=expand_timing(timing,indices)
        elif self.restoring_cache and old is base:
            # A saved completion has a different signature from its library
            # base. Load its own time file before falling back to the base.
            timing=None
        if self.song is not None and self.song is self.screen_song:self.stop_screen_lyrics()
        cursor,manual,follow=self.cursor,self.manual_index,self.follow.get()
        direct=result.get('direct')
        if direct:
            # Manual edits have their own persisted timing file. Keeping them
            # in the provider clock as well would resurrect a deleted edit.
            times=[None if (row.get('selected_source') or '').casefold()=='manual' else stamp
                   for row,stamp in zip(result['rows'],direct['times'])]
            direct={**direct,'times':times} if any(stamp is not None for stamp in times) else None
        song=self.live_lyrics.receive_merged(self.playback,base,lines,timing,prefer_saved=self.restoring_cache)
        self.current_lyric_merge={**result,'direct':direct}
        new_clock=(self.playback.key,self.playback.duration,direct) if direct else None
        if self.direct_result!=new_clock:self.direct_result=new_clock
        self.direct_map_cache=None;self.time_cache=None;self.cache_reuse=None
        tracking=self.cache_tracking
        self.cache_tracking=None
        if tracking and tracking[0]==self.playback.key and any(tracking[3:]):
            from recording_reuse import anniversary_reference, related_reference
            originals=tuple(normalized(line.original) for line in lines)
            reference=tracking[2] if originals==tracking[1] else anniversary_reference(lines,tracking[3])
            if reference is None and tracking[4]:reference=related_reference(lines,[tracking[4]])
            if reference is not None:self.cache_tracking=(tracking[0],originals,reference,*tracking[3:])
        self.select_song(song)
        self.cursor=mapping.get(cursor,min(cursor,len(lines)-1))
        self.manual_index=mapping.get(manual) if manual is not None else None
        self.follow.set(follow)
        self.reader_bridge.set_reference(self.playback.key,[line.original for line in lines],complete=True)
        if self.has_direct_sync() or self.has_spotify_clock():
            self.clear_realtime()
            self.screen_lyrics.observe(self.playback,needed=False,allow_ocr=False)
            self.reader_bridge.set_tracking('api_sync')
        self.last_render=None;self.render()
        if self.editor:self.editor_refresh()
        self.cache_current_lyrics()
        self.observe_auto_audio()
        return True

    def receive_direct_lyrics(self,key,result):
        """Restore legacy provider results. Live updates use queue_provider_lyrics."""
        if self.temporary_alsong or key!=self.playback.key or not self.playback.has_timeline:return
        if self.lyrics_deleted():return
        if result.get('annotation_only'):
            if self.merge_direct_annotations(result):
                self.last_render=None;self.render();self.cache_current_lyrics()
            return
        validate_times(result['times'],len(result['lines']))
        if not result['lines']:return
        annotations_changed=self.merge_direct_annotations(result)
        def priority(value):
            times=mapped_times(value,self.song.lines) if self.song else value['times']
            if self.live_lyrics.is_temporary(self.song):
                from lyric_completion import restore_spotify_rows
                if restore_spotify_rows(self.display_lines,value,self.timing) is not None:
                    times=value['times']
            coverage=sum(t is not None for t in times)/len(times) if times else 0
            return (coverage==1,coverage>=.8,coverage>0,value['source']=='Spotify',coverage)
        previous=(self.direct_result[2] if self.direct_result and self.direct_result[0]==key
                  and abs(self.direct_result[1]-self.playback.duration)<=2 else None)
        combined=prefer_spotify_timing(previous,result)
        if (combined is not None and previous and not previous.get('fallback') and self.song
                and {k:v for k,v in combined.items() if k!='fallback'}=={k:v for k,v in previous.items() if k!='fallback'}
                and mapped_times(combined,self.song.lines)==mapped_times(previous,self.song.lines)):
            combined=previous
        if combined is not None:result=combined
        if previous and (result is previous or combined is None and priority(previous)>priority(result)):
            if annotations_changed:
                self.last_render=None;self.render();self.cache_current_lyrics()
                self.observe_auto_audio()
            return
        if self.song is not None and self.song is self.screen_song:self.stop_screen_lyrics()
        if result.get('source')!='저장 캐시':self.cache_reuse=None
        self.direct_result=(key,self.playback.duration,result);self.direct_map_cache=None
        reference=result['lines']
        if (result.get('source') in ('ALSong','LyricsPlus') and self.display_lines
                and match_lyrics([line.original for line in reference],
                                 [line.original for line in self.display_lines]) is not None):
            reference=self.display_lines
        self.reader_bridge.set_reference(key,[line.original for line in reference],complete=True)
        if not self.song:
            self.select_song(self.live_lyrics.receive(self.playback,None,result['lines']))
        elif not self.live_lyrics.is_temporary(self.song):self.complete_saved_lyrics(result['lines'])
        self.complete_direct_originals()
        if self.has_direct_sync() or self.has_spotify_clock():
            self.spotify_current=None;self.spotify_anchor=None;self.clear_realtime()
            self.screen_lyrics.observe(self.playback,needed=False,allow_ocr=False)
            self.reader_bridge.set_tracking('api_sync')
        self.last_render=None;self.render()
        if self.editor:self.editor_refresh()
        self.cache_current_lyrics()
        self.observe_auto_audio()

    def merge_direct_annotations(self,result):
        if result.get('source')!='ALSong':return False
        return self.merge_current_annotations(result['lines'])

    def refresh_user_annotations(self):
        """Supplement the display without changing lyric identity or any clock."""
        if not self.song or not self.is_bound() or self.screen_cache_tainted:
            self.singer_edits.select(None)
            self.lyric_text_edits.select(None)
            self.singer_options=()
            self.stop_singer_input(redraw=False)
            self.user_annotation_signature=None;self.user_annotation_rows=();self.line_singers=()
            if self.line_readings:self.last_render=None
            self.line_readings=()
            if self.line_reading_request is not None:
                self.line_reading_request=None
                self.discard_pending_readings()
            return
        saved=match_saved_annotations(self.playback,self.display_lines,self.songs,
                                      self.lyric_annotations.display_lines)
        current=tuple(Line(old.original,old.reading if old.reading.strip() else extra.reading,
                           old.korean if old.korean.strip() else extra.korean) if extra else old
                      for old,extra in zip(self.display_lines,saved))
        artists=tuple(self.displayed_artists())
        reference=tuple(self.direct_result[2]['lines']) if (self.direct_result
            and self.direct_result[0]==self.playback.key
            and abs(self.direct_result[1]-self.playback.duration)<=2) else ()
        originals=tuple(line.original for line in current)
        link=self.temporary_alsong
        source=link.lines if link else current
        layout=(self.playback.key,tuple(line.original for line in source))
        path_key=(self.playback.key,tuple(line.original for line in link.source.lines)) if link else None
        self.lyric_text_edits.select(layout,path_key=path_key)
        if self.singer_edits.select(layout,path_key=path_key):
            self.stop_singer_input(redraw=False)
        options,solo_names=performer_options(artists,self.playback.album,self.store.directory/'group_credits.json')
        self.singer_roster.select(self.playback.key)
        choices=self.singer_roster.names
        if choices is None:
            # Previously assigned singers can still be picked before a roster is saved.
            candidates={}
            for name in (*options,*self.singer_edits.assigned_names):
                candidates.setdefault(artist_key(name),name)
            choices=tuple(candidates.values())[:len(SINGER_KEYS)]
        if self.singer_options!=choices:
            self.singer_options=choices
            self.singer_input_changed_row=None
        signature=(self.playback.key,originals,artists,tuple(line.original for line in reference))
        if signature!=self.user_annotation_signature:
            self.user_annotation_rows=self.user_annotations.match(self.playback,current,artists,reference_lines=reference)
            self.user_annotation_signature=signature
        singers=[]
        for index,row in enumerate(self.user_annotation_rows):
            source_index=self.annotation_row(index)
            override=self.singer_edits.overrides[source_index]
            singers.append(override if self.singer_edits.has_explicit(source_index) else row.singers or override or solo_names)
        self.line_singers=tuple(singers)
        merged=tuple(Line(old.original,old.reading if old.reading.strip() else row.reading,
                          old.korean if old.korean.strip() else row.korean)
                     for old,row in zip(current,self.user_annotation_rows))
        if merged!=self.display_lines:
            self.display_lines=merged
            if self.live_lyrics.song and self.live_lyrics.song.id==self.song.id:
                self.live_lyrics.lines=merged
            self.translated_count=sum(bool(line.korean.strip()) for line in merged)
            self.last_render=None
        if self.services_started:
            self.refresh_readings_in_background()
        else:self.set_display_readings(readings_for_display(self.display_lines))

    def set_display_readings(self,readings):
        if readings!=self.line_readings:
            self.line_readings=readings
            self.last_render=None

    def display_reading_inputs(self):
        rows=[]
        for index,line in enumerate(self.display_lines):
            row=self.annotation_row(index)
            supplied=self.lyric_text_edits.reading(row)
            corrected=self.lyric_text_edits.original(row)
            if supplied is not None:rows.append(('',supplied))
            elif corrected is not None:rows.append((corrected,''))
            else:rows.append((line.original,line.reading))
        return tuple(rows)

    def refresh_readings_in_background(self):
        rows=self.display_reading_inputs()
        request=(self.playback.key,self.song.id,rows)
        previous=self.line_reading_request
        if request==previous:return
        retained=dict(zip(previous[2],self.line_readings)) if previous and previous[:2]==request[:2] else {}
        self.line_reading_request=request
        self.set_display_readings(tuple(retained.get(row,row[1]) for row in rows))
        self.discard_pending_readings()
        if not any(not supplied.strip() and JA.search(text) for text,supplied in rows):return
        self.line_reading_queue.put_nowait(request)
        if self.line_reading_worker is None:
            # Reuse the converter initialized in this thread across tracks.
            self.line_reading_worker=threading.Thread(target=self.convert_readings,daemon=True,name='lyric-readings')
            self.line_reading_worker.start()

    def discard_pending_readings(self):
        try:self.line_reading_queue.get_nowait()
        except queue.Empty:pass

    def convert_readings(self):
        while not (self.closed or self.closing):
            try:request=self.line_reading_queue.get(timeout=1)
            except queue.Empty:continue
            lines=tuple(Line(text,supplied,'') for text,supplied in request[2])
            readings=readings_for_display(lines)
            self.messages.put(('lyric_readings',(request,readings)))

    def receive_lyric_readings(self,request,readings):
        if (request!=self.line_reading_request or not self.song or not self.playback.has_timeline
                or self.screen_cache_tainted or request[:2]!=(self.playback.key,self.song.id)
                or request[2]!=self.display_reading_inputs()):return
        self.set_display_readings(readings)

    def current_line_singers(self,index=None):
        if not self.song or not self.line_singers:return ()
        if index is None:index=self.displayed_index()
        index=0 if index is None else index
        return self.line_singers[index] if 0<=index<len(self.line_singers) else ()

    def current_line_singer_partial(self,index=None):
        if not self.song or not self.user_annotation_rows:return False
        if index is None:index=self.displayed_index()
        index=0 if index is None else index
        return bool(0<=index<len(self.user_annotation_rows) and not self.singer_edits.has_explicit(self.annotation_row(index))
                    and getattr(self.user_annotation_rows[index],'singer_partial',False))

    def merge_current_annotations(self,incoming):
        if self.user_lyric_override():return False
        if not self.is_bound() or self.screen_cache_tainted:return False
        current=self.display_lines
        if not current:return False
        sentences=match_verified_annotations(current,incoming)
        if sentences is not None:
            matches=sentences
        else:
            # Reading-only documents still need a verified whole-text alignment.
            alignment=match_lyrics([line.original for line in incoming],[line.original for line in current])
            if alignment is None:return False
            matches=[None]*len(current)
            targets={start:index for index,start in enumerate(alignment.target_offsets)}
            target_ends=(*alignment.target_offsets[1:],sum(len(normalized(line.original)) for line in current))
            source_ends=(*alignment.source_offsets[1:],len(alignment.source_to_target)-1)
            for extra,start,end in zip(incoming,alignment.source_offsets,source_ends):
                target=targets.get(alignment.source_to_target[start])
                if (target is not None and alignment.source_to_target[end]==target_ends[target]
                        and normalized(extra.original)==normalized(current[target].original)):
                    matches[target]=extra
        merged=tuple(Line(old.original,
                          old.reading if old.reading.strip() or (extra.original and normalized(old.original)!=normalized(extra.original))
                          else (extra.reading.strip() or old.reading),
                          old.korean if old.korean.strip() else (extra.korean.strip() or old.korean)) if extra else old
                     for old,extra in zip(current,matches))
        if merged==current:return False
        if self.live_lyrics.is_temporary(self.song):
            from lyrics import Song
            old=self.song
            self.song=Song(old.id,old.title,old.artist,old.album,merged)
            self.live_lyrics.song=self.song;self.live_lyrics.lines=merged
        else:
            self.live_lyrics.receive(self.playback,self.song,merged)
        # An annotation-only change never reselects rows or reloads their timing.
        self.display_lines=merged
        self.translated_count=sum(bool(line.korean.strip()) for line in merged)
        self.last_render=None
        if self.editor:self.editor_refresh()
        return True

    def complete_direct_originals(self):
        if (not self.direct_result or not self.live_lyrics.is_temporary(self.song)
                or self.bundled_manual_key==self.playback.key):return
        from lyric_completion import complete_lines,expand_timing,restore_spotify_rows
        from lyrics import Song
        completed=restore_spotify_rows(self.display_lines,self.direct_result[2],self.timing)
        if completed is None:
            completed=complete_lines(self.display_lines,self.direct_result[2]['lines'],minimum=1)
        if completed is None:return
        lines,indices=completed;old=self.song
        timing=expand_timing(self.timing,indices)
        song=Song(old.id,old.title,old.artist,old.album,lines)
        self.live_lyrics.song=song;self.live_lyrics.lines=lines;self.live_lyrics.timing=timing
        cursor=self.cursor;manual=self.manual_index;follow=self.follow.get()
        mapping={before:after for after,before in enumerate(indices) if before is not None}
        self.select_song(song)
        self.cursor=mapping.get(cursor,0);self.manual_index=mapping.get(manual) if manual is not None else None
        self.follow.set(follow)

    def has_saved_sync(self):
        if any(stamp is not None for stamp in self.direct_times()):
            return bool(self.song and self.timing['times'] and self.timing.get('source')!='LRCLIB'
                        and all(origin=='manual' for origin in timing_origins(self.timing)))
        return bool(self.song and self.timing['times'] and all(saved_rows(self.timing)))

    def clear_realtime(self):
        self.realtime_job=None;self.realtime_index=None;self.realtime_state=''
        self.realtime_matched_at=0;self.realtime_detail='';self.realtime_runtime={}

    def following_audio(self):
        return bool(not self.has_spotify_clock() and not self.has_direct_sync() and self.realtime_job and self.realtime_state in ('loading','listening','late') and
                    not self.realtime_job.cancelled.is_set() and self.is_bound() and self.follow.get() and
                    self.realtime_index is not None and time.monotonic()-self.realtime_matched_at<6 and
                    not self.has_saved_sync())

    def holding_audio(self):
        return bool(self.realtime_index is not None and self.is_bound() and self.follow.get()
                    and not self.has_spotify_clock() and not self.has_direct_sync() and not self.has_saved_sync()
                    and not self.following_audio() and not self.following_spotify()
                    and self.realtime_matched_at>=self.spotify_observed_at)

    def receive_realtime(self,job,event):
        stopped=getattr(job,'realtime_stop',None)
        if not self.realtime_tracking.get() or (stopped is not None and stopped.is_set()):return
        if (job.cancelled.is_set() or not getattr(job,'song',None) or not self.song or not self.is_bound() or self.has_spotify_clock() or self.has_direct_sync() or self.has_saved_sync() or
                getattr(job,'playback_key',None)!=self.playback.key or
                self.auto_analyzer.signature(job.song,getattr(job,'memory_only',False))!=
                self.auto_analyzer.signature(self.song,getattr(job,'memory_only',False))):return
        kind=event.get('kind')
        if kind=='realtime_status':
            self.realtime_job=job;self.realtime_state=event.get('state','failed')
            self.realtime_detail=event.get('text','') if self.realtime_state in ('failed','retrying','fallback') else ''
            if isinstance(event.get('runtime'),dict):self.realtime_runtime=event['runtime']
        elif kind=='realtime':
            now=time.monotonic();observed=event.get('observed_at',0);position=event.get('position',-1)
            projected=self.playback.base+((observed-self.playback.captured)*self.playback.rate if self.playback.playing else 0)
            if (not math.isfinite(observed) or not 0<=now-observed<=3
                    or observed<max(self.spotify_sample_after,self.realtime_tracking_since) or
                    not math.isfinite(position) or abs(projected-position)>1.5):return
            self.realtime_job=job;self.realtime_state='listening';self.realtime_detail=''
            if isinstance(event.get('runtime'),dict):self.realtime_runtime=event['runtime']
            match=event.get('match')
            if match:
                index=match.get('index')
                if type(index)==int and 0<=index<len(self.song.lines):
                    recovery=(match.get('recovery') is True and match.get('unique') is True
                              and match.get('confirmations',0)>=2 and match.get('confidence',0)>=.72
                              and match.get('characters',0)>=7 and match.get('similarity',0)>=.86
                              and match.get('match_method') in ('text','kana'))
                    maximum=10 if recovery else 3
                    global_recovery=(match.get('recovery') is True and match.get('unique') is True
                                     and match.get('global_recovery') is True
                                     and match.get('confirmations',0)>=2 and match.get('distinct_lines',0)>=2
                                     and match.get('confidence',0)>=.8 and match.get('similarity',0)>=.9
                                     and match.get('characters',0)>=6 and match.get('recovery_characters',0)>=13
                                     and match.get('recovery_start_index')==index-1
                                     and (self.realtime_index is None or index-1>self.realtime_index)
                                     and match.get('match_method') in ('text','kana'))
                    if global_recovery:maximum=len(self.song.lines)
                    if self.realtime_index is None or self.realtime_index<=index<=self.realtime_index+maximum:
                        self.realtime_index=index
                        self.realtime_matched_at=observed
        self.last_render=None;self.render()

    def following_spotify(self):
        return bool(self.screen_lyrics_allowed() and not self.has_spotify_clock() and not self.has_direct_sync() and not self.has_saved_sync() and not self.following_audio() and self.is_bound() and self.follow.get() and self.spotify_current is not None and time.monotonic()-self.spotify_observed_at<1.5)

    def receive_spotify_active(self,key,originals,index,observed_at=None,observed_position=None,reliable=True):
        if not self.screen_lyrics_allowed():return
        if key!=self.playback.key or not self.song or not self.is_bound():return
        if self.has_spotify_clock() or self.has_direct_sync():return
        if not 0<=index<len(originals):return
        now=time.monotonic()
        if observed_at is None:observed_at=now
        if (not math.isfinite(observed_at) or not 0<=now-observed_at<1.5
                or observed_at<self.spotify_sample_after):return
        if observed_position is None:observed_position=self.playback.position(observed_at)
        if not math.isfinite(observed_position) or not 0<=observed_position<=self.playback.duration:return
        if self.has_saved_sync():
            self.reader_bridge.set_tracking('saved_sync');return
        signature=(tuple(originals),self.song.lines)
        cached=getattr(self,'spotify_mapping',None)
        if cached is None or cached[0]!=signature:
            mapping,grouped=native_line_mapping(originals,self.song.lines)
            self.spotify_mapping=(signature,mapping,grouped)
        target_index=self.spotify_mapping[1].get(index)
        source_key=normalized(originals[index])
        verified=target_index is not None and (
            (index,target_index) in self.spotify_mapping[2]
            or normalized(self.song.lines[target_index].original)==source_key)
        previous=self.spotify_source_index
        resuming=not self.following_spotify()
        if previous is not None and self.spotify_anchor is not None:
            current=self.spotify_anchor[0]
            if index<previous:return
            if not verified:
                # Approximate wording stays local; proven split/merged spans may
                # hold the same row or recover from an earlier bad estimate.
                expected=min(current+index-previous,len(self.song.lines)-1)
                target_index=nearby_line(originals[index],self.song.lines,expected,current,min(expected+2,len(self.song.lines)-1))
        elif not verified:
            expected=round(index*(len(self.song.lines)-1)/max(1,len(originals)-1))
            target_index=nearby_line(originals[index],self.song.lines,expected)
        held=target_index is None and self.spotify_anchor is not None
        if held:target_index=self.spotify_anchor[0]
        if target_index is not None:
            exact=normalized(originals[index])==normalized(self.song.lines[target_index].original)
            try:
                self.native_timing.observe(self.song,self.playback.duration,index,target_index,
                    observed_position,observed_at,reliable and exact and self.playback.playing and self.follow.get())
            except (ValueError,OSError) as error:
                self.error_notice=(str(error),time.monotonic()+6)
            if not held and (self.spotify_anchor is None or self.spotify_anchor[0]!=target_index or resuming):self.spotify_anchor=(target_index,observed_position)
            self.spotify_source_index=index
            self.spotify_current=target_index;self.spotify_observed_at=observed_at
            self.reader_bridge.set_tracking('holding' if held else 'following',self.spotify_current)

    def estimated_index(self,times,position,offset=0,confirmed=None,*,clock=None):
        candidate=active_line(times,position,offset)
        # effective_times cached the measured anchors before filling its gaps.
        known=self.time_cache[0][1]
        limited=limit_vocalise(candidate,self.vocalise_rows,known,position if clock is None else clock,
                              confirmed,self.timing.get('offset',0))
        self.vocalise_waiting=limited!=candidate
        return limited

    def displayed_index(self,*,advance=True,position=None):
        # The playback clock is nonnegative. Looking ahead by the lead is
        # equivalent to subtracting it from each onset and clamping at zero.
        lead=self.lyric_advance_ms/1000 if advance else 0
        position=(self.playback.position() if position is None else position)+lead
        developer_mode=self.manual_sync_dialog is not None and self.is_bound()
        if not developer_mode and self.singer_input_index is not None and not self.singer_input_live:
            return self.singer_input_index
        direct=any(stamp is not None for stamp in self.direct_times())
        if (developer_mode and not direct
                and not (self.following_audio() or self.following_spotify() or self.holding_audio())):
            return active_line(self.timing['times'],position,self.timing['offset'])
        candidate=self.tracked_index(position,follow_playback=developer_mode)
        if (not self.song or not self.is_bound() or
                not developer_mode and self.manual_index is not None and not self.follow.get() or
                not direct):
            return candidate
        # Partial provider matches are already accepted by effective_times.
        # Live recognition may fill their gaps, but cannot cross those onsets.
        known=self.time_cache[0][1]
        position-=self.timing['offset']
        scheduled=active_line(self.time_cache[1],position)
        if scheduled is not None and known[scheduled] is not None:return scheduled
        lower=0;upper=len(known)-1;confirmed=None
        for index,stamp in enumerate(known):
            if stamp is None:continue
            if stamp<=position+1e-7:lower=index;confirmed=index
            else:upper=index-1;break
        if candidate is None:return confirmed
        return min(upper,max(lower,candidate)) if upper>=lower else None

    def tracked_index(self,position,*,follow_playback=False):
        self.vocalise_waiting=False
        if not self.song:
            return None
        if not follow_playback and self.manual_index is not None and not self.follow.get():
            return self.manual_index
        if self.is_bound():
            times, _ = self.effective_times()
            offset=self.timing['offset']
            if self.has_direct_sync() or self.has_spotify_clock():
                return self.estimated_index(times,position,offset)
            if self.following_audio():
                return blend_live_index(self.timing,times,position,self.realtime_index)
            if self.following_spotify():
                return blend_live_index(self.timing,times,position,self.spotify_current)
            if self.holding_audio():
                # A missed recognition sample is not evidence of the next row.
                # Keep holding when there are no confirmed onsets.
                return blend_live_index(self.timing,times,position,self.realtime_index)
            if self.spotify_anchor is not None and not self.has_saved_sync():
                anchor,observed_position=self.spotify_anchor
                if anchor<len(self.native_timing.times) and self.native_timing.times[anchor] is not None:
                    return self.estimated_index(times,position,offset,confirmed=anchor)
                if anchor<len(times) and times[anchor] is not None:
                    anchored=max(times[anchor],position-observed_position+times[anchor])
                    candidate=self.estimated_index(times,anchored,confirmed=anchor,clock=position)
                    return blend_live_index(self.timing,times,position,candidate)
            return self.estimated_index(times,position,offset)
        return None

    def render(self):
        if self.closed or self.closing:return
        self.update_transport_buttons()
        self.refresh_user_annotations()
        self.refresh_batch_translations()
        position = self.playback.position()
        index = self.displayed_index(position=position)
        visible_index=0 if index is None else index
        corrected_text=self.corrected_lyric_text(visible_index) if self.display_lines else None
        reading=self.display_lyric_reading(visible_index) if self.display_lines else ''
        corrected_translation=(self.lyric_text_edits.translation(self.annotation_row(visible_index))
                               if self.display_lines else None)
        dialog=self.manual_sync_dialog
        edit_index=dialog.selected_index() if dialog and dialog.matches() else visible_index
        if self.singer_input_index is not None and dialog:
            self.singer_input_index=edit_index
        if self.singer_input_changed_row!=edit_index:self.singer_input_changed_row=None
        clock = ''
        if self.playback.has_timeline:
            seconds = max(0, int(position))
            duration = max(0, int(self.playback.duration))
            clock = f'{seconds//60:02}:{seconds%60:02} / {duration//60:02}:{duration%60:02}'
        self.playback_badge.configure(text=clock)

        error_notice,expires=getattr(self,'error_notice',('',0))
        if time.monotonic()>=expires:error_notice=''
        help_text=self.connection_status()[1] if not self.song and not self.playback.title else ''
        signature = (self.song.id if self.song else None, index, self.displayed_artists(),
                     self.show_reading.get(), self.show_original.get(), bool(self.playback.title),
                     error_notice, help_text, self.current_line_singers(index),
                     self.current_line_singer_partial(index), self.singer_input_index is not None,
                     self.singer_input_live, self.singer_options, dialog is not None,
                     corrected_text, corrected_translation, reading, edit_index)
        if signature != self.last_render:
            self.last_render = signature
            if self.song and self.display_lines:
                original=corrected_text or self.display_lines[visible_index].original
                korean=self.display_lyric_translation(visible_index, self.show_original.get())
            else:
                original = self.playback.title or '아이마스 가사'
                korean = self.connection_status()[0]
            self.reading_label.configure(text=reading if self.show_reading.get() else '')
            self.original_label.configure(text=original if self.show_original.get() else '')
            self.korean_label.configure(text=korean if self.text_styles['korean'].get('visible',True) else '')
            for side,delta in (('previous',-1),('next',1)):
                row=visible_index+delta
                text=''
                if self.text_styles[side].get('visible',False) and self.song and 0<=row<len(self.display_lines):
                    if self.show_original.get():
                        text=self.corrected_lyric_text(row) or self.display_lines[row].original
                    elif self.text_styles['korean'].get('visible',True):
                        text=self.display_lyric_translation(row,False)
                    if not text and self.show_reading.get():
                        text=self.display_lyric_reading(row)
                self.side_lyrics[side].configure(text=text)
            self.current_singers.set_singers(self.current_line_singers(index),self.playback.album,
                                             partial=self.current_line_singer_partial(index))
            mode_text=error_notice or help_text
            if self.singer_input_index is not None:
                names='   '.join(f'{key} {short_performer_name(name,self.playback.album)}'
                                 for key,name in zip(SINGER_KEYS,self.singer_options))
                if self.manual_sync_dialog:
                    mode_text=f'개발자 모드 · 가창자 입력 · 선택 {edit_index+1}/{len(self.display_lines)} · Enter 선택 줄 기록'
                else:
                    following='따라가는 중 · Enter 고정' if self.singer_input_live else '고정 · Enter 따라가기'
                    mode_text=f'가창자 입력 · {visible_index+1}/{len(self.display_lines)} · {following}'
                mode_text+=' · ← → 소절 이동 · Q/W/E/R −5/−2/+1/+5초 · ↑ 앞 줄 끝 · ↓ 다음 시작 · Space 비우기 · Esc 종료'
                if names:mode_text+=f'\n{names}'
                else:mode_text+='\n가창자 정보 대기'
            elif self.manual_sync_dialog:
                mode_text='개발자 모드 · ← → 소절과 재생 위치 이동 · Q/W/E/R −5/−2/+1/+5초 · ↑ 앞 줄 끝 · ↓ 다음 시작'
            self.mode_label.configure(text=mode_text)
            self.update_track_title()
            self.request_fit_height()
        if time.monotonic() >= self.notice_until:
            message = self.playback.error or ''
            if self.status.cget('text') != message:
                self.status.configure(text=message)
                self.update_notice_visibility()
                self.request_fit_height()
        if self.editor:
            self.editor_clock.configure(text=f'Spotify {format_time(self.playback.position())} / {format_time(self.playback.duration)}')
        if dialog:
            if dialog.follow_current_song and dialog.window.state() != 'withdrawn':
                dialog.update_playhead()
            else:
                dialog.refresh()
        if self.workspace.overlay_requested:
            self.electron_overlay_position = position
            if self.electron_overlay_publish is None:
                self.electron_overlay_publish = self.root.after_idle(self.publish_electron_overlay)
        else:
            self.workspace.publish_developer_playback(position)

    def tick(self):
        if self.closed or self.closing:
            return
        if self.services_started:
            self.safe(self.playlist_lyrics.tick,interactive=False)
            preparing = self.missing_audio_collector and self.missing_audio_collector.playlist_request
            self.safe(lambda:self.catalog_sync.tick(allow_search=not (self.playlist_lyrics.owns_spotify or preparing
                                                                     or self.spotify_translation_sources.busy)),interactive=False)
        if self.spotify_translation_sources.busy:
            self.safe(self.spotify_translation_sources.tick,interactive=False)
        if self.track_notice_until and time.monotonic()>=self.track_notice_until:
            self.show_track_notice('')
        while True:
            try:
                kind, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == 'media':
                self.safe(lambda: self.set_playback(value),interactive=False)
            elif kind == 'media_control':
                self.finish_track_change(*value)
            elif kind == 'media_seek':
                self.finish_seek(*value)
            elif kind == 'media_playback':
                self.finish_playback(*value)
            elif kind == 'spotify_exit':
                collecting=self.playlist_lyrics.value.get('shutdown_requested') is True
                if self.spotify_lifecycle.get() and not collecting:
                    self.safe(lambda:mark_spotify_exit(self.store.directory),interactive=False)
                    self.close()
                    return
            elif kind == 'spotify_lifecycle_error':
                self.notify('Spotify 실행 상태 확인을 재시도합니다: '+value,12)
            elif kind == 'stamp_at' and self.manual_sync_dialog:
                self.safe(lambda:self.stamp(value),interactive=False)
            elif kind == 'action':
                actions = {'previous': lambda: self.move(-1), 'next': lambda: self.move(1),
                           'lock': self.toggle_lock, 'chrome':self.toggle_chrome,
                           'transparent':self.toggle_transparent}
                if value in actions:self.safe(actions[value],interactive=False)
            elif kind == 'hotkeys':
                self.lock_available = 6 not in value
                self.chrome_available = 7 not in value
                if not self.lock_available and self.locked and not self.tray:self.safe(self.toggle_lock,interactive=False)
                if self.settings.get('borderless',False) and not self.borderless.get():
                    self.safe(self.toggle_chrome,interactive=False)
                failed=[label for action,(key_id,label) in ACTIONS.items() if key_id in value and self.hotkeys.bindings.get(action) is not None]
                if failed:self.notify('단축키 등록 실패: '+', '.join(failed)+' · 우클릭 → 단축키 설정',12)
            elif kind == 'timing':
                self.safe(lambda: self.receive_timing(*value),interactive=False)
            elif kind == 'background_lyrics':
                self.safe(lambda:self.receive_background_lyrics(*value),interactive=False)
            elif kind == 'artist_catalog_ready':
                if self.background_lyrics.accept_artist_catalog(value):self.last_render=None
            elif kind == 'lyric_readings':
                self.receive_lyric_readings(*value)
            elif kind == 'related_lyrics':
                self.safe(lambda:self.receive_related_lyrics(*value),interactive=False)
            elif kind == 'merged_lyrics':
                self.safe(lambda:self.receive_merged_lyrics(*value),interactive=False)
            elif kind == 'lyricsplus':
                self.safe(lambda:self.receive_lyricsplus(value),interactive=False)
            elif kind == 'audio':
                self.safe(lambda: self.receive_audio(*value),interactive=False)
            elif kind == 'auto_audio':
                self.safe(lambda:self.receive_auto_audio(*value),interactive=False)
            elif kind == 'internal_reader':
                self.safe(lambda:self.internal_reader.receive(*value),interactive=False)
            elif kind == 'web_lyrics':
                self.safe(lambda:self.receive_web_lyrics(*value),interactive=False)
            elif kind == 'extension_lyrics':
                self.safe(lambda:self.receive_extension_lyrics(*value),interactive=False)
            elif kind == 'spotify_tracking':
                if not self.screen_lyrics_allowed():continue
                key,status=value
                if key==self.playback.key and status=='background_clock':self.spotify_current=None;self.spotify_observed_at=0
                if key==self.playback.key and not self.following_spotify():self.reader_bridge.set_tracking('saved_sync' if self.has_saved_sync() else status)
            elif kind == 'spotify_active':
                self.receive_spotify_active(*value)
            elif kind == 'spotify_lyrics':
                if not self.screen_lyrics_allowed():continue
                key,lines=value
                if key==self.playback.key:
                    self.reader_bridge.set_reference(key,[line.original for line in lines])
                    self.screen_status=f'Spotify 원문 {len(lines)}줄 확인'
                    if not self.song:
                        def receive_screen():
                            song=self.live_lyrics.receive(self.playback,None,lines)
                            if not any(t is not None for t in self.live_lyrics.timing['times']):self.live_lyrics.timing['source']='Spotify 화면 원문 · 추정 진행'
                            self.select_song(song)
                            self.screen_song=song
                            self.screen_cache_tainted=True
                        self.safe(receive_screen,interactive=False)
                    elif not self.live_lyrics.is_temporary(self.song):
                        def complete_screen():
                            if self.complete_saved_lyrics(lines):self.screen_cache_tainted=True
                        self.safe(complete_screen,interactive=False)
                    self.last_render=None
            elif kind == 'screen_lyrics':
                if not self.screen_lyrics_allowed():continue
                key,lines=value
                if key==self.playback.key:
                    self.reader_bridge.set_reference(key,lines)
                    self.screen_status=f'Spotify 화면 원문 {len(self.reader_bridge.references)}줄 확인'
                    self.last_render=None
            elif kind == 'screen_status':
                if not self.screen_lyrics_allowed():continue
                self.screen_status=value;self.last_render=None
            elif kind == 'reader_lyrics':
                self.safe(lambda:self.receive_reader_lyrics(*value),interactive=False)
            elif kind == 'reader_document':
                track,url=value
                if track==self.reader_bridge.snapshot()['track']:
                    existing=self.reader_bridge.document_urls.get(track)
                    if isinstance(url,dict) and url.get('learned') and isinstance(existing,str) and existing:continue
                    self.reader_bridge.document_urls[track]=url
                    self.safe(lambda:atomic_json(self.store.directory/'wiki_documents.json',self.reader_bridge.document_urls),interactive=False)
        self.render()
        if self.missing_audio_collector:
            self.missing_audio_collector.tick()
            if self.close_after_collection and not self.missing_audio_collector.stopping:
                self.close()
                return
        if self.manual_sync_dialog:
            self.safe(self.manual_sync_dialog.advance,interactive=False)
        index=self.displayed_index()
        self.reader_bridge.set_overlay({**self.input_status,'follow':self.follow.get(),
            'dictionary_open':self.explorer_dialog is not None,
            'dictionary_selected_title':((self.explorer_dialog.selected_song or {}).get('title','') if self.explorer_dialog else ''),
            'dictionary_spotify_links':len(self.explorer_dialog.library.value['spotify']) if self.explorer_dialog else None,
            'dictionary_title_grouping':self.explorer_dialog.hide_duplicates.get() if self.explorer_dialog else None,
            'dictionary_visible_rows':len(self.explorer_dialog.filtered) if self.explorer_dialog else None,
            'dictionary_hidden_rows':self.explorer_dialog.hidden_duplicate_count if self.explorer_dialog else None,
            'dictionary_auto_status':self.explorer_dialog.auto_status.get() if self.explorer_dialog else '',
            'catalog_sync_status':self.catalog_sync.status,
            'catalog_sync_enabled':self.catalog_sync.enabled,
            'playlist_lyrics_status':self.playlist_lyrics.status,
            'missing_audio':self.missing_audio_collector.snapshot() if self.missing_audio_collector else None,
            'playlist_lyrics_enabled':self.playlist_lyrics.enabled,
            'playlist_lyrics_retry_at':self.playlist_lyrics.value.get('retry_at'),
            'playlist_lyrics_enrichment_status':self.playlist_lyrics.enrichment_status,
            'playlist_lyrics_enrichment_retry_at':self.playlist_lyrics.value.get('enrichment_retry_at'),
            'index':index,'focus':str(self.root.tk.call('focus')),'locked':self.locked,
            'tray_registered':self.tray is not None,'overlay_visible':bool(self.root.winfo_viewable()),
            'overlay_render_backend':'electron' if self.electron_overlay_active else 'tk',
            'position':round(self.playback.position(),3),'lyric_advance_ms':self.lyric_advance_ms,
            'temporary':self.live_lyrics.is_temporary(self.song),
            'audio_state':self.realtime_state,'audio_detail':self.realtime_detail,'audio_index':self.realtime_index,
            'audio_runtime':self.realtime_runtime,
            'audio_activity':self.auto_analyzer.activity_snapshot(),
            'audio_following':self.following_audio(),'auto_status':self.auto_analyzer.status,
            'reader_status':self.internal_reader.status,
            'background_status':self.background_lyrics.status,'spotify_api_status':self.background_lyrics.spotify_status,
            'lyricsplus_status':self.lyricsplus_job.status,'lyricsplus_pending':self.lyricsplus_job.pending,
            'lyricsplus_enabled':self.lyricsplus_background.get(),
            'alsong_status':self.alsong_job.status,'alsong_pending':self.alsong_job.pending,
            'alsong_enabled':self.alsong_background.get(),
            'spotify_api_state':self.background_lyrics.spotify_state,
            'spotify_authenticated':self.background_lyrics.spotify_authenticated,
            'spotify_last_error':self.background_lyrics.spotify_last_error,
            'spotify_catalog_diagnostics':getattr(self.background_lyrics,'spotify_catalog_diagnostics',[]),
            'spotify_exit_code':self.background_lyrics.spotify_exit_code,
            'spotify_retry_count':self.background_lyrics.spotify_retry_count,
            'spotify_retry_pending':bool(self.background_lyrics.spotify_retry_at),
            'screen_lyrics_enabled':self.screen_lyrics.needed,
            'screen_source_active':self.song is not None and self.song is self.screen_song,
            'lyric_cache_hit':self.cache_hit,'lyric_cache_load_ms':self.cache_load_ms,
            'ai_translation_available':self.batch_translations is not None and not self.batch_translation_error,
            'ai_translation_dialog_open':self.translation_dialog is not None,
            'ai_translation_error':self.batch_translation_error,
            'lyric_cache_reused':bool(self.cache_reuse),
            'sync_reusing':self.background_lyrics.cached_sync,
            'spotify_lookup_pending':self.background_lyrics.spotify_pending,
            'track_controls_visible':all(button.winfo_ismapped() for button in (self.previous_track_button,self.next_track_button)),
            'seek_controls_visible':all(button.winfo_ismapped() for button in (self.track_start_button,self.track_end_button)),
            'play_pause_visible':bool(self.play_pause_button.winfo_ismapped()),
            'play_pause_text':self.play_pause_button.cget('text'),
            'volume_control_visible':bool(self.volume_button.winfo_ismapped()),
            'volume_popup_open':self.volume_popup is not None and self.volume_popup.visible,
            'playback_pending':self.pending_playback,
            'spotify_lifecycle_enabled':self.spotify_lifecycle.get(),
            'playback_badge_text':self.playback_badge.cget('text'),
            'seek_pending':self.pending_seek[0] if self.pending_seek else None,
            'track_change_pending':self.pending_track_change,
            'track_title_visible':bool(self.track_title_label.winfo_ismapped()),
            'overlay_manual_size':self.manual_window_size,
            'overlay_size':[self.root.winfo_width(),self.root.winfo_height()],
            'overlay_compact_metadata':self.compact_metadata,
            'overlay_compact_reading':self.compact_reading,
            'overlay_compact_hidden':sorted(self.compact_hidden),
            'overlay_compact_mode':self.compact_layout['mode'],
            'overlay_lyric_scale':self.lyric_scale,
            'track_title_text':self.track_title_label.cget('text'),
            'track_artist_visible':bool(self.track_artist_label.winfo_ismapped()),
            'track_artist_text':self.track_artist_label.cget('text'),
            'lyric_center_offset_px':round(self.lyric_content.winfo_rootx()+self.lyric_content.winfo_width()/2
                                          -self.root.winfo_rootx()-self.root.winfo_width()/2,1),
            'track_title_origin':[self.track_title_label.winfo_rootx()-self.root.winfo_rootx(),
                                  self.track_title_label.winfo_rooty()-self.root.winfo_rooty()],
            'display_artists':list(self.displayed_artists()),
            'artist_display_text':artist_label(self.displayed_artists(),self.playback.album),
            'character_outlines':[{'name':self.track_artist_label.cget('text')[start:end],'color':color}
                                  for start,end,color in self.track_artist_label.cget('outline_spans')],
            'connection_setup_open':self.connection_dialog is not None,
            'direct_source':self.direct_result[2]['source'] if self.direct_result else '',
            'direct_sync_rows':sum(t is not None for t in self.direct_times()),
            'direct_sync_active':self.has_direct_sync(),
            'spotify_clock_active':self.has_spotify_clock(),
            'direct_sync_untimed_rows':sum(t is None for t in self.direct_times()),
            'display_version':2,'translated_lines':self.translated_count,
            'app_process_id':os.getpid(),
            'reading_lines':sum(bool(value.strip()) for value in self.line_readings),
            'current_reading_available':bool(self.reading_label.cget('text')) if self.song else False,
            'user_annotated_lines':sum(bool(row.korean or row.reading or row.singers) for row in self.user_annotation_rows),
            'user_singer_lines':sum(bool(names) for names in self.line_singers),
            'current_singers':list(self.current_line_singers()),
            'current_singers_partial':self.current_line_singer_partial(),
            'current_singers_text':self.current_singers.cget('text'),
            'user_annotation_errors':len(self.user_annotations.errors),
            'display_line_count':len(self.display_lines),'supplemented_lines':self.live_lyrics.completion_count,
            'current_has_translation':bool(self.song and self.display_lines and self.display_lines[index or 0].korean.strip()),
            'vocalise_waiting':self.vocalise_waiting,'auto_enabled':self.auto_analyzer.enabled,
            'duration':round(self.playback.duration,3),
            'current_audio_jobs':[state for job,state in self.auto_analyzer.jobs.items() if job.analysis_key[0]==self.playback.key]})
        if self.audio_window and time.monotonic()-self.audio_progress_updated >= .5:
            self.refresh_audio_progress()
        if time.monotonic()-self.reader_observed >= .5:
            self.reader_observed=time.monotonic()
            if self.services_started:self.observe_lyricsplus()
            if self.services_started and not self.lyrics_deleted():self.safe(lambda:self.internal_reader.observe(self.reader_bridge.snapshot()),interactive=False)
            if self.connection_dialog:self.connection_dialog.refresh()
        self.root.after(80, self.tick)

    def complete_saved_lyrics(self,lines):
        if not self.song or self.live_lyrics.is_temporary(self.song):return False
        if self.user_lyric_override():return False
        old=self.song;cursor=self.cursor;manual=self.manual_index;follow=self.follow.get()
        completed=self.live_lyrics.complete_saved(self.playback,old,lines,self.timing)
        if completed is None:return False
        mapping={old:new for new,old in enumerate(self.live_lyrics.completion_indices) if old is not None}
        self.select_song(completed)
        # Seed the text-free timeline before an automatic job can merge results.
        self.save_current_timing(self.timing)
        self.cursor=mapping.get(cursor,min(cursor,len(completed.lines)-1))
        self.manual_index=mapping.get(manual) if manual is not None else None
        self.follow.set(follow)
        self.last_render=None
        if self.editor:self.editor_refresh()
        return True

    def receive_reader_lyrics(self,track,lines,supplement_only=False,reader_token=None,part_sources=(),documents=()):
        if self.temporary_alsong:return
        state=self.reader_bridge.snapshot()
        if track!=state['track'] or reader_token is not None and reader_token!=state['token']:return
        if self.lyrics_deleted():return
        if self.user_lyric_override():
            self.reader_bridge.set_receipt(track,'displayed',len(self.display_lines),self.translated_count,token=reader_token)
            return
        if documents and self.playback.has_timeline:
            snapshots=tuple(make_snapshot(source_id(url),rows,provider_id=url,title=title)
                            for url,title,rows in documents)
            if (self.current_lyric_merge and not self.merge_reader_receipts and not self.lyric_merge_job.running and
                    all(self.lyric_merge_job.sources.get((snapshot['source'].casefold(),snapshot['provider_id']))==snapshot
                        for snapshot in snapshots)):
                self.acknowledge_reader_lyrics(track,reader_token,part_sources)
                return
            self.merge_reader_receipts.append((track,reader_token,part_sources,lines,supplement_only,snapshots))
            self.queue_lyric_merge()
            return
        if (is_short_recording(self.playback.title) and self.has_direct_sync()
                and match_lyrics([line.original for line in lines],
                                 [line.original for line in self.display_lines]) is None):
            # A matching chorus in a full-length document cannot replace the
            # confirmed original and clock of a game/TV edit.
            self.reader_bridge.set_receipt(track,'display_failed',token=reader_token)
            self.notify('단축 버전 원문과 다른 가사여서 연결하지 않았습니다 · 현재 싱크 유지',12)
            return
        linked=self.settings.get('links',{}).get(self.playback.key)
        base=match_recording(self.songs,self.playback.title,self.playback.artist,self.playback.album,linked)
        try:
            # A late translation must not change the row signature used by an
            # ongoing capture or saved audio anchors. Verified sentence blocks
            # can enrich those rows without invalidating the recording.
            audio_rows=(not base and self.is_bound() and not self.screen_cache_tainted and
                        self.live_lyrics.is_temporary(self.song) and not self.has_direct_sync() and
                        ('audio' in timing_origins(self.timing) or any(
                            job.analysis_key==(self.playback.key,self.auto_analyzer.signature(self.song,True)) and
                            not job.cancelled.is_set() for job in self.auto_analyzer.jobs)))
            preserve_rows=(supplement_only and self.is_bound() and not self.screen_cache_tainted)
            if preserve_rows or (audio_rows and (
                    match_verified_annotations(self.display_lines,lines) is not None or
                    match_lyrics([line.original for line in lines],
                                 [line.original for line in self.display_lines]) is not None)):
                self.merge_current_annotations(lines)
                self.render()
            else:
                song=self.live_lyrics.receive(self.playback,base,lines)
                self.select_song(song)
                self.complete_direct_originals()
        except Exception:
            self.reader_bridge.set_receipt(track,'display_failed',token=reader_token)
            raise
        self.acknowledge_reader_lyrics(track,reader_token,part_sources)

    def finish_merge_reader_receipts(self,applied,provisional=False):
        receipts,self.merge_reader_receipts=self.merge_reader_receipts,[]
        state=self.reader_bridge.snapshot()
        for track,token,parts,lines,supplement,snapshots in receipts:
            if track!=state['track'] or token is not None and token!=state['token']:continue
            if applied or provisional:
                for snapshot in snapshots:self.lyric_merge_job.remember(snapshot)
            if applied:self.acknowledge_reader_lyrics(track,token,parts)
            elif provisional:self.receive_reader_lyrics(track,lines,supplement,token,parts)
            else:self.reader_bridge.set_receipt(track,'display_failed',token=token)

    def acknowledge_reader_lyrics(self,track,reader_token,part_sources):
        state=self.reader_bridge.snapshot()
        if track!=state['track'] or reader_token is not None and reader_token!=state['token']:return
        if part_sources:
            def apply_parts():
                direct=self.direct_result
                target=(direct[2]['lines'] if direct and direct[0]==self.playback.key else self.display_lines)
                if save_wiki_parts(self.store.directory,self.playback,target,part_sources,self.displayed_artists()):
                    self.user_annotations=UserAnnotations(self.store.directory)
                    self.user_annotation_signature=None
                    self.render()
            self.safe(apply_parts,interactive=False)
        self.reader_bridge.set_receipt(track,'displayed',len(self.display_lines),self.translated_count,token=reader_token)
        if self.is_bound():self.observe_auto_audio()
        self.cache_current_lyrics()
        if not self.translated_count:
            self.notify('원문 가사 연결 · 한국어 번역은 아직 확인되지 않았습니다.',10)
        elif self.translated_count<len(self.display_lines):
            self.notify(f'가사 연결 · 번역 {self.translated_count}/{len(self.display_lines)}줄 · 나머지 번역 미확인',10)
        else:self.notify('가사 연결 완료 · 다음 재생부터 저장한 가사를 바로 불러옵니다.',10)

    def save_current_timing(self,value):
        if self.temporary_alsong:
            self.save_alsong_timing(value, self.temporary_alsong.rows)
            self.live_lyrics.timing=value
            return
        if self.live_lyrics.is_temporary(self.song):
            validate_times(value['times'],len(self.song.lines))
            self.live_lyrics.timing_store.save(self.song,value)
            self.live_lyrics.timing=value
        else:self.store.save_timing(self.song,value)

    def annotation_row(self, index):
        return self.temporary_alsong.rows[index] if self.temporary_alsong else index

    def corrected_lyric_text(self, index):
        return self.lyric_text_edits.original(self.annotation_row(index))

    def display_lyric_line(self, index):
        line = self.display_lines[index]
        corrected = self.corrected_lyric_text(index)
        return Line(corrected, line.reading, line.korean) if corrected is not None else line

    def display_lyric_reading(self, index):
        reading = self.lyric_text_edits.reading(self.annotation_row(index))
        if reading is not None:
            return reading
        line = self.display_lines[index]
        corrected = self.corrected_lyric_text(index)
        if is_latin_only(corrected or line.original):
            return ''
        if corrected is not None:
            if not self.services_started:
                return readings_for_display((Line(corrected, '', ''),))[0]
            request=self.line_reading_request
            if not request or index>=len(request[2]) or request[2][index]!=(corrected,''):return ''
        if index<len(self.line_readings):return self.line_readings[index]
        return '' if corrected is not None else line.reading

    def display_lyric_translation(self, index, original_visible=True):
        corrected = self.lyric_text_edits.translation(self.annotation_row(index))
        if corrected is not None:
            return corrected
        return display_translation(self.display_lyric_line(index), original_visible,
                                   user_override=self.user_lyric_override())

    def move(self, delta):
        if self.manual_sync_dialog:
            self.manual_sync_dialog.step_row(delta)
            return
        if not self.song:
            return
        if self.singer_input_index is not None:
            previous=self.visible_lyric_index()
            index=max(0,min(len(self.display_lines)-1,previous+delta))
            if index!=previous:
                self.singer_input_live=False
                self.singer_input_index=index
                self.render()
                self.seek_lyric_row(index)
            return
        index = self.displayed_index()
        if index is None:
            index = -1 if delta > 0 else 0
        if self.is_bound():
            row = max(0,min(len(self.display_lines)-1,index+delta))
            if self.display_lines and row != index:
                self.seek_lyric_row(row,allow_estimate=False)
            return
        self.manual_index = max(0, min(len(self.song.lines)-1, index+delta))
        self.cursor = self.manual_index
        self.follow.set(False)
        if self.editor:
            self.select_editor_row(self.cursor)
        self.render()

    def resume_follow(self):
        self.stop_singer_input(redraw=False)
        self.follow.set(True)
        self.manual_index = None
        self.render()

    def offset(self, change):
        if not self.song:
            return
        updated = {**self.timing, 'offset': round(self.timing['offset'] + change, 2), 'manual_priority':True}
        self.save_current_timing(updated)
        self.timing = updated
        self.notify(f'가사를 {abs(updated["offset"]):.2f}초 ' + ('늦게' if updated['offset'] >= 0 else '빠르게') + ' 표시합니다.')
        self.render()

    def sync_capture_position(self,pressed_at=None):
        if self.pending_track_change or self.pending_seek or self.pending_playback:
            raise ValueError('Spotify 재생 위치가 바뀌는 중입니다. 이동이 끝난 뒤 다시 눌러 주세요.')
        if pressed_at is None:return self.playback.position()
        if (not math.isfinite(pressed_at) or not 0<=time.monotonic()-pressed_at<=2
                or pressed_at<self.spotify_sample_after):
            raise ValueError('곡 전환·탐색 전이거나 오래 지연된 기록 입력은 적용하지 않았습니다.')
        # Project a newer continuous playback snapshot back to the key press.
        elapsed=(pressed_at-self.playback.captured)*self.playback.rate if self.playback.playing else 0
        return min(self.playback.duration,max(0,self.playback.base+elapsed))

    def stamp(self,pressed_at=None,*,previous_end=False):
        if self.manual_sync_dialog:
            self.manual_sync_dialog.record_next(pressed_at)
            return
        if not self.is_bound():
            self.notify('현재 Spotify 곡에 연결된 가사와 재생 시간이 있어야 기록할 수 있습니다.')
            return
        singer_input=self.singer_input_index is not None
        current = self.visible_lyric_index() if singer_input or previous_end else self.displayed_index(advance=False)
        if current is None:current = 0  # The overlay shows the first line before its start.
        index = current if previous_end else current+1
        if previous_end and index == 0:
            self.show_track_notice('첫 소절 앞에는 기록할 소절이 없습니다.')
            return
        if index >= len(self.song.lines):
            self.notify('마지막 구절입니다. 다음 구절이 없어 시간표는 변경하지 않습니다.')
            return
        try:
            position=self.sync_capture_position(pressed_at)
        except ValueError as error:
            self.notify(str(error))
            return
        try:
            updated=edit_manual_time(self.timing,index,round(max(0,position-self.timing['offset']),3))
        except ValueError:
            if previous_end:
                self.show_track_notice('인접 소절의 저장 시각과 겹쳐 앞 소절 끝을 바꿀 수 없습니다.')
                return
            # A conflicting manual onset should block the write, not navigation.
            updated=None
            notice='다음 구절로 이동 · 기존 싱크와 충돌해 새 시각은 저장하지 않았습니다.'
            self.error_notice=(notice,time.monotonic()+6)
        else:
            updated['source']='직접 기록'
            self.save_current_timing(updated)
            self.timing=updated
            notice=f'{current+1}번 끝 → {index+1}번 전환 시각 저장'
        if previous_end:
            if self.editor:self.editor_refresh()
            self.show_track_notice(f'{index}번 끝 → {index+1}번 시작 {format_time(position)} 저장')
            self.render()
            return
        self.cursor = index
        if singer_input:
            self.singer_input_index=index
            self.singer_input_live=False
            self.queued_lyric_seek=None
            self.show_track_notice(notice if updated is None else f'{index+1}번 시작 {format_time(position)} 저장')
        else:
            self.manual_index = index
            self.follow.set(False)
            self.notify(notice)
        if self.editor:
            if updated is not None:self.editor_refresh()
            self.select_editor_row(self.cursor, preview=False)
        self.render()

    def toggle_lock(self):
        if not self.locked and not (self.lock_available or self.tray):
            self.notify('복귀 단축키를 사용할 수 없습니다. 단축키 설정에서 클릭 통과 키를 지정하세요.')
            return
        self.locked = not self.locked
        if self.locked:self.stop_singer_input()
        set_click_through(self.root, self.electron_overlay_active or self.locked)
        self.track_artist_label._schedule_layer()
        for column in self.current_singers.columns:column._schedule_layer()
        self.backdrop.click_through=self.locked;self.backdrop.sync()
        self.lock_button.configure(text='통과 해제' if self.locked else '클릭 통과')
        recovery = '트레이 메뉴에서 해제' if self.tray else self.shortcut_text('lock') + '로 해제'
        self.notify('클릭 통과 켜짐 · '+recovery if self.locked else '클릭 통과 해제됨')

    def fetch_timing(self, manual=False):
        if self.live_lyrics.is_temporary(self.song):return
        if not self.song:return
        song = self.song
        pending=self.query_running.get(song.id)
        if pending and self.recording_contexts.valid(pending,song):return
        if self.has_saved_sync() and not manual:
            return
        self.auto_queried.add(song.id)
        context=self.recording_contexts.capture(song,self.playback if self.is_bound() else None)
        self.query_running[song.id]=context
        duration = self.playback.duration if self.is_bound() else 0
        self.notify('공개된 싱크 가사 확인 중…')
        def worker():
            try:
                result = get_public_timing(song, duration)
                error = ''
            except Exception as exception:
                result = None
                error = f'공개 싱크 조회 실패 ({type(exception).__name__}) · 직접 기록은 사용할 수 있습니다.'
            self.messages.put(('timing', (song, result, error,context)))
        threading.Thread(target=worker, daemon=True, name='public-lyrics-timing').start()

    def receive_timing(self, song, result, error,context):
        if self.query_running.get(song.id) is not context:return
        self.query_running.pop(song.id)
        latest = next((s for s in self.songs if s.id == song.id), None)
        if latest is None or latest.fingerprint != song.fingerprint:
            return
        if not self.recording_contexts.valid(context,latest):
            if self.song and self.song.id==song.id:self.notify('재생 길이가 바뀌어 이전 공개 싱크 응답을 제외했습니다.',8)
            return
        current = self.store.timing(song)
        if result:
            backup=self.store.directory/'audio_backups'/(song.id+'.before-public.json')
            if any(t is not None for t in current['times']) and not backup.exists():
                atomic_json(backup,{**current,'fingerprint':song.fingerprint})
            updated,_=merge_candidates(current,result['times'],replace_audio=True)
            if current.get('source')=='LRCLIB' or 'manual' not in updated['origins']:
                updated['source']='LRCLIB'
            self.store.save_timing(song, updated)
            if self.song and self.song.id == song.id:
                self.timing = updated
                self.last_render=None;self.render()
                self.notify(f'공개 싱크 연결됨 · {result["matched"]}/{len(song.lines)}구절 · 빈 구절은 추정/직접 기록으로 보완합니다.')
                if self.editor:
                    self.editor_refresh()
        elif self.song and self.song.id == song.id:
            self.notify(error or '공개 싱크가 없습니다. 가사 길이로 추정하며 '+self.shortcut_text('align')+'로 현재 구절을 맞출 수 있습니다.', 12)

    def open_editor(self):
        if self.editor:
            self.editor.deiconify()
            self.editor.lift()
            return
        if self.locked:
            self.toggle_lock()
        window = self.editor = tk.Toplevel(self.root)
        window.title('가사 라이브러리 · 실시간 싱크 편집')
        window.geometry('880x760')
        window.minsize(560, 660)
        window.configure(bg=BG)
        window.attributes('-topmost', False)
        window.protocol('WM_DELETE_WINDOW', self.close_editor)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(3, weight=1)
        top = tk.Frame(window, bg=BG)
        top.grid(row=0, column=0, sticky='ew', padx=12, pady=10)
        self.button(top, '가사 폴더 가져오기', self.choose_folder).grid(row=0,column=1,padx=(0,8))
        self.button(top, '현재 곡에 연결', self.bind_current).grid(row=0,column=2)
        self.delete_lyrics_button=self.button(top, '선택 가사 삭제…', self.delete_selected_lyrics)
        self.delete_lyrics_button.grid(row=1,column=2,sticky='w',pady=(5,0))
        self.editor_clock = tk.Label(top, bg=BG, fg=ACCENT, font=('Malgun Gothic', 10))
        self.editor_clock.grid(row=2,column=0,columnspan=3,sticky='w',pady=(5,0))
        select = tk.Frame(window, bg=BG)
        select.grid(row=1, column=0, sticky='ew', padx=12)
        select.columnconfigure(0, weight=1)
        self.song_picker = ttk.Combobox(select, state='readonly', font=('Malgun Gothic', 11))
        self.song_picker.grid(row=0, column=0, sticky='ew')
        self.song_picker.bind('<<ComboboxSelected>>', self.pick_song)
        self.editor_help = tk.Label(window, bg=BG, fg=MUTED, justify='left', anchor='w',
                                    text='「끝 → 다음」: 보이는 구절이 끝나면 눌러 다음 구절로 전환·저장합니다.\n'
                                         '「이 구절 지금 맞춤」 또는 「시간 저장」: 선택한 구절의 시작 시각을 수정합니다.\n'
                                         '「이 구절 자동」: 수동 수정을 풀고 자동 싱크로 복원합니다. 나머지는 계속 자동입니다.\n'
                                         '「단축키 설정…」에서 모든 조작 키를 지정하거나 확인할 수 있습니다.',
                                    font=('Malgun Gothic', 10))
        self.editor_help.grid(row=2, column=0, sticky='ew', padx=12, pady=10)
        table = tk.Frame(window, bg=BG)
        table.grid(row=3, column=0, sticky='nsew', padx=12)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=('time', 'original', 'korean','mode'), displaycolumns=('time','mode','original','korean'),show='headings', selectmode='browse')
        for key, title, width in [('time','시작 시간',85), ('original','일본어 원문',300), ('korean','한국어 번역',330),('mode','적용',60)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=55, stretch=key not in ('time','mode'))
        self.tree.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<<TreeviewSelect>>', self.editor_row_selected)
        self.tree.bind('<ButtonRelease-1>', self.editor_row_selected)
        self.preview = tk.Label(window, text='', bg=BG, fg=ACCENT, justify='left', anchor='w',
                                font=('Malgun Gothic', 10), wraplength=800)
        self.preview.grid(row=4, column=0, sticky='ew', padx=12, pady=7)
        edit = tk.Frame(window, bg=BG)
        edit.grid(row=5, column=0, sticky='ew', padx=12, pady=5)
        tk.Label(edit, text='선택 구절 시간', bg=BG, fg=TEXT).grid(row=0,column=0)
        self.time_entry = ttk.Entry(edit, width=12)
        self.time_entry.grid(row=0,column=1,padx=7)
        self.button(edit, '시간 저장', self.save_selected_time).grid(row=0,column=2,padx=3)
        self.button(edit, '음원 분석…', self.open_audio).grid(row=0,column=3,padx=3)
        self.restore_auto_button=self.button(edit,'이 구절 자동',self.restore_selected_auto)
        self.restore_auto_button.grid(row=0,column=4,padx=3)
        self.button(edit,'표시 설정…',self.open_appearance).grid(row=1,column=3,padx=3)
        self.button(edit,'단축키 설정…',self.open_shortcuts).grid(row=1,column=4,padx=3)
        self.button(edit, '끝 → 다음', self.stamp).grid(row=1,column=0,sticky='ew',pady=4)
        self.button(edit, '이 구절 지금 맞춤', self.align_now).grid(row=1,column=1,sticky='ew',padx=3)
        self.button(edit, '자동 따라가기', self.resume_follow).grid(row=1,column=2,sticky='ew',padx=3)
        network = tk.Frame(window, bg=BG)
        network.grid(row=6, column=0, sticky='ew', padx=12, pady=5)
        self.button(network, '공개 싱크 재조회', lambda: self.fetch_timing(True)).pack(side='left', padx=3)
        self.button(network, 'LRC 시간표 가져오기', self.choose_lrc).pack(side='left', padx=3)
        tk.Checkbutton(network, text='공개 싱크 자동 조회', variable=self.online, bg=BG, fg=TEXT,
                       selectcolor=PANEL, activebackground=BG, command=lambda: self.safe(self.save_settings)).pack(side='left', padx=8)
        appearance = tk.Frame(window, bg=BG)
        appearance.grid(row=7, column=0, sticky='ew', padx=12, pady=(3, 10))
        tk.Label(appearance, text='투명도', bg=BG, fg=TEXT).grid(row=0, column=0)
        tk.Scale(appearance, from_=.35, to=1, resolution=.05, variable=self.opacity, orient='horizontal',
                 bg=BG, fg=TEXT, highlightthickness=0, length=130, command=self.apply_appearance).grid(row=0,column=1)
        tk.Label(appearance, text='글자', bg=BG, fg=TEXT).grid(row=0,column=2,padx=(8,3))
        ttk.Spinbox(appearance, from_=16, to=38, width=4, state='readonly', textvariable=self.font_size,
                    command=self.apply_appearance).grid(row=0,column=3)
        for column, label, variable in [(4,'독음',self.show_reading),(5,'원문',self.show_original)]:
            tk.Checkbutton(appearance, text=label, variable=variable, bg=BG, fg=TEXT,
                           selectcolor=PANEL, command=self.apply_appearance).grid(row=0,column=column,padx=6)
        self.button(appearance,'헤더 숨기기 / 복원',self.toggle_chrome).grid(row=1,column=0,columnspan=3,sticky='ew',padx=3,pady=4)
        self.button(appearance,'배경 투명 / 복원',self.toggle_transparent).grid(row=1,column=3,columnspan=3,sticky='ew',padx=3,pady=4)
        estimate = tk.Frame(window,bg=BG)
        estimate.grid(row=8,column=0,sticky='ew',padx=12,pady=(0,10))
        tk.Checkbutton(estimate,text='가사 길이로 자동 진행 (추정)',variable=self.use_estimate,bg=BG,fg=TEXT,
                       selectcolor=PANEL,command=lambda:self.safe(self.update_estimate)).grid(row=0,column=0,columnspan=5,sticky='w')
        tk.Label(estimate,text='전주(초)',bg=BG,fg=TEXT).grid(row=1,column=0)
        self.intro_entry = ttk.Entry(estimate,width=7)
        self.intro_entry.grid(row=1,column=1,padx=5)
        tk.Label(estimate,text='후주(초)',bg=BG,fg=TEXT).grid(row=1,column=2,padx=(12,0))
        self.outro_entry = ttk.Entry(estimate,width=7)
        self.outro_entry.grid(row=1,column=3,padx=5)
        self.button(estimate,'추정에 반영',self.update_estimate).grid(row=1,column=4,padx=8)
        window.bind('<Configure>', self.editor_resize)
        self.editor_refresh()
        self.hotkeys.register_window(window)

    def editor_resize(self, event):
        if event.widget is self.editor:
            self.preview.configure(wraplength=max(300, event.width-30))
            self.editor_help.configure(wraplength=max(300, event.width-30))
            available = max(200,event.width-195)
            self.tree.column('original',width=int(available*.48))
            self.tree.column('korean',width=int(available*.52))

    def editor_refresh(self):
        if not self.editor:
            return
        self.delete_lyrics_button.configure(state='normal' if self.song else 'disabled')
        self.picker_songs=self.songs+([self.live_lyrics.song] if self.live_lyrics.is_temporary(self.live_lyrics.song) else [])
        self.song_picker['values'] = [f'{s.album} · {s.title} — {artist_label((s.artist,),s.album)}' for s in self.picker_songs]
        if self.song:
            self.song_picker.current(next(i for i,s in enumerate(self.picker_songs) if s.id == self.song.id))
        else:
            self.song_picker.set('곡을 선택하세요')
        existing=self.tree.get_children()
        expected=tuple(str(i) for i in range(len(self.display_lines))) if self.song else ()
        editor_song_key=(self.song.id,tuple(line.original for line in self.song.lines)) if self.song else None
        reuse=existing==expected and getattr(self,'editor_song_key',None)==editor_song_key
        self.editor_song_key=editor_song_key
        if not reuse:self.tree.delete(*existing)
        if self.song:
            estimated_times, estimated = self.effective_times()
            for index in range(len(self.display_lines)):
                line=self.display_lyric_line(index)
                stamp = estimated_times[index]
                display = format_time(stamp)
                mode=self.time_sources[index]
                if mode=='추정':display='~'+display
                values=(display,line.original,self.display_lyric_translation(index),mode)
                if reuse:self.tree.item(str(index),values=values)
                else:self.tree.insert('', 'end', iid=str(index), values=values)
        for entry,key,default in [(self.intro_entry,'estimate_intro',12),(self.outro_entry,'estimate_outro',8)]:
            entry.delete(0,'end')
            entry.insert(0,str(self.timing.get(key,default) if self.timing else default))
        self.preview.configure(text='')

    def select_editor_row(self, index, preview=True):
        self.suppress_preview_id = None if preview else str(index)
        self.tree.selection_set(str(index))
        self.tree.see(str(index))
        if not preview:
            self.root.after_idle(lambda: setattr(self, 'suppress_preview_id', None))

    def editor_row_selected(self, _=None):
        selected = self.tree.selection()
        if not selected or not self.song:
            return
        index = int(selected[0])
        self.cursor = index
        self.time_entry.delete(0, 'end')
        stamp = self.timing['times'][index]
        if stamp is not None:
            self.time_entry.insert(0, format_time(stamp))
        reading = self.display_lyric_reading(index)
        korean=self.display_lyric_translation(index, original_visible=False)
        self.preview.configure(text=f'{index+1}/{len(self.song.lines)}  {reading}\n{korean}')
        # Selecting a row previews it; the automatic button resumes playback following.
        if selected[0] != self.suppress_preview_id:
            self.manual_index = index
            self.follow.set(False)
        self.render()

    def pick_song(self, _=None):
        index = self.song_picker.current()
        if index >= 0:
            self.safe(lambda: self.select_song(self.picker_songs[index]))
            self.select_editor_row(0)

    def save_selected_time(self):
        if not self.song or not self.tree.selection():
            return
        index = int(self.tree.selection()[0])
        updated=edit_manual_time(self.timing,index,parse_time(self.time_entry.get()))
        updated['source']='직접 편집'
        self.save_current_timing(updated)
        self.timing = updated
        self.editor_refresh()
        self.select_editor_row(index)
        self.notify('구절 시작 시간을 저장했습니다.')

    def restore_selected_auto(self):
        if not self.song or not self.tree.selection():return
        index=int(self.tree.selection()[0]);candidate=None
        if not self.live_lyrics.is_temporary(self.song):
            draft=read_json(self.store.directory/'audio_drafts'/(self.song.id+'.json'),{})
            if draft.get('fingerprint')==self.song.fingerprint:
                self.check_audio_candidate(self.song,draft)
                validate_times(draft.get('times'),len(self.song.lines));candidate=draft['times'][index]
        if candidate is None and self.is_bound():
            candidate=self.native_timing.select(self.song,self.playback.duration)[index]
        updated=restore_auto_time(self.timing,index,candidate)
        self.save_current_timing(updated);self.timing=updated
        self.editor_refresh();self.select_editor_row(index)
        self.notify(f'{index+1}번 수동 수정 해제 · 자동 싱크로 복원했습니다.' if updated['times'][index] is not None else
                    f'{index+1}번 수동 수정 해제 · 자동 추적을 사용합니다. 저장된 자동 시각이 없거나 인접 구절과 충돌합니다.')

    def bind_current(self):
        if self.live_lyrics.is_temporary(self.song):
            self.notify('현재 곡에 메모리로 연결되어 있습니다.');return
        if not self.song or not self.playback.title:
            self.notify('Spotify 곡을 재생한 뒤 연결할 가사를 선택하세요.')
            return
        self.settings.setdefault('links', {})[self.playback.key] = self.song.id
        self.save_settings()
        self.notify('현재 Spotify 곡과 선택한 가사를 연결했습니다.')

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.editor, title='3줄 가사 TXT 폴더 선택')
        if not folder:
            return
        imported = import_folder(folder)
        combined = {s.id:s for s in self.songs}
        combined.update({s.id:s for s in imported})
        songs = list(combined.values())
        self.store.save_library(songs)
        self.songs = songs
        self.settings['source_folder'] = folder
        self.save_settings()
        snapshot = self.playback
        self.playback = Playback()
        self.set_playback(snapshot)
        self.editor_refresh()
        self.notify(f'{len(imported)}곡 가져오기 완료 · 기존 시간표는 가사 내용이 같으면 유지됩니다.')

    def choose_lrc(self):
        if not self.song:
            return
        path = filedialog.askopenfilename(parent=self.editor, title='현재 곡의 일본어 LRC 선택',
                                         filetypes=[('LRC 가사', '*.lrc'), ('텍스트', '*.txt')])
        if not path:
            return
        times = align_lines(self.song.lines, parse_lrc(Path(path).read_text(encoding='utf-8-sig')))
        matched = sum(t is not None for t in times)
        if not matched:
            raise ValueError('원문과 일치하는 구절이 없습니다. 일본어 LRC인지 확인하세요.')
        # Preview the concrete replacement before overwriting manual timings.
        if not messagebox.askyesno('LRC 시간표 적용', f'{matched}/{len(times)}구절이 일치합니다.\n'
                                   '현재 곡의 시간표를 이 결과로 바꿀까요?', parent=self.editor):
            return
        updated = {'times':times, 'offset':self.timing['offset'], 'source':'LRC 가져오기'}
        self.save_current_timing(updated)
        self.timing = updated
        self.editor_refresh()
        self.resume_follow()

    def open_appearance(self):
        if self.appearance_dialog:
            self.appearance_dialog.window.lift()
            return
        self.appearance_dialog=AppearanceDialog(self)

    def open_manual_lyrics(self):
        if self.manual_dialog:
            self.manual_dialog.window.lift();return
        if self.locked:self.toggle_lock()
        self.manual_dialog=ManualLyricsDialog(self)

    def open_manual_sync(self, *, search=True):
        if self.manual_sync_dialog:
            self.manual_sync_dialog.window.deiconify()
            self.manual_sync_dialog.window.lift()
            return
        dialog=self.manual_sync_dialog=ManualSyncDialog(self)
        self.hotkeys.developer_input_windows=(self.root,self.backdrop.window)
        dialog.searching=search
        if search:
            if self.locked:self.toggle_lock()
            if self.is_bound() and self.display_lines:
                dialog.load()
            else:
                dialog.advance()
            def focus_overlay():
                if (self.manual_sync_dialog is dialog and self.root.winfo_viewable()
                        and not self.locked and not self.electron_overlay_active):
                    self.root.focus_force()
            self.root.after_idle(focus_overlay)
        self.render()

    def open_developer_mode(self):
        if self.manual_sync_dialog:
            self.open_manual_sync(search=False)
            return
        self.open_manual_sync(search=False)
        self.manual_sync_dialog.follow_current_song = True
        if self.is_bound() and self.display_lines:
            self.manual_sync_dialog.load()

    def edit_developer_lyric_rows(self,index,text=None):
        from lyric_completion import expand_timing
        from lyric_edits import save_lyric_edit

        if self.temporary_alsong:
            if text is None:self.remove_temporary_alsong_line(index)
            else:self.insert_temporary_alsong_line(index,text)
            return
        song=self.song
        if not self.is_bound() or not 0<=index<len(song.lines):
            raise ValueError('수정할 가사 줄을 선택하세요.')
        indices=list(range(len(song.lines)))
        if text is None:
            if len(indices)<=1:raise ValueError('가사는 한 줄 이상 남겨야 합니다.')
            indices.pop(index)
        else:
            text=text.strip()
            if not text or any(char in text for char in '\r\n\0'):
                raise ValueError('한 줄의 가사를 입력하세요.')
            indices.insert(index,None)
        lines=tuple(self.display_lines[i] if i is not None else Line(text,'','') for i in indices)
        incoming=replace(song,lines=lines)
        target=next((saved for saved in self.songs if saved.id==song.id),None) or song
        # Keep actual provider onsets and AI candidates in their original row order.
        timing=self.timing
        candidates,origins=self.manual_sync_baseline()
        if any(origin=='audio' for origin in origins) or timing.get('source')=='LRCLIB':
            timing={**timing,'times':candidates,'origins':origins}
        if timing.get('source')=='LRCLIB':
            timing={**timing,'source':'직접 편집 / 공개 싱크'}
        timing=expand_timing(timing,indices)
        if text is not None:
            timing['cleared_rows']=sorted({index,*timing.get('cleared_rows',())})
        key=(self.playback.key,tuple(line.original for line in lines))
        annotations={}
        for edits in (self.singer_edits,self.lyric_text_edits):
            if edits.error:raise edits.error
        rows={new:self.singer_edits.rows[old] for new,old in enumerate(indices) if old in self.singer_edits.rows}
        cleared={new for new,old in enumerate(indices) if old in self.singer_edits.cleared}
        # If a deleted row supplied the carried singer, reestablish it on the next surviving row.
        carry=None
        for new,old in enumerate(indices):
            if old is None:continue
            carry=rows.get(new,carry)
            prior=self.singer_edits.overrides[old] if old<len(self.singer_edits.overrides) else None
            if new not in cleared and prior and prior!=carry:
                rows[new]=carry=prior
        annotations[self.singer_edits.path_for(key)]=self.singer_edits.document(rows,cleared)
        annotations[self.lyric_text_edits.path_for(key)]={new:self.lyric_text_edits.rows[old]
                                                          for new,old in enumerate(indices) if old in self.lyric_text_edits.rows}
        source=self.settings.get('lyric_sources',{}).get(song.id)
        songs,settings,saved,_=save_lyric_edit(self.store,self.songs,self.settings,target,incoming,source,
                                              self.playback.key,row_timing=timing,row_annotations=annotations)
        self.songs,self.settings=songs,settings
        self.reset_edited_lyrics(song)
        self.select_song(saved)
        self.reader_bridge.set_reference(self.playback.key,[line.original for line in lines],complete=True)

    def link_temporary_alsong(self, result, *, confirmed=False):
        playback=self.playback
        if not playback.has_timeline or (self.pending_track_change and not confirmed):
            raise ValueError('재생 곡이 바뀌는 중입니다. 이동이 끝나면 다시 연결하세요.')
        lines=result['lines']
        if not lines:
            raise ValueError('연결할 알송 가사가 비어 있습니다.')
        validate_times(result['times'],len(lines))
        identifier=hashlib.sha256(playback.key.encode()).hexdigest()[:24]
        song=Song(f'alsong-{identifier}-{result["provider_id"]}',playback.title,playback.artist,playback.album,lines)
        saved=self.live_lyrics.timing_store.load(song)
        base=saved or {'times':[None]*len(lines),'offset':0.,'source':'알송 임시 연결'}
        removed=set(base.get('removed_alsong_rows', ()))
        added=base.get('added_alsong_rows',())
        order=list(range(len(lines)))
        for index,entry in enumerate(added,len(lines)):
            order.insert(order.index(entry['before']),index)
        rows=tuple(index for index in order if index not in removed)
        from lyric_completion import expand_timing
        timing=expand_timing(base,[index if index<len(lines) else None for index in rows])
        for visible,index in enumerate(rows):
            if index>=len(lines):
                entry=added[index-len(lines)]
                timing['times'][visible]=entry['time']
                timing['origins'][visible]='manual' if entry['time'] is not None else None
                if entry['cleared']:timing.setdefault('cleared_rows',[]).append(visible)
        timing=merge_candidates(timing,[result['times'][index] if index<len(lines) else None for index in rows])[0]
        previous=(self.song, copy(self.live_lyrics), self.direct_result, self.current_lyric_merge,
                  self.cache_tracking, self.cache_reuse, self.screen_song, self.screen_cache_tainted)
        if self.temporary_alsong:
            previous=self.temporary_alsong.previous
        self.stop_screen_lyrics()
        self.direct_result=self.current_lyric_merge=self.cache_tracking=self.cache_reuse=None
        additions=tuple((entry['before'],entry['text']) for entry in added)
        self.temporary_alsong=TemporaryAlsong(playback.duration,previous,song,rows,tuple(result['times']),confirmed,additions)
        self.singer_input_changed_row=None
        self.show_temporary_alsong(timing)

    def confirmed_alsong_path(self):
        identifier=hashlib.sha256(self.playback.key.encode()).hexdigest()
        return self.store.directory/'confirmed_alsong'/f'{identifier}.json'

    def confirm_alsong(self):
        link=self.temporary_alsong
        if not link or not self.is_bound() or self.pending_track_change:
            raise ValueError('현재 곡에 알송 가사를 먼저 연결하세요.')
        atomic_json(self.confirmed_alsong_path(),{
            'duration':link.duration, 'provider_id':link.source.id.rsplit('-',1)[-1],
            'lines':link.source.to_dict()['lines'], 'times':link.provider_times})
        link.confirmed=True

    def restore_confirmed_alsong(self):
        from recording_context import known_duration

        value=read_json(self.confirmed_alsong_path(),None)
        if value is None:return
        if not isinstance(value,dict):
            raise ValueError('저장 형식이 올바르지 않습니다.')
        duration=known_duration(value.get('duration'))
        if duration is None:
            raise ValueError('저장된 곡 길이가 올바르지 않습니다.')
        if abs(self.playback.duration-duration)>2:return
        try:
            identifier=value['provider_id']
            if not isinstance(identifier,str) or not identifier.isascii() or not identifier.isdecimal():
                raise ValueError('알송 등록 번호가 올바르지 않습니다.')
            lines=tuple(Line(**row) for row in value['lines'])
            if any(not all(isinstance(part,str) for part in (line.original,line.reading,line.korean)) for line in lines):
                raise ValueError('가사 형식이 올바르지 않습니다.')
            self.link_temporary_alsong({**value,'lines':lines},confirmed=True)
        except (KeyError,TypeError) as error:
            raise ValueError('저장 형식이 올바르지 않습니다.') from error

    def show_temporary_alsong(self,timing):
        link=self.temporary_alsong
        all_lines=link.lines
        lines=tuple(all_lines[index] for index in link.rows)
        song=replace(link.source,lines=lines)
        self.live_lyrics.clear()
        self.live_lyrics.track,self.live_lyrics.song,self.live_lyrics.lines,self.live_lyrics.timing=self.playback.key,song,lines,timing
        self.queued_lyric_seek=None
        self.time_cache=None
        self.select_song(song)
        self.reader_bridge.set_reference(self.playback.key,[line.original for line in lines],complete=True)

    def save_alsong_timing(self,value,rows,*,additions=None):
        from lyric_completion import expand_timing
        link=self.temporary_alsong
        source=link.source
        additions=link.additions if additions is None else additions
        positions={original:visible for visible,original in enumerate(rows)}
        indices=[positions.get(index) for index in range(len(source.lines)+len(additions))]
        timing=expand_timing(value,indices)
        times=[stamp if origin=='manual' else None for stamp,origin in zip(timing['times'],timing['origins'])]
        count=len(source.lines)
        cleared=set(timing.get('cleared_rows',()))
        saved={'times':times[:count],'offset':timing['offset'],'source':timing['source'],
               'origins':['manual' if stamp is not None else None for stamp in times[:count]],
               'cleared_rows':sorted(index for index in cleared if index<count),
               'removed_alsong_rows':[index for index in range(len(indices)) if index not in positions],
               'added_alsong_rows':[{'before':before,'text':text,'time':times[index],'cleared':index in cleared}
                                    for index,(before,text) in enumerate(additions,count)]}
        self.live_lyrics.timing_store.save(source,saved)

    def insert_temporary_alsong_line(self,index,text):
        from lyric_completion import expand_timing
        link=self.temporary_alsong
        if not link or not 0<=index<len(link.rows):
            raise ValueError('가사를 추가할 위치를 선택하세요.')
        text=text.strip()
        if not text or any(char in text for char in '\r\n\0'):
            raise ValueError('한 줄의 가사를 입력하세요.')
        new_index=len(link.source.lines)+len(link.additions)
        additions=(*link.additions,(link.rows[index],text))
        rows=(*link.rows[:index],new_index,*link.rows[index:])
        previous=list(range(len(link.rows)))
        previous.insert(index,None)
        timing=expand_timing(self.timing,previous)
        self.save_alsong_timing(timing,rows,additions=additions)
        link.rows,link.additions=rows,additions
        self.singer_input_changed_row=None
        self.show_temporary_alsong(timing)

    def remove_temporary_alsong_line(self,index):
        from lyric_completion import expand_timing
        link=self.temporary_alsong
        if not link or not 0<=index<len(link.rows) or len(link.rows)<=1:
            raise ValueError('가사는 한 줄 이상 남겨야 합니다.')
        rows=link.rows[:index]+link.rows[index+1:]
        retained=[i for i in range(len(link.rows)) if i!=index]
        timing=expand_timing(self.timing,retained)
        self.save_alsong_timing(timing,rows)
        link.rows=rows
        self.singer_input_changed_row=None
        self.show_temporary_alsong(timing)

    def unlink_temporary_alsong(self):
        if not self.temporary_alsong:return
        previous=self.temporary_alsong.previous
        self.temporary_alsong=None
        (song,self.live_lyrics,self.direct_result,self.current_lyric_merge,self.cache_tracking,self.cache_reuse,
         self.screen_song,self.screen_cache_tainted)=previous
        self.time_cache=None
        self.select_song(song)
        if song:self.reader_bridge.set_reference(self.playback.key,[line.original for line in song.lines],complete=True)
        else:self.reader_bridge.clear_reference(self.playback.key)
        if self.lyric_merge_job.sources:self.queue_lyric_merge()

    def clear_confirmed_alsong(self):
        link=self.temporary_alsong
        if not link or not link.confirmed or not self.is_bound():
            return
        self.confirmed_alsong_path().unlink(missing_ok=True)
        self.unlink_temporary_alsong()

    def manual_sync_baseline(self):
        from local_analysis import playlist_auto_times
        from recording_context import recording_key
        from sync_reanalysis import manual_review_candidate
        from sqlite3 import Error

        song, playback = self.song, self.playback
        if not song or not self.is_bound():
            return [], []
        if self.temporary_alsong:
            return self.timing['times'], timing_origins(self.timing)
        job = self.current_local_analysis(verified_playlist=True)
        previous = playlist_auto_times(job) if job else []
        identity, track_id = getattr(self, 'local_playlist_track', (None, None))
        if identity != (playback.key, playback.duration):
            record = self.playlist_lyrics.store.find(playback)
            track_id = record['track']['id'] if record else None
        try:
            candidate = manual_review_candidate(self.store.directory, song, track_id or '',
                recording_key(playback.title, playback.artist, playback.album), playback.duration)
        except (Error, OSError, ValueError):
            candidate = []
        offset = self.timing['offset']
        origins = timing_origins(self.timing)
        times = [stamp if origin == 'manual' and self.timing.get('source') != 'LRCLIB' else None
                 for stamp, origin in zip(self.timing['times'], origins)]
        base = {'times': times, 'offset': offset, 'origins': ['manual' if t is not None else None for t in times],
                'cleared_rows': self.timing.get('cleared_rows')}
        direct = self.direct_times()
        if len(direct) == len(times):
            base = merge_candidates(base,[stamp+offset if stamp is not None else None for stamp in direct])[0]
        temporary = self.live_lyrics.is_temporary(song)
        try:
            draft = read_json(self.auto_analyzer.draft_path(song, temporary), {})
        except (OSError, ValueError):
            draft = {}
        draft_times = []
        if (isinstance(draft, dict) and draft.get('fingerprint') == self.auto_analyzer.signature(song, temporary)
                and self.recording_contexts.candidate_matches(song,draft,playback)):
            try:
                validate_times(draft.get('times'), len(times))
            except ValueError:
                pass
            else:
                draft_times = draft['times']
                details = draft.get('line_alignment') or []
                if len(details) == len(draft_times):
                    draft_times = [t if row.get('quality') != 'low' else None for t,row in zip(draft_times,details)]
        saved = [stamp + offset if stamp is not None and origin == 'audio' else None
                 for stamp, origin in zip(self.timing['times'], origins)]
        public = self.timing['times'] if self.timing.get('source')=='LRCLIB' else ()
        for source in (candidate, previous, saved, draft_times, public):
            if len(source) != len(times):
                continue
            values = [stamp if stamp is not None and 0 <= stamp < playback.duration else None for stamp in source]
            base = merge_candidates(base,values)[0]
        return base['times'], timing_origins(base)

    def open_batch_translation(self):
        if self.batch_translation_error:raise ValueError(self.batch_translation_error)
        if not self.translation_dialog:
            if self.locked:self.toggle_lock()
            from batch_translation_dialog import BatchTranslationDialog
            self.translation_dialog=BatchTranslationDialog(self)
        self.raise_translation_window(self.translation_dialog.window)

    def open_translation_resources(self):
        if self.translation_resource_dialog:
            self.translation_resource_dialog.refresh()
        else:
            if self.locked:self.toggle_lock()
            from translation_resource_dialog import TranslationResourceDialog
            self.translation_resource_dialog=TranslationResourceDialog(self)
        self.raise_translation_window(self.translation_resource_dialog.window)

    @staticmethod
    def raise_translation_window(window):
        window.deiconify()
        # Developer mode runs in Electron, so a new Tk window can initially open behind it.
        def activate():
            window.attributes('-topmost', True)
            window.lift()
            window.focus_force()
            window.after(175, lambda: window.attributes('-topmost', False) if window.winfo_exists() else None)
        window.after_idle(activate)

    def translation_resource_details(self,ident):
        return self.personal_resources.details(ident)

    def list_translation_resources(self):
        return self.personal_resources.refresh()

    def set_translation_resource_included(self,ident,included):
        self.spotify_translation_sources.set_included(self.personal_resources.spotify_track(ident),included)
        if self.translation_dialog:self.translation_dialog.refresh_requests()

    def translation_resource_actions(self,ident):
        return self.personal_resources.actions(ident)

    def update_translation_resource(self,ident,index,field,value):
        item,timing=self.personal_resources.update(ident,index,field,value,parse_time)
        if self.playback.key==item.key:
            self.lyric_text_edits.select(None)
            self.singer_edits.select(None)
            self.singer_roster.key=None
            if timing is not None and self.song and self.song.id==item.clock[1].id:
                if self.temporary_alsong:
                    self.reload_managed_alsong_timing()
                else:
                    self.timing=timing
                    if self.live_lyrics.is_temporary(self.song):self.live_lyrics.timing=timing
                    self.time_cache=None
            self.last_render=None
            self.render()
            if self.manual_sync_dialog and self.manual_sync_dialog.originals:
                self.manual_sync_dialog.update_times()
            if self.editor:self.editor_refresh()
        if field=='korean' and self.translation_dialog:self.translation_dialog.refresh_requests()

    def reload_managed_alsong_timing(self):
        link=self.temporary_alsong
        if not link:return
        provider_id=link.source.id.rsplit('-',1)[-1]
        self.link_temporary_alsong(dict(provider_id=provider_id,lines=link.source.lines,times=link.provider_times),
                                   confirmed=link.confirmed)

    def fill_batch_annotations(self,title,artist,album,lines,*,preserve_supplied=False):
        if self.batch_translation_error:return tuple(lines)
        try:return self.batch_translations.fill(title,artist,album,lines,preserve_supplied=preserve_supplied)
        except (OSError,ValueError) as error:
            self.batch_translation_error=str(error)
            return tuple(lines)

    def capture_translation_lyrics(self):
        if self.batch_translation_error:return
        try:self.batch_translations.capture(self.playback.title,self.playback.artist,self.playback.album,self.display_lines)
        except (OSError,ValueError) as error:
            self.batch_translation_error=str(error)
            self.notify('AI 번역용 원문 보관 실패: '+str(error),12)

    def collect_translation_documents(self, *, spotify_only=False):
        """Keep all available originals for repeatable, offline AI requests."""
        if self.batch_translation_error:raise ValueError(self.batch_translation_error)
        archive=self.batch_translations
        imported={track['id']:track['included'] for track in self.spotify_translation_sources.records()}
        tracks=(tuple(track_id for track_id,included in imported.items() if included) if spotify_only
                else (track['id'] for track in self.playlist_lyrics.store.metadata()))
        selected=set()
        excluded=set()
        for track_id in tracks:
            saved=self.playlist_lyrics.store.load(track_id)
            if saved:
                stored=saved['track']
                doc_id=archive.capture(stored['title'],stored['artists'][0],stored['album'],saved['lines'])
                if track_id in imported:
                    (selected if imported[track_id] else excluded).add(doc_id)
        if spotify_only:
            return archive.documents(selected)
        for playback,cached in self.lyric_cache.entries():
            archive.capture(playback.title,playback.artist,playback.album,cached['lines'])
        for song in self.songs:
            if song.lines:
                archive.capture(song.title,song.artist,song.album,self.lyric_annotations.display_lines(song))
        return archive.documents(excluded=excluded-selected)

    def refresh_batch_translations(self):
        if not self.song or self.screen_cache_tainted or self.song is self.screen_song:return
        lines=self.display_lines
        preserve_supplied=self.user_lyric_override()
        if not self.live_lyrics.is_temporary(self.song):
            lines=self.fill_batch_annotations(self.song.title,self.song.artist,self.song.album,lines,
                                              preserve_supplied=preserve_supplied)
        if self.is_bound():
            lines=self.fill_batch_annotations(self.playback.title,self.playback.artist,self.playback.album,lines,
                                              preserve_supplied=preserve_supplied)
            if self.batch_translation_source and self.batch_translation_source[0]==self.playback.key:
                lines=self.fill_batch_annotations(*self.batch_translation_source[1],lines,preserve_supplied=preserve_supplied)
            if self.cache_reuse:
                lines=self.fill_batch_annotations(*self.cache_reuse['source_key'].split('\0'),lines,
                                                  preserve_supplied=preserve_supplied)
            if any(not line.korean.strip() for line in lines):
                translations=self.playlist_lyrics.store.find_annotations(
                    self.playback.title,self.playback.album,tuple(line.original for line in lines))
                if translations:
                    lines=tuple(Line(line.original,line.reading,line.korean if line.korean.strip() else korean)
                                for line,korean in zip(lines,translations))
        if lines!=self.display_lines:
            self.display_lines=lines
            if self.live_lyrics.song and self.live_lyrics.song.id==self.song.id:self.live_lyrics.lines=lines
            self.translated_count=sum(bool(line.korean.strip()) for line in lines)
            self.last_render=None

    def apply_translation_reply(self,text):
        if self.batch_translation_error:raise ValueError(self.batch_translation_error)
        result=self.batch_translations.apply_reply(text)
        self.last_render=None;self.render()
        if self.editor:self.editor_refresh()
        if self.translation_resource_dialog:self.translation_resource_dialog.refresh()
        if result['lines']:
            notice=f"AI 번역 신규 저장 · {result['songs']}곡 {result['lines']}구절"
        elif result.get('changed_latin_lines'):
            notice='AI 번역에서 바뀐 영어 표현을 확인해 주세요.'
        else:
            notice=('기존 번역 유지' if result['preserved_lines'] else '이미 반영된 답변')
            notice+=f" · {result['received_songs']}곡 {result['received_lines']}구절 확인"
        self.notify(notice,10)
        return result

    def lyrics_deleted(self,key=None):
        return (self.playback.key if key is None else key) in self.settings.get('deleted_lyrics',[])

    def user_lyric_override(self,song=None):
        song=self.song if song is None else song
        return bool(song and self.settings.get('lyric_sources',{}).get(song.id,{}).get('user_override'))

    def lyric_edit_target(self,song,playback_key=None):
        linked=self.settings.get('links',{}).get(playback_key)
        if linked:
            target=next((old for old in self.songs if old.id==linked),None)
            if target:return target
        candidates=[old for old in self.songs if old.id==song.id or
                    tuple(normalized(v) for v in (old.title,old.artist,old.album))==
                    tuple(normalized(v) for v in (song.title,song.artist,song.album))]
        if len(candidates)>1:raise ValueError('같은 곡이 여러 개입니다. 라이브러리에서 대상 곡을 현재 곡에 연결한 뒤 저장하세요.')
        return candidates[0] if candidates else None

    def reset_edited_lyrics(self,target=None):
        if target:
            self.query_running.pop(target.id,None)
            for job in list(self.auto_analyzer.jobs)+([self.audio_job] if self.audio_job else []):
                if ((job.song and job.song.id==target.id) or
                        (not job.song and getattr(job,'analysis_key',(None,))[0]==self.playback.key)):
                    job.cancelled.set()
                    threading.Thread(target=job.cancel,daemon=True,name='edited-lyrics-cancel').start()
        self.live_lyrics.clear();self.clear_realtime()
        self.direct_result=None;self.direct_map_cache=None;self.cache_tracking=None;self.cache_reuse=None
        self.lyric_merge_job.reset();self.current_lyric_merge=None
        self.cache_hit=False;self.screen_song=None;self.screen_cache_tainted=False
        self.user_annotation_signature=None;self.time_cache=None
        self.reader_bridge.clear_reference(self.playback.key)
        if self.internal_reader.job:
            self.internal_reader.job.cancel();self.internal_reader.job=None
        self.internal_reader.last_key=''

    def save_user_lyrics(self,song,source,playback_key=None,parent=None):
        from lyric_edits import save_lyric_edit
        target=self.lyric_edit_target(song,playback_key)
        current=(self.song if playback_key==self.playback.key and self.is_bound() else None)
        previous=target or current
        if previous and not messagebox.askyesno('가사 덮어쓰기 주의',
                f'「{previous.title}」의 기존 가사 {len(previous.lines)}구절을 새 가사 {len(song.lines)}구절로 덮어쓸까요?\n\n'
                '원문·독음·번역이 바뀝니다. 원문이나 줄 구성이 달라지면 기존 싱크를 초기화합니다.\n'
                '기존 저장 내용은 백업하며, 이후 자동 수신 가사는 이 내용을 바꾸지 않습니다.',
                parent=parent or self.editor or self.root,icon='warning',default='no'):
            return False
        if target is None and current and self.live_lyrics.is_temporary(current):target=current
        songs,settings,saved,backup=save_lyric_edit(self.store,self.songs,self.settings,target,song,source,playback_key)
        self.songs,self.settings=songs,settings
        selected=bool(previous and self.song and previous.id==self.song.id)
        if selected or playback_key==self.playback.key:
            self.reset_edited_lyrics(previous);self.select_song(saved)
        else:
            linked=self.settings.get('links',{}).get(self.playback.key)
            matched=match_recording(self.songs,self.playback.title,self.playback.artist,self.playback.album,linked)
            if matched and matched.id==saved.id:
                self.reset_edited_lyrics(previous);self.select_song(saved)
            elif self.editor:self.editor_refresh()
        if self.is_bound():
            self.reader_bridge.set_reference(self.playback.key,[line.original for line in self.song.lines],complete=True)
            self.cache_current_lyrics();self.observe_auto_audio()
        self.notify(f'{saved.title} · {len(saved.lines)}구절 저장'+(' · 기존 가사 백업 완료' if backup else ''))
        return True

    def delete_selected_lyrics(self):
        from lyric_edits import save_lyric_edit
        target=self.song
        if not target:return False
        completed=self.live_lyrics.completion_base
        if completed and self.live_lyrics.is_temporary(target):
            target=next((song for song in self.songs if song.id==completed),target)
        key=self.playback.key if self.is_bound() else None
        if not messagebox.askyesno('가사 삭제 주의',
                f'「{target.title}」의 저장 가사와 싱크를 삭제할까요?\n\n'
                '기존 저장 내용은 백업합니다. 이 곡은 가사를 다시 저장할 때까지 자동으로 불러오지 않습니다.',
                parent=self.editor or self.root,icon='warning',default='no'):
            return False
        songs,settings,_,backup=save_lyric_edit(self.store,self.songs,self.settings,target,playback_key=key)
        self.songs,self.settings=songs,settings
        self.reset_edited_lyrics(target);self.select_song(None);self.observe_auto_audio()
        self.notify(f'{target.title} · 가사 삭제 및 백업 완료 · 직접 입력 또는 위키 가져오기로 다시 저장하세요.',12)
        return True

    def add_manual_song(self,song,playback_key=None,parent=None):
        return self.save_user_lyrics(song,{'source':'직접 입력'},playback_key,parent)

    def open_web_lyrics(self):
        if self.web_dialog:
            self.web_dialog.window.lift()
            return
        if self.locked:self.toggle_lock()
        self.web_dialog=WebLyricsDialog(self)

    def choose_extension_inbox(self):
        folder=filedialog.askdirectory(title='확장이 저장하는 SpotifyLyricsOverlay 폴더',initialdir=self.extension_inbox.directory)
        if folder:
            self.extension_inbox.directory=Path(folder)
            self.settings['extension_inbox']=folder
            self.save_settings()
            self.notify('확장 수신 폴더를 변경했습니다.')

    def receive_extension_lyrics(self,key,result,error):
        if error:
            self.notify(error,12)
        else:
            title,lines,source=result
            candidates=[s for s in self.songs if normalized(s.title)==normalized(title)]
            if candidates:
                linked=self.settings.get('links',{}).get(self.playback.key)
                matched=match_song(candidates,title,self.playback.album,linked) if normalized(title)==normalized(self.playback.title) else (candidates[0] if len(candidates)==1 else None)
                if matched is None:raise ValueError('같은 제목의 곡이 여러 개여서 번역 연결을 보류했습니다.')
                if self.user_lyric_override(matched):
                    self.extension_inbox.acknowledge(key)
                    return
                count=self.lyric_annotations.save(matched,lines,source)
                if self.song and self.song.id==matched.id:
                    self.display_lines=self.lyric_annotations.display_lines(matched)
                    self.last_render=None
                    self.render()
                self.notify(f'{title} · 일본어 {count}구절의 번역·독음 저장 · 기존 가사와 싱크 유지')
            else:
                current=normalized(title)==normalized(self.playback.title)
                self.add_web_song(make_song(title,self.playback.artist if current else '',self.playback.album if current else '',lines),source)
        self.extension_inbox.acknowledge(key)

    def fetch_web_lyrics(self,title,artist,album,url='',dialog=None,playback=None,*,imas=False):
        generation=self.lyric_merge_job.generation if playback is not None else None
        key=('imas' if imas else 'wiki',title,url,id(dialog) if dialog else None,generation)
        if key in self.web_running:return
        self.web_running.add(key)
        references=tuple(line.original for line in self.display_lines) if playback is not None else ()
        def worker():
            try:
                if imas:
                    candidate=imas_lyrics.find_candidate(title,imas_lyrics.series_for(album,artist))
                    if not candidate:
                        raise ValueError('아이마스 전용 출처에서 일치하는 원문을 찾지 못했습니다.')
                    lines=candidate['lines']
                    source={'url':candidate['url'],'title':title,'source':candidate['source']}
                else:
                    options={'reference_lines':references,'merge_partial':True} if playback is not None else {}
                    lines,source=fetch_lyrics(title,url,**options)
                error=''
            except Exception as exception:
                lines,source=None,None
                error=str(exception) if isinstance(exception,ValueError) else f'가사 문서 연결 실패 ({type(exception).__name__})'
            self.messages.put(('web_lyrics',(key,title,artist,album,lines,source,error,dialog,playback,generation)))
        threading.Thread(target=worker,daemon=True,name='wiki-lyrics').start()

    def receive_web_lyrics(self,key,title,artist,album,lines,source,error,dialog,playback=None,generation=None):
        self.web_running.discard(key)
        if dialog is not None:
            if dialog is self.web_dialog:dialog.receive(lines,source,error)
            return
        if playback is not None and (generation!=self.lyric_merge_job.generation or
                playback.key!=self.playback.key or abs(playback.duration-self.playback.duration)>.5
                or self.lyrics_deleted()):return
        if error:
            if key[0]=='imas':return
            self.notify(error,12)
            if '접근을 제한' in error:
                self.web_auto.set(False)
                self.save_settings()
            return
        if playback is not None:
            url=source['url']
            name=source.get('source')
            if name and self.display_lines:return
            if self.lyric_merge_job.remember(make_snapshot(name or source_id(url),lines,provider_id=url,title=source['title'])):
                self.queue_lyric_merge()
            return
        self.add_web_song(make_song(title,artist,album,lines),source)

    def add_web_song(self,song,source,*,interactive=False,playback_key=None,parent=None):
        if interactive:return self.save_user_lyrics(song,source,playback_key,parent)
        if self.lyrics_deleted('\0'.join((song.title,song.artist,song.album))):return False
        if any(s.id==song.id or (s.title==song.title and s.album==song.album) for s in self.songs):
            raise ValueError('이미 등록된 곡입니다. 기존 가사와 직접 기록을 보존하기 위해 덮어쓰지 않습니다.')
        updated=self.songs+[song]
        self.store.save_library(updated)
        self.songs=updated
        self.settings.setdefault('lyric_sources',{})[song.id]=source or {}
        self.save_settings()
        linked=self.settings.get('links',{}).get(self.playback.key)
        matched=match_recording(self.songs,self.playback.title,self.playback.artist,self.playback.album,linked)
        if matched:self.select_song(matched);self.observe_auto_audio()
        elif self.editor:self.editor_refresh()
        self.notify(f'{song.title} · 번역과 자동 독음 {len(song.lines)}구절을 저장했습니다.')

    def toggle_auto_audio(self):
        try:self.auto_analyzer.set_enabled(self.auto_audio.get())
        except ValueError:
            self.auto_audio.set(False)
            self.save_settings()
            self.open_audio_install()
            raise
        self.save_settings()
        self.observe_auto_audio()
        self.audio_status.set(self.auto_analyzer.status)
        self.last_render=None;self.render()

    def set_realtime_tracking(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('재생 중 가사 추적 상태를 확인하세요.')
        previous=self.auto_analyzer.realtime_enabled
        self.realtime_tracking.set(enabled)
        try:self.save_settings()
        except OSError:
            self.realtime_tracking.set(previous)
            self.settings['realtime_tracking']=previous
            raise
        if enabled == previous:return
        self.auto_analyzer.realtime_enabled=enabled
        self.realtime_tracking_since=time.monotonic()
        self.clear_realtime()
        jobs=tuple(self.auto_analyzer.jobs)
        if self.audio_job:jobs+=(self.audio_job,)
        for job in jobs:job.set_realtime_enabled(enabled)
        self.last_render=None
        self.render()

    def receive_auto_audio(self,job,event):
        if event.get('kind') in ('realtime','realtime_status'):
            if job in self.auto_analyzer.jobs and self.auto_analyzer.enabled:
                self.receive_realtime(job,event)
            return
        if job is self.realtime_job and (event.get('kind') in ('error','finished','result') or
                event.get('kind')=='progress' and event.get('state') in ('queued','analyzing')):self.clear_realtime()
        songs=self.songs+([self.live_lyrics.song] if self.live_lyrics.is_temporary(self.live_lyrics.song) else [])
        owned=job in self.auto_analyzer.jobs
        song = self._receive_analysis_job(job, event, songs)
        if song and self.song and self.song.id==song.id:
            temporary=self.live_lyrics.is_temporary(song)
            self.timing=self.auto_analyzer.timing(song,temporary)
            if temporary:self.live_lyrics.timing=self.timing
            self.render()
            if self.editor:self.editor_refresh()
        if not self.audio_job:self.audio_status.set(self.auto_analyzer.status)
        self.refresh_audio_progress()
        if owned and event.get('kind')=='finished' and event.get('cleanup_errors'):
            self.notify_audio_cleanup(event)

    def _receive_analysis_job(self, job, event, songs):
        collector = self.missing_audio_collector
        if (getattr(job, 'playlist_analysis', False) is True and event.get('kind') == 'result'
                and (not collector or not collector.accepts_result(job))):
            event = {'kind': 'error', 'text': 'Spotify에서 재생한 곡이 분석 대상과 달라 결과를 제외했습니다.'}
        try:
            return self.auto_analyzer.receive(job, event, songs)
        finally:
            if collector:
                collector.receive(job, event)

    def notify_audio_cleanup(self,event):
        errors=event.get('cleanup_errors')
        if not isinstance(errors,list):return
        stages={error.get('stage') for error in errors if isinstance(error,dict)
                and error.get('stage') in ('realtime','analysis_lock','stdout','temporary')}
        if not stages:return
        self.notify('임시 음원 파일 정리 실패 · 분석 창에서 폴더 위치를 확인하세요.' if 'temporary' in stages else
                    '분석 자원 정리 경고 · 분석 창에서 상세를 확인하세요.',12)

    def open_updates(self):
        if self.update_dialog:self.update_dialog.window.deiconify()
        else:self.update_dialog=UpdateDialog(self)
        self.update_dialog.window.lift()

    def open_audio_install(self):
        if self.audio_install_dialog:self.audio_install_dialog.window.lift()
        else:self.audio_install_dialog=AudioInstallDialog(self)

    def open_audio(self, *, playlist=False):
        if self.audio_window:
            self.audio_window.deiconify()
            self.audio_window.lift()
            if playlist:
                self.audio_tabs.select(self.audio_playlist_tab)
                self.select_audio_tab()
            self.refresh_audio_progress()
            return
        if self.locked:
            self.toggle_lock()
        if not self.audio_job:
            self.audio_song = self.song
            self.audio_draft = None
            self.audio_draft_readonly = self.live_lyrics.is_temporary(self.audio_song)
            if self.audio_song:
                path=(self.auto_analyzer.draft_path(self.audio_song,True) if self.audio_draft_readonly
                      else self.audio_draft_path(self.audio_song))
                signature=(self.auto_analyzer.signature(self.audio_song,True) if self.audio_draft_readonly
                           else self.audio_song.fingerprint)
                candidate = read_json(path, {})
                if not isinstance(candidate,dict):
                    raise ValueError('저장된 분석 후보 형식을 확인할 수 없습니다. 원본은 보존했습니다.')
                if candidate.get('fingerprint') == signature:
                    validate_times(candidate.get('times'), len(self.audio_song.lines))
                    self.audio_draft = candidate
                    self.audio_status.set('실시간 가사 분석 후보 · 보기 전용' if self.audio_draft_readonly else
                                          '저장된 분석 후보입니다. 빈 구절에 적용하거나 다시 분석할 수 있습니다.')
                else:
                    self.audio_status.set('분석 대기를 누른 뒤 Spotify에서 선택한 곡을 처음부터 재생하세요.')
            if self.auto_analyzer.jobs or self.auto_analyzer.enabled:
                self.audio_status.set(self.auto_analyzer.status)
            elif self.auto_analyzer.last_progress:
                self.audio_status.set(self.auto_analyzer.last_progress.text)
            elif self.audio_manual_progress:
                self.audio_status.set(self.audio_manual_progress.text)
            elif not self.audio_draft:
                self.audio_status.set('진행 중인 분석 없음 · 자동 분석을 켜거나 분석 대기를 시작하세요.')
        window = self.audio_window = tk.Toplevel(self.root)
        window.title('개발자 모드 · 음원 분석')
        window.configure(bg=BG)
        window.geometry('680x560')
        window.minsize(560, 460)
        window.attributes('-topmost', False)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(2, weight=1)
        window.protocol('WM_DELETE_WINDOW', self.close_audio)
        self.audio_help = tk.Label(window, bg=BG, fg=TEXT, justify='left', anchor='w',
            font=('Malgun Gothic', 10), wraplength=620)
        self.audio_help.grid(row=0, column=0, sticky='ew', padx=14, pady=(10,6))
        self.audio_progress = tk.Label(window, textvariable=self.audio_status, bg=BG, fg=ACCENT,
                                        justify='left', anchor='nw', height=2, wraplength=620, font=('Malgun Gothic', 10))
        self.audio_progress.grid(row=1, column=0, sticky='ew', padx=14, pady=(0,6))
        self.audio_tabs=ttk.Notebook(window)
        self.audio_tabs.grid(row=2,column=0,sticky='nsew',padx=14)
        progress_frame=tk.Frame(self.audio_tabs,bg=BG)
        self.audio_tabs.add(progress_frame,text='현재 곡 분석')
        progress_frame.columnconfigure(0,weight=1)
        progress_frame.rowconfigure(0,weight=1)
        self.audio_queue_tree=ttk.Treeview(progress_frame,columns=('title','state','progress','remaining'),show='headings',selectmode='browse',height=4)
        for column,title,width in (('title','곡',185),('state','단계',125),('progress','진행률',60),('remaining','현재 단계 남음',120)):
            self.audio_queue_tree.heading(column,text=title)
            self.audio_queue_tree.column(column,width=width,minwidth=90 if column=='title' else width,stretch=column=='title')
        self.audio_queue_tree.grid(row=0,column=0,sticky='nsew')
        queue_scroll=ttk.Scrollbar(progress_frame,orient='vertical',command=self.audio_queue_tree.yview)
        queue_scroll.grid(row=0,column=1,sticky='ns')
        self.audio_queue_tree.configure(yscrollcommand=queue_scroll.set)
        self.audio_queue_tree.bind('<<TreeviewSelect>>',lambda event:self.refresh_audio_progress())
        self.audio_progress_bar=ttk.Progressbar(progress_frame,mode='determinate',maximum=100)
        self.audio_progress_bar.grid(row=1,column=0,columnspan=2,sticky='ew',pady=(6,4))
        self.audio_progress_detail=tk.Text(progress_frame,height=3,wrap='word',bg=BG,fg=TEXT,
                                           relief='flat',font=('Malgun Gothic',9),state='disabled')
        self.audio_progress_detail.grid(row=2,column=0,sticky='ew',pady=(0,4))
        detail_scroll=ttk.Scrollbar(progress_frame,orient='vertical',command=self.audio_progress_detail.yview)
        detail_scroll.grid(row=2,column=1,sticky='ns')
        self.audio_progress_detail.configure(yscrollcommand=detail_scroll.set)
        frame = tk.Frame(self.audio_tabs, bg=BG)
        self.audio_tabs.add(frame,text='싱크 후보')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.audio_tree = ttk.Treeview(frame, columns=('time','text','state'), displaycolumns=('time','text'), show='headings')
        self.audio_tree.heading('time', text='분석 후보 시간')
        self.audio_tree.heading('text', text='일본어 원문 · 빈 시간은 인식 불확실')
        self.audio_tree.column('time', width=95, minwidth=95, stretch=False)
        self.audio_tree.column('text', width=420, minwidth=150)
        self.audio_tree.heading('state', text='음소 정렬 상태')
        self.audio_tree.column('state', width=110, minwidth=100, stretch=False)
        self.audio_tree.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.audio_tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.audio_tree.configure(yscrollcommand=scrollbar.set)
        self.audio_playlist_tab = ttk.Frame(self.audio_tabs)
        self.audio_tabs.add(self.audio_playlist_tab, text='재생목록 분석')
        tools_frame = ttk.Frame(self.audio_tabs, padding=(16, 10))
        self.audio_tabs.add(tools_frame,text='분석 도구')
        for row,(title,detail,action) in enumerate((
            ('로컬 음원 분석', '파일을 선택하여 현재 곡의 싱크와 가창자를 분석합니다.', self.open_local_analysis),
            ('싱크 일괄 재분석 관리', '기존 분석곡과 새 후보를 비교하고 적용합니다.', self.open_sync_reanalysis),
            ('AI 음원 분석 설치', '분석 모델과 실행 환경을 설치하거나 설치 상태를 확인합니다.', self.open_audio_install),
        )):
            self.button(tools_frame,title,action).grid(row=row,column=0,sticky='ew',padx=(0,12),pady=5)
            ttk.Label(tools_frame,text=detail,wraplength=330).grid(row=row,column=1,sticky='w')
        actions = self.audio_actions = tk.Frame(window, bg=BG)
        controls=self.audio_controls=tk.Frame(window,bg=BG)
        controls.grid(row=3,column=0,sticky='ew',padx=14,pady=(8,0))
        tk.Checkbutton(controls,text='자동 분석 · Spotify 소리만 · 완료 후 싱크 저장',variable=self.auto_audio,bg=BG,fg=TEXT,
                       selectcolor=PANEL,command=lambda:self.safe(self.toggle_auto_audio)).pack(anchor='w')
        tk.Checkbutton(controls,text='미등록 곡은 위키와 아이마스 전용 출처 조회',variable=self.web_auto,bg=BG,fg=TEXT,
                       selectcolor=PANEL,command=lambda:self.safe(self.save_settings)).pack(anchor='w')
        tools=tk.Frame(controls,bg=BG);tools.pack(anchor='w')
        from japanese_alignment import LEGACY, CTC, HYBRID
        engine_labels={LEGACY:'stable-ts',CTC:'RomajiASR · 검토용',HYBRID:'RomajiASR + pyshiro · 검토용'}
        alignment=self.settings.get('audio_alignment')
        alignment=alignment if isinstance(alignment,dict) else {}
        selector=ttk.Combobox(tools,state='readonly',values=tuple(engine_labels.values()),width=26)
        selector.set(engine_labels.get(alignment.get('engine'),engine_labels[LEGACY]))
        selector.pack(side='left',padx=(0,6))
        def change_engine():
            self.settings['audio_alignment']={**alignment,'engine':tuple(engine_labels)[selector.current()]}
            self.auto_analyzer.alignment=self.settings['audio_alignment']
            self.save_settings()
        selector.bind('<<ComboboxSelected>>',lambda event:self.safe(change_engine))
        actions.grid(row=4, column=0, sticky='ew', padx=14, pady=12)
        self.audio_start_button = self.button(actions, '분석 시작', self.start_audio)
        self.audio_start_button.pack(side='left', padx=(0,5))
        self.audio_cancel_button = self.button(actions, '분석 취소', self.cancel_audio)
        self.audio_cancel_button.pack(side='left', padx=5)
        self.audio_apply_button = self.button(actions, '빈 구절에 적용', self.apply_audio)
        self.audio_apply_button.pack(side='right')
        def resize(event):
            if event.widget is window:
                self.audio_help.configure(wraplength=max(300,event.width-30))
                self.audio_progress.configure(wraplength=max(300,event.width-30))
        window.bind('<Configure>', resize)
        self.audio_tabs.bind('<<NotebookTabChanged>>', self.select_audio_tab)
        if playlist:
            self.audio_tabs.select(self.audio_playlist_tab)
        self.select_audio_tab()
        self.refresh_audio()
        self.refresh_audio_progress()

    def select_audio_tab(self, _event=None):
        if not self.audio_window or self.closing:
            return
        if self.audio_tabs.select() == str(self.audio_playlist_tab):
            self.audio_help.configure(text='한 곡을 Spotify에서 끝까지 무음 재생한 뒤 분석합니다. 분석이 끝나면 다음 곡으로 넘어갑니다.\n'
                                           '재분석에 필요한 경우에만 분석 데이터를 보관합니다.')
            for section in (self.audio_progress, self.audio_controls, self.audio_actions):
                section.grid_remove()
            if self.audio_playlist_panel is None:
                from playlist_analysis_panel import PlaylistAnalysisPanel
                self.audio_playlist_panel = PlaylistAnalysisPanel(self.audio_playlist_tab, self)
                self.audio_playlist_panel.pack(fill='both', expand=True)
        else:
            self.audio_help.configure(text='Spotify에서 곡을 처음부터 재생하면 싱크를 분석합니다.\n'
                                           '재분석용 데이터가 있는 곡은 바로 다시 분석할 수 있습니다.')
            for section in (self.audio_progress, self.audio_controls, self.audio_actions):
                section.grid()

    def refresh_audio_progress(self):
        if not self.audio_window:return
        collector = self.missing_audio_collector
        self.audio_start_button.configure(state='disabled' if self.audio_job or (collector and collector.owns_playback) else 'normal')
        if self.audio_playlist_panel:self.audio_playlist_panel.refresh()
        self.audio_progress_updated=time.monotonic()
        entries=[]
        def entry(key,progress):
            return key,dict(progress.snapshot(),title=progress.title,mode=progress.mode,state=progress.phase)
        if self.audio_manual_progress:entries.append(entry('manual',self.audio_manual_progress))
        entries.extend(entry(str(id(job)),progress) for job,progress in self.auto_analyzer.progress.items()
                       if not getattr(job,'obsolete_waiting',False))
        recent=self.auto_analyzer.activity_snapshot()['recent']
        entries.extend(('auto-last' if index==0 else 'auto-recent-'+str(index),record)
                       for index,record in enumerate(recent))
        if not recent and self.auto_analyzer.last_progress:
            entries.append(entry('auto-last',self.auto_analyzer.last_progress))
        tree=self.audio_queue_tree
        wanted={key for key,_ in entries}
        for key in tree.get_children():
            if key not in wanted:tree.delete(key)
        snapshots={}
        for key,snapshot in entries:
            snapshots[key]=snapshot
            values=(snapshot['title'],snapshot['mode']+' · '+snapshot['phase'],snapshot['progress'],snapshot['remaining'])
            if tree.exists(key):
                if tuple(str(v) for v in tree.item(key,'values'))!=values:tree.item(key,values=values)
            else:tree.insert('','end',iid=key,values=values)
        if not tree.selection() and entries:
            current=next((str(id(job)) for job in self.auto_analyzer.progress if job.analysis_key[0]==self.playback.key),None)
            selected=('manual' if self.audio_job else current) or next((key for key,p in entries if p['state'] not in TERMINAL),entries[0][0])
            tree.selection_set(selected)
        selected=tree.selection()
        snapshot=snapshots.get(selected[0]) if selected else None
        detail=snapshot['detail'] if snapshot else '진행 중인 분석이 없습니다.\n수집·대기·전체 분석을 곡별로 표시합니다.'
        self.audio_progress_bar.configure(value=snapshot['percent'] if snapshot and snapshot['percent'] is not None else 0)
        if self.audio_progress_detail.get('1.0','end-1c')!=detail:
            self.audio_progress_detail.configure(state='normal')
            self.audio_progress_detail.delete('1.0','end')
            self.audio_progress_detail.insert('1.0',detail)
            self.audio_progress_detail.configure(state='disabled')

    def audio_draft_path(self, song):
        return self.store.directory / 'audio_drafts' / (song.id+'.json')

    def check_audio_candidate(self,song,candidate):
        linked=self.settings.get('links',{}).get(self.playback.key)
        bound=match_recording(self.songs,self.playback.title,self.playback.artist,self.playback.album,linked)
        playback=self.playback if bound and bound.id==song.id and self.playback.has_timeline else None
        if not self.recording_contexts.candidate_matches(song,candidate,playback):
            raise ValueError('분석 후보의 재생 길이가 현재 녹음과 다릅니다. 원본과 수동 기록은 유지했습니다.')

    def refresh_audio(self):
        if not self.audio_window:
            return
        self.audio_tree.delete(*self.audio_tree.get_children())
        details=(self.audio_draft or {}).get('line_alignment') or []
        self.audio_tree.configure(displaycolumns=('time','text','state') if details else ('time','text'))
        if self.audio_song:
            self.audio_window.title(f'음원 분석 · {self.audio_song.title}')
            times = self.audio_draft['times'] if self.audio_draft else [None]*len(self.audio_song.lines)
            for index, line in enumerate(self.audio_song.lines):
                values=(format_time(times[index]),line.original)
                if index<len(details):
                    row=details[index]
                    status={'candidate':'CTC 후보','confirmed':'두 모델 일치','conflict':'시작점 충돌',
                            'missing':'매칭 실패','retained':'저장된 시각'}.get(row.get('status'),'')
                    if row.get('status')=='candidate' and row.get('engine')=='hybrid_japanese':
                        status='두 모델 일치 · 후보'
                    if row.get('reason')=='reading_unavailable':status='독음 확인 필요'
                    values+=status,
                self.audio_tree.insert('', 'end', iid=str(index), values=values)
        busy = self.audio_job is not None
        self.audio_cancel_button.configure(state='normal' if busy else 'disabled')
        has_times=self.audio_draft and any(value is not None for value in self.audio_draft['times'])
        self.audio_apply_button.configure(state='normal' if has_times and not busy and not self.audio_draft_readonly else 'disabled')
        self.audio_tabs.tab(1,text='실시간 가사 분석 후보 · 보기 전용' if self.audio_draft_readonly else '싱크 후보')

    def start_audio(self):
        if self.live_lyrics.is_temporary(self.song):
            raise ValueError('메모리 전용 가사는 파일을 만드는 음원 분석 대신 추정 싱크와 실시간 보정을 사용합니다.')
        if self.audio_job:
            return
        if self.missing_audio_collector and self.missing_audio_collector.owns_playback:
            raise ValueError('재생목록 분석이 끝난 뒤 현재 곡을 분석하세요.')
        if not self.is_bound():
            raise ValueError('Spotify에서 분석할 곡을 선택하고 현재 곡에 연결된 가사를 선택하세요.')
        if not 10 <= self.playback.duration <= 900:
            raise ValueError('10초~15분 길이의 곡만 분석할 수 있습니다.')
        audio_ready(BASE)
        self.audio_song = self.song
        context=self.recording_contexts.capture(self.song,self.playback)
        capture_cache=self.store.directory/'audio_captures'
        cached_capture=find_capture(capture_cache,self.playback.key,self.playback.duration)
        self.audio_job = AudioJob(BASE, self.song, self.playback, self.messages,realtime_device=self.settings.get('audio_device','auto'),
                                  alignment=self.settings.get('audio_alignment'),capture_cache=capture_cache,cached_capture=cached_capture,
                                  realtime_enabled=self.realtime_tracking.get())
        self.audio_job.recording_context=context
        self.audio_manual_progress = AudioProgress(self.song.title,mode='직접')
        self.audio_draft = None
        self.audio_draft_readonly = False
        self.audio_status.set('보관한 분석용 음원으로 재분석합니다.' if cached_capture else
                              '준비 중 · 이 곡을 Spotify에서 처음부터 재생해 주세요.')
        self.refresh_audio()
        self.refresh_audio_progress()

    def cancel_audio(self):
        if self.audio_job:
            self.audio_job.cancel()
            if self.audio_manual_progress:self.audio_manual_progress.update({'kind':'progress'},cancelled=True)
            self.audio_status.set('분석을 취소하고 임시 음원을 정리하고 있습니다.')
            self.refresh_audio_progress()

    def receive_audio(self, job, event):
        if job is not self.audio_job:
            return
        kind = event.get('kind')
        if self.audio_manual_progress and kind!='result':
            self.audio_manual_progress.update(event,cancelled=job.cancelled.is_set())
            self.refresh_audio_progress()
        if kind in ('realtime','realtime_status'):
            if self.recording_contexts.valid(getattr(job,'recording_context',None),job.song):self.receive_realtime(job,event)
            return
        if job is self.realtime_job and (kind in ('error','finished','result') or
                kind=='progress' and event.get('state') in ('queued','analyzing')):self.clear_realtime()
        if kind == 'finished':
            if job.cancelled.is_set():
                self.audio_status.set('분석 취소됨 · 기존 시간표는 그대로입니다.')
            self.audio_job = None
            self.refresh_audio()
            if event.get('cleanup_errors'):self.notify_audio_cleanup(event)
            return
        if job.cancelled.is_set():
            return
        if kind == 'progress':
            self.audio_status.set(event['text'])
        elif kind == 'error':
            self.audio_status.set(event['text'])
            self.notify(event['text'],12)
        elif kind == 'result':
            latest = next((s for s in self.songs if s.id == job.song.id), None)
            if latest is None or latest.fingerprint != job.song.fingerprint:
                self.audio_status.set('가사 내용이 바뀌어 오래된 분석 결과를 적용하지 않았습니다.')
                if self.audio_manual_progress:self.audio_manual_progress.update({'kind':'progress','state':'discarded','text':self.audio_status.get()})
                return
            context=getattr(job,'recording_context',None)
            if not self.recording_contexts.valid(context,latest):
                self.audio_status.set('재생 길이가 바뀌어 이전 분석 결과를 제외했습니다. 기존 기록은 유지됩니다.')
                if self.audio_manual_progress:self.audio_manual_progress.update({'kind':'progress','state':'discarded','text':self.audio_status.get()})
                return
            result = event['result']
            try:
                validate_times(result.get('times'), len(latest.lines))
                result = {**result, 'fingerprint':latest.fingerprint, 'model':result.get('model','faster-whisper-small'),
                          'duration':context.duration,'recording_key':context.key}
                atomic_json(self.audio_draft_path(latest), result)
            except (OSError,ValueError):
                self.audio_status.set('분석 후보 저장 실패 · 기존 후보와 시간표는 보존했습니다.')
                if self.audio_manual_progress:
                    self.audio_manual_progress.update({'kind':'error','text':self.audio_status.get()})
                    self.refresh_audio_progress()
                raise
            self.audio_draft = result
            self.audio_draft_readonly = False
            self.audio_song = latest
            matched = sum(t is not None for t in result['times'])
            partial = ' · 확인된 일부 구간만 분석' if result.get('partial') else ''
            fallback=' · stable-ts 폴백' if result.get('fallback_reason') else ''
            if result.get('refinement_warning'):
                fallback=' · pyshiro 오류, RomajiASR 단독 후보'
            cleanup = (' · 재분석용 데이터 정리 실패' if result.get('capture_retry_needed') is False
                       and not job.release_retry_capture() else '')
            self.audio_status.set(f'분석 완료 · {matched}/{len(latest.lines)}구절 후보{partial}{fallback}{cleanup}. 확인 후 빈 구절에 적용하세요. 기존 기록은 보존됩니다.')
            if self.audio_manual_progress:
                self.audio_manual_progress.update({'kind':'result','text':self.audio_status.get()})
                self.refresh_audio_progress()
            self.refresh_audio()

    def apply_audio(self):
        if self.audio_job or not self.audio_song or not self.audio_draft:
            return
        if self.audio_draft_readonly:
            raise ValueError('실시간 가사 분석 후보는 보기 전용입니다. 저장된 시간표와 수동 기록은 변경하지 않습니다.')
        song = next((s for s in self.songs if s.id == self.audio_song.id), None)
        if song is None or song.fingerprint != self.audio_draft.get('fingerprint'):
            raise ValueError('가사 내용이 바뀌었습니다. 다시 분석해 주세요.')
        self.check_audio_candidate(song,self.audio_draft)
        current = self.store.timing(song)
        updated, added = merge_candidates(current, self.audio_draft['times'])
        if not added:
            self.audio_status.set('채울 수 있는 빈 구절이 없습니다. 기존 시간과 충돌하는 후보는 제외했습니다.')
            return
        keep_finalized=self.audio_draft.get('review_only') and current.get('audio_finalized') is True
        updated['audio_finalized']=bool(keep_finalized or not self.audio_draft.get('partial',False))
        updated['audio_method']=self.audio_draft.get('method','')
        self.store.save_timing(song, updated)
        cleanup = ''
        if all(stamp is not None for stamp in updated['times']) and self.audio_draft.get('capture_retry_needed') is True:
            cache = self.store.directory / 'audio_captures'
            duration = self.audio_draft['duration']
            path = cache / self.audio_draft['recording_key'] / str(round(duration)) / CAPTURE_NAME
            try:discard_private_capture(path, cache, duration)
            except OSError:cleanup = ' · 재분석용 데이터 정리 실패'
        if self.song and self.song.id == song.id:
            self.timing = updated
            self.resume_follow()
            if self.editor:
                self.editor_refresh()
        self.audio_status.set(f'{added}개 빈 구절에 적용했습니다{cleanup}. 기존 기록과 보정값은 보존했습니다. 들어 보며 미세 보정하세요.')

    def close_audio(self):
        # Manual capture follows this dialog; automatic jobs continue independently.
        self.cancel_audio()
        if self.audio_playlist_panel:
            self.audio_playlist_panel.close()
            self.audio_playlist_panel = None
        if self.audio_window:
            self.audio_window.destroy()
            self.audio_window = None

    def close_editor(self):
        if self.locked:self.toggle_lock()
        self.safe(self.save_settings)
        if self.editor:
            self.editor.destroy()
            self.editor = None

    def close(self):
        if self.closed or self.closing:return
        collector=self.missing_audio_collector
        if collector and collector.owns_playback:
            self.close_after_collection=True
            collector.stop()
            return
        self.closing=True
        if self.audio_playlist_panel:
            self.audio_playlist_panel.close()
            self.audio_playlist_panel = None
        self.workspace.close()
        if self.appearance_dialog:self.appearance_dialog.close()
        if getattr(self, 'sync_reanalysis', None):
            self.sync_reanalysis.close()
        if getattr(self,'update_dialog',None):self.update_dialog.close()
        if getattr(self,'update_controller',None):self.update_controller.close()
        if self.manual_sync_dialog:self.manual_sync_dialog.close()
        self.hide_track_title_tip()
        if self.translation_dialog:self.translation_dialog.close()
        if self.translation_resource_dialog:self.translation_resource_dialog.close()
        if self.explorer_dialog:self.explorer_dialog.close()
        if self.volume_popup:self.volume_popup.close()
        for hint in self.control_hints:hint.hide()
        self.safe(self.save_settings)
        drain=any(getattr(job,'incremental',False) and not getattr(job,'waiting_for_lyrics',False)
                  and callable(getattr(job,'finish_capture',None)) and not job.cancelled.is_set()
                  for job in self.auto_analyzer.jobs)
        if drain:
            # Preserve window geometry before withdrawing. Nothing scheduled by
            # the normal UI may reopen windows or mutate lyric identity while
            # the already-owned workers finish saving their captured segments.
            for pending in self.root.tk.splitlist(self.root.tk.call('after','info')):
                self.root.after_cancel(pending)
            windows=[self.root]
            while windows:
                window=windows.pop()
                windows.extend(window.winfo_children())
                if isinstance(window,(tk.Tk,tk.Toplevel)):window.withdraw()
            if self.audio_job:
                self.audio_job.close()
                self.audio_job=None
            self.auto_analyzer.begin_shutdown()
            self._drain_closing_audio()
            return
        self._finish_close()

    def _drain_closing_audio(self):
        if self.closed:return
        songs=self.songs+([self.live_lyrics.song] if self.live_lyrics.is_temporary(self.live_lyrics.song) else [])
        while True:
            try:kind,value=self.messages.get_nowait()
            except queue.Empty:break
            if kind not in ('audio','auto_audio'):continue
            job,event=value
            if job not in self.auto_analyzer.jobs or event.get('kind') in ('realtime','realtime_status'):continue
            self.safe(lambda:self._receive_analysis_job(job,event,songs),interactive=False)
        if self.auto_analyzer.jobs:self.root.after(80,self._drain_closing_audio)
        else:self._finish_close()

    def _finish_close(self):
        if self.closed:return
        self.closed = True
        if self.tray:self.tray.stop()
        if getattr(self, 'local_analysis_dialog', None):
            self.local_analysis_dialog.close()
        if getattr(self, 'sync_reanalysis_dialog', None):
            self.sync_reanalysis_dialog.close()
        self.catalog_sync.close()
        self.playlist_lyrics.close()
        self.spotify_translation_sources.close()
        self.audio_installer.close()
        if self.audio_install_dialog:self.audio_install_dialog.close()
        self.extension_inbox.close()
        self.internal_reader.close()
        self.background_lyrics.close()
        self.lyric_merge_job.reset()
        self.lyricsplus_job.close()
        self.alsong_job.close()
        self.reader_bridge.close()
        if self.audio_job:
            self.audio_job.close()
        self.auto_analyzer.close()
        self.screen_lyrics.stop()
        self.monitor.stop()
        self.lifecycle.stop()
        self.hotkeys.stop()
        for pending in self.root.tk.splitlist(self.root.tk.call('after','info')):
            self.root.after_cancel(pending)
        self.root.destroy()


def enable_timing_trace():
    """Opt-in, local timing diagnostics without credentials or lyric contents."""
    import cProfile
    import json
    import os
    from functools import wraps

    folder=os.environ.get('SPOTIFY_LYRICS_TIMING_DIR')
    if not folder:return
    directory=Path(folder)
    directory.mkdir(parents=True,exist_ok=True)
    destination=directory/('app-'+str(os.getpid())+'.jsonl')

    def instrument(name):
        original=getattr(LyricsApp,name)
        @wraps(original)
        def timed(self,*args,**kwargs):
            changed=name=='set_playback' and args[0].key!=self.playback.key
            profile=cProfile.Profile() if changed and os.environ.get('SPOTIFY_LYRICS_TIMING_PROFILE')=='1' else None
            started=time.perf_counter()
            if profile:profile.enable()
            try:return original(self,*args,**kwargs)
            finally:
                elapsed=(time.perf_counter()-started)*1000
                if profile:
                    profile.disable()
                    profile.dump_stats(str(directory/('track-'+str(os.getpid())+'.prof')))
                if changed or elapsed>=100 or name in ('receive_background_lyrics','publish_electron_overlay'):
                    value={'stage':name,'at':time.monotonic(),'ms':round(elapsed,3),
                           'track':hashlib.sha256(self.playback.key.encode()).hexdigest(),
                           'rows':len(self.display_lines),'layout_pending':self.pending_height_fit is not None,
                           'snapshot_age_ms':round((time.monotonic()-self.playback.captured)*1000,3)}
                    try:
                        with destination.open('a',encoding='utf-8') as output:
                            output.write(json.dumps(value)+'\n')
                    except OSError:pass
        setattr(LyricsApp,name,timed)
    for name in ('set_playback','restore_cached_lyrics','render','receive_background_lyrics',
                 'receive_merged_lyrics','publish_electron_overlay','tick','fit_height'):
        instrument(name)


def main():
    enable_timing_trace()
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--import-folder', type=Path)
    parser.add_argument('--no-media', action='store_true', help='오프라인 UI 확인')
    parser.add_argument('--connect-spotify',action='store_true',help='Spotify 로그인 연결 창 열기')
    parser.add_argument('--dictionary', action='store_true', help='음악 사전을 함께 열기')
    parser.add_argument('--capture-missing-audio', action='store_true', help='음원 분석의 재생목록 분석 탭 열기')
    parser.add_argument('--spotify-auto-start',action='store_true',help='Spotify 연동 자동 실행')
    parser.add_argument('--watch-spotify',action='store_true',help='Spotify 실행 감시')
    args = parser.parse_args()
    needs_seed = frozen() or not args.data_dir.exists()
    if args.watch_spotify:
        from spotify_lifecycle import watch
        watch(args.data_dir)
        return
    with SingleInstance(args.data_dir) as instance, WindowActivation(args.data_dir, owner=instance.acquired) as activation:
        if not instance.acquired:
            if args.spotify_auto_start or activation.request():return
            root=tk.Tk();root.withdraw()
            shortcuts=validate_bindings(Store(args.data_dir).settings().get('hotkeys'))
            messagebox.showinfo('가사 앱 실행 중','이미 실행 중입니다. 알림 영역의 앱 아이콘을 확인하세요.\n헤더 복원: '+
                                shortcut_label(shortcuts['chrome']),parent=root)
            root.destroy()
            return
        from data_updates import apply_pending
        try:args.data_update_result=apply_pending(args.data_dir)
        except (ValueError,OSError) as error:args.data_update_error=str(error)
        if needs_seed:
            try:seed_data(args.data_dir)
            except (ValueError,OSError) as error:args.data_update_error=str(error)
        clear_spotify_exit(args.data_dir)
        args.window_activation = activation
        run(args)


def run(args):
    store = Store(args.data_dir)
    if args.import_folder:
        imported = import_folder(args.import_folder)
        songs = {song.id: song for song in store.library()}
        songs.update({song.id: song for song in imported})
        settings = store.settings()
        store.save_library(list(songs.values()))
        settings['source_folder'] = str(args.import_folder.resolve())
        store.save_settings(settings)
    register_bundled_fonts()
    root = tk.Tk()
    try:
        app = LyricsApp(root, store, start_services=not args.no_media,
                        spotify_auto_start=getattr(args,'spotify_auto_start',False),
                        activation=getattr(args,'window_activation',None))
        if getattr(args,'data_update_error',''):
            root.after(700,lambda:messagebox.showerror('자료 업데이트 적용 실패',
                '새 자료를 적용하지 못했습니다. 기존 자료로 앱을 시작했습니다.\n\n'+args.data_update_error,parent=root))
        elif getattr(args,'data_update_result',{}).get('status')=='applied':
            result=args.data_update_result
            notice=f"자료 업데이트 적용 · 신규 {result.get('added_files',0)} · 보완 {result.get('updated_files',0)}"
            if result.get('deferred_files',0):notice+=f" · 보류 {result['deferred_files']} (기존 자료 보존)"
            root.after(900,lambda:app.notify(notice,15))
        if not args.no_media:root.after(300,lambda:app.show_startup(getattr(args,'connect_spotify',False)))
        if getattr(args, 'dictionary', False):root.after(600, app.show_music_dictionary)
        if getattr(args, 'capture_missing_audio', False):
            root.after(800, lambda:app.safe(lambda:app.open_audio(playlist=True)))
        root.mainloop()
    except (ValueError, OSError) as error:
        messagebox.showerror('가사 앱 시작 실패', str(error), parent=root)
        root.destroy()


if __name__ == '__main__':
    main()

