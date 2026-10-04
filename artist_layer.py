"""Per-pixel-alpha artist artwork above a Tk window, without a color-key fringe."""
import ctypes
from collections import deque
from ctypes import wintypes
import math
import sys

from PIL import Image


def premultiplied_bgra(image):
    """Return top-down BGRA pixels in the format required by UpdateLayeredWindow."""
    if image.mode!='RGBA':
        raise ValueError('Artist artwork must be an RGBA image')
    red,green,blue,alpha=image.convert('RGBa').split()
    return Image.merge('RGBA',(blue,green,red,alpha)).tobytes()


class _BitmapHeader(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('width',wintypes.LONG),('height',wintypes.LONG),
              ('planes',wintypes.WORD),('bit_count',wintypes.WORD),
              ('compression',wintypes.DWORD),('image_size',wintypes.DWORD),
              ('x_pixels_per_meter',wintypes.LONG),('y_pixels_per_meter',wintypes.LONG),
              ('colors_used',wintypes.DWORD),('colors_important',wintypes.DWORD)]


class _BitmapInfo(ctypes.Structure):
    _fields_=[('header',_BitmapHeader),('colors',wintypes.DWORD*1)]


class _BlendFunction(ctypes.Structure):
    _fields_=[('operation',wintypes.BYTE),('flags',wintypes.BYTE),
              ('opacity',wintypes.BYTE),('alpha_format',wintypes.BYTE)]


class _MouseTracking(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('flags',wintypes.DWORD),
              ('window',wintypes.HWND),('hover_time',wintypes.DWORD)]


class _WindowsAPI:
    def __init__(self):
        user=ctypes.WinDLL('user32',use_last_error=True)
        gdi=ctypes.WinDLL('gdi32',use_last_error=True)
        controls=ctypes.WinDLL('comctl32',use_last_error=True)
        signatures={
            'CreateWindowExW':(user,wintypes.HWND,[wintypes.DWORD,wintypes.LPCWSTR,
                wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,
                ctypes.c_int,wintypes.HWND,wintypes.HMENU,wintypes.HINSTANCE,ctypes.c_void_p]),
            'GetAncestor':(user,wintypes.HWND,[wintypes.HWND,wintypes.UINT]),
            'GetWindow':(user,wintypes.HWND,[wintypes.HWND,wintypes.UINT]),
            'IsWindow':(user,wintypes.BOOL,[wintypes.HWND]),
            'ShowWindow':(user,wintypes.BOOL,[wintypes.HWND,ctypes.c_int]),
            'DestroyWindow':(user,wintypes.BOOL,[wintypes.HWND]),
            'GetWindowLongPtrW':(user,ctypes.c_ssize_t,[wintypes.HWND,ctypes.c_int]),
            'SetWindowLongPtrW':(user,ctypes.c_ssize_t,[wintypes.HWND,ctypes.c_int,ctypes.c_ssize_t]),
            'CallWindowProcW':(user,ctypes.c_ssize_t,[ctypes.c_void_p,wintypes.HWND,
                wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]),
            'SendMessageW':(user,ctypes.c_ssize_t,[wintypes.HWND,wintypes.UINT,
                wintypes.WPARAM,wintypes.LPARAM]),
            'MapWindowPoints':(user,ctypes.c_int,[wintypes.HWND,wintypes.HWND,
                ctypes.POINTER(wintypes.POINT),wintypes.UINT]),
            'TrackMouseEvent':(user,wintypes.BOOL,[ctypes.POINTER(_MouseTracking)]),
            'SetCapture':(user,wintypes.HWND,[wintypes.HWND]),
            'GetCapture':(user,wintypes.HWND,[]),
            'ReleaseCapture':(user,wintypes.BOOL,[]),
            'GetDC':(user,wintypes.HDC,[wintypes.HWND]),
            'ReleaseDC':(user,ctypes.c_int,[wintypes.HWND,wintypes.HDC]),
            'SetWindowPos':(user,wintypes.BOOL,[wintypes.HWND,wintypes.HWND,ctypes.c_int,
                ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT]),
            'UpdateLayeredWindow':(user,wintypes.BOOL,[wintypes.HWND,wintypes.HDC,
                ctypes.POINTER(wintypes.POINT),ctypes.POINTER(wintypes.SIZE),wintypes.HDC,
                ctypes.POINTER(wintypes.POINT),wintypes.DWORD,
                ctypes.POINTER(_BlendFunction),wintypes.DWORD]),
            'CreateCompatibleDC':(gdi,wintypes.HDC,[wintypes.HDC]),
            'CreateDIBSection':(gdi,wintypes.HBITMAP,[wintypes.HDC,
                ctypes.POINTER(_BitmapInfo),wintypes.UINT,ctypes.POINTER(ctypes.c_void_p),
                wintypes.HANDLE,wintypes.DWORD]),
            'SelectObject':(gdi,wintypes.HANDLE,[wintypes.HDC,wintypes.HANDLE]),
            'DeleteObject':(gdi,wintypes.BOOL,[wintypes.HANDLE]),
            'DeleteDC':(gdi,wintypes.BOOL,[wintypes.HDC]),
            'SetWindowSubclass':(controls,wintypes.BOOL,[wintypes.HWND,ctypes.c_void_p,
                ctypes.c_size_t,ctypes.c_size_t]),
            'RemoveWindowSubclass':(controls,wintypes.BOOL,[wintypes.HWND,ctypes.c_void_p,
                ctypes.c_size_t]),
            'DefSubclassProc':(controls,ctypes.c_ssize_t,[wintypes.HWND,wintypes.UINT,
                wintypes.WPARAM,wintypes.LPARAM]),
        }
        for name,(library,result,arguments) in signatures.items():
            function=getattr(library,name)
            function.restype=result;function.argtypes=arguments
            setattr(self,name,function)


def _checked(value):
    if not value:raise ctypes.WinError(ctypes.get_last_error())
    return value


class ArtistLayer:
    """Owned artwork forwarding input to Tk; use the widget's Tk thread only.

    Coordinates passed to show() are screen pixels. The caller keeps the artwork
    aligned with its widget and hides it on withdrawal or clipping. close() is
    safe after either this layer or its owner has already been destroyed.
    """
    def __init__(self,widget):
        if sys.platform!='win32':
            raise OSError('ArtistLayer requires Windows per-pixel alpha windows')
        self._api=_WindowsAPI()
        self._widget=widget
        self._scheduler=widget._root()
        self._widget_hwnd=widget.winfo_id()
        self._root_hwnd=widget.winfo_toplevel().winfo_id()
        self._closed=False
        self._tracking=False
        self._inside=False
        self._size=(1,1)
        self._origin=(0,0)
        self._visible=False
        self._events=deque()
        self._wheel_counts={'layer':0,'owner':0,'tk':0,'routed':0}
        self._hooked=set()
        self._poll_task=None
        callback_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,wintypes.HWND,
                                        wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)
        self._callback=callback_type(self._window_proc)
        subclass_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,wintypes.HWND,wintypes.UINT,
            wintypes.WPARAM,wintypes.LPARAM,ctypes.c_size_t,ctypes.c_size_t)
        self._owner_callback=subclass_type(self._owner_window_proc)
        self._hwnd=None
        self._create_window()
        self._wheel_binding=self._scheduler.bind('<MouseWheel>',self._owner_wheel,add='+')

    def _create_window(self):
        self._owner=_checked(self._api.GetAncestor(self._widget_hwnd,2))
        # A color-keyed Tk Canvas has no hit-testable pixels under this artwork.
        # Receive its input here and forward it; locked owners still pass through.
        styles=0x00080000|0x08000000|0x00000080
        styles|=self._api.GetWindowLongPtrW(self._owner,-20)&0x20
        self._hwnd=_checked(self._api.CreateWindowExW(styles,'STATIC',
            'SpotifyLyricsOverlay artist artwork',0x80000000,0,0,1,1,
            self._owner,None,None,None))
        self._previous_proc=_checked(self._api.SetWindowLongPtrW(self._hwnd,-4,
            ctypes.cast(self._callback,ctypes.c_void_p).value))
        self._tracking=False
        self._inside=False

    def _window_proc(self,hwnd,message,wparam,lparam):
        api=self._api
        if message==0x21:return 3  # WM_MOUSEACTIVATE / MA_NOACTIVATE
        if message in (0x200,0x201,0x202,0x203,0x204,0x205,0x206,0x207,0x208,0x209,
                       0x20A,0x20E,0x2A3) and api.IsWindow(self._widget_hwnd):
            if message in (0x20A,0x20E):self._wheel_counts['layer']+=1
            # Tk's native dispatcher rejects this separate HWND. Queue plain
            # data only: calling Tk from a ctypes WndProc can re-enter Tcl while
            # _tkinter has released its thread state. A Tk timer delivers events.
            try:self._pointer_message(hwnd,message,wparam,lparam)
            except Exception:self._events.append((None,sys.exc_info()))
            return 0
        return api.CallWindowProcW(self._previous_proc,hwnd,message,wparam,lparam)

    def _owner_window_proc(self,hwnd,message,wparam,lparam,subclass_id,reference):
        api=self._api
        if message==0x82:  # Remove only our subclass during WM_NCDESTROY.
            api.RemoveWindowSubclass(hwnd,self._owner_callback,id(self))
            self._hooked.discard(hwnd)
        elif message in (0x20A,0x20E):
            self._wheel_counts['owner']+=1
            x=ctypes.c_short(lparam&0xffff).value-self._origin[0]
            y=ctypes.c_short((lparam>>16)&0xffff).value-self._origin[1]
            if (not self._closed and self._visible
                    and not api.GetWindowLongPtrW(self._owner,-20)&0x20
                    and 0<=x<self._size[0] and 0<=y<self._size[1]):
                self._wheel_counts['routed']+=1
                try:self._pointer_message(self._hwnd,message,wparam,lparam)
                except Exception:self._events.append((None,sys.exc_info()))
                return 0
        return api.DefSubclassProc(hwnd,message,wparam,lparam)

    def _attach_owner_hooks(self):
        for hwnd in (self._root_hwnd,self._owner):
            if hwnd not in self._hooked:
                _checked(self._api.SetWindowSubclass(hwnd,self._owner_callback,id(self),0))
                self._hooked.add(hwnd)

    def _pointer_message(self,hwnd,message,wparam,lparam):
        api=self._api
        if message==0x2A3:
            self._tracking=False
            if self._inside:self._events.append(('<Leave>',{}))
            self._inside=False
            return
        point=wintypes.POINT(ctypes.c_short(lparam&0xffff).value,
                            ctypes.c_short((lparam>>16)&0xffff).value)
        wheel=message in (0x20A,0x20E)
        inside=0<=point.x<self._size[0] and 0<=point.y<self._size[1]
        source=None if wheel else hwnd
        screen=wintypes.POINT(point.x,point.y)
        api.MapWindowPoints(source,None,ctypes.byref(screen),1)
        api.MapWindowPoints(source,self._widget_hwnd,ctypes.byref(point),1)
        state=(1 if wparam&4 else 0)|(4 if wparam&8 else 0)
        state|=(256 if wparam&1 else 0)|(512 if wparam&16 else 0)|(1024 if wparam&2 else 0)
        options=dict(x=point.x,y=point.y,rootx=screen.x,rooty=screen.y,state=state)
        if message==0x200:
            if inside and not self._inside:self._events.append(('<Enter>',options))
            if not inside and self._inside:self._events.append(('<Leave>',options))
            self._inside=inside
            if inside and not self._tracking:
                tracking=_MouseTracking(ctypes.sizeof(_MouseTracking),2,hwnd,0)
                self._tracking=bool(api.TrackMouseEvent(ctypes.byref(tracking)))
            if self._events and self._events[-1][0]=='<Motion>':self._events.pop()
            self._events.append(('<Motion>',options))
        elif wheel:
            if message==0x20E:options['state']|=1
            options['delta']=ctypes.c_short((wparam>>16)&0xffff).value
            self._events.append(('<MouseWheel>',options))
        else:
            button={0x201:1,0x202:1,0x203:1,0x204:3,0x205:3,0x206:3,
                    0x207:2,0x208:2,0x209:2}[message]
            release=message in (0x202,0x205,0x208)
            if not release:api.SetCapture(hwnd)
            self._events.append((f'<Button{"Release" if release else "Press"}-{button}>',options))
            if release and api.GetCapture()==hwnd:api.ReleaseCapture()

    def _flush_events(self):
        self._poll_task=None
        if self._closed or not self._api.IsWindow(self._widget_hwnd):return
        locked=self._sync_input_style()
        while self._events:
            sequence,options=self._events.popleft()
            if sequence is None:self._scheduler.report_callback_exception(*options)
            elif not locked:self._widget.event_generate(sequence,when='tail',**options)
        self._poll_task=self._scheduler.after(16,self._flush_events)

    def _sync_input_style(self):
        api=self._api
        if not api.IsWindow(self._hwnd):return False
        owner_transparent=api.GetWindowLongPtrW(self._owner,-20)&0x20
        style=api.GetWindowLongPtrW(self._hwnd,-20)
        if style&0x20!=owner_transparent:
            _checked(api.SetWindowLongPtrW(self._hwnd,-20,(style&~0x20)|owner_transparent))
        return bool(owner_transparent)

    def _owner_wheel(self,event):
        # Windows can deliver a wheel to the focused owner instead of the
        # no-activate artwork HWND. Route only this viewport; native/Canvas
        # events already handled by the header must never be delivered twice.
        self._wheel_counts['tk']+=1
        if self._closed or not self._visible or not event.delta:return
        source=str(event.widget);target=str(self._widget)
        if source==target or source.startswith(target+'.'):return
        if event.widget.winfo_toplevel()!=self._widget.winfo_toplevel():return
        if self._api.GetWindowLongPtrW(self._owner,-20)&0x20:return
        x=event.x_root-self._origin[0];y=event.y_root-self._origin[1]
        if 0<=x<self._size[0] and 0<=y<self._size[1]:
            self._wheel_counts['routed']+=1
            self._widget.event_generate('<MouseWheel>',x=x,y=y,
                rootx=event.x_root,rooty=event.y_root,delta=event.delta,
                state=event.state,when='tail')
            return 'break'

    def show(self,image,x,y,opacity=1.0):
        if self._closed or not self._api.IsWindow(self._widget_hwnd):
            raise RuntimeError('ArtistLayer has been closed')
        if not math.isfinite(opacity) or not 0<=opacity<=1:
            raise ValueError('Artist artwork opacity must be between zero and one')
        width,height=image.size
        if width<1 or height<1:
            self.hide();return
        pixels=premultiplied_bgra(image)
        api=self._api
        self._size=(width,height)
        self._origin=(round(x),round(y))
        owner=api.GetAncestor(self._widget_hwnd,2)
        if owner!=self._owner or not api.IsWindow(self._hwnd):
            if api.IsWindow(self._hwnd):_checked(api.DestroyWindow(self._hwnd))
            self._create_window()
        self._attach_owner_hooks()
        self._sync_input_style()
        screen=_checked(api.GetDC(None))
        memory=bitmap=previous=None
        try:
            memory=_checked(api.CreateCompatibleDC(screen))
            info=_BitmapInfo()
            info.header=_BitmapHeader(ctypes.sizeof(_BitmapHeader),width,-height,1,32,
                                      0,len(pixels),0,0,0,0)
            address=ctypes.c_void_p()
            bitmap=_checked(api.CreateDIBSection(screen,ctypes.byref(info),0,
                                                 ctypes.byref(address),None,0))
            previous=_checked(api.SelectObject(memory,bitmap))
            ctypes.memmove(address,pixels,len(pixels))
            destination=wintypes.POINT(round(x),round(y))
            size=wintypes.SIZE(width,height)
            source=wintypes.POINT(0,0)
            blend=_BlendFunction(0,0,round(opacity*255),1)
            _checked(api.UpdateLayeredWindow(self._hwnd,screen,ctypes.byref(destination),
                ctypes.byref(size),memory,ctypes.byref(source),0,ctypes.byref(blend),2))
        finally:
            if previous:api.SelectObject(memory,previous)
            if bitmap:api.DeleteObject(bitmap)
            if memory:api.DeleteDC(memory)
            api.ReleaseDC(None,screen)
        # Insert directly above the owner, below its menus and other owned popups.
        # Using HWND_TOPMOST here would incorrectly cover those popups.
        above=api.GetWindow(self._owner,3)  # GW_HWNDPREV
        flags=0x0001|0x0002|0x0010|0x0040|0x0200  # no size/move/activate/owner-z; show
        if above==self._hwnd:flags|=0x0004  # already correctly ordered
        _checked(api.SetWindowPos(self._hwnd,above,0,0,0,0,flags))
        self._visible=True
        if self._poll_task is None:self._poll_task=self._scheduler.after(16,self._flush_events)

    def hide(self):
        self._visible=False
        if self._poll_task is not None:
            self._scheduler.after_cancel(self._poll_task)
            self._poll_task=None
        if self._hwnd and self._api.IsWindow(self._hwnd):
            self._api.ShowWindow(self._hwnd,0)
        if self._inside and self._api.IsWindow(self._widget_hwnd):
            self._widget.event_generate('<Leave>',when='tail')
        self._inside=False;self._tracking=False
        self._events.clear()

    def close(self):
        for hwnd in tuple(self._hooked):
            if self._api.IsWindow(hwnd):
                _checked(self._api.RemoveWindowSubclass(hwnd,self._owner_callback,id(self)))
        self._hooked.clear()
        if self._wheel_binding in (self._scheduler._tclCommands or ()):
            self._scheduler.unbind('<MouseWheel>',self._wheel_binding)
        self._wheel_binding=None
        if self._poll_task is not None:
            self._scheduler.after_cancel(self._poll_task)
            self._poll_task=None
        if self._hwnd and self._api.IsWindow(self._hwnd):
            _checked(self._api.DestroyWindow(self._hwnd))
        self._hwnd=None
        self._closed=True
        self._visible=False
        self._events.clear()
