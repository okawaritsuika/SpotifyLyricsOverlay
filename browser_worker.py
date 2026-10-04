"""Disposable WebView2 host. Source text is returned on stdout, never written to disk."""
import json
from pathlib import Path
import queue
import sys
import threading
import time
from urllib.parse import quote,urlsplit
from lyric_annotations import merge_verified_annotations
from reader_bridge import same_lyric_title,same_document_title,metadata_alias_match
from browser_visibility import prepare_background_window
from web_lyrics import validate_url,source_id,document_name,parse_table_text,namu_url

BASE=Path(__file__).resolve().parent
SCRIPTS=('lyric_text.js','document_match.js','reader.js','source_suggestions.js')


class BrowserTimeout(TimeoutError):
    def __init__(self,stage,operation='문서 처리'):
        self.stage=stage
        label,seconds={'startup':('브라우저 초기화',20),'navigation':('원본 문서 로딩',20),
                       'evaluation':(operation,12)}[stage]
        super().__init__(f'내부 브라우저 시간 초과 · {label} ({seconds}초)')


class BrowserScriptError(RuntimeError):
    def __init__(self,operation):
        super().__init__(f'내부 브라우저 처리 실패 · {operation} 스크립트 응답을 확인하지 못했습니다.')


def error_result(error):
    if isinstance(error,BrowserTimeout):
        return {'kind':'error','error_code':'timeout','stage':error.stage,'text':str(error)}
    if isinstance(error,BrowserScriptError):
        return {'kind':'error','error_code':'script','stage':'evaluation','text':str(error)}
    return {'kind':'error','text':f'내부 브라우저 연결 실패 ({type(error).__name__}) · 가사 웹 연결 설정에서 원본 확인을 누르세요.'}


def emit(kind,**value):
    print(json.dumps({'kind':kind,**value},ensure_ascii=False),flush=True)


def document_key(url):
    # Compatibility spellings can be comparable titles but distinct wiki paths.
    # Decode URL escapes without folding e.g. Resonance⁺ into Resonance+.
    return source_id(url),document_name(url)


class DomReadyBridge:
    """Initialize the normal pywebview bridge once per top-level navigation.

    Fandom's article DOM can be ready while ads keep NavigationCompleted pending.
    DOMContentLoaded is sufficient for the reader; its normal access-challenge,
    source URL and lyric matching checks still decide whether to accept content.
    """
    def __init__(self,completed):
        self.completed=completed
        self.navigation_id=None
        self.initialized=False

    def starting(self,sender,args):
        self.navigation_id=args.NavigationId
        self.initialized=False

    def dom_ready(self,sender,args):
        try:url=validate_url(str(sender.Source))
        except (TypeError,ValueError):return
        if urlsplit(url).hostname!='sidem-ko.fandom.com':return
        self.finish(sender,args)

    def finish(self,sender,args):
        if args.NavigationId!=self.navigation_id or self.initialized:return
        self.initialized=True
        self.completed(sender,args)


def install_dom_readiness(window):
    # Install on the WinForms thread after the initial local page is ready.
    # Do not set pywebview's readiness flags ourselves: its bridge must really
    # finish initializing before SourceBrowser may evaluate the reader scripts.
    from System import Action
    def install():
        backend=window.native.browser
        view=backend.webview
        bridge=DomReadyBridge(backend.on_navigation_completed)
        view.NavigationCompleted-=backend.on_navigation_completed
        view.NavigationStarting+=bridge.starting
        view.CoreWebView2.DOMContentLoaded+=bridge.dom_ready
        view.NavigationCompleted+=bridge.finish
        window._source_dom_bridge=bridge
    window.native.Invoke(Action(install))


def connect_page(browser,state,report,selected_url=''):
    """Bounded discovery with the same classification rules as the extension."""
    first=selected_url or state.get('document_url') or state.get('suggested_document_url') or 'https://namu.wiki/w/'+quote(browser.clean_title(state['title']),safe='')
    remaining=[validate_url(first)];visited=[];seen=set();choices=[];searched=False;unexamined=False
    primary=None;primary_lines=();lines=();supplements=[];supplied_sources=[]
    complement_url='';complement_checked=False
    manual=bool(selected_url or state.get('document_url') and not state.get('document_learned'))
    def finish(status,**metadata):
        event=lyric_result(primary,lines=lines)
        event['value'].update(discovery_status=status,**metadata)
        if supplements:event['value']['supplements']=supplements
        return event
    def accept(result):
        nonlocal primary,primary_lines,lines,unexamined
        supplied=translation_lines(result)
        if primary is None:
            primary=result;primary_lines=lines=supplied
        elif lines and supplied:
            # Discovery's title/reference guard still applies to every source.
            # A whole-document comparison additionally verifies complements;
            # no matched snippet alone can donate a sentence's translation.
            name=document_name(result['url'])
            eligible=(same_lyric_title(name,state['title']) or
                      same_document_title(name,document_name(primary['url'])) or
                      metadata_alias_match(name,state['title'],state.get('artist',''),result.get('titleAlias')))
            if eligible:
                _,contributors=merge_verified_annotations(primary_lines,[supplied])
                size=len(primary['text'])+sum(len(item['text']) for item in supplements)+len(result['text'])
                if contributors and size>500000:unexamined=True
                elif contributors:
                    supplements.append(lyric_result(result)['value']);supplied_sources.append(supplied)
                    lines,_=merge_verified_annotations(primary_lines,supplied_sources)
        return manual or bool(lines and all(line.korean.strip() for line in lines))
    def enqueue(urls):
        queued={document_key(url) for url in remaining}
        for candidate in urls:
            candidate=validate_url(candidate);key=document_key(candidate)
            if key not in seen and key not in queued:remaining.append(candidate);queued.add(key)
    while remaining and len(visited)<8:
        url=remaining.pop(0)
        key=document_key(url)
        if key in seen:continue
        visited.append(url);seen.add(key)
        report('progress',text=f'내부 브라우저 · 문서 {len(visited)}개째 확인 중')
        try:
            browser.navigate(url)
            pending={'visited':visited,'candidates':remaining,'searched':searched,'selectedURL':selected_url,'unexamined':unexamined}
            result=browser.read(state,pending)
        except (BrowserTimeout,BrowserScriptError) as error:
            if primary is None:raise
            return finish('error',discovery_error=error_result(error))
        unexamined=unexamined or bool(result.get('unexamined'))
        if result['status']=='blocked':
            return finish('blocked',discovery_url=url) if primary else {'kind':'blocked','url':url}
        if result.get('url'):seen.add(document_key(result['url']))
        remaining[:]=[candidate for candidate in remaining if document_key(candidate) not in seen]
        if result['status']=='matched' and accept(result):return finish('manual' if manual else 'complete')
        if result['status']=='needs_confirmation' and document_key(result['url']) not in {document_key(choice) for choice in choices}:
            choices.append(result['url'])
        enqueue(result.get('candidates',[]))
        if not manual and not complement_checked:
            complement_checked=True
            # Page links and autocomplete deliberately stay on their own site.
            # Add the other supported source in the host, as an unapproved lead.
            # A SideM page establishes the source family; in the reverse
            # direction require the bridge's explicit SideM album suggestion.
            if source_id(first)=='sidem':
                complement_url=namu_url(browser.clean_title(state['title']))
            else:
                suggested=state.get('suggested_document_url','')
                try:
                    if (source_id(suggested)=='sidem'
                            and same_lyric_title(document_name(suggested),state['title'])):
                        complement_url=validate_url(suggested)
                except ValueError:pass
            if complement_url:enqueue([complement_url])
        if not remaining and not searched:
            searched=True
            try:enqueue(browser.suggestions(state['title']))
            except (BrowserTimeout,BrowserScriptError) as error:
                if primary is None:raise
                return finish('error',discovery_error=error_result(error))
        room=8-len(visited)
        if len(remaining)>room:unexamined=True
        # Reserve one of the existing eight slots so same-site variants cannot
        # crowd out the second source. Never extend the navigation limit.
        if room and complement_url in remaining[room:]:
            remaining=remaining[:room-1]+[complement_url]
        else:remaining=remaining[:room]
    if primary:return finish('bounded' if unexamined or remaining else 'complete')
    if len(choices)==1 and searched and not remaining and not unexamined:
        url=choices[0];browser.navigate(url)
        result=browser.read(state,{'visited':visited,'candidates':[],'searched':True,'uniqueTitleURL':url})
        if result['status']=='matched':return lyric_result(result)
        if result['status']=='blocked':return {'kind':'blocked','url':url}
    if choices:return {'kind':'choices','urls':choices[:8]}
    return {'kind':'error','error_code':'no_table','text':'일치하는 가사 표를 찾지 못했습니다. 연결 설정에서 원본을 확인하세요.'}


def translation_lines(result):
    # Only classify supplied Korean here. Readings are generated by the app;
    # doing that in every candidate would delay a bounded source search.
    try:return parse_table_text(result['text'],reader=lambda text:'',strict=False,source=source_id(result['url']))
    except ValueError:return ()


def lyric_result(result,lines=None):
    if lines is None:lines=translation_lines(result)
    translated=sum(bool(line.korean.strip()) for line in lines)
    return {'kind':'lyrics','value':{'url':result['url'],'text':result['text'],
            **({'table_html':result['table_html']} if isinstance(result.get('table_html'),str) and len(result['table_html'])<=200000 else {}),
            'match_mode':result['matchMode'],'user_selected':result.get('selected',False),
            'title_alias':result.get('titleAlias'),'line_count':len(lines),
            'translated_lines':translated,'original_only':not translated,
            'translation_complete':bool(lines and translated==len(lines)),
            'discovery_status':'complete'}}


class SourceBrowser:
    def __init__(self,window,stopped):self.window,self.stopped=window,stopped

    def clean_title(self,title):
        self.window.evaluate_js((BASE/'extension/document_match.js').read_text(encoding='utf-8'))
        return self.window.evaluate_js('documentTitle('+json.dumps(title)+')')

    def navigate(self,url):
        if self.stopped.is_set():raise RuntimeError('cancelled')
        url=validate_url(url)
        if self.window.events.loaded.is_set():
            try:current=document_key(self.window.get_current_url())
            except (ValueError,TypeError):current=''
            if current and current==document_key(url):
                # WebView2 does not navigate when Source is assigned the same URI.
                # Clearing loaded in that case would wait for an event that never comes.
                self.inject();return
        self.window.events.loaded.clear()
        self.window.load_url(url)
        if not self.window.events.loaded.wait(20):raise BrowserTimeout('navigation')
        self.inject()

    def inject(self):
        validate_url(self.window.get_current_url())
        for name in SCRIPTS:self.window.evaluate_js((BASE/'extension'/name).read_text(encoding='utf-8'))
        self.window.evaluate_js((BASE/'native_page.js').read_text(encoding='utf-8'))

    def evaluate(self,script,operation='문서 처리'):
        response=queue.Queue(maxsize=1)
        def received(value):
            try:response.put_nowait(value)
            except queue.Full:pass
        dispatched=self.window.evaluate_js('Promise.resolve().then(()=>('+script+')).catch(()=>({status:"script_error"}))',callback=received)
        # pywebview returns True after registering a Promise callback. A synchronous
        # eval failure instead returns None and never invokes that callback.
        if dispatched is None and response.empty():raise BrowserScriptError(operation)
        deadline=time.monotonic()+12
        while not self.stopped.is_set() and time.monotonic()<deadline:
            try:
                value=response.get(timeout=.2)
                if isinstance(value,dict) and value.get('status')=='script_error':raise BrowserScriptError(operation)
                return value
            except queue.Empty:pass
        if self.stopped.is_set():raise RuntimeError('cancelled')
        raise BrowserTimeout('evaluation',operation)

    def read(self,state,pending):
        # Session tokens and native commands never enter the remote page.
        page_state={key:state.get(key,[] if key=='reference_lines' else '') for key in ('title','artist','document_url','document_learned','reference_lines')}
        return self.evaluate('nativePageRead('+json.dumps(page_state,ensure_ascii=False)+','+json.dumps(pending,ensure_ascii=False)+')','가사 표 읽기')

    def suggestions(self,title):
        self.window.evaluate_js('window.__originalPageReader.restore()')
        result=self.evaluate('sourceSuggestions('+json.dumps(title)+')','문서 후보 검색')
        return result if isinstance(result,list) else []


def main():
    request=json.loads(sys.stdin.readline())
    state=request['state'];visible=bool(request.get('visible'))
    import webview
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER']=False
    webview.settings['ALLOW_DOWNLOADS']=False
    stopped=threading.Event()
    window=webview.create_window('Spotify 가사 · 원본 확인',html='<html><body>가사 문서 연결 중</body></html>',
        width=760,height=640,hidden=not visible,focus=visible)
    if not visible:window.events.initialized+=prepare_background_window
    window.events.closed+=lambda:stopped.set()
    def control():
        for line in sys.stdin:
            try:command=json.loads(line)
            except ValueError:continue
            if command.get('action')=='close':break
        stopped.set()
        try:window.destroy()
        except Exception:pass
    def run():
        threading.Thread(target=control,daemon=True).start()
        try:
            if not window.events.loaded.wait(20):raise BrowserTimeout('startup')
            install_dom_readiness(window)
            browser=SourceBrowser(window,stopped)
            result=connect_page(browser,state,emit,request.get('selected_url',''))
            emit(**result)
            if result['kind']=='lyrics':stopped.wait(20)
            elif visible:
                # User can handle a site prompt; never solve or bypass it here.
                deadline=time.monotonic()+900
                while not stopped.wait(2) and time.monotonic()<deadline:
                    try:
                        browser.inject()
                        value=browser.read(state,{'visited':[],'candidates':[]})
                        if value['status']=='matched':emit(**lyric_result(value));stopped.wait(20);break
                    except (ValueError,TimeoutError):continue
        except Exception as error:
            if not stopped.is_set():emit(**error_result(error))
        finally:
            stopped.set()
            try:window.destroy()
            except Exception:pass
    profile=Path(request['profile'])
    profile.mkdir(parents=True,exist_ok=True)
    webview.start(run,gui='edgechromium',private_mode=False,storage_path=str(profile))


if __name__=='__main__':main()
