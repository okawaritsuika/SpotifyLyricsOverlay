"""A notification-area icon whose callbacks run on the Tk event thread."""
import ctypes
from ctypes import wintypes
import sys
import uuid


class WindowsTray:
    """Create and destroy on the Tk thread; on_menu receives screen x and y."""
    def __init__(self, root, icon_path, on_toggle, on_menu):
        self.root = root
        self.icon_path = str(icon_path)
        self.on_toggle = on_toggle
        self.on_menu = on_menu
        self.hwnd = None
        self.icon = None
        self._active = False
        self._popup_open = False

    def start(self):
        if sys.platform != 'win32':
            return False
        if self._active:
            return True
        user = self.user = ctypes.WinDLL('user32', use_last_error=True)
        shell = self.shell = ctypes.WinDLL('shell32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        result = ctypes.c_ssize_t
        proc_type = ctypes.WINFUNCTYPE(result, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', proc_type),
                        ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                        ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HICON),
                        ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                        ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR)]

        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('hWnd', wintypes.HWND),
                        ('uID', wintypes.UINT), ('uFlags', wintypes.UINT),
                        ('uCallbackMessage', wintypes.UINT), ('hIcon', wintypes.HICON),
                        ('szTip', wintypes.WCHAR * 128), ('dwState', wintypes.DWORD),
                        ('dwStateMask', wintypes.DWORD), ('szInfo', wintypes.WCHAR * 256),
                        ('uTimeoutOrVersion', wintypes.UINT), ('szInfoTitle', wintypes.WCHAR * 64),
                        ('dwInfoFlags', wintypes.DWORD), ('guidItem', ctypes.c_byte * 16),
                        ('hBalloonIcon', wintypes.HICON)]

        user.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user.DefWindowProcW.restype = result
        user.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASS),)
        user.RegisterClassW.restype = wintypes.ATOM
        user.UnregisterClassW.argtypes = (wintypes.LPCWSTR, wintypes.HINSTANCE)
        user.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.HWND, wintypes.HANDLE,
                                        wintypes.HINSTANCE, ctypes.c_void_p)
        user.CreateWindowExW.restype = wintypes.HWND
        user.DestroyWindow.argtypes = (wintypes.HWND,)
        user.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user.CreatePopupMenu.argtypes = ()
        user.CreatePopupMenu.restype = wintypes.HMENU
        user.AppendMenuW.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
        user.AppendMenuW.restype = wintypes.BOOL
        user.TrackPopupMenuEx.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                          ctypes.c_int, wintypes.HWND, ctypes.c_void_p)
        user.TrackPopupMenuEx.restype = wintypes.UINT
        user.DestroyMenu.argtypes = (wintypes.HMENU,)
        user.EndMenu.argtypes = ()
        user.LoadImageW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT)
        user.LoadImageW.restype = wintypes.HANDLE
        user.DestroyIcon.argtypes = (wintypes.HICON,)
        user.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
        user.RegisterWindowMessageW.restype = wintypes.UINT
        user.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        user.GetSystemMetrics.argtypes = (ctypes.c_int,)
        user.GetSystemMetrics.restype = ctypes.c_int
        kernel.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel.GetModuleHandleW.restype = wintypes.HINSTANCE
        shell.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA))
        shell.Shell_NotifyIconW.restype = wintypes.BOOL
        self.instance = kernel.GetModuleHandleW(None)
        self.class_name = 'SpotifyLyricsTray-' + uuid.uuid4().hex
        self.taskbar_created = user.RegisterWindowMessageW('TaskbarCreated')
        self._notify_type = NOTIFYICONDATA
        self._message = 0x8001  # WM_APP + 1

        @proc_type
        def window_proc(hwnd, message, wparam, lparam):
            if self._active and message == self.taskbar_created:
                self._add_icon()
                return 0
            if self._active and message == self._message and wparam == 1:
                if lparam == 0x0202:  # WM_LBUTTONUP
                    self.root.after_idle(self._dispatch_toggle)
                elif lparam == 0x0205:  # WM_RBUTTONUP
                    point = wintypes.POINT()
                    if user.GetCursorPos(ctypes.byref(point)):
                        self.root.after_idle(self._dispatch_menu, point.x, point.y)
                return 0
            return user.DefWindowProcW(hwnd, message, wparam, lparam)

        self._window_proc = window_proc  # Keep callback alive until the class is unregistered.
        wc = WNDCLASS()
        wc.lpfnWndProc = window_proc
        wc.hInstance = self.instance
        wc.lpszClassName = self.class_name
        if not user.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.hwnd = user.CreateWindowExW(0x80, self.class_name, '', 0x80000000, -32000, -32000, 1, 1,
                                             None, None, self.instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self.icon = user.LoadImageW(None, self.icon_path, 1,
                                        user.GetSystemMetrics(49), user.GetSystemMetrics(50), 0x10)
            if not self.icon:
                raise ctypes.WinError(ctypes.get_last_error())
            self._active = True
            if not self._add_icon():
                raise OSError('Could not register the notification icon')
        except Exception:
            self.stop()
            raise
        return True

    def _data(self):
        data = self._notify_type()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = 0x1 | 0x2 | 0x4  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = self._message
        data.hIcon = self.icon
        data.szTip = 'Spotify Lyrics Overlay'
        return data

    def _add_icon(self):
        data = self._data()
        return bool(self.shell.Shell_NotifyIconW(0, ctypes.byref(data)))

    def _dispatch_toggle(self):
        if self._active:
            self.on_toggle()

    def _dispatch_menu(self, x, y):
        if self._active:
            self.on_menu(x, y)

    def popup(self, x, y, items):
        """Show a native menu; items are None or label/command/checked/children dicts."""
        if not self._active or self._popup_open:
            return
        user = self.user
        actions = {}
        next_id = 1

        def build(entries):
            nonlocal next_id
            menu = user.CreatePopupMenu()
            if not menu:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                for item in entries:
                    if item is None:
                        ok = user.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
                    elif 'children' in item:
                        child = build(item['children'])
                        ok = user.AppendMenuW(menu, 0x10, child, item['label'])  # MF_POPUP
                        if not ok:
                            user.DestroyMenu(child)
                    else:
                        command_id = next_id
                        next_id += 1
                        actions[command_id] = item['command']
                        flags = 0x8 if item.get('checked', False) else 0  # MF_CHECKED
                        ok = user.AppendMenuW(menu, flags, command_id, item['label'])
                    if not ok:
                        raise ctypes.WinError(ctypes.get_last_error())
            except Exception:
                user.DestroyMenu(menu)
                raise
            return menu

        menu = build(items)
        self._popup_open = True
        chosen = 0
        try:
            # The owner is a one-pixel tool window off-screen. It must be visible
            # to become foreground, so external clicks dismiss a shell menu.
            user.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
            user.SetForegroundWindow(self.hwnd)
            chosen = user.TrackPopupMenuEx(menu, 0x100 | 0x2, x, y, self.hwnd, None)
            if self.hwnd:
                user.PostMessageW(self.hwnd, 0, 0, 0)  # WM_NULL per shell menu convention
        finally:
            if self.hwnd:
                user.ShowWindow(self.hwnd, 0)  # SW_HIDE
            user.DestroyMenu(menu)
            self._popup_open = False
        if self._active and chosen in actions:
            actions[chosen]()

    def stop(self):
        if not hasattr(self, 'user'):
            return
        if self._popup_open:
            self.user.EndMenu()
        if self._active:
            data = self._data()
            self.shell.Shell_NotifyIconW(2, ctypes.byref(data))  # NIM_DELETE
        self._active = False
        if self.hwnd:
            self.user.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.icon:
            self.user.DestroyIcon(self.icon)
            self.icon = None
        if getattr(self, 'class_name', None):
            self.user.UnregisterClassW(self.class_name, self.instance)
            self.class_name = None
