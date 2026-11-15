"""Entry point for the GUI and the small set of packaged child workers."""
import os
import sys


def restore_pipes():
    # pythonw/PyInstaller windowed mode sets the Python streams to None even
    # when a parent supplied valid Win32 pipe handles.
    import ctypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
    kernel.GetStdHandle.restype = ctypes.c_void_p
    for name, identifier, mode in (('stdin', -10, 'r'), ('stdout', -11, 'w'), ('stderr', -12, 'w')):
        existing=getattr(sys,name)
        if existing is not None:
            existing.reconfigure(encoding='utf-8',errors='replace')
            continue
        handle = kernel.GetStdHandle(identifier & 0xffffffff)
        if handle and handle != ctypes.c_void_p(-1).value:
            try:
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY if mode == 'r' else os.O_WRONLY)
                stream = os.fdopen(descriptor, mode, encoding='utf-8', errors='replace', buffering=1)
            except OSError:stream = open(os.devnull, mode, encoding='utf-8')
        else:stream = open(os.devnull, mode, encoding='utf-8')
        setattr(sys, name, stream)


if __name__ == '__main__':
    restore_pipes()
    browser_workers = ('spotify_worker', 'browser_worker')
    module = sys.argv[2] if len(sys.argv) >= 3 and sys.argv[1] == '--worker' else None
    from app_paths import prepare_external_processes
    if module not in browser_workers:
        prepare_external_processes()
    if module is not None:
        import runpy
        if module not in ('spotify_controls', 'optional_runtime', 'app_updates') + browser_workers:raise SystemExit(2)
        sys.argv = [module] + (['--worker'] if module == 'app_updates' else []) + sys.argv[3:]
        runpy.run_module(module, run_name='__main__')
    elif len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        import json
        from pathlib import Path
        import ssl
        import tkinter as tk
        from app_paths import seed_data, resource_dir
        from app import LyricsApp
        from lyrics import Line, Song
        from media import Playback
        from storage import Store
        from web_lyrics import reading
        tls=ssl.create_default_context()
        if not tls.check_hostname or tls.verify_mode != ssl.CERT_REQUIRED:
            raise RuntimeError('Frozen TLS certificate verification is unavailable.')
        directory=Path(sys.argv[2]).resolve()
        seed_data(directory)
        root=tk.Tk()
        app=LyricsApp(root,Store(directory),start_services=False)
        if os.environ.get('SPOTIFY_LYRICS_SELF_TEST_SAVED_PLAYBACK') == '1':
            from timing_quality import timing_origins
            from time import perf_counter

            def check_saved(title, digest, rows, *, translated=0, spotify=0, manual=0):
                entry=resource_dir()/'resources'/'seed_data'/'lyrics_cache'/(digest+'.json')
                value=json.loads(entry.read_bytes())
                playback=Playback(*value['key'].split('\0'),duration=value['duration'])
                started=perf_counter()
                app.set_playback(playback)
                app.render()
                elapsed=round((perf_counter()-started)*1000,1)
                result={'rows':len(app.display_lines), 'translated':app.translated_count,
                        'spotify':sum(stamp is not None for stamp in app.direct_times()),
                        'manual':timing_origins(app.timing).count('manual'), 'elapsed_ms':elapsed}
                if (playback.title!=title or result['rows']!=rows or result['translated']<translated
                        or result['spotify']<spotify or result['manual']<manual):
                    raise ValueError('Packaged saved playback verification failed: '+str(result))
                return result

            root.withdraw()
            result={
                'BEAM':check_saved('BEAM','f1b6951ad369b3e977766df83a289e8c2b090c690c7ace535a5563edd58f8f13',
                                   47,translated=41,spotify=47),
                'FANTASTIC GATHERING!':check_saved('FANTASTIC GATHERING!',
                    '869c2cc3e20477c6ea96b6119ac665c9781f11edebb9dd0dd7d7d648e3a07137',85,manual=81)}
            app.close()
            (directory/'self-test.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
            raise SystemExit(0)
        root.update()
        app.open_explorer()
        root.update()
        app.update_controller.checked=True
        app.open_updates()
        root.update()
        sample = Song('local-analysis-smoke', 'Local audio smoke', 'Fixture', 'Fixture',
                      (Line('テスト', '', ''),))
        app.songs.append(sample)
        app.song, app.timing, app.display_lines = sample, app.store.timing(sample), sample.lines
        app.playback = Playback(sample.title, sample.artist, sample.album, duration=3)
        app.open_local_analysis()
        app.open_sync_reanalysis()
        root.update()
        result={'ok':True,'songs':len(app.songs),'reading':reading('日本語'),
                'catalog_present':(resource_dir()/'resources/song_catalog.json').is_file(),
                'gui':bool(root.winfo_exists()),'dictionary':bool(app.explorer_dialog),
                'update_dialog':bool(app.update_dialog),
                'local_analysis_dialog':bool(app.local_analysis_dialog.window.winfo_exists()),
                'sync_reanalysis_dialog':bool(app.sync_reanalysis_dialog.window.winfo_exists()),
                'tls_runtime':True,
                'lyric_advance_ms':app.lyric_advance_ms,
                'local_analysis_worker':(resource_dir()/'local_analysis_worker.py').is_file()}
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
        from winrt.windows.media.ocr import OcrEngine
        import uiautomation
        result['media_runtime']=bool(GlobalSystemMediaTransportControlsSessionManager and OcrEngine)
        result['automation_runtime']=bool(uiautomation.GetRootControl())
        from importlib.util import find_spec
        result['browser_module']=bool(find_spec('webview') and find_spec('clr'))
        result['optional_bundled']=[name for name in ('torch','faster_whisper') if find_spec(name)]
        if os.environ.get('SPOTIFY_LYRICS_SELF_TEST_PROVIDERS') == '1':
            from alsong_provider import search_alsong
            from public_lyrics import _request
            result['alsong_matches']=len(search_alsong('チャンピオン','星井美希'))
            result['public_matches']=len(_request({'track_name':'Imagine','artist_name':'John Lennon'}))
        app.close()
        (directory/'self-test.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    else:
        from app import main
        main()
