"""Electron windows backed by the Python app over authenticated loopback."""
from copy import deepcopy
from base64 import b64encode
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import secrets
import socket
import subprocess
import sys
import threading

from appearance import (DEFAULT_BACKGROUND, DEFAULT_BACKGROUND_OPACITY, DEFAULT_STYLES,
                        DEFAULT_TRANSPARENT, OUTLINE_BLUR_MAX, OUTLINE_STEP, OUTLINE_WIDTH_MAX,
                        compact_from_settings, styles_from_settings, valid_color)
from app_paths import frozen
from artist_colors import character_color_spans
from catalog_workspace import CatalogWorkspace
from lyric_performers import short_performer_name
from overlay_fonts import font_families
from sync import lyric_advance_ms
from user_annotations import SINGER_KEYS


def electron_path(base):
    runtime = Path(sys.executable).parent if frozen() else base
    candidates = (runtime/'desktop_ui/node_modules/electron/dist/electron.exe',
                  runtime/'electron-runtime/electron.exe')
    return next((file for file in candidates if file.is_file()), None)


def appearance_draft(app):
    styles = deepcopy(app.text_styles)
    for key, style in styles.items():
        style.setdefault('visible', key not in ('previous', 'next'))
    styles['original']['visible'] = app.show_original.get()
    styles['reading']['visible'] = app.show_reading.get()
    return dict(text_styles=styles, opacity=app.opacity.get(),
                background_opacity=app.background_opacity.get(), background_color=app.background_color,
                transparent=app.transparent.get(), compact_layout=deepcopy(app.compact_layout))


def checked_draft(value):
    styles = styles_from_settings(value)
    for key, style in styles.items():
        style.setdefault('visible', key not in ('previous', 'next'))
    return dict(text_styles=styles, opacity=number(value.get('opacity'), .15, 1),
                background_opacity=number(value.get('background_opacity'), 0, 1),
                background_color=valid_color(value.get('background_color'), DEFAULT_BACKGROUND),
                transparent=value.get('transparent') is True, compact_layout=compact_from_settings(value))


def number(value, minimum, maximum):
    if type(value) not in (int,float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{minimum}~{maximum} 사이의 값을 입력하세요.')
    return value


class DesktopWorkspace:
    def __init__(self, app, base):
        self.app, self.base = app, base
        self.events = queue.Queue()
        self.process = None
        self.channel = None
        self.music_catalog = CatalogWorkspace(app)
        self.spotify = None
        self.developer_dialog = None
        self.developer_subscribed = False
        self.last_developer_playback = None
        self.overlay_requested = False
        self.overlay_ready = False
        self.last_overlay = None
        self.write_lock = threading.Lock()
        self.pending = None
        self.requested = None
        self.ready = False
        self.callback = None
        self.playing = False

    def show(self, view, fallback):
        self.requested = view
        self.pending = fallback
        if self.process and self.process.poll() is None:
            if self.ready:
                self.send({'view':view})
            return
        if not self.start(view):
            self.pending = None
            fallback()

    def start_overlay(self):
        if not (self.base/'desktop_ui/overlay.html').is_file():
            return
        self.overlay_requested = True
        if not (self.process and self.process.poll() is None) and not self.start('overlay'):
            self.overlay_requested = False

    def start(self, view):
        executable = electron_path(self.base)
        entry = self.base/'desktop_ui/main.cjs'
        if not executable or not entry.is_file():
            return False
        self.ready = False
        listener = socket.socket()
        try:
            listener.bind(('127.0.0.1',0))
            listener.listen(1)
            listener.settimeout(10)
            secret = secrets.token_hex(32)
            self.process = subprocess.Popen([str(executable),str(entry),view],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=self.base,
                env={**os.environ,'SPOTIFY_OVERLAY_WORKSPACE_PROFILE':str(self.app.store.directory.resolve()/'workspace_profile'),
                     'SPOTIFY_OVERLAY_WORKSPACE_PORT':str(listener.getsockname()[1]),'SPOTIFY_OVERLAY_WORKSPACE_SECRET':secret},
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except OSError:
            listener.close()
            return False
        process = self.process

        def read():
            try:
                with listener:
                    channel, _ = listener.accept()
                with channel, channel.makefile('rb') as stream:
                    channel.settimeout(10)
                    hello = stream.readline(200)
                    if not secrets.compare_digest(hello.rstrip(b'\r\n'),secret.encode('ascii')):
                        return
                    channel.settimeout(None)
                    with self.write_lock:
                        if self.process is not process:
                            return
                        self.channel = channel
                    while line := stream.readline(200_001):
                        if len(line) > 200_000:
                            break
                        try:self.events.put((process,json.loads(line)))
                        except ValueError:pass
            except OSError:
                pass
            finally:
                self.events.put((process,None))
        threading.Thread(target=read, name='electron-workspace', daemon=True).start()
        if self.callback is None:
            self.callback = self.app.root.after(35,self.poll)
        return True

    def send(self,value):
        with self.write_lock:
            if self.channel:
                try:
                    self.channel.sendall((json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8'))
                except (OSError,ValueError):
                    pass

    def poll(self):
        self.callback = None
        for _ in range(20):
            try:process,message = self.events.get_nowait()
            except queue.Empty:break
            if process is not self.process:
                continue
            if message is None:
                with self.write_lock:
                    self.channel = None
                if process.poll() is None:
                    process.terminate()
                self.stop_overlay()
                self.process = None
                self.ready = False
                self.leave_developer()
                fallback = self.pending
                self.pending = None
                self.requested = None
                if fallback:
                    fallback()
                continue
            if not isinstance(message,dict):
                continue
            if message.get('ready') is True:
                self.ready = True
                if self.requested:
                    self.send({'view':self.requested})
                continue
            if message.get('workspaceShown') is True:
                self.pending = None
                continue
            if message.get('workspaceFailed') is True:
                fallback = self.pending
                self.pending = None
                if fallback:
                    fallback()
                continue
            if message.get('workspaceClosed') is True:
                self.leave_developer()
                self.requested = None
                self.pending = None
                continue
            if message.get('overlayReady') is True and self.overlay_requested and not self.overlay_ready:
                handle = message.get('hwnd')
                if isinstance(handle, str) and len(handle) <= 20 and handle.isascii() and handle.isdecimal():
                    self.app.set_electron_overlay_active(True, hwnd=int(handle))
                    self.overlay_ready = True
                continue
            if message.get('overlayFailed') is True:
                self.stop_overlay()
                continue
            ident, method, payload = message.get('id'), message.get('method'), message.get('payload',{})
            if type(ident) is not int or not isinstance(method,str) or not isinstance(payload,dict):
                continue
            try:
                if method == 'catalog.play':
                    self.play(ident,payload)
                    continue
                result = self.dispatch(method,payload)
                self.send({'id':ident,'result':result})
            except (ValueError, OSError) as error:
                self.send({'id':ident,'error':str(error)})
        if self.process:
            self.callback = self.app.root.after(35,self.poll)

    def stop_overlay(self):
        self.overlay_requested = False
        self.last_overlay = None
        if self.overlay_ready:
            self.overlay_ready = False
            self.app.set_electron_overlay_active(False)

    def leave_developer(self):
        self.developer_subscribed = False
        self.last_developer_playback = None
        if self.developer_dialog and self.app.manual_sync_dialog is self.developer_dialog:
            self.developer_dialog.close()
        self.developer_dialog = None

    def close(self):
        if self.callback is not None:
            self.app.root.after_cancel(self.callback)
            self.callback = None
        self.leave_developer()
        self.music_catalog.close()
        if self.spotify:
            self.spotify.close()
        if self.process:
            self.send({'close':True})
            with self.write_lock:
                if self.channel:
                    try:self.channel.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                else:self.process.terminate()
                self.channel = None
                self.process = None

    def dispatch(self, method, payload):
        scope, _, action = method.partition('.')
        if scope == 'overlay' and self.overlay_ready:
            return self.app.electron_overlay_action(action,payload)
        if scope == 'appearance':
            return self.appearance(action,payload)
        if scope == 'catalog':
            return self.music_catalog.dispatch(action,payload)
        if scope == 'developer':
            return self.developer(action,payload)
        raise ValueError('지원하지 않는 동작입니다.')

    def appearance(self, action, payload):
        app = self.app
        existing = app.settings.get('appearance_presets')
        presets = existing if isinstance(existing,dict) else {}
        if action == 'get':
            return dict(draft=appearance_draft(app), presets=sorted(presets), families=font_families(app.root),
                        outline_limits=dict(outline_width=OUTLINE_WIDTH_MAX,outline_blur=OUTLINE_BLUR_MAX),
                        outline_step=OUTLINE_STEP,
                        overlay=dict(borderless=app.borderless.get(), width=app.root.winfo_width(),
                            height=app.root.winfo_height(), icons={kind:b64encode(image.tk.call(str(image),'data','-format','png')).decode('ascii')
                                for kind,image in app.transport_images.items() if kind!='pause'}))
        if action == 'reset':
            return checked_draft(dict(text_styles=DEFAULT_STYLES, opacity=.94,
                background_opacity=DEFAULT_BACKGROUND_OPACITY,
                background_color=DEFAULT_BACKGROUND, transparent=DEFAULT_TRANSPARENT))
        if action == 'presetLoad':
            name = payload.get('name')
            if not isinstance(name,str) or not isinstance(presets.get(name),dict):
                raise ValueError('프리셋을 찾지 못했습니다.')
            return checked_draft(presets[name])
        if action == 'presetSave':
            name = payload.get('name')
            if not isinstance(name,str) or not (name := name.strip()) or len(name)>80:
                raise ValueError('프리셋 이름을 1~80자로 입력하세요.')
            replace = payload.get('replace') is True
            if (replace and name not in presets) or (not replace and any(n.casefold()==name.casefold() for n in presets)):
                raise ValueError('저장할 프리셋 이름을 확인하세요.')
            draft = payload.get('draft')
            if not isinstance(draft,dict):
                raise ValueError('저장할 설정을 확인하세요.')
            app.settings['appearance_presets'] = {**presets, name:checked_draft(draft)}
            try:app.save_settings()
            except OSError:
                app.settings['appearance_presets'] = presets
                raise
            return sorted(app.settings['appearance_presets'])
        if action == 'apply':
            draft = checked_draft(payload)
            old = appearance_draft(app)

            def apply(value):
                app.text_styles = value['text_styles']
                app.font_size.set(app.text_styles['original']['size'])
                app.show_original.set(app.text_styles['original']['visible'])
                app.show_reading.set(app.text_styles['reading']['visible'])
                app.opacity.set(value['opacity'])
                app.background_opacity.set(value['background_opacity'])
                app.background_color = value['background_color']
                app.transparent.set(value['transparent'])
                app.compact_layout = value['compact_layout']
                app.apply_appearance()

            apply(draft)
            try:app.save_settings()
            except OSError:
                apply(old)
                raise
            return True
        raise ValueError('지원하지 않는 표시 설정 동작입니다.')

    def play(self,ident,payload):
        from explorer_library import ExplorerLibrary
        from spotify_catalog import SpotifyCatalogError
        from spotify_desktop import DesktopSpotify
        if self.playing:
            raise ValueError('앞에서 요청한 곡의 재생을 확인하고 있습니다.')
        song = self.music_catalog.get_song(payload.get('id'))
        track = self.music_catalog.track_for(song,ExplorerLibrary(self.app.store.directory))
        if not track:
            raise ValueError('연결된 Spotify 트랙이 없습니다.')
        if self.spotify is None:
            self.spotify = DesktopSpotify(self.app)
        self.playing = True
        process=self.process
        def run():
            try:
                if not self.spotify.play(track):
                    raise ValueError('곡 화면을 열었습니다. Spotify에서 재생 버튼을 눌러 주세요.')
                reply={'id':ident,'result':True}
            except (OSError,ValueError,SpotifyCatalogError) as error:
                reply={'id':ident,'error':str(error)}
            finally:self.playing = False
            if self.process is process:
                self.send(reply)
        threading.Thread(target=run,name='workspace-spotify',daemon=True).start()

    def developer_cursor(self, dialog):
        if not dialog or not dialog.matches():
            return '', None
        app = self.app
        token = hashlib.sha256((app.playback.key+'\0'+app.song.id+'\0'+app.song.fingerprint).encode()).hexdigest()
        current = app.visible_lyric_index()
        return token, current if 0 <= current < len(dialog.originals) else None

    def publish_developer_playback(self, position):
        message = {}
        if self.overlay_requested and self.ready:
            overlay = self.app.electron_overlay_snapshot(position)
            if overlay != self.last_overlay:
                self.last_overlay = overlay
                message['overlay'] = overlay
        if self.developer_subscribed:
            track, current = self.developer_cursor(self.app.manual_sync_dialog)
            playback = dict(track=track, current=current, position=position)
            if playback != self.last_developer_playback:
                self.last_developer_playback = playback
                message['developerPlayback'] = playback
        if message:
            self.send(message)

    def developer_state(self, *, include_rows=True):
        app = self.app
        dialog = app.manual_sync_dialog
        token, current = self.developer_cursor(dialog)
        matched = bool(token)
        collector = app.missing_audio_collector
        transport_busy = bool(app.pending_track_change or app.pending_seek or app.pending_playback
                              or (collector and collector.owns_playback))
        live = dict(track=token, current=current, matched=matched, position=app.playback.position(),
                    selected=dialog.selected_index() if matched else None,
                    playing=app.playback.playing, seeking=bool(app.pending_seek),
                    recordingCursor=dialog.index if matched else None,
                    singerInput=app.singer_input_index is not None,
                    transportBusy=transport_busy, active=app.playback.has_timeline)
        if not include_rows:
            return live
        rows = []
        if matched:
            direct = app.direct_times()
            offset = app.timing['offset']
            for index, original in enumerate(dialog.originals):
                stamp = dialog.row_stamp(index)
                origin = dialog.baseline[1][index]
                if (origin=='audio' and stamp is not None and index < len(direct) and direct[index] is not None
                        and math.isclose(stamp,max(0.,direct[index]+offset),abs_tol=1e-6)):
                    origin = 'provider'
                rows.append(dict(index=index, original=app.corrected_lyric_text(index) or original,
                                 reading=app.display_lyric_reading(index),
                                 translation=app.display_lyric_translation(index),
                                 singers=app.current_line_singers(index), time=stamp,timingOrigin=origin))
        singer_names = set(app.singer_options) if matched else set()
        singer_names.update(name for row in rows for name in row['singers'])
        singer_colors = {}
        for name in singer_names:
            spans = character_color_spans(name, app.playback.album or 'THE IDOLM@STER')
            if len(spans) == 1 and spans[0][:2] == (0, len(name)) and isinstance(spans[0][2], str):
                singer_colors[name] = spans[0][2]
        return dict(title=app.playback.title,artist=app.playback.artist,album=app.playback.album,
                    auto=app.auto_audio.get(), realtimeTracking=app.realtime_tracking.get(), **live,
                    workPlaylist=app.settings.get('developer_work_playlist', ''),
                    offset=app.timing['offset'] if matched else 0, lead=app.lyric_advance_ms, rows=rows,
                    singerOptions=list(app.singer_options) if matched else [],
                    singerColors=singer_colors,
                    singerLabels={name:short_performer_name(name,app.playback.album) for name in singer_names},
                    singerEditable=bool(matched and dialog.can_edit_singers()),
                    alsong=('confirmed' if app.temporary_alsong.confirmed else 'temporary')
                           if app.temporary_alsong else '',
                    source=(app.timing.get('source') or '') if matched else '',
                    status=dialog.status.cget('text') if dialog else '')

    def developer(self,action,payload):
        app = self.app
        if action == 'leave':
            self.leave_developer()
            return True
        if not app.manual_sync_dialog:
            app.open_developer_mode()
            self.developer_dialog = app.manual_sync_dialog
            self.developer_dialog.window.withdraw()
        dialog = app.manual_sync_dialog
        if action == 'get':
            self.developer_subscribed = self.ready
            return self.developer_state()
        if action == 'position':
            return self.developer_state(include_rows=False)
        collector = app.missing_audio_collector
        if collector and collector.owns_playback and action in ('step', 'transport', 'seek'):
            raise ValueError('재생목록을 분석하는 동안 Spotify 재생 위치를 바꿀 수 없습니다.')
        if action == 'open':
            tools={'connection':app.open_connection,'audio':app.open_audio,
                   'alsong':dialog.open_alsong,'collection':dialog.open_collection,
                   'translation':app.open_batch_translation,'translationResources':app.open_translation_resources}
            name = payload.get('tool')
            if not isinstance(name,str) or name not in tools:
                raise ValueError('도구를 찾지 못했습니다.')
            tools[name]()
        elif action == 'setAuto':
            if type(payload.get('enabled')) is not bool:
                raise ValueError('자동 분석 상태를 확인하세요.')
            app.auto_audio.set(payload['enabled'])
            app.toggle_auto_audio()
        elif action == 'setRealtimeTracking':
            app.set_realtime_tracking(payload.get('enabled'))
        elif action == 'lead':
            app.lyric_advance_ms=lyric_advance_ms(number(payload.get('value'),0,500))
            app.save_settings()
            app.render()
        elif action == 'step':
            seconds = number(payload.get('seconds'),-10,10)
            key = {-5:'q',-2:'w',1:'e',5:'r'}.get(seconds)
            if key:
                app.seek_by_key(key)
            elif app.playback.has_timeline:
                app.request_lyric_seek(app.playback.position()+seconds)
        elif action == 'transport':
            name = payload.get('tool')
            tools = {'toggle':app.toggle_playback,'first':dialog.rewind,'next':dialog.next_track}
            if not isinstance(name,str) or name not in tools:
                raise ValueError('재생 도구를 찾지 못했습니다.')
            tools[name]()
        elif action == 'openWorkPlaylist':
            dialog.open_work_playlist(payload.get('value'))
        elif action == 'offset':
            dialog.change_offset(number(payload.get('delta'),-.5,.5))
        elif action == 'seek':
            index = self.developer_index(dialog,payload)
            dialog.seek_row(index)
        elif action in ('select','record','clear','saveLine','lineTool'):
            index = self.developer_index(dialog,payload)
            if action == 'saveLine':
                field, value = payload.get('field'), payload.get('value')
                if field not in ('original','reading','translation') or not isinstance(value,str) or len(value)>4000:
                    raise ValueError('가사 입력값을 확인하세요.')
                app.lyric_text_edits.save(app.annotation_row(index),value.strip(),
                    field='korean' if field=='translation' else field)
                app.render()
                dialog.update_times()
            else:
                dialog.select_for_edit(index)
                if action == 'record':dialog.record()
                if action == 'clear':dialog.clear_time()
                if action == 'lineTool':
                    name = payload.get('tool')
                    if name == 'roster':
                        dialog.edit_singers(parent=app.root if dialog.window.state()=='withdrawn' else dialog.window)
                    elif name == 'singerInput':
                        enabled = payload.get('enabled')
                        if type(enabled) is not bool:
                            raise ValueError('가창자 입력 상태를 확인하세요.')
                        if not enabled:
                            app.stop_singer_input()
                        elif app.singer_input_index is None and dialog.can_edit_singers():
                            app.toggle_singer_input()
                    elif name == 'recordEnd':
                        dialog.record_previous_end()
                    elif name == 'recordNext':
                        if dialog.navigation_available():dialog.record_row(index,index)
                    elif name == 'listen':
                        dialog.listen()
                    elif name == 'confirmAlsong':
                        dialog.confirm_alsong()
                    elif name == 'unlinkAlsong':
                        dialog.unlink_alsong()
                    elif name == 'singer':
                        key = payload.get('key')
                        if not isinstance(key,str) or not (len(key)==1 and key in SINGER_KEYS or key in ('space','BackSpace')):
                            raise ValueError('가창자 입력값을 확인하세요.')
                        if dialog.can_edit_singers():
                            app.singer_assignment_key(key,index=index)
                    elif name == 'remove':
                        dialog.change_lyric_rows(index)
                    elif name == 'add':
                        value = payload.get('value')
                        if not isinstance(value,str) or len(value)>4000:
                            raise ValueError('한 줄의 가사를 입력하세요.')
                        dialog.change_lyric_rows(index,value)
                    else:
                        raise ValueError('가사 편집 도구를 찾지 못했습니다.')
        else:
            raise ValueError('지원하지 않는 개발자 모드 동작입니다.')
        return self.developer_state()

    def developer_index(self,dialog,payload):
        state = self.developer_state(include_rows=False)
        index = payload.get('index')
        if (not state['matched'] or payload.get('track') != state['track']
                or type(index) is not int or not 0<=index<len(dialog.originals)):
            raise ValueError('재생 곡이나 가사가 바뀌었습니다. 다시 선택하세요.')
        return index
