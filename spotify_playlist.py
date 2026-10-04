"""Validate observed playlist read requests and public track facts, without I/O."""
import copy
import hashlib
import json
import re
from urllib.parse import parse_qs,urlparse

from spotify_api import search_tracks

MAX_PLAYLIST_ITEMS=50000
PLAYLIST_OPERATIONS={'fetchPlaylist','fetchPlaylistContents','getPlaylist','getPlaylistContents','getPlaylistItems'}
PLAYLIST_PATHS={'/pathfinder/v1/query','/pathfinder/v2/query','/pathfinder/query'}


def observed_request(url,body='',method='GET'):
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.netloc!='api-partner.spotify.com' or parsed.path not in PLAYLIST_PATHS:return None
    if method not in ('GET','POST'):return None
    try:
        if body:
            if len(body)>262144:return None
            value=json.loads(body)
        else:value={key:items[0] for key,items in parse_qs(parsed.query).items()}
        if not isinstance(value,dict):return None
        variables=value.get('variables',{})
        if isinstance(variables,str):variables=json.loads(variables)
        extensions=value.get('extensions',{})
        if isinstance(extensions,str):extensions=json.loads(extensions)
        if not isinstance(variables,dict) or not isinstance(extensions,dict):return None
        return {'path':parsed.path,'method':method,'operationName':value.get('operationName'),
                'variables':variables,'extensions':extensions}
    except (ValueError,TypeError,AttributeError):return None


def playlist_request_diagnostic(request,playlist_id):
    """No values, URLs, hashes, headers or arbitrary variable names leave the worker."""
    if not isinstance(request,dict):return {}
    operation=request.get('operationName')
    facts={}
    if isinstance(operation,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',operation):facts['operation']=operation
    variables=request.get('variables',{})
    facts['variable_keys']=[key for key in ('uri','playlistUri','id','playlistId','offset','limit') if key in variables]
    facts['playlist_matches']=any(variables.get(key) in ('spotify:playlist:'+playlist_id,playlist_id)
                                  for key in ('uri','playlistUri','id','playlistId'))
    facts['has_persisted_query']=isinstance(request.get('extensions',{}).get('persistedQuery'),dict)
    return facts


def playlist_template(request,playlist_id):
    if not isinstance(request,dict) or request.get('operationName') not in PLAYLIST_OPERATIONS:return None
    if request.get('path') not in PLAYLIST_PATHS or request.get('method') not in ('GET','POST'):return None
    variables=request.get('variables',{})
    if not any(variables.get(key)=='spotify:playlist:'+playlist_id for key in ('uri','playlistUri')):
        if not any(variables.get(key)==playlist_id for key in ('id','playlistId')):return None
    offset,limit=variables.get('offset'),variables.get('limit')
    if type(offset) is not int or not 0<=offset<=MAX_PLAYLIST_ITEMS:return None
    if type(limit) is not int or not 1<=limit<=500:return None
    persisted=request.get('extensions',{}).get('persistedQuery',{})
    if not isinstance(persisted,dict) or persisted.get('version')!=1:return None
    digest=persisted.get('sha256Hash')
    if not isinstance(digest,str) or not re.fullmatch(r'[a-fA-F0-9]{64}',digest):return None
    return copy.deepcopy(request)


def playlist_page(payload,playlist_id,offset):
    """Return validated tracks plus raw row counts; local/missing rows still count."""
    if not isinstance(payload,dict) or payload.get('errors'):raise ValueError('response')
    data=payload.get('data')
    if not isinstance(data,dict):raise ValueError('response')
    playlist=data.get('playlistV2',data.get('playlist'))
    if not isinstance(playlist,dict):raise ValueError('shape')
    uri=playlist.get('uri')
    if uri and uri!='spotify:playlist:'+playlist_id:raise ValueError('playlist_mismatch')
    content=playlist.get('content',playlist.get('contents'))
    if not isinstance(content,dict):raise ValueError('shape')
    items=content.get('items')
    total=content.get('totalCount')
    if not isinstance(items,list) or len(items)>500:raise ValueError('shape')
    if type(total) is not int or not 0<=total<=MAX_PLAYLIST_ITEMS:raise ValueError('total')
    paging=content.get('pagingInfo',{})
    if isinstance(paging,dict) and 'offset' in paging and paging['offset']!=offset:raise ValueError('offset')
    if offset<0 or offset+len(items)>total:raise ValueError('total')
    if not items and offset<total:raise ValueError('no_progress')
    identities=[];ordered=[]
    for position,item in enumerate(items):
        row={'index':offset+position,'status':'unavailable','track_id':'','track':None}
        ordered.append(row)
        if not isinstance(item,dict):identities.append(None);continue
        wrapper=item.get('itemV2',item.get('item',item.get('track',item)))
        if not isinstance(wrapper,dict):identities.append(None);continue
        value=wrapper.get('data',wrapper)
        if not isinstance(value,dict):identities.append(None);continue
        identities.append(value.get('uri') or wrapper.get('_uri') or value.get('__typename'))
        kind=value.get('__typename','Track')
        if kind=='LocalTrack':row['status']='local';continue
        if kind not in ('Track','NotFound','Unavailable'):
            row['status']='unsupported';continue
        if kind!='Track':continue
        track_uri=value.get('uri') or wrapper.get('_uri') or ''
        identifier=re.fullmatch(r'spotify:track:([A-Za-z0-9]{22})',track_uri) if isinstance(track_uri,str) else None
        if identifier:row['track_id']=identifier[1]
        row['status']='incomplete'
        artists=value.get('artists')
        if not isinstance(artists,dict) or not isinstance(artists.get('items'),list):continue
        artist_count=artists.get('totalCount')
        # A partial performer list could match the wrong recording or replace
        # complete cached metadata. It still counts as a visited playlist row.
        if artist_count is not None and (type(artist_count) is not int or artist_count!=len(artists['items'])):continue
        # Validate each row separately so duplicate, missing and rejected rows
        # retain their original positions in the manifest.
        parsed_wrapper=wrapper
        # Observed playlist responses name this field trackDuration, unlike
        # search results. Do not replace a present but invalid duration field.
        if 'duration' not in value and 'trackDuration' in value:
            parsed_wrapper={**wrapper,'data':{**value,'duration':value['trackDuration']}}
        tracks=search_tracks({'data':{'searchV2':{'tracksV2':{'items':[{'item':parsed_wrapper}]}}}})
        if not tracks:continue
        track=tracks[0]
        row.update(status='track',track_id=track['id'],track={**track,
            'url':'https://open.spotify.com/track/'+track['id'],
            'source_url':'https://open.spotify.com/playlist/'+playlist_id,
            'is_playable':None,'checked_at':''})
    unique={row['track_id']:row['track'] for row in ordered if row['status']=='track'}
    fingerprint=hashlib.sha256(json.dumps(identities,ensure_ascii=False).encode('utf-8')).hexdigest()
    return {'tracks':list(unique.values()),'start':offset,'entries':ordered,'offset':offset+len(items),'total':total,
            'count':len(items),'fingerprint':fingerprint,'complete':offset+len(items)==total}
