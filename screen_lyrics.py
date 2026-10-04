"""Read visible Spotify lyric text locally with Windows OCR; never save images/text."""
import asyncio
import ctypes
from ctypes import wintypes
import re
import threading
import time

from lyrics import normalized


def lyric_rows(rows,width,height,excluded=()):
    excluded={normalized(x) for x in excluded}
    values=[]
    for text,x,y,w,h in rows:
        key=normalized(text)
        if len(key)<6 or key in excluded or not re.search(r'[\u3040-\u30ff]',text):continue
        if h<16 or x<0 or y<0 or x+w>width+2 or y+h>height+2:continue
        if key not in {normalized(v) for v in values}:values.append(text)
    return tuple(values[:20])


def matching_lines(references,originals):
    originals=[normalized(x) for x in originals]
    matches={normalized(ref) for ref in references if len(normalized(ref))>=6 and
             any(normalized(ref) in line or (len(line)>=6 and line in normalized(ref)) for line in originals)}
    return len(matches)>=2


def capture_spotify():
    """Capture only a foreground Spotify window's central lyrics area into RAM."""
    user=ctypes.WinDLL('user32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    user.GetForegroundWindow.restype=wintypes.HWND
    hwnd=user.GetForegroundWindow()
    user.GetWindowThreadProcessId.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.DWORD))
    pid=wintypes.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
    kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD);kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
    handle=kernel.OpenProcess(0x1000,False,pid.value)
    if not handle:return None
    try:
        path=ctypes.create_unicode_buffer(32768);size=wintypes.DWORD(len(path))
        kernel.QueryFullProcessImageNameW.argtypes=(wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD))
        if not kernel.QueryFullProcessImageNameW(handle,0,path,ctypes.byref(size)) or path.value.lower().split('\\')[-1]!='spotify.exe':return None
    finally:kernel.CloseHandle(handle)
    rect=wintypes.RECT();user.GetWindowRect.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.RECT))
    if not user.GetWindowRect(hwnd,ctypes.byref(rect)):return None
    width,height=rect.right-rect.left,rect.bottom-rect.top
    if not 600<=width<=5000 or not 350<=height<=4000:return None
    gdi=ctypes.WinDLL('gdi32',use_last_error=True)
    user.GetWindowDC.argtypes=(wintypes.HWND,);user.GetWindowDC.restype=wintypes.HDC
    user.ReleaseDC.argtypes=(wintypes.HWND,wintypes.HDC)
    gdi.CreateCompatibleDC.argtypes=(wintypes.HDC,);gdi.CreateCompatibleDC.restype=wintypes.HDC
    gdi.DeleteDC.argtypes=(wintypes.HDC,)
    gdi.CreateDIBSection.argtypes=(wintypes.HDC,ctypes.c_void_p,wintypes.UINT,ctypes.POINTER(ctypes.c_void_p),wintypes.HANDLE,wintypes.DWORD)
    gdi.CreateDIBSection.restype=wintypes.HBITMAP
    gdi.SelectObject.argtypes=(wintypes.HDC,wintypes.HANDLE);gdi.SelectObject.restype=wintypes.HANDLE
    gdi.DeleteObject.argtypes=(wintypes.HANDLE,)
    user.PrintWindow.argtypes=(wintypes.HWND,wintypes.HDC,wintypes.UINT)
    class Header(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('width',wintypes.LONG),('height',wintypes.LONG),('planes',wintypes.WORD),('bits',wintypes.WORD),('compression',wintypes.DWORD),('image_size',wintypes.DWORD),('xppm',wintypes.LONG),('yppm',wintypes.LONG),('used',wintypes.DWORD),('important',wintypes.DWORD)]
    header=Header(ctypes.sizeof(Header),width,-height,1,32,0,width*height*4,0,0,0,0)
    dc=user.GetWindowDC(hwnd);memory=gdi.CreateCompatibleDC(dc);bits=ctypes.c_void_p();bitmap=None;old=None
    try:
        bitmap=gdi.CreateDIBSection(dc,ctypes.byref(header),0,ctypes.byref(bits),None,0)
        if not bitmap:raise OSError('Spotify 화면 메모리 생성 실패')
        old=gdi.SelectObject(memory,bitmap)
        if not user.PrintWindow(hwnd,memory,2):return None
        if user.GetForegroundWindow()!=hwnd:return None
        left,right=int(width*.31),int(width*.78);top,bottom=int(height*.13),int(height*.88)
        data=b''.join(ctypes.string_at(bits.value+(y*width+left)*4,(right-left)*4) for y in range(top,bottom))
        return right-left,bottom-top,data
    finally:
        if old:gdi.SelectObject(memory,old)
        if bitmap:gdi.DeleteObject(bitmap)
        if memory:gdi.DeleteDC(memory)
        if dc:user.ReleaseDC(hwnd,dc)


def accessible_rows(texts):
    result=[];complete=False
    for text in texts:
        if re.search(r'가사 제공|lyrics provided by|歌詞提供',text,re.I):complete=True;break
        if text.strip()=='♪':continue
        if text and len(text)<300 and not re.search(r'[가-힣]',text) and re.search(r'[\u3040-\u30ff\u3400-\u9fffA-Za-z]',text):result.append(text)
    return tuple(result) if complete and len(result)>=2 else ()


UNSYNCED=re.compile(r'가사가 아직 동기화되지 않았습니다\.?|these lyrics (?:aren.t|are not) synced yet\.?',re.I)


def static_rows(text):
    """Only call with the complete TextRange of the lyrics main region."""
    if not isinstance(text,str) or len(text)>200000:return ()
    rows=[row.strip() for row in text.replace('\r','\n').split('\n') if row.strip()]
    if not rows or not UNSYNCED.fullmatch(rows[0]):return ()
    result=[]
    for row in rows[1:]:
        if re.search(r'가사 제공|lyrics provided by|歌詞提供',row,re.I):break
        if row in ('♪','\ufffc'):continue
        if re.search(r'[가-힣]',row) or len(row)>300:return ()
        if re.search(r'[\u3040-\u30ff\u3400-\u9fffA-Za-z]',row):result.append(row)
    return tuple(result) if 2<=len(result)<=600 else ()


def process_is_spotify(pid):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD);kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=(wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:return False
    try:
        path=ctypes.create_unicode_buffer(32768);size=wintypes.DWORD(len(path))
        return bool(kernel.QueryFullProcessImageNameW(handle,0,path,ctypes.byref(size)) and path.value.lower().split('\\')[-1]=='spotify.exe')
    finally:kernel.CloseHandle(handle)


def highlighted_index(colors):
    # Spotify's active line is neutral white; future/past lines use a dimmer tint.
    bright=[]
    for index,color in enumerate(colors):
        if not isinstance(color,int) or isinstance(color,bool):continue
        rgb=(color&255,(color>>8)&255,(color>>16)&255)
        if min(rgb)>=225 and max(rgb)-min(rgb)<=25:bright.append(index)
    return bright[0] if len(bright)==1 else None


def scan_highlight(handles,attribute,previous=None,full=False):
    def read(index):
        try:return handles[index].GetAttributeValue(attribute) if handles[index] else None
        except Exception:return None
    sampled={}
    if previous is not None and not full:
        nearby=list(range(max(0,previous-1),min(len(handles),previous+3)))
        sampled={index:read(index) for index in nearby}
        found=highlighted_index(list(sampled.values()))
        if found is not None:return nearby[found],True
    colors=[sampled[index] if index in sampled else read(index) for index in range(len(handles))]
    return highlighted_index(colors),any(isinstance(color,int) for color in colors)


def window_minimized(handle):
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.IsIconic.argtypes=(wintypes.HWND,);user.IsIconic.restype=wintypes.BOOL
    return bool(handle and user.IsIconic(handle))


class BackgroundHighlight:
    """Allow background UIA updates, but stop refreshing an unchanged sample forever."""
    def __init__(self):
        self.index=None;self.position=None

    def accepts(self,index,position,minimized):
        if not minimized:
            self.index=index;self.position=None
            return index is not None
        if self.position is None or (index is not None and index!=self.index):
            self.index=index;self.position=position
        # Playback time does not advance while paused. A change immediately resumes
        # native tracking. Eight seconds is a fallback heuristic, not proof of a freeze.
        return index is not None and abs(position-self.position)<8


def lyric_regions(window,auto,started):
    for count,(control,depth) in enumerate(auto.WalkControl(window,maxDepth=30)):
        if count>1000 or time.monotonic()-started>3:break
        try:
            role=control.GetPropertyValue(auto.PropertyId.AriaRoleProperty)
            if role=='main':
                yield control,False
            elif control.ControlType==auto.ControlType.TextControl and re.search(r'가사 제공|lyrics provided by|歌詞提供',control.Name,re.I):
                # Fullscreen lyrics have no main landmark. Start at their footer
                # and use the smallest local group that contains the lyric rows.
                ancestor=control
                for _ in range(8):
                    ancestor=ancestor.GetParentControl()
                    if not ancestor or ancestor.GetPropertyValue(auto.PropertyId.AriaRoleProperty) in ('document','main'):break
                    yield ancestor,True
        except Exception:continue  # Spotify may replace an element mid-walk.


def accessible_spotify_lyrics(playback,auto,on_window=None):
    for window in auto.GetRootControl().GetChildren():
        if normalized(playback.title) not in normalized(window.Name) or not process_is_spotify(window.ProcessId):continue
        handle=window.NativeWindowHandle
        if on_window:on_window(handle)
        started=time.monotonic()
        for control,fullscreen in lyric_regions(window,auto,started):
            texts=[];text_controls=[];navigation=False
            for index,(child,_) in enumerate(auto.WalkControl(control,maxDepth=15)):
                if index>800 or time.monotonic()-started>4:break
                if fullscreen and child.ControlType in (auto.ControlType.ButtonControl,auto.ControlType.HyperlinkControl,auto.ControlType.SliderControl):
                    navigation=True;break
                if child.ControlType==auto.ControlType.TextControl:texts.append(child.Name);text_controls.append(child)
            originals=accessible_rows(texts)
            unsynced=any(UNSYNCED.fullmatch(text.strip()) for text in texts)
            if navigation or (not originals and not unsynced):continue
            pattern=None;ancestor=control
            for _ in range(15):
                if not ancestor:break
                try:pattern=ancestor.GetPattern(auto.PatternId.TextPattern)
                except Exception:pattern=None
                if pattern:break
                ancestor=ancestor.GetParentControl()
            # Static lyrics expose only visible rows as child controls, and have
            # no attribution footer. The full main TextRange contains the rest.
            if pattern and unsynced:
                try:
                    region=auto.TextRange(textRange=pattern.pattern.RangeFromChild(control.Element))
                    originals=static_rows(region.GetText(200001))
                except Exception:originals=()
                if originals:return originals,None  # No synchronized highlight.
            if not originals:continue
            handles=[];cursor=0
            for child,text in zip(text_controls,texts):
                if cursor>=len(originals):break
                if text!=originals[cursor]:continue
                text_range=None
                if pattern:
                    try:text_range=auto.TextRange(textRange=pattern.pattern.RangeFromChild(child.Element))
                    except Exception:
                        if originals.count(text)==1:
                            try:text_range=pattern.DocumentRange.FindText(text,False,False)
                            except Exception:pass
                handles.append(text_range);cursor+=1
            return originals,handles
    return (),[]


class ScreenLyricsMonitor:
    def __init__(self,messages):
        self.messages=messages;self.stop_event=threading.Event();self.playback=None;self.read_track='';self.window_handle=0
        self.needed=True;self.allow_ocr=True;self.static_track=''
        self.thread=threading.Thread(target=self.run,daemon=True,name='spotify-local-ocr')
    def start(self):self.thread.start()
    def observe(self,playback,needed=True,allow_ocr=True):
        if not self.playback or self.playback.key!=playback.key:self.read_track=''
        self.playback=playback
        self.needed=needed
        self.allow_ocr=allow_ocr
    def stop(self):self.stop_event.set()
    def run(self):
        try:
            import uiautomation as auto
            with auto.UIAutomationInitializerInThread():asyncio.run(self.poll(auto))
        except Exception as error:self.messages.put(('screen_status','Spotify 화면 읽기 사용 불가 ('+type(error).__name__+')'))
    async def poll(self,auto):
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.graphics.imaging import SoftwareBitmap,BitmapPixelFormat,BitmapAlphaMode
        from winrt.windows.storage.streams import DataWriter
        engine=None;ocr_initialized=False
        pending_track='';changed_at=0;active_handles=[];active_originals=();unavailable_since=None
        previous_highlight=None;last_full_scan=0;last_emitted=0
        last_accessibility_read=time.monotonic()
        background=BackgroundHighlight()
        while not self.stop_event.is_set():
            if not self.needed:
                await asyncio.sleep(.25);continue
            playback=self.playback
            if playback and playback.title:
                if playback.key!=pending_track:
                    pending_track=playback.key;changed_at=time.monotonic();active_handles=[];active_originals=();unavailable_since=None
                    previous_highlight=None;last_full_scan=0
                    last_accessibility_read=changed_at
                    background=BackgroundHighlight()
                minimized=bool(self.window_handle and window_minimized(self.window_handle))
                if playback.key==self.read_track and playback.key==self.static_track:
                    # Spotify may replace its initial unsynced view after loading.
                    if time.monotonic()-last_accessibility_read<5:
                        await asyncio.sleep(.5);continue
                    self.read_track=''
                if playback.key==self.read_track:
                    scan_started=time.monotonic()
                    full=scan_started-last_full_scan>=1
                    index,available=scan_highlight(active_handles,auto.TextAttributeId.ForegroundColorAttribute,previous_highlight,full)
                    observed_at=time.monotonic()
                    observed_position=playback.position(observed_at)
                    if full:last_full_scan=scan_started
                    fresh=background.accepts(index,playback.position(),minimized)
                    consistent=bool(self.playback and self.playback.key==playback.key
                                    and abs(self.playback.position(observed_at)-observed_position)<2)
                    if fresh and consistent:
                        if index!=previous_highlight or scan_started-last_emitted>=.5:
                            self.messages.put(('spotify_active',(playback.key,active_originals,index,
                                               observed_at,observed_position,observed_at-scan_started<=.25)));last_emitted=scan_started
                        previous_highlight=index
                    else:
                        self.messages.put(('spotify_tracking',(playback.key,'background_clock' if minimized else ('강조 구절 확인 중' if available else '강조 색상 정보 대기'))))
                    # Detached TextRanges can still return colors. Refresh them
                    # when no unique current line is found, not only on COM errors.
                    if index is not None:unavailable_since=None
                    else:
                        if unavailable_since is None:unavailable_since=time.monotonic()
                        if time.monotonic()-unavailable_since>=3:
                            self.read_track='';unavailable_since=None
                    # Account for COM read time instead of adding a fixed delay after it.
                    await asyncio.sleep(max(.025,.1-(time.monotonic()-scan_started)));continue
                if time.monotonic()-changed_at<2:
                    await asyncio.sleep(.5);continue
                try:originals,handles=accessible_spotify_lyrics(playback,auto,lambda handle:setattr(self,'window_handle',handle))
                except Exception:originals,handles=(),[]
                last_accessibility_read=time.monotonic()
                if originals:
                    if self.playback and self.playback.key==playback.key:
                        if originals!=active_originals:
                            from lyrics import Line
                            from web_lyrics import reading
                            lines=tuple(Line(text,reading(text),'') for text in originals)
                            self.messages.put(('spotify_lyrics',(playback.key,lines)))
                        self.read_track=playback.key
                        self.static_track=playback.key if handles is None else ''
                        active_handles=handles or [];active_originals=originals
                        if handles is None:self.messages.put(('spotify_tracking',(playback.key,'동기화되지 않은 Spotify 원문 · 저장 싱크 사용')))
                        previous_highlight=None;last_full_scan=0
                        background=BackgroundHighlight()
                    await asyncio.sleep(.05);continue
                if not self.allow_ocr:
                    await asyncio.sleep(1);continue
                if not ocr_initialized:
                    engine=OcrEngine.try_create_from_language(Language('ja'));ocr_initialized=True
                if engine is None:
                    await asyncio.sleep(1);continue
                captured=capture_spotify()
                if captured:
                    width,height,data=captured
                    writer=DataWriter();writer.write_bytes(data);buffer=writer.detach_buffer();writer.close()
                    bitmap=SoftwareBitmap(BitmapPixelFormat.BGRA8,width,height,BitmapAlphaMode.IGNORE)
                    try:
                        bitmap.copy_from_buffer(buffer);result=await engine.recognize_async(bitmap)
                        rows=[]
                        for line in result.lines:
                            boxes=[word.bounding_rect for word in line.words]
                            if not boxes:continue
                            x=min(b.x for b in boxes);y=min(b.y for b in boxes)
                            rows.append((line.text,x,y,max(b.x+b.width for b in boxes)-x,max(b.y+b.height for b in boxes)-y))
                        lines=lyric_rows(rows,width,height,(playback.title,playback.artist,playback.album))
                        if lines and self.playback and self.playback.key==playback.key:self.messages.put(('screen_lyrics',(playback.key,lines)))
                    finally:bitmap.close()
            await asyncio.sleep(1)
