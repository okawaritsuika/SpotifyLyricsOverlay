"""Spotify-only output routing and Core Audio inventory; no default-device setter.

The private AudioPolicyConfig ABI is the Windows 11 interface documented by
EarTrumpet's own interop declarations, not a public Microsoft compatibility API:
https://github.com/File-New-Project/EarTrumpet/blob/master/EarTrumpet/Interop/MMDeviceAPI/IAudioPolicyConfigFactoryVariantFor21H2.cs
https://github.com/File-New-Project/EarTrumpet/blob/master/EarTrumpet/DataModel/WindowsAudio/Internal/AudioPolicyConfigService.cs
All COM interfaces are created, used, and released inside one caller's context.
"""
import ctypes as C
from contextlib import closing, contextmanager
import sys
import uuid

from process_audio import DWORD, GUID, HRESULT, PTR, ProcessEntry, _method, _release, spotify_process_id


def _guid(value):return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)
_CLASS_ENUMERATOR=_guid('bcde0395-e52f-467c-8e3d-c4579291692e')
_IID_ENUMERATOR=_guid('a95664d2-9614-4f35-a746-de8db63617e6')
_IID_VOLUME=_guid('5cdf2c82-841e-4546-9722-0cf74078229a')
_IID_MANAGER=_guid('77aa99a0-1bd6-484f-8bc7-2c654c9a9b6f')
_IID_CONTROL=_guid('bfb7ff88-7239-4fc9-8fa2-07c950be9c6d')
_IID_SESSION_VOLUME=_guid('87ce5498-68d6-44e5-9215-6da47ef883d8')
_IID_POLICY=_guid('ab3d4648-e242-459f-b02f-541c70306324')
_POLICY_PREFIX='\\\\?\\SWD#MMDEVAPI#'
_POLICY_SUFFIX='#{e6327cad-dcec-4949-ae8a-991e976a79d2}'


def _check(result,action):
    if result<0:raise ValueError(f'Spotify 전용 출력 {action} 실패 (0x{result & 0xffffffff:08X}).')


def process_identity(pid):
    if sys.platform!='win32':return None
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[DWORD,C.c_int,DWORD];kernel.OpenProcess.restype=PTR
    kernel.GetProcessTimes.argtypes=[PTR,*([C.POINTER(C.c_uint64)]*4)]
    kernel.QueryFullProcessImageNameW.argtypes=[PTR,DWORD,C.c_wchar_p,C.POINTER(DWORD)]
    kernel.CloseHandle.argtypes=[PTR]
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:
        if C.get_last_error()==87:return None
        raise ValueError('Spotify 전용 출력: 이전 실행의 종료 여부를 확인하지 못했습니다.')
    try:
        times=[C.c_uint64() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*(C.byref(item) for item in times)):raise C.WinError(C.get_last_error())
        if times[1].value:return None
        name=C.create_unicode_buffer(32768);length=DWORD(len(name))
        if not kernel.QueryFullProcessImageNameW(handle,0,name,C.byref(length)):raise C.WinError(C.get_last_error())
        return {'pid':pid,'created':times[0].value,'path':name.value}
    finally:kernel.CloseHandle(handle)


def spotify_processes():
    root=spotify_process_id()
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[DWORD,DWORD];kernel.CreateToolhelp32Snapshot.restype=PTR
    kernel.Process32FirstW.argtypes=[PTR,C.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes=[PTR,C.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes=[PTR]
    snapshot=kernel.CreateToolhelp32Snapshot(2,0)
    if snapshot==PTR(-1).value:raise C.WinError(C.get_last_error())
    parents={};entry=ProcessEntry();entry.size=C.sizeof(entry)
    try:
        more=kernel.Process32FirstW(snapshot,C.byref(entry))
        while more:
            if entry.name.casefold()=='spotify.exe':parents[entry.pid]=entry.parent
            more=kernel.Process32NextW(snapshot,C.byref(entry))
    finally:kernel.CloseHandle(snapshot)
    selected={root}
    while True:
        added={pid for pid,parent in parents.items() if parent in selected}-selected
        if not added:break
        selected.update(added)
    result={}
    for pid in selected:
        value=process_identity(pid)
        if value and value['path'].replace('/','\\').rsplit('\\',1)[-1].casefold()=='spotify.exe':
            result[pid]=value
    if root not in result:raise ValueError('Spotify 프로세스가 변경되었습니다. 다시 시도하세요.')
    return result


def endpoint_preference(identifier):
    return _POLICY_PREFIX+identifier+_POLICY_SUFFIX if identifier else ''


class PropertyKey(C.Structure):
    _fields_=[('fmtid',GUID),('pid',DWORD)]


class PropertyValue(C.Structure):
    _fields_=[('vt',C.c_ushort),('reserved',C.c_ushort*3),('value',PTR),('tail',PTR)]


class AudioRouting:
    def __init__(self):
        if sys.platform!='win32':
            raise ValueError('Spotify 오디오 세션은 Windows에서 사용할 수 있습니다.')
        self.enumerator=PTR();self.policy=PTR();self.initialized=False
        self.ole=C.WinDLL('ole32');self.runtime=C.WinDLL('combase')
        self.ole.CoInitializeEx.argtypes=[PTR,DWORD];self.ole.CoInitializeEx.restype=HRESULT
        self.ole.CoCreateInstance.argtypes=[C.POINTER(GUID),PTR,DWORD,C.POINTER(GUID),C.POINTER(PTR)]
        self.ole.CoCreateInstance.restype=HRESULT;self.ole.CoTaskMemFree.argtypes=[PTR]
        self.ole.PropVariantClear.argtypes=[C.POINTER(PropertyValue)]
        self.runtime.WindowsCreateString.argtypes=[C.c_wchar_p,DWORD,C.POINTER(PTR)]
        self.runtime.WindowsCreateString.restype=HRESULT
        self.runtime.WindowsDeleteString.argtypes=[PTR]
        self.runtime.WindowsGetStringRawBuffer.argtypes=[PTR,C.POINTER(DWORD)]
        self.runtime.WindowsGetStringRawBuffer.restype=C.c_wchar_p
        self.runtime.RoGetActivationFactory.argtypes=[PTR,C.POINTER(GUID),C.POINTER(PTR)]
        self.runtime.RoGetActivationFactory.restype=HRESULT
        try:
            status=self.ole.CoInitializeEx(None,0)
            if status!=-2147417850:_check(status,'준비');self.initialized=True
            _check(self.ole.CoCreateInstance(C.byref(_CLASS_ENUMERATOR),None,1,
                C.byref(_IID_ENUMERATOR),C.byref(self.enumerator)),'장치 연결')
        except BaseException:self.close();raise

    def _activate(self,device,iid):
        pointer=PTR()
        _check(_method(device,3,C.POINTER(GUID),DWORD,PTR,C.POINTER(PTR))(
            device,C.byref(iid),23,None,C.byref(pointer)),'장치 인터페이스 연결')
        return pointer

    def _device(self,identifier):
        device=PTR()
        _check(_method(self.enumerator,5,C.c_wchar_p,C.POINTER(PTR))(
            self.enumerator,identifier,C.byref(device)),'저장된 장치 연결')
        return device

    def _id(self,device):
        pointer=PTR()
        try:
            _check(_method(device,5,C.POINTER(PTR))(device,C.byref(pointer)),'장치 식별')
            return C.wstring_at(pointer)
        finally:
            if pointer:self.ole.CoTaskMemFree(pointer)

    def outputs(self):
        collection=PTR();result=[]
        try:
            _check(_method(self.enumerator,3,C.c_int,DWORD,C.POINTER(PTR))(
                self.enumerator,0,1,C.byref(collection)),'재생 장치 목록')
            count=DWORD();_check(_method(collection,3,C.POINTER(DWORD))(collection,C.byref(count)),'장치 수 확인')
            for index in range(count.value):
                device=PTR();store=PTR();value=PropertyValue()
                try:
                    _check(_method(collection,4,DWORD,C.POINTER(PTR))(collection,index,C.byref(device)),'장치 읽기')
                    identifier=self._id(device)
                    _check(_method(device,4,DWORD,C.POINTER(PTR))(device,0,C.byref(store)),'장치 이름 연결')
                    key=PropertyKey(_guid('a45c254e-df1c-4efd-8020-67d146a850e0'),14)
                    _check(_method(store,5,C.POINTER(PropertyKey),C.POINTER(PropertyValue))(store,C.byref(key),C.byref(value)),'장치 이름')
                    name=C.wstring_at(value.value) if value.vt==31 and value.value else identifier
                    result.append({'id':identifier,'name':name})
                finally:self.ole.PropVariantClear(C.byref(value));_release(store);_release(device)
            return result
        finally:_release(collection)

    def defaults(self):
        result={}
        for role in range(3):
            device=PTR()
            try:
                _check(_method(self.enumerator,4,C.c_int,C.c_int,C.POINTER(PTR))(
                    self.enumerator,0,role,C.byref(device)),'기본 재생 장치 확인')
                result[role]=self._id(device)
            finally:_release(device)
        return result

    def get_mute(self,identifier):
        device=self._device(identifier);volume=PTR()
        try:
            volume=self._activate(device,_IID_VOLUME);value=C.c_int()
            _check(_method(volume,15,C.POINTER(C.c_int))(volume,C.byref(value)),'음소거 확인')
            return bool(value.value)
        finally:_release(volume);_release(device)

    def snapshot(self):return {item['id']:self.get_mute(item['id']) for item in self.outputs()}

    def set_mute(self,identifier,muted):
        device=self._device(identifier);volume=PTR()
        try:
            volume=self._activate(device,_IID_VOLUME)
            _check(_method(volume,14,C.c_int,PTR)(volume,bool(muted),None),'전용 장치 음소거 변경')
            actual=C.c_int();_check(_method(volume,15,C.POINTER(C.c_int))(volume,C.byref(actual)),'음소거 재확인')
            if bool(actual.value)!=bool(muted):raise ValueError('Spotify 전용 장치의 음소거 변경이 반영되지 않았습니다.')
        finally:_release(volume);_release(device)

    def _session_controls(self):
        for output in self.outputs():
            device=self._device(output['id']);manager=PTR();enumerator=PTR()
            try:
                manager=self._activate(device,_IID_MANAGER)
                _check(_method(manager,5,C.POINTER(PTR))(manager,C.byref(enumerator)),'앱 세션 목록')
                count=C.c_int();_check(_method(enumerator,3,C.POINTER(C.c_int))(enumerator,C.byref(count)),'앱 세션 수')
                for index in range(count.value):
                    control=PTR();control2=PTR();instance=PTR()
                    try:
                        _check(_method(enumerator,4,C.c_int,C.POINTER(PTR))(enumerator,index,C.byref(control)),'앱 세션 읽기')
                        _check(_method(control,0,C.POINTER(GUID),C.POINTER(PTR))(
                            control,C.byref(_IID_CONTROL),C.byref(control2)),'앱 세션 식별')
                        pid=DWORD();state=C.c_int()
                        status=_method(control2,14,C.POINTER(DWORD))(control2,C.byref(pid));_check(status,'앱 프로세스 확인')
                        _check(_method(control,3,C.POINTER(C.c_int))(control,C.byref(state)),'앱 활성 상태')
                        system_status=_method(control2,15)(control2)
                        _check(system_status,'시스템 소리 구분')
                        _check(_method(control2,13,C.POINTER(PTR))(control2,C.byref(instance)),'앱 세션 ID')
                        yield ({'endpoint':output['id'],'pid':pid.value,'shared':status!=0,
                                'system':system_status==0,'active':state.value==1,'instance':C.wstring_at(instance)},
                               state.value, control)
                    finally:
                        if instance:self.ole.CoTaskMemFree(instance)
                        _release(control2);_release(control)
            finally:_release(enumerator);_release(manager);_release(device)

    def sessions(self):
        with closing(self._session_controls()) as controls:
            return [info for info, _, _ in controls]

    @contextmanager
    def spotify_volume(self):
        """Expose the active Spotify session, or the paused session on its output."""
        from screen_lyrics import process_is_spotify

        preferred_outputs = self.defaults()
        volume = PTR()
        best = (-1, -1)
        try:
            with closing(self._session_controls()) as controls:
                for info, state, control in controls:
                    if state == 2 or info['system'] or info['shared'] or not process_is_spotify(info['pid']):
                        continue
                    endpoint = info['endpoint']
                    # Prefer multimedia output when multiple persisted sessions are idle.
                    priority = (info['active'], 2 if endpoint == preferred_outputs[1]
                                else int(endpoint in preferred_outputs.values()))
                    if priority <= best:
                        continue
                    candidate = PTR()
                    _check(_method(control,0,C.POINTER(GUID),C.POINTER(PTR))(
                        control,C.byref(_IID_SESSION_VOLUME),C.byref(candidate)), '앱 음량 연결')
                    _release(volume)
                    volume, best = candidate, priority
            yield volume if volume else None
        finally:
            _release(volume)

    def _string(self,value):
        handle=PTR()
        if value:_check(self.runtime.WindowsCreateString(value,len(value),C.byref(handle)),'출력 경로 문자열')
        return handle

    def _policy(self):
        if sys.getwindowsversion().build<22000:
            raise ValueError('Spotify 전용 무음 출력은 Windows 11에서 사용할 수 있습니다.')
        if not self.policy:
            name=self._string('Windows.Media.Internal.AudioPolicyConfig')
            try:_check(self.runtime.RoGetActivationFactory(name,C.byref(_IID_POLICY),C.byref(self.policy)),'앱 출력 경로 기능 연결')
            finally:self.runtime.WindowsDeleteString(name)
        return self.policy

    def get_route(self,pid,role):
        value=PTR();policy=self._policy()
        try:
            status=_method(policy,26,DWORD,C.c_int,C.c_int,C.POINTER(PTR))(
                policy,pid,0,role,C.byref(value))
            if status==-2147023728:return ''  # ERROR_NOT_FOUND = system default.
            _check(status,'앱의 기존 출력 경로 확인')
            return self.runtime.WindowsGetStringRawBuffer(value,None) or ''
        finally:self.runtime.WindowsDeleteString(value)

    def set_route(self,pid,role,preference):
        value=self._string(preference);policy=self._policy()
        try:_check(_method(policy,25,DWORD,C.c_int,C.c_int,PTR)(policy,pid,0,role,value),'Spotify 출력 경로 변경')
        finally:self.runtime.WindowsDeleteString(value)
        if self.get_route(pid,role).casefold()!=preference.casefold():
            raise ValueError('Spotify 출력 경로 변경이 반영되지 않았습니다.')

    def close(self):
        _release(self.policy);self.policy=PTR();_release(self.enumerator);self.enumerator=PTR()
        if self.initialized:self.ole.CoUninitialize();self.initialized=False

    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
