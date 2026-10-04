"""Dedicated Spotify web session. Account headers never leave this process."""
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from datetime import datetime,timezone
from urllib.error import HTTPError
from urllib.parse import quote,urlparse,urlencode
from urllib.request import HTTPRedirectHandler,Request,build_opener

from spotify_api import lyric_lines,search_query,search_request_query,search_tracks,select_track
from artist_metadata import validated_track
from browser_visibility import prepare_background_window,show_original_window
from spotify_catalog import catalog_diagnostic_record,catalog_request_diagnostic,catalog_query_diagnostic,catalog_page_kind
from spotify_playlist import observed_request,playlist_request_diagnostic,playlist_template,playlist_page

MAX_RESPONSE=4_000_000


def playlist_track_shapes(payload):
    """Describe three public track schemas; never return field values or raw keys."""
    def kind(value):
        return {dict:'object',list:'array',str:'string',int:'number',float:'number',bool:'boolean',type(None):'null'}.get(type(value),'other')
    def number(value):
        return value if type(value) in (int,float) and 0<=value<=604800000 else None
    def mapping(value):return value if isinstance(value,dict) else {}
    data=mapping(mapping(payload).get('data'))
    playlist=mapping(data.get('playlistV2',data.get('playlist')))
    content=mapping(playlist.get('content',playlist.get('contents')))
    items=content.get('items')
    if not isinstance(items,list):return []
    samples=[]
    for item in items[:3]:
        item=mapping(item);wrapper=mapping(item.get('itemV2',item.get('item',item.get('track',item))))
        value=mapping(wrapper.get('data',wrapper));artists=value.get('artists');artist_data=mapping(artists)
        artist_items=artist_data.get('items');first=mapping(artist_items[0]) if isinstance(artist_items,list) and artist_items else {}
        duration=value.get('duration');duration_data=mapping(duration)
        alternate=value.get('trackDuration');alternate_data=mapping(alternate)
        album=value.get('albumOfTrack');album_data=mapping(album)
        typename=value.get('__typename')
        samples.append({'data_keys':[key for key in ('uri','name','artists','duration','trackDuration','albumOfTrack','album','__typename') if key in value],
            'data_uri_present':isinstance(value.get('uri'),str) and bool(value['uri']),
            'wrapper_uri_present':any(isinstance(wrapper.get(key),str) and bool(wrapper[key]) for key in ('uri','_uri')),
            'name_present':isinstance(value.get('name'),str) and bool(value['name']),
            'track_type':typename if typename in ('Track','LocalTrack','NotFound','Unavailable') else 'other' if typename else 'missing',
            'artist_container':kind(artists),'artist_items_count':len(artist_items) if isinstance(artist_items,list) else None,
            'artist_total_count':number(artist_data.get('totalCount')) if type(artist_data.get('totalCount')) is int else None,
            'first_artist_keys':[key for key in ('profile','name','data','uri','id','__typename') if key in first],
            'first_artist_name_present':isinstance(first.get('name'),str) and bool(first['name']),
            'first_artist_profile_name_present':isinstance(mapping(first.get('profile')).get('name'),str),
            'duration_container':kind(duration),'duration_keys':[key for key in ('totalMilliseconds','milliseconds','millis','totalSeconds') if key in duration_data],
            'duration_total_milliseconds':number(duration_data.get('totalMilliseconds')),
            'duration_milliseconds':number(duration_data.get('milliseconds')),'duration_millis':number(duration_data.get('millis')),
            'track_duration_container':kind(alternate),'track_duration_keys':[key for key in ('totalMilliseconds','milliseconds','millis','totalSeconds') if key in alternate_data],
            'track_duration_total_milliseconds':number(alternate_data.get('totalMilliseconds')),
            'album_container':kind(album),'album_keys':[key for key in ('name','uri','__typename') if key in album_data],
            'album_name_present':isinstance(album_data.get('name'),str) and bool(album_data['name'])})
    return samples


def catalog_tracks(payload):
    """Only public track facts from the web player's actual search response."""
    tracks=search_tracks(payload)
    availability={}
    search=payload.get('data',{}).get('searchV2',{}) if isinstance(payload,dict) else {}
    for name in ('tracksV2','tracks','topResultsV2'):
        section=search.get(name) or {}
        if not isinstance(section,dict):continue
        items=section.get('itemsV2') or section.get('items') or []
        if not isinstance(items,list):continue
        for entry in items[:100]:
            if not isinstance(entry,dict):continue
            wrapper=entry.get('item',entry)
            if not isinstance(wrapper,dict):continue
            data=wrapper.get('data',wrapper)
            if not isinstance(data,dict):continue
            uri=data.get('uri') or wrapper.get('_uri') or wrapper.get('uri','')
            if not isinstance(uri,str):continue
            match=re.fullmatch(r'spotify:track:([A-Za-z0-9]{22})',uri)
            if not match:continue
            playability=data.get('playability')
            playability=playability if isinstance(playability,dict) else {}
            playable=playability.get('playable',data.get('isPlayable'))
            if not isinstance(playable,bool):playable=None
            reason=playability.get('reason','')
            if not isinstance(reason,str) or reason.upper()=='PLAYABLE':reason=''
            availability[match[1]]=(playable,reason[:200] if playable is False else '')
    checked=datetime.now(timezone.utc).isoformat(timespec='seconds')
    unique={}
    for track in tracks:
        playable,restriction=availability.get(track['id'],(None,''))
        unique[track['id']]={**track,'url':'https://open.spotify.com/track/'+track['id'],
                             'is_playable':playable,'restriction':restriction,'isrc':'','checked_at':checked}
    return list(unique.values())


def emit(kind,**values):
    print(json.dumps({'kind':kind,**values},ensure_ascii=False),flush=True)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,request,file,code,message,headers,newurl):
        return None


def fetch_lyrics(track_id,authorization,client_token=''):
    # Native lyric requests carrying account authorization have this fixed host.
    import re
    if not re.fullmatch(r'[A-Za-z0-9]{22}',track_id):raise ValueError('track id')
    headers={'Authorization':authorization,'Accept':'application/json','App-Platform':'WebPlayer'}
    if client_token:headers['client-token']=client_token
    request=Request('https://spclient.wg.spotify.com/color-lyrics/v2/track/'+track_id+
                    '?format=json&vocalRemoval=false&market=from_token',headers=headers)
    with build_opener(NoRedirect()).open(request,timeout=12) as response:
        data=response.read(MAX_RESPONSE+1)
        if len(data)>MAX_RESPONSE:raise ValueError('response size')
        return json.loads(data)


def retry_after_seconds(value):
    from email.utils import parsedate_to_datetime
    import math
    try:
        seconds=float(value)
        if not math.isfinite(seconds):return 60
        return max(1,math.ceil(seconds))
    except (ValueError,TypeError,OverflowError):
        try:return max(1,math.ceil((parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()))
        except (ValueError,TypeError,OverflowError,AttributeError):return 60


def fetch_playlist_page(template,playlist_id,offset,authorization,client_token='',extra_headers=None):
    template=playlist_template(template,playlist_id)
    if not template:raise ValueError('template')
    template['variables']['offset']=offset
    payload={key:template[key] for key in ('operationName','variables','extensions')}
    headers={'Authorization':authorization,'Accept':'application/json','App-Platform':'WebPlayer'}
    for name,value in (extra_headers or {}).items():
        if name in ('Spotify-App-Version','App-Platform','Client-Version','User-Agent','Origin','Referer') and isinstance(value,str):headers[name]=value
    if client_token:headers['client-token']=client_token
    url='https://api-partner.spotify.com'+template['path']
    if template['method']=='POST':
        headers['Content-Type']='application/json'
        request=Request(url,data=json.dumps(payload,separators=(',',':')).encode('utf-8'),headers=headers,method='POST')
    else:
        query={key:json.dumps(value,separators=(',',':')) if isinstance(value,dict) else value for key,value in payload.items()}
        request=Request(url+'?'+urlencode(query),headers=headers)
    with build_opener(NoRedirect()).open(request,timeout=15) as response:
        raw=response.read(MAX_RESPONSE+1)
        if len(raw)>MAX_RESPONSE:raise ValueError('response size')
        return json.loads(raw)


def response_kind(url):
    parsed=urlparse(url)
    if parsed.scheme!='https':return ''
    if parsed.hostname=='open.spotify.com' and parsed.path in ('/api/token','/get_access_token'):return 'token'
    if parsed.hostname=='api-partner.spotify.com' and parsed.path in (
        '/pathfinder/v1/query','/pathfinder/v2/query','/pathfinder/query'):return 'search'
    return ''


def auth_header_allowed(url):
    parsed=urlparse(url)
    return parsed.scheme=='https' and parsed.hostname in (
        'api-partner.spotify.com','spclient.wg.spotify.com','api.spotify.com')


class SpotifyBrowser:
    def __init__(self,window,commands,stopped,login_requested=False,diagnostics=False):
        self.window,self.commands,self.stopped=window,commands,stopped
        self.login_requested=login_requested
        self.network=queue.Queue();self.authorization='';self.client_token=''
        self.authenticated=False;self.pending=None;self.generation=0
        self.active_query='';self.search_deadline=0;self.fetching=False
        self.last_status=None;self.auth_deadline=time.monotonic()+25;self.auth_retries=0
        self.catalog_pending=None;self.catalog_active=None;self.catalog_query='';self.catalog_deadline=0
        self.diagnostics=diagnostics;self.diagnostic_attempt=0
        self.playlist=None;self.playlist_retry_until=0
        self.batch=None;self.batch_generation=0;self.batch_inflight=None
        self.batch_retry_until=0;self.batch_next_at=0;self.batch_credentials=None

    def finish_batch(self,lines=None,error='',code='',retry_after=0):
        request=self.batch
        if not request:return
        self.batch=None
        lines=lines or []
        emit('batch_lyrics_result',request_id=request['request_id'],provider_id=request['track']['id'],
             source='Spotify',lines=lines,synced=bool(lines) and all(row['time'] is not None for row in lines),
             error=error,code=code,retry_after=retry_after)

    def start_batch(self,command):
        request_id=command.get('request_id')
        if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',request_id):return
        supplied=command.get('track')
        track=None
        if isinstance(supplied,dict) and isinstance(supplied.get('artists'),list) and supplied['artists']:
            request={key:supplied.get(key) for key in ('title','album','duration')}
            request['artist']=supplied['artists'][0]
            track=validated_track(request,supplied)
        identifier=supplied.get('id') if isinstance(supplied,dict) else ''
        if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9]{22}',identifier):identifier=''
        def reject(code,message,retry_after=0):
            emit('batch_lyrics_result',request_id=request_id,provider_id=identifier,source='Spotify',lines=[],
                 synced=False,error=message,code=code,retry_after=retry_after)
        if not track:reject('query','Spotify 곡 ID와 전체 곡 정보를 확인해 주세요.');return
        now=time.monotonic()
        if now<self.batch_retry_until:
            reject('rate_limited','Spotify 요청 제한 대기 중입니다.',retry_after_seconds(self.batch_retry_until-now));return
        if not self.authenticated or not self.authorization:
            reject('login_required','기존 Spotify 연결 창에서 먼저 로그인해 주세요.');return
        if (self.batch or self.batch_inflight is not None or self.fetching or self.active_query or self.search_deadline
                or self.catalog_pending or self.catalog_active or self.playlist or now<self.batch_next_at):
            reject('busy','기존 Spotify 조회가 끝난 뒤 가사를 수집합니다.',1);return
        self.batch_generation+=1;generation=self.batch_generation
        self.batch={'request_id':request_id,'track':track,'generation':generation,'deadline':now+20}
        self.batch_inflight=generation
        authorization,client=self.batch_credentials or (self.authorization,self.client_token)
        def fetch():
            try:
                payload=fetch_lyrics(track['id'],authorization,client)
                self.network.put(('batch_lyrics',generation,request_id,track['id'],payload))
            except HTTPError as error:
                retry=retry_after_seconds(error.headers.get('Retry-After') if error.headers else None) if error.code==429 else 0
                self.network.put(('batch_lyrics_error',generation,request_id,track['id'],int(error.code),retry))
            except Exception:self.network.put(('batch_lyrics_error',generation,request_id,track['id'],0,0))
        threading.Thread(target=fetch,daemon=True,name='spotify-batch-lyrics').start()

    def receive_batch(self,event):
        generation,request_id,identifier=event[1:4]
        if generation==self.batch_inflight:
            self.batch_inflight=None;self.batch_next_at=time.monotonic()+1
        # Even a cancelled request's 429 must pause every following batch item.
        if event[0]=='batch_lyrics_error' and event[4]==429:
            self.batch_retry_until=max(self.batch_retry_until,time.monotonic()+(event[5] or 60))
        request=self.batch
        if (not request or request['generation']!=generation or request['request_id']!=request_id
                or request['track']['id']!=identifier):return
        if event[0]=='batch_lyrics_error':
            status=event[4]
            code={404:'not_found',401:'login_required',403:'login_required',429:'rate_limited'}.get(status,'response')
            message={'not_found':'Spotify에서 이 곡의 가사를 제공하지 않습니다.',
                     'login_required':'Spotify 가사 연결의 로그인 상태를 확인해 주세요.',
                     'rate_limited':'Spotify 요청 제한 · 지정된 대기 후 다시 시도합니다.'}.get(code,'Spotify 가사 응답을 확인하지 못했습니다.')
            self.finish_batch(error=message,code=code,retry_after=event[5] if status==429 else 0);return
        try:lines=lyric_lines(event[4],request['track']['duration'])
        except (ValueError,TypeError,AttributeError):
            # Preserve readable originals when supplied timestamps are invalid;
            # this result explicitly has no verified synchronization.
            try:lines=lyric_lines({'lyrics':{**event[4]['lyrics'],'syncType':'UNSYNCED'}})
            except (ValueError,TypeError,AttributeError,KeyError):
                self.finish_batch(error='Spotify 가사 내용을 확인하지 못했습니다.',code='response');return
        if len(lines)>1500:
            self.finish_batch(error='Spotify 가사의 줄 수가 저장 범위를 초과했습니다.',code='response');return
        self.finish_batch(lines)

    def playlist_diagnostic(self,phase,**facts):
        request=self.playlist
        if not request or request['diagnostics']>=40 and phase!='public_track_shape':return
        request['diagnostics']+=1
        emit('catalog_playlist_diagnostic',request_id=request['request_id'],value={'phase':phase,**facts})

    def finish_playlist(self,tracks=None,complete=False,error='',code='',retry_after=0,entries=None,start=None):
        request=self.playlist
        if not request:return
        self.playlist=None
        emit('catalog_playlist_result',request_id=request['request_id'],playlist_id=request['playlist_id'],
             tracks=tracks or [],offset=request['offset'],total=request['total'],complete=complete,
             error=error,code=code,retry_after=retry_after,entries=entries or [],
             start=request['offset'] if start is None else start)

    def playlist_failure(self,status=0,retry_after=0):
        self.playlist_diagnostic('native_error',http_status=status)
        if status==429:
            seconds=retry_after or 60;self.playlist_retry_until=time.monotonic()+seconds
            self.finish_playlist(error='Spotify 요청 제한 · 지정된 대기 후 다시 시도합니다.',code='rate_limited',retry_after=seconds)
        else:self.finish_playlist(error='Spotify 플레이리스트 응답을 확인하지 못했습니다.',
                                  code='login_required' if status in (401,403) else 'response')

    def accept_playlist_page(self,payload,offset):
        request=self.playlist
        if not request or offset!=request['offset']:return
        if not request.get('shape_reported'):
            request['shape_reported']=True
            self.playlist_diagnostic('public_track_shape',samples=playlist_track_shapes(payload))
        try:page=playlist_page(payload,request['playlist_id'],offset)
        except (ValueError,TypeError,AttributeError):
            self.finish_playlist(error='Spotify 플레이리스트 페이지를 확인하지 못했습니다.',code='response');return
        if request['total'] is not None and request['total']!=page['total']:
            self.finish_playlist(error='수집 중 플레이리스트 곡 수가 바뀌어 다시 확인해야 합니다.',code='changed');return
        if page['count'] and page['fingerprint'] in request['pages']:
            self.finish_playlist(error='Spotify가 같은 페이지를 반복해 수집을 중단했습니다.',code='no_progress');return
        request['pages'].add(page['fingerprint']);request['total']=page['total'];request['offset']=page['offset']
        request['fetching']=False;request['next_at']=time.monotonic()+1;request['deadline']=time.monotonic()+30
        if page['complete']:
            self.finish_playlist(page['tracks'],complete=True,entries=page.get('entries',[]),start=page.get('start',offset));return
        emit('catalog_playlist_progress',request_id=request['request_id'],playlist_id=request['playlist_id'],
             tracks=page['tracks'],offset=request['offset'],total=request['total'],complete=False,error='',code='',
             entries=page.get('entries',[]),start=page.get('start',offset))

    def tick_playlist(self,now=None):
        request=self.playlist
        if not request:return
        now=time.monotonic() if now is None else now
        if now>=request['deadline']:
            self.finish_playlist(error='Spotify 플레이리스트 조회가 지연되었습니다.',code='timeout');return
        if not self.authenticated:
            self.finish_playlist(error='기존 Spotify 연결 창에서 먼저 로그인해 주세요.',code='login_required');return
        if self.fetching or self.search_deadline or self.active_query:return
        if not request['started']:
            request['started']=True
            url='https://open.spotify.com/playlist/'+request['playlist_id']
            try:
                self.playlist_diagnostic('navigation')
                if self.window.get_current_url()==url:self.reload()
                else:self.navigate_search(url)
            except Exception:self.finish_playlist(error='Spotify 플레이리스트 화면을 열지 못했습니다.',code='browser')
            return
        if not request['template'] or request['fetching'] or now<request['next_at']:return
        request['fetching']=True;request['deadline']=now+25
        request_id,playlist_id,offset=request['request_id'],request['playlist_id'],request['offset']
        template,headers=request['template'],request['headers']
        # Reuse the credentials of the successful observed request; the web
        # player can renew its token without repeating the legacy token route.
        authorization=headers.get('Authorization') or self.authorization
        client=headers.get('client-token') or self.client_token
        def fetch():
            try:
                payload=fetch_playlist_page(template,playlist_id,offset,authorization,client,headers)
                self.network.put(('playlist_page',request_id,offset,payload))
            except HTTPError as error:
                retry=retry_after_seconds(error.headers.get('Retry-After')) if error.code==429 else 0
                self.network.put(('playlist_error',request_id,int(error.code),retry))
            except Exception:self.network.put(('playlist_error',request_id,0,0))
        threading.Thread(target=fetch,daemon=True,name='spotify-playlist-api').start()

    def catalog_diagnostic(self,phase,**facts):
        request=self.catalog_active or self.catalog_pending
        if not self.diagnostics or not request:return
        value={'phase':phase,'attempt':self.diagnostic_attempt,
               'elapsed':max(0,time.monotonic()-(request['deadline']-35)),
               'authenticated':self.authenticated,'lyric_pending':bool(self.pending),
               'lyric_search_pending':bool(self.active_query or self.search_deadline),
               'lyric_fetch_pending':self.fetching,'catalog_pending':bool(self.catalog_pending),
               'catalog_active':bool(self.catalog_active),**facts}
        emit('catalog_diagnostic',value=catalog_diagnostic_record(value))

    def finish_catalog(self,tracks=None,error='',code=''):
        request=self.catalog_active or self.catalog_pending
        self.catalog_diagnostic('finished',result=code or 'success')
        self.catalog_active=None;self.catalog_pending=None;self.catalog_query='';self.catalog_deadline=0
        if request:
            emit('catalog_result',request_id=request['request_id'],tracks=tracks or [],error=error,code=code)

    def tick_catalog(self,now=None):
        request=self.catalog_active or self.catalog_pending
        if not request:return
        now=time.monotonic() if now is None else now
        if now>=request['deadline'] or self.catalog_deadline and now>=self.catalog_deadline:
            self.finish_catalog(error='Spotify 곡 검색이 지연되었습니다. 잠시 후 다시 시도해 주세요.',code='timeout');return
        if not self.authenticated:
            self.finish_catalog(error='기존 Spotify 연결 창에서 먼저 로그인해 주세요.',code='login_required');return
        if self.catalog_active or self.fetching or self.search_deadline or self.active_query:return
        self.catalog_active=self.catalog_pending;self.catalog_pending=None
        self.catalog_query=request['title'].strip();self.catalog_deadline=min(request['deadline'],now+25)
        url='https://open.spotify.com/search/'+quote(self.catalog_query,safe='')
        try:
            self.catalog_diagnostic('navigation_requested')
            current=self.window.get_current_url()
            self.catalog_diagnostic('navigation',page=catalog_page_kind(current),navigation='reload' if current==url else 'load')
            if current==url:self.reload()
            else:self.navigate_search(url)
        except Exception:
            self.finish_catalog(error='Spotify 검색 화면을 열지 못했습니다. 기존 Spotify 연결을 확인해 주세요.',code='browser')

    def status(self,state,text):
        key=self.pending['key'] if self.pending else ''
        kind='metadata_status' if self.pending and self.pending.get('metadata_only') else 'status'
        value=(kind,state,text,self.authenticated,key)
        if value!=self.last_status:
            self.last_status=value;emit(kind,state=state,text=text,authenticated=self.authenticated,key=key)

    def install_hooks(self):
        # CoreWebView2 and GetContentAsync must be accessed on their GUI thread.
        from System import Action
        from System.IO import StreamReader
        from System.Text import Encoding
        def request_body(request):
            try:
                stream=request.Content
                if stream is None:return ''
                position=stream.Position if stream.CanSeek else None
                reader=StreamReader(stream,Encoding.UTF8,True,1024,True)
                content=reader.ReadToEnd()
                reader.Dispose()
                if position is not None:stream.Position=position
                return str(content) if len(content)<=MAX_RESPONSE else ''
            except Exception:return ''
        def receive(sender,args):
            diagnostic_request=self.catalog_active if self.diagnostics else None
            playlist_request=self.playlist if self.playlist and self.playlist['started'] else None
            diagnostic_facts={}
            def observe(phase,**facts):
                if diagnostic_request:
                    self.network.put(('catalog_diagnostic',diagnostic_request['request_id'],phase,{**diagnostic_facts,**facts}))
            try:
                url=str(args.Request.Uri);kind=response_kind(url)
                if auth_header_allowed(url):
                    headers=args.Request.Headers
                    authorization=str(headers.GetHeader('Authorization')) if headers.Contains('Authorization') else ''
                    client=str(headers.GetHeader('client-token')) if headers.Contains('client-token') else ''
                    if authorization.startswith('Bearer '):self.network.put(('headers',authorization,client))
                if not kind:
                    if diagnostic_request:
                        facts=catalog_request_diagnostic(url,method=str(args.Request.Method))
                        if facts:
                            facts.pop('phase',None)
                            observe('response',http_status=int(args.Response.StatusCode),**facts)
                    return
                query=None
                if kind=='search':
                    method=str(args.Request.Method)
                    body=request_body(args.Request) if method=='POST' else ''
                    if playlist_request:
                        observed=observed_request(url,body,method)
                        facts=playlist_request_diagnostic(observed,playlist_request['playlist_id'])
                        if facts:
                            facts['http_status']=int(args.Response.StatusCode)
                            self.network.put(('playlist_diagnostic',playlist_request['request_id'],'response',facts))
                        template=playlist_template(observed,playlist_request['playlist_id'])
                        if template:
                            extra={}
                            for name in ('Spotify-App-Version','App-Platform','Client-Version','User-Agent','Origin','Referer','Authorization','client-token'):
                                if args.Request.Headers.Contains(name):extra[name]=str(args.Request.Headers.GetHeader(name))
                            kind='playlist_observed';query=(playlist_request['request_id'],template,extra)
                    if kind!='playlist_observed':query=search_request_query(url,body)
                    if kind=='search' and diagnostic_request:
                        facts=catalog_request_diagnostic(url,body,method,body_read=method=='POST')
                        facts.pop('phase',None)
                        diagnostic_facts={**facts,'http_status':int(args.Response.StatusCode),
                                          'query_accepted':query is not None,
                                          'query_matches':query==diagnostic_request['title'].strip(),
                                          **catalog_query_diagnostic(query,diagnostic_request['title'].strip())}
                        observe('response')
                    if query is None:return
                status=int(args.Response.StatusCode)
                if status!=200:
                    if kind=='playlist_observed':
                        retry=60
                        if status==429:
                            try:retry=retry_after_seconds(args.Response.Headers.GetHeader('Retry-After'))
                            except Exception:pass
                        self.network.put(('playlist_error',query[0],status,retry if status==429 else 0));return
                    self.network.put(('response_error',kind,query,status));return
                task=args.Response.GetContentAsync()
                if kind=='search':observe('task_waiting')
                def read():
                    try:
                        deadline=time.monotonic()+10
                        while not task.IsCompleted:
                            if self.stopped.wait(.05):
                                if kind=='search':observe('read_cancelled')
                                return
                            if time.monotonic()>deadline:
                                if kind=='search':observe('read_timeout')
                                return
                        if task.IsFaulted or task.IsCanceled:
                            if kind=='search':observe('task_failed')
                            return
                        stream=task.Result
                        if stream is None:
                            if kind=='search':observe('stream_unavailable')
                            return
                        if kind=='search':observe('stream_reading')
                        reader=StreamReader(stream,Encoding.UTF8)
                        # Character bound prevents accidental huge response allocations.
                        from System import Array,Char
                        buffer=Array.CreateInstance(Char,8192);parts=[];size=0
                        while True:
                            count=reader.Read(buffer,0,len(buffer))
                            if not count:break
                            size+=count
                            if size>MAX_RESPONSE:raise ValueError('response size')
                            parts.append(''.join(str(buffer[i]) for i in range(count)))
                        reader.Dispose()
                        value=json.loads(''.join(parts))
                        if kind in ('search','playlist_observed') and isinstance(value,dict) and isinstance(value.get('data'),dict):
                            if authorization.startswith('Bearer '):self.network.put(('batch_credentials',authorization,client))
                        if kind=='search':observe('parsed')
                        self.network.put((kind,query,value))
                    except Exception:
                        if kind=='search':observe('read_failed')
                        if kind=='playlist_observed':
                            self.network.put(('playlist_error',query[0],0,0));return
                        self.network.put(('response_error',kind,query,0))
                threading.Thread(target=read,daemon=True,name='spotify-response').start()
            except Exception:
                # Exception messages may contain request URLs or headers.
                self.network.put(('response_error','browser',None,0))
        def install():
            core=self.window.native.browser.webview.CoreWebView2
            core.WebResourceResponseReceived+=receive
        self._receive=receive
        self.window.native.Invoke(Action(install))

    def begin_search(self):
        if not self.pending or not self.authenticated:return
        self.active_query=search_query(self.pending);self.fetching=False
        self.search_deadline=time.monotonic()+25
        self.status('searching','Spotify에서 현재 곡과 싱크를 확인하는 중')
        url='https://open.spotify.com/search/'+quote(self.active_query,safe='')
        if self.window.get_current_url()==url:
            # Assigning the same WebView2 Source does not trigger another request.
            self.reload()
        else:self.navigate_search(url)

    def navigate_search(self,url):
        # pywebview's Source=System.Uri(url) expands %2F on this runtime,
        # splitting titles such as 1/3 into different search route segments.
        from System import Action
        self.window.native.Invoke(Action(lambda:self.window.native.browser.webview.CoreWebView2.Navigate(url)))

    def reload(self):
        from System import Action
        self.window.native.Invoke(Action(lambda:self.window.native.browser.webview.CoreWebView2.Reload()))

    def command(self,command):
        action=command.get('action')
        if action=='close':
            self.finish_batch(error='Spotify 연결을 닫아 가사 수집을 취소했습니다.',code='cancelled')
            self.finish_playlist(error='Spotify 연결을 닫아 수집을 취소했습니다.',code='cancelled')
            self.finish_catalog(error='Spotify 연결을 닫아 검색을 취소했습니다.',code='cancelled');return False
        if action=='batch_lyrics':self.start_batch(command);return True
        if action=='batch_lyrics_cancel':
            if self.batch and self.batch['request_id']==command.get('request_id'):
                self.finish_batch(error='Spotify 가사 수집을 취소했습니다.',code='cancelled')
            return True
        if action=='catalog_cancel':
            if self.batch and self.batch['request_id']==command.get('request_id'):
                self.finish_batch(error='Spotify 가사 수집을 취소했습니다.',code='cancelled')
            if self.playlist and self.playlist['request_id']==command.get('request_id'):
                self.finish_playlist(error='Spotify 플레이리스트 수집을 취소했습니다.',code='cancelled')
            request=self.catalog_active or self.catalog_pending
            if request and request['request_id']==command.get('request_id'):
                self.finish_catalog(error='Spotify 곡 검색을 취소했습니다.',code='cancelled')
            return True
        if action=='catalog_playlist':
            request_id=command.get('request_id');playlist_id=command.get('playlist_id');offset=command.get('offset',0)
            if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',request_id):return True
            result={'request_id':request_id,'playlist_id':playlist_id if isinstance(playlist_id,str) and re.fullmatch(r'[a-zA-Z0-9]{22}',playlist_id) else '',
                    'tracks':[],'offset':offset if type(offset) is int and 0<=offset<=50000 else 0,'total':None,'complete':False}
            if not result['playlist_id'] or type(offset) is not int or not 0<=offset<=50000:
                emit('catalog_playlist_result',**result,error='플레이리스트와 시작 위치를 확인해 주세요.',code='query');return True
            if self.playlist or self.catalog_active or self.catalog_pending or self.batch:
                emit('catalog_playlist_result',**result,error='다른 Spotify 사전 조회가 진행 중입니다.',code='busy');return True
            if time.monotonic()<self.playlist_retry_until:
                retry=retry_after_seconds(self.playlist_retry_until-time.monotonic())
                emit('catalog_playlist_result',**result,error='Spotify 요청 제한 대기 중입니다.',code='rate_limited',retry_after=retry);return True
            self.playlist={**result,'started':False,'template':None,'headers':{},'fetching':False,'next_at':0,
                           'deadline':time.monotonic()+35,'pages':set(),'diagnostics':0}
            self.tick_playlist();return True
        if action=='catalog_search':
            request_id=command.get('request_id')
            if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',request_id):return True
            if not isinstance(command.get('title'),str) or not command['title'].strip() or len(command['title'])>4000:
                emit('catalog_result',request_id=request_id,tracks=[],error='검색할 곡명을 확인해 주세요.',code='query');return True
            if self.catalog_pending or self.catalog_active or self.playlist or self.batch:
                emit('catalog_result',request_id=request_id,tracks=[],error='다른 곡 검색이 진행 중입니다.',code='busy');return True
            self.catalog_pending={**command,'deadline':time.monotonic()+35}
            self.diagnostic_attempt+=1;self.catalog_diagnostic('queued')
            self.tick_catalog();return True
        if action in ('lookup','refresh','show'):
            self.finish_batch(error='현재 재생곡의 가사 연결을 우선해 배치 가사 수집을 중단했습니다.',code='interrupted')
            self.finish_playlist(error='현재 재생곡의 가사 연결을 우선해 플레이리스트 수집을 중단했습니다.',code='interrupted')
            self.finish_catalog(error='현재 재생곡의 가사 연결을 우선해 사전 검색을 중단했습니다. 다시 시도해 주세요.',code='interrupted')
        if action=='show':
            if not self.authenticated:
                show_original_window(self.window)
                self.auth_deadline=time.monotonic()+25
                self.window.load_url('https://accounts.spotify.com/login?continue=https%3A%2F%2Fopen.spotify.com%2F')
            elif self.pending:self.begin_search()
        elif action=='refresh':
            self.generation+=1;self.auth_retries=0
            self.active_query='';self.fetching=False;self.search_deadline=0
            if self.authenticated:self.begin_search()
            else:
                self.auth_deadline=time.monotonic()+25
                self.window.load_url('https://open.spotify.com/')
        elif action=='lookup':
            self.generation+=1;self.auth_retries=0
            self.pending=command if isinstance(command.get('key'),str) and command.get('key') and command.get('title') else None
            self.active_query='';self.fetching=False;self.search_deadline=0
            if self.pending and self.authenticated:self.begin_search()
            elif self.pending and not self.auth_deadline:
                # Each track needs a terminal response after login has failed;
                # otherwise the parent waits forever and blocks other providers.
                self.status('login_required','Spotify 연결 창에서 로그인 상태를 확인하세요.')
        return True

    def receive(self,event):
        kind=event[0]
        if kind=='batch_credentials':
            if self.authenticated:self.batch_credentials=(event[1],event[2])
        elif kind in ('batch_lyrics','batch_lyrics_error'):self.receive_batch(event)
        elif kind=='playlist_diagnostic':
            if self.playlist and self.playlist['request_id']==event[1]:self.playlist_diagnostic(event[2],**event[3])
        elif kind=='playlist_observed':
            request_id,template,headers=event[1]
            request=self.playlist
            if not request or request['request_id']!=request_id or request['template']:return
            offset=template['variables']['offset']
            try:page=playlist_page(event[2],request['playlist_id'],offset)
            except (ValueError,TypeError,AttributeError):
                self.playlist_diagnostic('unrecognized_page');return
            request['template']=template;request['headers']=headers
            request['deadline']=time.monotonic()+30;request['next_at']=time.monotonic()+1
            self.playlist_diagnostic('template_ready',total=page['total'],count=page['count'])
            if offset==request['offset']:self.accept_playlist_page(event[2],offset)
        elif kind=='playlist_page':
            if self.playlist and self.playlist['request_id']==event[1]:self.accept_playlist_page(event[3],event[2])
        elif kind=='playlist_error':
            if self.playlist and self.playlist['request_id']==event[1]:self.playlist_failure(event[2],event[3])
        elif kind=='catalog_diagnostic':
            request=self.catalog_active or self.catalog_pending
            if request and request['request_id']==event[1]:self.catalog_diagnostic(event[2],**event[3])
        elif kind=='headers':
            # An old anonymous request can finish after an authenticated token.
            # Only the token endpoint may replace a verified account token.
            if not self.authorization:self.authorization=event[1]
            if event[2]:self.client_token=event[2]
        elif kind=='token':
            value=event[2]
            if not isinstance(value,dict):return
            token=value.get('accessToken')
            if value.get('isAnonymous') is False and isinstance(token,str) and token:
                first=not self.authenticated
                self.authenticated=True;self.authorization='Bearer '+token
                self.batch_credentials=None
                self.auth_deadline=0
                if first:
                    self.window.hide()
                    self.status('ready','Spotify 계정 연결됨 · 화면 없이 가사 조회 가능')
                    self.begin_search()
            elif value.get('isAnonymous') is True:
                self.generation+=1;self.fetching=False;self.search_deadline=0
                self.auth_deadline=0
                self.authenticated=False;self.authorization='';self.active_query=''
                self.batch_credentials=None
                self.finish_batch(error='기존 Spotify 연결 창에서 먼저 로그인해 주세요.',code='login_required')
                self.finish_playlist(error='기존 Spotify 연결 창에서 먼저 로그인해 주세요.',code='login_required')
                self.finish_catalog(error='기존 Spotify 연결 창에서 먼저 로그인해 주세요.',code='login_required')
                self.status('login_required','Spotify 연결 창에서 로그인하세요. 가사창은 켤 필요가 없습니다.')
        elif kind=='search':
            if self.catalog_active and event[1]==self.catalog_query:
                if not self.authenticated:
                    self.finish_catalog(error='기존 Spotify 연결 창에서 먼저 로그인해 주세요.',code='login_required');return
                try:tracks=catalog_tracks(event[2])
                except (ValueError,TypeError,AttributeError):
                    self.finish_catalog(error='Spotify 검색 결과를 읽지 못했습니다. 다시 시도해 주세요.',code='response');return
                self.finish_catalog(tracks);return
            if not self.authenticated or not self.pending or self.fetching or event[1]!=self.active_query:return
            candidate=select_track(self.pending,search_tracks(event[2]))
            if not candidate:
                self.search_deadline=0;self.active_query=''
                self.status('not_found','Spotify에서 제목·가수·길이가 일치하는 곡을 확정하지 못했습니다.');return
            metadata=validated_track(self.pending,candidate)
            if metadata:
                emit('metadata',key=self.pending['key'],request_duration=self.pending['duration'],track=metadata)
            if self.pending.get('metadata_only'):
                self.search_deadline=0;self.active_query=''
                self.status('ready' if metadata else 'not_found','Spotify 가수 정보 확인 완료')
                return
            self.fetching=True;self.search_deadline=0
            generation,key=self.generation,self.pending['key']
            authorization,client=self.authorization,self.client_token
            def fetch():
                try:
                    payload=fetch_lyrics(candidate['id'],authorization,client)
                    self.network.put(('lyrics',generation,key,candidate,payload))
                except HTTPError as error:self.network.put(('fetch_error',generation,int(error.code)))
                except Exception:self.network.put(('fetch_error',generation,0))
            threading.Thread(target=fetch,daemon=True,name='spotify-lyrics-api').start()
        elif kind=='lyrics':
            if event[1]!=self.generation:return
            self.fetching=False;self.active_query=''
            try:lines=lyric_lines(event[4],event[3]['duration'])
            except ValueError:
                # Invalid/missing timestamps must not hide otherwise valid originals.
                # Validate the text again, retaining no untrusted timing values.
                try:lines=lyric_lines({'lyrics':{**event[4]['lyrics'],'syncType':'UNSYNCED'}})
                except (KeyError,TypeError,ValueError):
                    self.status('error','Spotify 가사 응답을 확인하지 못했습니다. 연결을 다시 시도하세요.');return
            emit('lyrics',key=event[2],source='Spotify',track_id=event[3]['id'],lines=lines)
            self.window.hide()
            if all(line['time'] is not None for line in lines):
                self.status('ready','Spotify 싱크 연결됨 · 최소화하거나 가사창을 닫아도 됩니다.')
            else:self.status('unsynced','Spotify 원문 연결됨 · 현재 줄별 싱크를 사용할 수 없습니다.')
        elif kind=='fetch_error':
            if event[1]!=self.generation:return
            self.fetching=False;self.active_query=''
            code=event[2]
            if code==404:self.status('not_found','이 곡은 Spotify 가사 API에서 가사를 제공하지 않습니다.')
            elif code==401:
                self.authenticated=False;self.authorization=''
                if self.auth_retries<1:
                    self.auth_retries+=1;self.auth_deadline=time.monotonic()+25
                    self.status('searching','Spotify 인증을 새로 확인하는 중')
                    # Let the normal web player renew its session, once, hidden.
                    self.reload()
                else:self.status('login_required','Spotify 인증이 만료되었습니다. 연결 창에서 다시 로그인하세요.')
            elif code==429:self.status('error','Spotify 요청이 잠시 제한되었습니다. 잠시 후 연결을 다시 시도하세요.')
            else:self.status('error','Spotify 가사 조회 실패'+(f' (HTTP {code})' if code else '')+' · 연결을 다시 시도하세요.')
        elif kind=='response_error' and event[1]=='search':
            if self.catalog_active and event[2]==self.catalog_query:
                limited=len(event)>3 and event[3]==429
                self.finish_catalog(error='Spotify 요청 제한 · 잠시 후 다시 확인합니다.' if limited else
                    'Spotify 곡 검색 응답을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.',
                    code='rate_limited' if limited else 'response')
            elif event[2]==self.active_query:
                self.search_deadline=0;self.active_query=''
                self.status('error','Spotify 곡 검색 응답을 확인하지 못했습니다. 연결을 다시 시도하세요.')

    def run(self):
        if not self.window.events.loaded.wait(20):raise TimeoutError('startup')
        self.install_hooks()
        self.window.load_url('https://accounts.spotify.com/login?continue=https%3A%2F%2Fopen.spotify.com%2F'
                             if self.login_requested else 'https://open.spotify.com/')
        while not self.stopped.is_set():
            try:
                command=self.commands.get(timeout=.1)
                if not self.command(command):break
            except queue.Empty:pass
            for unused in range(100):
                try:self.receive(self.network.get_nowait())
                except queue.Empty:break
            now=time.monotonic()
            if self.batch and now>=self.batch['deadline']:
                self.finish_batch(error='Spotify 배치 가사 조회가 지연되었습니다.',code='timeout')
            if not self.authenticated and self.auth_deadline and now>=self.auth_deadline:
                self.auth_deadline=0
                self.status('login_required','Spotify 연결 창에서 로그인하거나 연결 상태를 확인하세요.')
            if self.search_deadline and now>=self.search_deadline:
                self.search_deadline=0;self.active_query=''
                self.status('error','Spotify 곡 검색이 25초 안에 응답하지 않았습니다. 연결 창을 확인하세요.')
            self.tick_catalog(now)
            self.tick_playlist(now)


def main():
    request=json.loads(sys.stdin.readline())
    profile=Path(request['profile']).resolve();profile.mkdir(parents=True,exist_ok=True)
    import webview
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER']=False
    webview.settings['ALLOW_DOWNLOADS']=False
    visible=bool(request.get('visible'))
    window=webview.create_window('Spotify 가사 · 계정 연결',html='<html><body>Spotify 연결 준비 중</body></html>',
                                width=900,height=720,hidden=not visible,focus=visible)
    if not visible:window.events.initialized+=prepare_background_window
    stopped=threading.Event();commands=queue.Queue()
    window.events.closed+=lambda:stopped.set()
    def control():
        for line in sys.stdin:
            try:command=json.loads(line)
            except ValueError:continue
            if not isinstance(command,dict):continue
            commands.put(command)
            if command.get('action')=='close':return
        commands.put({'action':'close'})
    def run():
        threading.Thread(target=control,daemon=True,name='spotify-control').start()
        try:SpotifyBrowser(window,commands,stopped,login_requested=visible,
                           diagnostics=os.environ.get('SPOTIFY_CATALOG_DIAGNOSTICS')=='1').run()
        except Exception as error:
            if not stopped.is_set():emit('status',state='error',text=f'Spotify 연결 브라우저 실패 ({type(error).__name__}) · 연결을 다시 시도하세요.')
        finally:
            stopped.set()
            try:window.destroy()
            except Exception:pass
    webview.start(run,gui='edgechromium',private_mode=False,storage_path=str(profile))


if __name__=='__main__':main()
