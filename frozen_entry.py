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
    from app_paths import prepare_external_processes
    prepare_external_processes()
    if len(sys.argv) >= 3 and sys.argv[1] == '--worker':
        import runpy
        module = sys.argv[2]
        if module not in ('spotify_controls', 'optional_runtime', 'app_updates'):raise SystemExit(2)
        sys.argv = [module] + (['--worker'] if module == 'app_updates' else []) + sys.argv[3:]
        runpy.run_module(module, run_name='__main__')
    elif len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        import json
        from pathlib import Path
        import tkinter as tk
        from app_paths import seed_data, resource_dir
        from app import LyricsApp
        from storage import Store
        from web_lyrics import reading
        directory=Path(sys.argv[2]).resolve()
        seed_data(directory)
        root=tk.Tk()
        app=LyricsApp(root,Store(directory),start_services=False)
        root.update()
        app.open_explorer()
        root.update()
        app.update_controller.checked=True
        app.open_updates()
        root.update()
        result={'ok':True,'songs':len(app.songs),'reading':reading('日本語'),
                'catalog_present':(resource_dir()/'resources/song_catalog.json').is_file(),
                'gui':bool(root.winfo_exists()),'dictionary':bool(app.explorer_dialog),
                'update_dialog':bool(app.update_dialog)}
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
        from winrt.windows.media.ocr import OcrEngine
        import uiautomation
        result['media_runtime']=bool(GlobalSystemMediaTransportControlsSessionManager and OcrEngine)
        result['automation_runtime']=bool(uiautomation.GetRootControl())
        from importlib.util import find_spec
        result['optional_bundled']=[name for name in ('torch','faster_whisper','webview') if find_spec(name)]
        app.close()
        (directory/'self-test.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    else:
        from app import main
        main()
