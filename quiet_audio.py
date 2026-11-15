"""Isolate Spotify on one approved, non-default playback device.

Spotify's session stays unmuted because muting it also destroys capture audio.
Only the dedicated output is muted. Its prior mute and Spotify's prior per-app
routes are journaled before mutation and restored after collection stops.
"""
import os
from pathlib import Path
import time

from spotify_audio_route import AudioRouting, endpoint_preference, process_identity, spotify_processes
from storage import atomic_json, read_json


APPROVED_ENDPOINT_NAME='스피커(Virtual Desktop Audio)'


def _process_identity(pid):
    value=process_identity(pid)
    return value['created'] if value else None


class QuietAudio:
    spotify_only=True

    def __init__(self,state_path,backend_factory=None,endpoint_id=None):
        self.path=Path(state_path)
        self.backend_factory=backend_factory or AudioRouting
        self.endpoint_id=endpoint_id
        self.endpoint_name=''
        self.enabled=False
        self._state=None

    def _save(self):
        atomic_json(self.path,{'version':2,'pid':os.getpid(),'created':_process_identity(os.getpid()),**self._state})

    def _load(self):
        value=read_json(self.path,None)
        if value is None:return None
        def integer(number):return type(number) is int and number>0
        valid=(isinstance(value,dict) and value.get('version')==2 and integer(value.get('pid'))
               and integer(value.get('created')) and isinstance(value.get('endpoint'),dict)
               and isinstance(value.get('routes'),dict))
        if valid:
            endpoint=value['endpoint']
            valid=(isinstance(endpoint.get('id'),str) and bool(endpoint['id'])
                   and isinstance(endpoint.get('name'),str) and isinstance(endpoint.get('muted'),bool)
                   and isinstance(endpoint.get('restore_pending'),bool))
            for key,route in value['routes'].items():
                if (not isinstance(key,str) or not isinstance(route,dict) or not integer(route.get('pid'))
                        or not integer(route.get('created')) or not isinstance(route.get('path'),str)
                        or route['path'].casefold()!=key or not isinstance(route.get('roles'),list)
                        or len(route['roles'])!=3 or any(not isinstance(item,str) for item in route['roles'])):
                    valid=False;break
        if not valid:raise ValueError('Spotify 전용 출력 복원 기록을 확인할 수 없습니다. 원본은 보존했습니다.')
        if value['pid']!=os.getpid() and _process_identity(value['pid'])==value['created']:
            raise ValueError('다른 프로그램 창에서 조용한 습득이 실행 중입니다. 해당 창에서 먼저 중지하세요.')
        return {'endpoint':value['endpoint'],'routes':value['routes']}

    def _select(self,audio):
        choices=[item for item in audio.outputs() if
                 (item['id']==self.endpoint_id if self.endpoint_id else item['name']==APPROVED_ENDPOINT_NAME)]
        if len(choices)!=1:
            raise ValueError('전용 Virtual Desktop Audio 출력 장치를 하나로 확인하지 못했습니다. 다른 소리는 변경하지 않았습니다.')
        return choices[0]

    @staticmethod
    def _guard(audio,target,processes,sessions,capture_pids=()):
        if target in audio.defaults().values():
            raise ValueError('전용 출력이 Windows 기본 장치로 지정되어 있습니다. 다른 앱 소리를 보호하기 위해 수집을 중지합니다.')
        # Windows keeps a system-sounds session on each output, including the
        # reserved non-default sink. Its temporary activity is not another app
        # taking this sink; the real default outputs are never muted here.
        conflicts=[item for item in sessions if item['endpoint']==target and item['active']
                   and item.get('system') is not True
                   and (item['pid'] not in processes and item['pid'] not in capture_pids or item['shared'])]
        if conflicts:
            names=[]
            for item in conflicts:
                try:owner=process_identity(item['pid']) if item['pid'] else None
                except (OSError,ValueError):owner=None
                name=owner['path'].replace('\\','/').rsplit('/',1)[-1] if owner else '확인되지 않은 소리 세션'
                label=f"{name} (PID {item['pid']})"+(' · 공유 세션' if item['shared'] else '')
                if label not in names:names.append(label)
            raise ValueError('전용 출력 장치를 다른 앱이 사용 중입니다: '+', '.join(names)+
                             '. 다른 앱 소리를 보호하기 위해 수집을 중지합니다.')

    def _route(self,audio,processes,sessions):
        pids=sorted({item['pid'] for item in sessions if item['pid'] in processes and not item['shared']})
        if not pids:raise ValueError('Spotify 소리 세션이 없습니다. Spotify에서 곡을 한 번 재생한 뒤 다시 시작하세요.')
        additions={}
        for pid in pids:
            process=processes[pid];key=process['path'].casefold()
            if key not in self._state['routes'] and key not in additions:
                additions[key]={**process,'roles':[audio.get_route(pid,role) for role in range(3)]}
        if additions:
            previous=self._state['routes'];self._state['routes']={**previous,**additions}
            try:self._save()
            except Exception:self._state['routes']=previous;raise
        target=self._state['endpoint']['id']
        if not audio.get_mute(target):audio.set_mute(target,True)
        preference=endpoint_preference(target)
        for pid in pids:
            for role in range(3):
                if audio.get_route(pid,role).casefold()!=preference.casefold():audio.set_route(pid,role,preference)
        deadline=time.monotonic()+3
        while True:
            remaining=[item for item in audio.sessions() if item['pid'] in processes and
                       item['active'] and item['endpoint']!=target]
            if not remaining:return
            if time.monotonic()>=deadline:raise ValueError('Spotify의 전용 출력 이동을 확인하지 못했습니다. 수집을 시작하지 않습니다.')
            time.sleep(.1)

    def enable(self):
        if self.enabled:return
        self.restore()
        with self.backend_factory() as audio:
            processes=spotify_processes();sessions=audio.sessions();target=self._select(audio)
            self._guard(audio,target['id'],processes,sessions)
            self.endpoint_name=target['name']
            self._state={'endpoint':{**target,'muted':audio.get_mute(target['id']),'restore_pending':True},'routes':{}}
            try:self._save()
            except Exception:self._state=None;raise
            try:
                self._route(audio,processes,sessions);self.enabled=True
            except Exception as error:
                try:self.restore()
                except Exception as recovery:raise ValueError(f'{error} 복원 확인 필요: {recovery}') from error
                raise

    def maintain(self,capture_processes=()):
        if not self.enabled:return
        # Process loopback owns a session too. Identify our live workers before
        # taking the session snapshot: a worker can exit during enumeration.
        # Retain Popen handles for this call so their PIDs cannot be reused.
        capture_processes=tuple(capture_processes)
        capture_pids={process.pid for process in capture_processes if process.poll() is None}
        with self.backend_factory() as audio:
            processes=spotify_processes();sessions=audio.sessions()
            self._guard(audio,self._state['endpoint']['id'],processes,sessions,capture_pids)
            self._route(audio,processes,sessions)

    def restore(self):
        self.enabled=False
        if self._state is None:self._state=self._load()
        if self._state is None:return
        failures=[]
        with self.backend_factory() as audio:
            if self._state['routes']:
                try:processes=spotify_processes()
                except (OSError,ValueError):processes={}
                for key,saved in list(self._state['routes'].items()):
                    # Preferences are app-wide. A verified new Spotify process
                    # with the same executable restores them after a restart.
                    candidates=sorted((process for process in processes.values() if process['path'].casefold()==key),
                                      key=lambda item:item['pid']!=saved['pid'])
                    restored=False
                    for process in candidates:
                        try:
                            for role in range(3):audio.get_route(process['pid'],role)
                        except (OSError,ValueError):continue  # A non-audio helper PID.
                        try:
                            for role,value in enumerate(saved['roles']):audio.set_route(process['pid'],role,value)
                            restored=True
                        except (OSError,ValueError) as error:failures.append(str(error))
                        break
                    if restored:del self._state['routes'][key]
                    else:failures.append('Spotify를 다시 실행한 뒤 원래 출력 경로 복원을 재시도하세요.')
            endpoint=self._state['endpoint']
            if endpoint['restore_pending']:
                try:audio.set_mute(endpoint['id'],endpoint['muted'])
                except (OSError,ValueError) as error:failures.append(str(error))
                else:endpoint['restore_pending']=False
        if self._state['routes'] or self._state['endpoint']['restore_pending']:
            self._save()
            raise ValueError('Spotify 전용 출력 복원을 마치지 못했습니다. '+' / '.join(failures))
        if self.path.exists():self.path.unlink()
        self._state=None
