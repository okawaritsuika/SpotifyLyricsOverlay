"""Optional short-lived WebView reader; all page text crosses memory pipes only."""
import importlib.util
import json
from pathlib import Path
import subprocess
import threading
import time
from web_lyrics import validate_url
from app_paths import frozen, webview2_available, worker_command, worker_environment


def available(base):
    if frozen():
        return webview2_available()
    return (Path(base)/'.browser_deps'/'webview').is_dir() or importlib.util.find_spec('webview') is not None


class BrowserJob:
    def __init__(self,base,state,messages,visible=False,selected_url='',profile=None):
        self.key=state['token']+':'+state['track']
        self.state=state
        self.cancelled=threading.Event()
        self.messages=messages
        environment=worker_environment(base,browser=True)
        self.process=subprocess.Popen(worker_command(base,'browser_worker'),
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,encoding='utf-8',env=environment,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0))
        self.process.stdin.write(json.dumps({'state':state,'visible':visible,'selected_url':selected_url,
                                            'profile':str(profile or Path(base)/'data/browser_profile')},ensure_ascii=False)+'\n')
        self.process.stdin.flush()
        self.thread=threading.Thread(target=self._read,daemon=True,name='internal-lyric-browser')
        self.thread.start()

    def _read(self):
        terminal=False
        try:
            for line in self.process.stdout:
                if self.cancelled.is_set():continue
                try:event=json.loads(line)
                except ValueError:continue
                if not isinstance(event,dict):continue
                if event.get('kind') in ('lyrics','choices','blocked','error'):terminal=True
                self.messages.put(('internal_reader',(self,event)))
        finally:
            self.process.stdout.close()
            try:self.process.stdin.close()
            except OSError:pass
            try:code=self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.terminate();code=self.process.wait(timeout=3)
            if not terminal and not self.cancelled.is_set():
                self.messages.put(('internal_reader',(self,{'kind':'error','text':f'내부 브라우저 종료 (코드 {code}) · 연결 설정에서 다시 시도하세요.'})))
            self.messages.put(('internal_reader',(self,{'kind':'finished'})))

    def cancel(self):
        if self.cancelled.is_set():return
        self.cancelled.set()
        try:
            self.process.stdin.write('{"action":"close"}\n');self.process.stdin.flush()
        except (OSError,ValueError):pass
        def cleanup():
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=3)
        threading.Thread(target=cleanup,daemon=True,name='internal-browser-cleanup').start()

    def close(self):
        self.cancel();self.thread.join(timeout=4)


class InternalReader:
    def __init__(self,base,bridge,messages,job_factory=BrowserJob,profile=None):
        self.base,self.bridge,self.messages=Path(base),bridge,messages
        self.job_factory=job_factory
        self.profile=profile
        self.enabled=False;self.job=None;self.last_key='';self.awaiting=False
        self.status='내부 브라우저 꺼짐';self.choices=[];self.last_url='';self.retry_references=False
        self.started_with_references=False
        self.timeout_retry=None;self.timeout_retries=0;self.connection_options=(False,'')
        self.search_context=None
        self.translation_search_status=''

    def set_enabled(self,value):
        self.timeout_retry=None;self.timeout_retries=0
        self.search_context=None
        if value and not available(self.base):
            self.enabled=False
            if self.job:self.job.cancel();self.job=None
            self.status=('내부 브라우저에 Microsoft Edge WebView2 Runtime이 필요합니다. 가사 연결 → Spotify 연결에서 설치 페이지를 열 수 있습니다.' if frozen()
                         else '내부 브라우저 설치가 필요합니다. setup-browser.ps1을 실행하세요.')
            raise ValueError(self.status)
        self.enabled=value
        if self.job:self.job.cancel();self.job=None
        self.last_key='';self.awaiting=False;self.choices=[]
        self.status='내부 브라우저 · 다음 재생 곡 연결 대기' if value else 'Chrome 확장 연결 사용'

    def observe(self,state):
        if not self.enabled:return
        key=state['token']+':'+state['track']
        if key==self.last_key:
            if self.awaiting and state.get('receipt',{}).get('status')=='displayed':
                self.awaiting=False
                receipt=state.get('receipt',{})
                translated=receipt.get('translated_lines')
                total=receipt.get('line_count',0)
                if translated==0:self.status='원문 연결 · 한국어 번역 미확인 · 다른 출처 확인 필요'
                elif translated is not None and translated<total:
                    self.status=f'가사 연결 · 한국어 번역 {translated}/{total}줄 · 나머지 미확인'
                else:self.status='내부 연동 완료 · 브라우저 종료 · 가사 표시 중'
                if translated is not None and translated<total:
                    if self.translation_search_status=='blocked':self.status+=' · 추가 출처의 원본 확인 필요'
                    elif self.translation_search_status=='error':self.status+=' · 추가 출처 확인 중 오류'
                if self.job:self.job.cancel();self.job=None
            elif self.awaiting and state.get('receipt',{}).get('status')=='display_failed':
                self.awaiting=False;self.status='가사창 표시 실패 · 연결 설정에서 다시 시도하세요.'
                if self.job:self.job.cancel();self.job=None
            elif self.retry_references and len(state.get('reference_lines',[]))>=2:
                self.retry_references=False
                visible,selected_url=self.connection_options
                self.connect(state,visible=visible,selected_url=selected_url,_automatic=True)
            elif (self.timeout_retry and self.timeout_retry[0]==key and self.job is None
                  and state.get('playing') and time.monotonic()>=self.timeout_retry[1]):
                visible,selected_url=self.connection_options
                self.connect(state,visible=visible,selected_url=selected_url,_automatic=True)
            return
        self.timeout_retry=None;self.timeout_retries=0
        self.search_context=None
        if self.job:self.job.cancel();self.job=None
        self.awaiting=False;self.choices=[]
        if state.get('title') and state.get('playing'):self.connect(state)

    def connect(self,state=None,visible=False,selected_url='',_automatic=False):
        if not self.enabled:return
        state=state or self.bridge.snapshot()
        if not state.get('title'):raise ValueError('Spotify에서 곡을 먼저 선택하세요.')
        if selected_url:validate_url(selected_url)
        key=state['token']+':'+state['track']
        if _automatic and self.search_context and self.search_context[0]==key:
            state={**state,**self.search_context[1]}
        else:
            # Keep a chosen search source through retries without approving its lyrics.
            self.search_context=(key,{name:state.get(name,False if name=='document_learned' else '')
                                      for name in ('document_url','document_learned','suggested_document_url')})
        self.timeout_retry=None
        if not _automatic:self.timeout_retries=0
        self.connection_options=(visible,selected_url)
        if self.job:self.job.cancel();self.job=None
        self.last_key=key
        self.awaiting=False;self.choices=[];self.retry_references=False
        self.translation_search_status=''
        self.started_with_references=len(state.get('reference_lines',[]))>=2
        self.status='내부 브라우저에서 현재 곡 문서 확인 중'
        try:self.job=self.job_factory(self.base,state,self.messages,visible=visible,selected_url=selected_url,profile=self.profile)
        except OSError:
            self.status='내부 브라우저 실행 실패 · 연결 설정에서 다시 시도하세요.'
            raise

    def receive(self,job,event):
        if job is not self.job or job.cancelled.is_set() or not self.enabled:return
        state=self.bridge.snapshot()
        if job.key!=state['token']+':'+state['track']:return
        kind=event.get('kind')
        if kind=='finished':self.job=None;return
        if kind=='progress':self.status=event.get('text','문서 확인 중')
        elif kind=='lyrics':
            self.timeout_retry=None
            value={**event['value'],'track':state['track'],'token':state['token']}
            self.translation_search_status=value.get('discovery_status','')
            try:reply=self.bridge.receive(value)
            except ValueError as error:
                self.status=str(error);job.cancel();self.job=None;return
            self.awaiting=reply=='queued';self.last_url=value['url']
            self.status='가사창 표시 확인 중' if self.awaiting else '곡이 바뀌어 이전 문서 연결을 취소했습니다.'
            if not self.awaiting:job.cancel();self.job=None
        elif kind=='choices':
            self.timeout_retry=None
            self.choices=[validate_url(url) for url in event.get('urls',[])][:8]
            # The worker keeps its starting snapshot. References can arrive
            # while discovery runs, so its result may still need one fresh read.
            self.retry_references=not self.started_with_references
            self.status='문서 후보 확인 필요 · 가사 웹 연결 설정에서 선택하세요.'
        elif kind=='blocked':
            self.timeout_retry=None
            self.last_url=event.get('url','');self.retry_references=False
            self.status='원본 사이트 확인 필요 · 연결 설정의 원본 확인을 누르세요.'
        elif kind=='error':
            self.timeout_retry=None
            # The first hidden search can finish before Spotify/API originals
            # arrive. New references permit one fresh match with the same guards.
            self.retry_references=event.get('error_code')=='no_table' and not self.started_with_references
            self.status=event.get('text','내부 브라우저 연결 실패')
            if event.get('error_code')=='timeout' and self.timeout_retries<2:
                delay=(5,15)[self.timeout_retries];self.timeout_retries+=1
                self.timeout_retry=(self.last_key,time.monotonic()+delay)
                self.status+=f' · {delay}초 후 자동 재시도 ({self.timeout_retries}/2)'

    def close(self):
        self.enabled=False
        self.timeout_retry=None
        self.search_context=None
        if self.job:self.job.close();self.job=None
