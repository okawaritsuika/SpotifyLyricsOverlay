"""Validate Spotify search/lyrics data without browser or account access."""
import json
import math
import re
import unicodedata
from urllib.parse import parse_qs,urlparse


def text_key(value):
    return ' '.join(unicodedata.normalize('NFKC',str(value or '')).casefold().split())


def search_query(request):
    return ' '.join(str(request.get(name,'')).strip() for name in ('title','artist')).strip()


def search_request_query(url,body=''):
    """Only accept a real Spotify search operation, never arbitrary response JSON."""
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname!='api-partner.spotify.com':return None
    if parsed.path not in ('/pathfinder/v1/query','/pathfinder/v2/query','/pathfinder/query'):return None
    try:
        if body:
            request=json.loads(body)
        else:
            params=parse_qs(parsed.query)
            request={name:values[0] for name,values in params.items()}
        operation=request.get('operationName')
        if operation not in ('searchDesktop','searchTracks','searchTopResultsList'):return None
        variables=request.get('variables',{})
        if isinstance(variables,str):variables=json.loads(variables)
        query=variables.get('query' if operation=='searchTopResultsList' else 'searchTerm')
        return query if isinstance(query,str) and query else None
    except (ValueError,TypeError,AttributeError,IndexError):return None


def search_tracks(payload):
    """Read track results from both signed-in and anonymous web player layouts."""
    try:
        search=payload['data']['searchV2']
        entries=[]
        for name in ('tracksV2','tracks','topResultsV2'):
            section=search.get(name) or {}
            items=section.get('itemsV2') or section.get('items') or []
            if isinstance(items,list):entries.extend(items[:100])
    except (KeyError,TypeError,AttributeError):return []
    tracks=[]
    for entry in entries:
        try:
            wrapper=entry.get('item',entry)
            data=wrapper.get('data',wrapper)
            if data.get('__typename','Track')!='Track':continue
            uri=data.get('uri') or wrapper.get('_uri') or wrapper.get('uri','')
            match=re.fullmatch(r'spotify:track:([A-Za-z0-9]{22})',uri)
            if not match:continue
            artist_data=data.get('artists',{})
            artist_items=artist_data.get('items',[])
            if not isinstance(artist_items,list):continue
            artist_count=artist_data.get('totalCount')
            # A known partial list must not match a solo recording or replace
            # complete artist metadata for the same Spotify track ID.
            if artist_count is not None and (type(artist_count) is not int or artist_count!=len(artist_items)):continue
            artists=[a.get('profile',{}).get('name','') for a in artist_items]
            duration=float(data.get('duration',{}).get('totalMilliseconds',0))/1000
            if (not isinstance(data.get('name'),str) or not data['name'].strip() or len(data['name'])>4000
                    or not 0<len(artists)<=200 or any(not isinstance(name,str) or not name.strip() or len(name)>512 for name in artists)
                    or not isinstance(data.get('albumOfTrack',{}).get('name',''),str)
                    or not math.isfinite(duration) or duration<=0):continue
            tracks.append({'id':match[1],'title':data['name'],'artists':artists,
                           'album':data.get('albumOfTrack',{}).get('name',''),'duration':duration})
        except (TypeError,ValueError,AttributeError,OverflowError):continue
    return tracks


def artist_matches(artist,names):
    wanted=text_key(artist)
    if not wanted:return False
    # GSMTC may provide only the lead artist, or the full joined list.
    variants=[names[0]] if names else []
    variants.extend(separator.join(names) for separator in (', ','; ',' / ',' & ','、'))
    return wanted in {text_key(value) for value in variants}


def select_track(request,tracks):
    """Never remove version suffixes when choosing a timestamp recording."""
    try:duration=float(request.get('duration',0))
    except (TypeError,ValueError):return None
    if not math.isfinite(duration) or duration<=0:return None
    candidates={}
    for track in tracks:
        if (text_key(track.get('title'))!=text_key(request.get('title'))
            or not artist_matches(request.get('artist'),track.get('artists',[]))):continue
        try:delta=abs(float(track['duration'])-duration)
        except (TypeError,ValueError,KeyError):continue
        if not math.isfinite(delta) or delta>2:continue
        candidates[track['id']]=track
    album=text_key(request.get('album'))
    album_matches=[track for track in candidates.values() if album and text_key(track.get('album'))==album]
    # A known album is part of the recording identity. A different release can
    # contain a live/re-recorded take with the same title, singer and length.
    choices=album_matches if album else list(candidates.values())
    return choices[0] if len(choices)==1 else None


def lyric_lines(payload,duration=0):
    """Return validated originals and optional seconds; reject corrupt timing."""
    lyrics=payload.get('lyrics') if isinstance(payload,dict) else None
    if not isinstance(lyrics,dict) or not isinstance(lyrics.get('lines'),list):raise ValueError('lyrics')
    synced=lyrics.get('syncType')=='LINE_SYNCED'
    result=[];previous=-1.0
    if len(lyrics['lines'])>2000:raise ValueError('line count')
    for line in lyrics['lines']:
        if not isinstance(line,dict):raise ValueError('line')
        words=line.get('words')
        if not isinstance(words,str) or len(words)>4000:raise ValueError('words')
        words=words.strip()
        if not words or words in ('♪','♫','♬'):continue
        stamp=None
        if synced:
            raw=line.get('startTimeMs')
            if isinstance(raw,bool):raise ValueError('timestamp')
            try:stamp=float(raw)/1000
            except (TypeError,ValueError,OverflowError):raise ValueError('timestamp') from None
            if not math.isfinite(stamp) or stamp<previous or stamp<0 or stamp>604800:raise ValueError('timestamp')
            if duration and stamp>duration+2:raise ValueError('duration')
            previous=stamp
            if result and result[-1]['time']==stamp:
                result[-1]['text']+=' '+words
                continue
        result.append({'text':words,'time':stamp})
    if not result:raise ValueError('empty lyrics')
    return result
