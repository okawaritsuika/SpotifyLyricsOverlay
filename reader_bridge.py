"""Exchange current Spotify metadata and transient lyrics over loopback only."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import re
import secrets
import threading
import time

from lyrics import normalized
from lyric_annotations import merge_verified_annotations
import unicodedata
from screen_lyrics import matching_lines
from wiki_singers import extract_colored_parts
from web_lyrics import parse_table_text,validate_url,source_id,document_name,document_identity,sidem_url

PORT=47831

def _document_name(value):
    value=unicodedata.normalize('NFKC',value).strip()
    clean=re.sub(r'\s*(?:[-–—]\s*|[([]\s*|\s+)(?:(?:19|20)\d{2}\s*(?:ver(?:sion)?\.?|remaster(?:ed)?)|(?:ver(?:sion)?\.?|remaster(?:ed)?)\s*(?:19|20)\d{2})\s*[)\]]?\s*$','',value,flags=re.I).strip()
    # A year inside a singer/version group must not leave half a title behind.
    return clean if _balanced_groups(clean) else value


def document_title(value):
    return normalized(_document_name(value))


def _balanced_groups(value):
    stack=[]
    for char in value:
        if char in '([':stack.append(char)
        elif char in ')]':
            if not stack or stack.pop()!=('(' if char==')' else '['):return False
    return not stack


def _trailing_group(value):
    if not value or value[-1] not in ')]':return None
    stack=[]
    for index in range(len(value)-1,-1,-1):
        char=value[index]
        if char in ')]':stack.append(char)
        elif char in '([':
            if not stack or stack.pop()!=(')' if char=='(' else ']'):return None
            if not stack:return value[:index].strip(),value[index+1:-1].strip()
    return None


def _version_suffix(value):
    return bool(re.search(r'(?:^|[^A-Za-z0-9]|(?:19|20)\d{2}\s*)(?:ver(?:sion)?\.?|recording|remaster(?:ed)?|버전|バージョン)(?:\s*(?:19|20)\d{2})?\s*$',value,flags=re.I))


def _recording_titles(value):
    title=_document_name(value);titles=[title]
    for _ in range(8):
        group=_trailing_group(title)
        if group and _version_suffix(group[1]):base=group[0]
        else:
            suffix=re.match(r'^(.*)\s+[-–—]\s*(.+)$',title)
            base=suffix[1].strip() if suffix and _version_suffix(suffix[2]) else ''
        if not base:break
        title=_document_name(base);titles.append(title)
    return titles


def recording_base(value):
    return _recording_titles(value)[-1]


def same_document_title(name,title):
    expected=document_title(title)
    if not expected:return False
    if document_title(name)==expected:return True
    plain=_document_name(name)
    while (group:=_trailing_group(plain)) and not _version_suffix(group[1]):
        plain=_document_name(group[0])
        if document_title(plain)==expected:return True
    return False


def same_lyric_title(name,title):
    return any(same_document_title(name,variant) for variant in _recording_titles(title))


def metadata_alias_match(name,title,artist,evidence):
    if (not isinstance(evidence,dict) or not isinstance(evidence.get('title'),str)
            or not isinstance(evidence.get('artist'),str) or same_lyric_title(name,title)):return False
    variants=_recording_titles(title);plain=variants[-1]
    while len(variants)<9 and (group:=_trailing_group(plain)) and group[0]:
        plain=_document_name(group[0]);variants.append(plain)
    if not any(same_document_title(name,variant) for variant in variants):return False
    formal=normalized(recording_base(title))
    performer=normalized(re.sub(r'\([^)]*\)','',unicodedata.normalize('NFKC',artist)))
    return (len(formal)>=6 and len(performer)>=3 and normalized(evidence['title'])==formal
            and normalized(evidence['artist'])==performer)


def same_document_url(left,right):
    if not left or not right:return False
    try:
        paths=[document_identity(url) for url in (left,right)]
    except ValueError:return False
    return paths[0]==paths[1]


class ReaderBridge:
    def __init__(self,port=PORT,messages=None,document_urls=None):
        self.document_urls=document_urls or {}
        self.references=()
        self.full_references=()
        self.receipt={'status':'waiting','line_count':0}
        self.pending_documents=[]
        self.pending_translation_sources=[]
        self.tracking={'status':'waiting','index':None}
        self.overlay={}
        self.part_artists=()
        self.backend='chrome'
        self.port=port
        self.lock=threading.Lock()
        self.state={'title':'','artist':'','track':'','playing':False}
        self.updated=0
        self.messages=messages
        self.token=secrets.token_urlsafe(32)
        self.server=None
        self.thread=None

    def set_backend(self,backend):
        with self.lock:
            if backend==self.backend:return
            self.backend=backend;self.token=secrets.token_urlsafe(32)
            self.receipt={'status':'waiting','line_count':0}
            self.pending_documents.clear();self.pending_translation_sources.clear()

    def observe(self,playback):
        with self.lock:
            track=hashlib.sha256(playback.key.encode()).hexdigest()
            if track!=self.state['track']:
                self.part_artists=()
                self.references=();self.full_references=();self.receipt={'status':'waiting','line_count':0};self.tracking={'status':'waiting','index':None}
                self.pending_documents.clear()
                self.pending_translation_sources.clear()
            self.state={'title':playback.title[:300],
                        'artist':playback.artist[:300],
                        'track':hashlib.sha256(playback.key.encode()).hexdigest(),
                        'playing':bool(playback.title and playback.playing and not playback.error)}
            # A suggestion is never an approved document and cannot bypass reference matching.
            if re.search(r'\bTHE\s+IDOLM@STER\s+SideM\b',getattr(playback,'album',''),re.I):
                self.state['suggested_document_url']=sidem_url(_document_name(playback.title))
            self.updated=time.monotonic()

    def snapshot(self):
        with self.lock:
            document=self.document_urls.get(self.state['track'],'')
            learned=isinstance(document,dict) and document.get('learned') is True
            document_url=document.get('url','') if isinstance(document,dict) else document
            return {**self.state,'reader_backend':self.backend,'playing':self.state['playing'] and time.monotonic()-self.updated<5,'token':self.token,'reference_lines':self.references,'full_reference_lines':self.full_references,
                    'document_url':document_url,'document_learned':learned,'receipt':dict(self.receipt),'tracking':dict(self.tracking),'overlay':dict(self.overlay)}

    def set_overlay(self,value):
        with self.lock:
            self.overlay=dict(value)
            self.part_artists=tuple(value.get('display_artists',()))

    def set_reference(self,key,lines,complete=False):
        with self.lock:
            if hashlib.sha256(key.encode()).hexdigest()!=self.state['track']:return
            lines=tuple(lines)
            if complete:
                # Full provider/library documents retain order and repetitions;
                # screen fragments still supply only the existing matching hints.
                self.full_references=(lines if len(lines)<=2000 and all(isinstance(line,str) and len(line)<=10000 for line in lines)
                                      and sum(len(line) for line in lines)<=100000 else ())
            values=list(self.references)
            for line in lines:
                if line not in values and 6<=len(normalized(line))<=300:values.append(line)
            self.references=tuple(values[-30:])

    def clear_reference(self,key):
        with self.lock:
            if hashlib.sha256(key.encode()).hexdigest()==self.state['track']:self.references=();self.full_references=()

    def set_tracking(self,status,index=None):
        with self.lock:self.tracking={'status':status,'index':index}

    def set_receipt(self,track,status,count=0,translated_count=None,token=None):
        with self.lock:
            if track!=self.state['track'] or token is not None and token!=self.token:return
            sources=self.receipt.get('translation_sources',[]) if status=='displayed' else []
            if status in ('displayed','display_failed') and self.pending_documents:
                pending_track,document=self.pending_documents.pop(0)
                pending_sources=self.pending_translation_sources.pop(0)
                sources=list(pending_sources) if status=='displayed' and pending_track==track else []
                if status=='displayed' and pending_track==track and document is not None:
                    self.messages.put(('reader_document',(track,document)))
            self.receipt={'status':status,'line_count':count}
            if translated_count is not None:self.receipt['translated_lines']=translated_count
            if sources:self.receipt['translation_sources']=sources

    def receive(self,value):
        if (not isinstance(value,dict) or not isinstance(value.get('token'),str)
                or not value['token'].isascii() or not secrets.compare_digest(value['token'],self.token)):
            raise ValueError('앱 연결이 바뀌었습니다. 현재 곡을 다시 확인하세요.')
        if self.messages is None:raise ValueError('가사 수신이 준비되지 않았습니다.')
        state=self.snapshot()
        if value['token']!=state['token']:return 'stale'
        if value.get('track')!=state['track'] or time.monotonic()-self.updated>=5:return 'stale'
        text=value.get('text');url=value.get('url')
        if not isinstance(text,str) or not isinstance(url,str) or not 0<len(text)<=500000:
            raise ValueError('가사 표의 내용을 확인하세요.')
        validate_url(url)
        title=document_name(url)
        try:lines=parse_table_text(text,strict=False,source=source_id(url))
        except ValueError:
            self.set_receipt(state['track'],'invalid_table',token=state['token']);raise
        expected=state['document_url']
        explicitly_linked=(same_document_url(expected,url) and
                           (not state['document_learned'] or len(state['reference_lines'])<2))
        matched=matching_lines(state['reference_lines'],[line.original for line in lines])
        selected=value.get('user_selected') is True
        without_reference=len(state['reference_lines'])<2
        title_matches=same_document_title(title,state['title'])
        metadata_match=without_reference and title_matches and value.get('match_mode') in ('title','metadata','unique_title')
        recording_match=(without_reference and value.get('match_mode') in ('metadata','unique_title')
                         and same_lyric_title(title,state['title']))
        alias_match=(without_reference and value.get('match_mode')=='metadata_alias'
                     and metadata_alias_match(title,state['title'],state['artist'],value.get('title_alias')))
        if not explicitly_linked and not selected and not without_reference and not matched:
            self.set_receipt(state['track'],'unmatched',token=state['token']);raise ValueError('Spotify 원문과 일치하는 구절을 확인하지 못했습니다.')
        if not explicitly_linked and not selected and not metadata_match and not recording_match and not alias_match and normalized(title)!=normalized(state['title']) and not matched:
            self.set_receipt(state['track'],'wrong_document',token=state['token']);raise ValueError('현재 곡과 문서 제목이 달라 연결하지 않았습니다.')
        # A matched source may contain only originals or an incomplete translation.
        # Each additional document is parsed using its own table convention and
        # must align to the complete accepted original before filling any gap.
        extras=value.get('supplements',[])
        if not isinstance(extras,list) or len(extras)>7:
            raise ValueError('보충 가사 출처 개수를 확인하세요.')
        part_sources=[]
        with self.lock:
            artists=(state['artist'],*self.part_artists) if state['track']==self.state['track'] else ()
        def collect_parts(document,rows):
            html=document.get('table_html')
            if not isinstance(html,str) or not 0<len(html)<=200000:return
            if source_id(document['url'])!='namu':return
            try:profile=extract_colored_parts(html,rows,artists=artists)
            except (ValueError,TypeError,RecursionError):return
            if profile:part_sources.append({'url':document['url'],'lines':rows,'profile':profile})
        collect_parts(value,lines)
        supplied=[];source_urls=[];size=len(text)
        for extra in extras:
            if not isinstance(extra,dict):continue
            body=extra.get('text');address=extra.get('url')
            if not isinstance(body,str) or not isinstance(address,str):continue
            size+=len(body)
            if size>500000:raise ValueError('보충 가사 표가 너무 큽니다.')
            try:
                validate_url(address);name=document_name(address)
                if not (same_lyric_title(name,state['title']) or same_document_title(name,title)
                        or metadata_alias_match(name,state['title'],state['artist'],extra.get('title_alias'))):
                    continue
                rows=parse_table_text(body,strict=False,source=source_id(address))
            except ValueError:continue
            supplied.append(rows);source_urls.append(address)
            collect_parts(extra,rows)
        translation_sources=[url] if any(line.korean.strip() for line in lines) else []
        if supplied:
            lines,contributors=merge_verified_annotations(lines,supplied)
            translation_sources.extend(source_urls[index] for index in contributors)
        if selected or (explicitly_linked and not state['document_learned']):document=url
        elif expected and not state['document_learned']:document=None
        else:document={'url':url,'learned':True}
        with self.lock:
            if (value['token']!=self.token or state['track']!=self.state['track']
                    or time.monotonic()-self.updated>=5):return 'stale'
            self.receipt={'status':'queued','line_count':len(lines)}
            if translation_sources:self.receipt['translation_sources']=list(dict.fromkeys(translation_sources))
            # Only URLs wait for the UI acknowledgement. Keep documents and
            # translation sources in lyric delivery order, not latest receipt order.
            self.pending_documents.append((state['track'],document))
            self.pending_translation_sources.append(tuple(dict.fromkeys(translation_sources)))
            payload=(state['track'],lines,bool(supplied),state['token'])
            if part_sources:payload+= (part_sources,)
            self.messages.put(('reader_lyrics',payload))
        return 'queued'

    def start(self):
        bridge=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def setup(self):
                super().setup();self.connection.settimeout(3)
            def allowed(self):
                client=self.headers.get('X-Reader-Extension','')
                origin=self.headers.get('Origin','')
                if self.command=='OPTIONS' and origin.startswith('chrome-extension://'):
                    client=origin.removeprefix('chrome-extension://')
                return bool(re.fullmatch('[a-p]{32}',client)) and origin in ('','chrome-extension://'+client) and self.headers.get('Host')==f'127.0.0.1:{bridge.port}'
            def reply(self,status,value):
                body=json.dumps(value,ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                origin=self.headers.get('Origin','')
                if re.fullmatch(r'chrome-extension://[a-p]{32}',origin):self.send_header('Access-Control-Allow-Origin',origin)
                self.send_header('Access-Control-Allow-Headers','X-Reader-Extension, Content-Type')
                self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
            def do_OPTIONS(self):self.reply(200 if self.allowed() else 403,{})
            def do_GET(self):
                if not self.allowed():return self.reply(403,{'error':'Extension access only'})
                if self.path!='/state':return self.reply(404,{})
                self.reply(200,bridge.snapshot())
            def do_POST(self):
                if not self.allowed():return self.reply(403,{'error':'Extension access only'})
                if self.path!='/lyrics':return self.reply(404,{})
                if bridge.backend=='internal':return self.reply(200,{'status':'disabled'})
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=1_000_000:raise ValueError('가사 전송 크기를 확인하세요.')
                    if self.headers.get_content_type()!='application/json':raise ValueError('JSON 전송만 지원합니다.')
                    value=json.loads(self.rfile.read(size).decode('utf-8'))
                    self.reply(200,{'status':bridge.receive(value)})
                except (ValueError,UnicodeError):
                    # Never echo received page text into errors or logs.
                    reason=bridge.snapshot()['receipt']['status']
                    error={'invalid_table':'가사 표의 일본어·한글 발음·한국어 번역 묶음을 구분하지 못했습니다.',
                           'unmatched':'Spotify 원문과 문서 가사가 일치하지 않습니다.',
                           'wrong_document':'지정 문서 주소와 현재 곡이 일치하지 않습니다.'}.get(reason,'현재 곡·문서 제목·가사 표 형식 또는 앱 연결을 확인하세요.')
                    self.reply(400,{'error':error})
        self.server=ThreadingHTTPServer(('127.0.0.1',self.port),Handler)
        self.server.daemon_threads=True
        self.port=self.server.server_port
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True,name='reader-metadata-bridge')
        self.thread.start()

    def close(self):
        if self.server:
            self.server.shutdown();self.server.server_close()
            self.thread.join(timeout=2)
            self.server=None
