"""Windows process-loopback capture; never fall back to the system mix.

Uses ActivateAudioInterfaceAsync with INCLUDE_TARGET_PROCESS_TREE. The capture
worker owns COM, the event and buffers; no model or extra native package needed.
"""
import ctypes as C
import queue
import sys
import threading
import time
import uuid


HRESULT=C.c_int32
DWORD=C.c_uint32
PTR=C.c_void_p
GUID=C.c_ubyte*16
IID_CLIENT=GUID.from_buffer_copy(uuid.UUID('1cb9ad4c-dbfa-4c32-b178-c2f568a703b2').bytes_le)
IID_CAPTURE=GUID.from_buffer_copy(uuid.UUID('c8adbd64-e71e-48a0-a4de-185c395cd317').bytes_le)
_SINK_IIDS={uuid.UUID(value).bytes_le for value in (
    '00000000-0000-0000-c000-000000000046',  # IUnknown
    '94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90',  # IAgileObject
    '41d949ab-9862-444a-80f6-c261334da5eb')}
_ACTIVATIONS={}


def ensure_supported():
    if sys.platform!='win32' or sys.getwindowsversion().build<20348:
        raise ValueError('Spotify 전용 음원 분석은 Windows 11 또는 빌드 20348 이상이 필요합니다.')


def _check(result,action):
    if result<0:raise ValueError(f'Spotify 소리 {action} 실패 (0x{result & 0xffffffff:08X}). 전체 PC 소리는 수집하지 않습니다.')


def _method(pointer,index,*arguments,result=HRESULT):
    table=C.cast(pointer,C.POINTER(C.POINTER(PTR))).contents
    return C.WINFUNCTYPE(result,PTR,*arguments)(table[index])


def _release(pointer):
    if pointer:_method(pointer,2,result=DWORD)(pointer)


class ProcessEntry(C.Structure):
    _fields_=[('size',DWORD),('usage',DWORD),('pid',DWORD),('heap',C.c_size_t),
              ('module',DWORD),('threads',DWORD),('parent',DWORD),('priority',C.c_int32),
              ('flags',DWORD),('name',C.c_wchar*260)]


def _spotify_process_roots():
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[DWORD,DWORD];kernel.CreateToolhelp32Snapshot.restype=PTR
    kernel.Process32FirstW.argtypes=[PTR,C.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes=[PTR,C.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes=[PTR]
    snapshot=kernel.CreateToolhelp32Snapshot(2,0)
    if snapshot==PTR(-1).value:raise C.WinError(C.get_last_error())
    processes={};entry=ProcessEntry();entry.size=C.sizeof(entry)
    try:
        more=kernel.Process32FirstW(snapshot,C.byref(entry))
        while more:
            if entry.name.casefold()=='spotify.exe':processes[entry.pid]=entry.parent
            more=kernel.Process32NextW(snapshot,C.byref(entry))
    finally:kernel.CloseHandle(snapshot)
    return [pid for pid,parent in processes.items() if parent not in processes]


def spotify_process_id():
    ensure_supported()
    roots=_spotify_process_roots()
    if len(roots)!=1:
        raise ValueError('Spotify 데스크톱 앱의 소리 연결을 확인할 수 없습니다. Spotify 앱을 실행한 뒤 다시 시도하세요.')
    return roots[0]


def _wait_for_spotify_process():
    """Spotify's process tree can be briefly missing or ambiguous during a track launch."""
    deadline=time.monotonic()+4
    while True:
        try:return spotify_process_id()
        except ValueError:
            if time.monotonic()>=deadline:raise
            time.sleep(.2)


class ActivationParams(C.Structure):
    _fields_=[('activation_type',DWORD),('pid',DWORD),('mode',DWORD)]


class Blob(C.Structure):
    _fields_=[('size',DWORD),('data',PTR)]


class PropVariant(C.Structure):
    _fields_=[('vt',C.c_ushort),('reserved',C.c_ushort*3),('blob',Blob)]


class WaveFormat(C.Structure):
    _pack_=2
    _fields_=[('tag',C.c_ushort),('channels',C.c_ushort),('rate',DWORD),
              ('bytes_per_second',DWORD),('block',C.c_ushort),('bits',C.c_ushort),('extra',C.c_ushort)]


class _Activation:
    def __init__(self,pid):
        self.done=threading.Event();self.result=0;self.client=PTR();self.references=1
        self.params=ActivationParams(1,pid,0)  # PROCESS_LOOPBACK / INCLUDE tree
        self.variant=PropVariant(65,(C.c_ushort*3)(),Blob(C.sizeof(self.params),C.addressof(self.params)))
        query=C.WINFUNCTYPE(HRESULT,PTR,PTR,C.POINTER(PTR))(self.query)
        add=C.WINFUNCTYPE(DWORD,PTR)(self.add_ref)
        release=C.WINFUNCTYPE(DWORD,PTR)(self.release)
        complete=C.WINFUNCTYPE(HRESULT,PTR,PTR)(self.complete)
        self.callbacks=(query,add,release,complete)
        self.table=(PTR*4)(*(C.cast(fn,PTR).value for fn in self.callbacks))
        self.object=(PTR*1)(C.addressof(self.table));self.pointer=C.cast(self.object,PTR)
        # Native COM retains this callback even if activation times out.
        _ACTIVATIONS[self.pointer.value]=self

    def query(self,this,iid,output):
        if C.string_at(iid,16) in _SINK_IIDS:
            output[0]=this;self.add_ref(this);return 0
        output[0]=None;return -2147467262  # E_NOINTERFACE

    def add_ref(self,this):
        self.references+=1;return self.references

    def release(self,this):
        self.references-=1
        if self.references==0:
            _release(self.client);self.client=PTR()
            _ACTIVATIONS.pop(self.pointer.value,None)
        return self.references

    def complete(self,this,operation):
        status=HRESULT()
        try:
            self.result=_method(operation,3,C.POINTER(HRESULT),C.POINTER(PTR))(operation,C.byref(status),C.byref(self.client))
            if self.result>=0:self.result=status.value
        except Exception:self.result=-2147467259
        finally:self.done.set()
        return 0


class ProcessLoopback:
    rate=44100
    channels=2

    def __init__(self,pid=None):
        ensure_supported()
        self.pid=spotify_process_id() if pid is None else pid
        self.client=PTR();self.capture=PTR();self.event=None;self.started=False
        self.co_initialized=False;self.first_packet=True
        self.kernel=C.WinDLL('kernel32',use_last_error=True)
        self.ole=C.WinDLL('ole32')
        self.kernel.CreateEventW.argtypes=[PTR,C.c_int,C.c_int,C.c_wchar_p];self.kernel.CreateEventW.restype=PTR
        self.kernel.CloseHandle.argtypes=[PTR]
        self.kernel.WaitForSingleObject.argtypes=[PTR,DWORD];self.kernel.WaitForSingleObject.restype=DWORD
        self.kernel.QueryPerformanceCounter.argtypes=[C.POINTER(C.c_int64)]
        self.kernel.QueryPerformanceFrequency.argtypes=[C.POINTER(C.c_int64)]
        self.ole.CoInitializeEx.argtypes=[PTR,DWORD];self.ole.CoInitializeEx.restype=HRESULT
        try:self._open()
        except BaseException:self.close();raise

    def _open(self):
        _check(self.ole.CoInitializeEx(None,0),'준비');self.co_initialized=True
        activation=_Activation(self.pid);operation=PTR()
        api=C.WinDLL('Mmdevapi').ActivateAudioInterfaceAsync
        api.argtypes=[C.c_wchar_p,C.POINTER(GUID),C.POINTER(PropVariant),PTR,C.POINTER(PTR)];api.restype=HRESULT
        try:
            _check(api('VAD\\Process_Loopback',C.byref(IID_CLIENT),C.byref(activation.variant),activation.pointer,C.byref(operation)),'연결')
            if not activation.done.wait(8):raise ValueError('Spotify 전용 소리 연결 시간이 초과됐습니다.')
            self.client=activation.client;activation.client=PTR();_check(activation.result,'연결')
        finally:
            _release(operation);activation.release(activation.pointer)
        fmt=WaveFormat(1,self.channels,self.rate,self.rate*self.channels*2,self.channels*2,16,0)
        flags=0x20000|0x40000|0x80000000  # LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM
        _check(_method(self.client,3,C.c_int,DWORD,C.c_int64,C.c_int64,C.POINTER(WaveFormat),PTR)(
            self.client,0,flags,0,0,C.byref(fmt),None),'형식 설정')
        _check(_method(self.client,14,C.POINTER(GUID),C.POINTER(PTR))(
            self.client,C.byref(IID_CAPTURE),C.byref(self.capture)),'버퍼 연결')
        self.event=self.kernel.CreateEventW(None,False,False,None)
        if not self.event:raise C.WinError(C.get_last_error())
        _check(_method(self.client,13,PTR)(self.client,self.event),'이벤트 설정')
        self._next=_method(self.capture,5,C.POINTER(DWORD))
        self._get=_method(self.capture,3,C.POINTER(PTR),C.POINTER(DWORD),C.POINTER(DWORD),C.POINTER(C.c_uint64),C.POINTER(C.c_uint64))
        self._free=_method(self.capture,4,DWORD)

    def start(self):
        frequency=C.c_int64();counter=C.c_int64()
        self.kernel.QueryPerformanceFrequency(C.byref(frequency))
        before=time.monotonic();self.kernel.QueryPerformanceCounter(C.byref(counter));after=time.monotonic()
        self.clock_origin=(before+after)/2-counter.value/frequency.value
        _check(_method(self.client,10)(self.client),'시작');self.started=True

    def read(self,timeout=.25):
        frames=DWORD();_check(self._next(self.capture,C.byref(frames)),'버퍼 확인')
        if not frames.value:
            waited=self.kernel.WaitForSingleObject(self.event,max(1,int(timeout*1000)))
            if waited==258:return None
            if waited!=0:raise C.WinError(C.get_last_error())
            _check(self._next(self.capture,C.byref(frames)),'버퍼 확인')
            if not frames.value:return None
        data=PTR();flags=DWORD();position=C.c_uint64();qpc=C.c_uint64()
        _check(self._get(self.capture,C.byref(data),C.byref(frames),C.byref(flags),C.byref(position),C.byref(qpc)),'읽기')
        try:
            if flags.value&1 and not self.first_packet:
                raise ValueError('Spotify 오디오 수집이 끊겼습니다. 이번 수집은 저장하지 않습니다.')
            self.first_packet=False
            size=frames.value*self.channels*2
            chunk=bytes(size) if flags.value&2 else C.string_at(data,size)
            frame_at=None if flags.value&4 or not qpc.value else self.clock_origin+qpc.value/10000000
            return chunk,frames.value,frame_at
        finally:_check(self._free(self.capture,frames),'버퍼 반환')

    def close(self):
        if self.started:
            _method(self.client,11)(self.client);self.started=False
        _release(self.capture);self.capture=PTR()
        _release(self.client);self.client=PTR()
        if self.event:self.kernel.CloseHandle(self.event);self.event=None
        if self.co_initialized:self.ole.CoUninitialize();self.co_initialized=False

    def __enter__(self):return self
    def __exit__(self,*exc):self.close()


class BufferedProcessLoopback:
    """Drain WASAPI on its owning COM thread while consumers write previews."""
    rate=ProcessLoopback.rate
    channels=ProcessLoopback.channels

    def __init__(self,pid=None,buffer_seconds=5.0):
        if not 0 < buffer_seconds <= 10:
            raise ValueError('오디오 대기 버퍼는 0초 초과, 10초 이하여야 합니다.')
        self.pid=pid;self.buffer_seconds=buffer_seconds
        self.packets=queue.Queue(maxsize=1024)
        self.queued_frames=0;self.queue_lock=threading.Lock()
        self.ready=threading.Event();self.start_requested=threading.Event()
        self.start_done=threading.Event();self.stop_requested=threading.Event()
        self.finished=threading.Event();self.error=None;self.error_reported=False;self.closed=False
        self.thread=threading.Thread(target=self._capture,name='spotify-audio-capture',daemon=True)
        self.thread.start()
        if not self.ready.wait(12):
            self.close()
            raise ValueError('Spotify 전용 수집 스레드 준비 시간이 초과됐습니다.')
        if self.error is not None:
            self.close()
            self._raise_error()

    def _capture(self):
        try:
            # COM initialization, interface calls and release stay on one
            # thread. Creating the wrapper does not start audio collection.
            pid=self.pid if self.pid is not None else _wait_for_spotify_process()
            with ProcessLoopback(pid) as stream:
                self.rate,self.channels=stream.rate,stream.channels
                self.ready.set()
                self.start_requested.wait()
                if self.stop_requested.is_set():return
                stream.start();self.start_done.set()
                while not self.stop_requested.is_set():
                    packet=stream.read(timeout=.1)
                    if packet is None:continue
                    if self.stop_requested.is_set():break
                    frames=packet[1]
                    with self.queue_lock:
                        if self.queued_frames+frames > self.rate*self.buffer_seconds or self.packets.full():
                            raise ValueError('오디오 처리 대기 버퍼가 가득 차 수집을 중단했습니다. 이번 수집은 저장하지 않습니다.')
                        self.queued_frames+=frames
                        self.packets.put_nowait(packet)
        except Exception as error:self.error=error
        finally:
            self.ready.set();self.start_done.set();self.finished.set()

    def start(self):
        if self.closed:raise ValueError('이미 닫은 Spotify 소리 수집입니다.')
        self.start_requested.set()
        if not self.start_done.wait(10):
            self.close()
            raise ValueError('Spotify 전용 소리 수집 시작 시간이 초과됐습니다.')
        self._raise_error()

    def _raise_error(self):
        if self.error is not None:
            self.error_reported=True
            raise self.error

    def read(self,timeout=.25):
        self._raise_error()
        if self.closed:return None
        try:packet=self.packets.get(timeout=timeout)
        except queue.Empty:
            self._raise_error()
            return None
        with self.queue_lock:self.queued_frames-=packet[1]
        self._raise_error()
        return packet

    def close(self):
        self.closed=True
        self.stop_requested.set();self.start_requested.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise ValueError('Spotify 소리 수집 스레드가 종료되지 않았습니다.')
        with self.queue_lock:
            while True:
                try:self.packets.get_nowait()
                except queue.Empty:break
            self.queued_frames=0
        # A last read may race the reader thread's failure or COM release.
        # Surface an as-yet unreported failure before callers finalize audio.
        if not self.error_reported:self._raise_error()

    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
