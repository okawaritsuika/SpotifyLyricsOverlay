"""Native styling and configurable shortcuts, including side-specific Shift."""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import sys
import threading
import time
from shortcuts import ACTIONS,ShortcutMatcher,validate_bindings


def install_shortcut_hook(user32,messages,bindings,enabled):
    class KeyboardEvent(ctypes.Structure):
        _fields_=[('vkCode',wintypes.DWORD),('scanCode',wintypes.DWORD),('flags',wintypes.DWORD),('time',wintypes.DWORD),('extra',ctypes.c_size_t)]
    callback_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,ctypes.c_int,wintypes.WPARAM,wintypes.LPARAM)
    user32.GetAsyncKeyState.argtypes=(ctypes.c_int,);user32.GetAsyncKeyState.restype=ctypes.c_short
    keys=set(ShortcutMatcher.modifier_bits)|{binding['key'] for binding in bindings.values() if binding}
    shortcut=ShortcutMatcher(bindings,[key for key in keys if user32.GetAsyncKeyState(key)&0x8000])
    user32.CallNextHookEx.argtypes=(wintypes.HANDLE,ctypes.c_int,wintypes.WPARAM,wintypes.LPARAM)
    user32.CallNextHookEx.restype=ctypes.c_ssize_t
    @callback_type
    def callback(code,message,data):
        if code>=0 and message in (0x100,0x101,0x104,0x105):
            event=ctypes.cast(data,ctypes.POINTER(KeyboardEvent)).contents
            key=event.vkCode
            if key==0x10:key=0xA1 if event.scanCode==0x36 else 0xA0
            # Finish an already captured chord even when its modifier was released first.
            plain_arrow=0x25<=key<=0x28 and not shortcut.held and key not in shortcut.captured
            active=enabled()
            blocked=plain_arrow and active and not enabled(plain_arrow=True)
            consumed,action=shortcut.event(key,message in (0x100,0x104),enabled=active,blocked=blocked)
            if action:messages.put(('stamp_at',time.monotonic()) if action=='stamp' else ('action',action))
            if consumed:return 1
        return user32.CallNextHookEx(None,code,message,data)
    user32.SetWindowsHookExW.argtypes=(ctypes.c_int,callback_type,wintypes.HINSTANCE,wintypes.DWORD)
    user32.SetWindowsHookExW.restype=wintypes.HANDLE
    user32.UnhookWindowsHookEx.argtypes=(wintypes.HANDLE,)
    return user32.SetWindowsHookExW(13,callback,None,0),callback


class SingleInstance:
    """Keep a data directory's hotkeys, recorder and settings under one process."""
    def __init__(self,directory):
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateMutexW.argtypes=(ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR)
        self.kernel.CreateMutexW.restype=wintypes.HANDLE
        self.kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
        key=hashlib.sha256(os.path.normcase(str(Path(directory).resolve())).encode()).hexdigest()[:24]
        self.handle=self.kernel.CreateMutexW(None,False,'Local\\SpotifyLyricsOverlay-'+key)
        error=ctypes.get_last_error()
        if not self.handle:raise ctypes.WinError(error)
        self.acquired=error!=183

    def __enter__(self):return self
    def __exit__(self,*_):
        if self.handle:self.kernel.CloseHandle(self.handle);self.handle=None


def work_area():
    rect = wintypes.RECT()
    if sys.platform == 'win32' and ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, 1280, 720


def set_click_through(root, enabled):
    if sys.platform != 'win32':
        return
    user32 = ctypes.windll.user32
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    get_style = user32.GetWindowLongPtrW
    set_style = user32.SetWindowLongPtrW
    get_style.argtypes = (wintypes.HWND, ctypes.c_int)
    get_style.restype = ctypes.c_ssize_t
    set_style.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
    set_style.restype = ctypes.c_ssize_t
    root.update_idletasks()
    hwnd = user32.GetAncestor(root.winfo_id(), 2)
    style = get_style(hwnd, -20)
    set_style(hwnd, -20, style | 0x80020 if enabled else style & ~0x20)


class GlobalHotkeys:
    def __init__(self, messages, bindings=None):
        self.messages = messages
        self.bindings=validate_bindings(bindings)
        self.windows={};self.window_handles=frozenset();self.input_focus=None
        self.suspended=False;self.changed=threading.Event();self.ready=threading.Event()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name='lyrics-hotkeys')

    def register_window(self,window):
        """Register an overlay/editor on the UI thread; never call Tk in the hook."""
        if sys.platform!='win32' or window in self.windows:return
        user32=ctypes.windll.user32
        user32.GetAncestor.argtypes=(wintypes.HWND,wintypes.UINT)
        user32.GetAncestor.restype=wintypes.HWND
        focus_check=None
        def refresh(event=None):
            if event is not None and event.widget is not window:return
            self.windows[window]=user32.GetAncestor(window.winfo_id(),2)
            self.window_handles=frozenset(handle for handle in self.windows.values() if handle)
        def destroyed(event):
            nonlocal focus_check
            focused=self.input_focus
            if focused and (event.widget is focused[1] or event.widget is focused[0]):self.input_focus=None
            if event.widget is not window:return
            if focus_check is not None:window.after_cancel(focus_check);focus_check=None
            self.windows.pop(window,None)
            self.window_handles=frozenset(handle for handle in self.windows.values() if handle)
        def focus_in(event):
            widget=event.widget
            widget_class=widget.winfo_class()
            input_class=widget_class in ('Entry','TEntry','Text','TCombobox','Spinbox','TSpinbox')
            self.input_focus=(window,widget,widget_class=='TCombobox') if input_class else None
        def focus_out(event):
            nonlocal focus_check
            focused=self.input_focus
            if not focused or event.widget is not focused[1]:return
            if not focused[2]:self.input_focus=None;return
            def settled():
                nonlocal focus_check
                focus_check=None
                # Tk's combobox popup has its own bindtags, outside the editor.
                if self.input_focus is not focused:return
                current=str(window.tk.call('focus'))
                if not current.startswith(str(focused[1])+'.popdown.'):self.input_focus=None
            if focus_check is not None:window.after_cancel(focus_check)
            focus_check=window.after_idle(settled)
        refresh()
        window.bind('<Map>',refresh,add='+')
        window.bind('<Destroy>',destroyed,add='+')
        window.bind('<FocusIn>',focus_in,add='+')
        window.bind('<FocusOut>',focus_out,add='+')

    def focused(self,plain_arrow=False):
        if self.suspended or sys.platform!='win32':return False
        user32=ctypes.windll.user32
        user32.GetForegroundWindow.restype=wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.DWORD))
        window=user32.GetForegroundWindow()
        if not window or window not in self.window_handles:return False
        focused=self.input_focus
        if plain_arrow and focused and self.windows.get(focused[0])==window:return False
        process=wintypes.DWORD()
        user32.GetWindowThreadProcessId(window,ctypes.byref(process))
        return process.value==os.getpid()

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def configure(self,bindings):
        self.bindings=validate_bindings(bindings);self.ready.clear();self.changed.set()

    def suspend(self,value):
        self.suspended=bool(value);self.ready.clear();self.changed.set()

    def _run(self):
        if sys.platform != 'win32':
            self.messages.put(('hotkeys', [value[0] for value in ACTIONS.values()]))
            return
        user32 = ctypes.windll.user32
        hook=None;callback=None;self.changed.set()
        message = wintypes.MSG()
        try:
            while not self.stop_event.wait(.01):
                if self.changed.is_set():
                    self.changed.clear()
                    if hook:user32.UnhookWindowsHookEx(hook);hook=None
                    failed=[]
                    if not self.suspended:
                        bindings={action:binding for action,binding in self.bindings.items() if binding}
                        failed=[ACTIONS[action][0] for action,binding in self.bindings.items() if binding is None]
                        # RegisterHotKey reserves arrows even when another app has
                        # focus. Inspect foreground ownership at each key event and
                        # pass through outside the registered overlay/editor.
                        if bindings:
                            hook,callback=install_shortcut_hook(user32,self.messages,bindings,self.focused)
                            if not hook:failed.extend(ACTIONS[action][0] for action in bindings)
                        self.messages.put(('hotkeys',failed))
                    self.ready.set()
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if hook:user32.UnhookWindowsHookEx(hook)
