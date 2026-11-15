"""Pure Spotify link and candidate helpers; uses no API credentials or account data."""
from difflib import SequenceMatcher
import json
import math
import re
import unicodedata
from urllib.parse import parse_qs,unquote,urlparse

from spotify_api import text_key


def catalog_diagnostic_record(value):
    """Accept only bounded operational facts, never request or response content."""
    if not isinstance(value,dict):return {}
    enums={
        'phase':{'queued','navigation_requested','navigation','response','task_waiting','stream_reading',
                 'parsed','stream_unavailable','read_failed','read_timeout','read_cancelled','task_failed','finished'},
        'page':{'home','search','login','track','other'},
        'navigation':{'load','reload'},'method':{'GET','POST','other'},
        'endpoint':{'pathfinder_v1','pathfinder_v2','pathfinder','pathfinder_other','partner_other'},
        'request_format':{'query','object','array','empty','invalid','unread'},
        'result':{'success','timeout','login_required','browser','cancelled','interrupted','rate_limited','response'},
    }
    result={key:item for key,allowed in enums.items()
            if isinstance(item:=value.get(key),str) and item in allowed}
    if 'phase' not in result:return {}
    for key in ('authenticated','lyric_pending','lyric_search_pending','lyric_fetch_pending',
                'catalog_pending','catalog_active','query_accepted','query_matches',
                'query_trim_matches','query_casefold_matches','query_nfkc_matches','query_normalized_matches',
                'query_unquoted_matches','query_percent_decoded_matches'):
        if isinstance(value.get(key),bool):result[key]=value[key]
    for key,minimum,maximum in (('attempt',1,1_000_000_000),('http_status',100,599),
                                ('query_length',0,10000),('requested_length',0,10000)):
        item=value.get(key)
        if type(item) is int and minimum<=item<=maximum:result[key]=item
    elapsed=value.get('elapsed')
    if type(elapsed) in (int,float) and 0<=elapsed<=600:result['elapsed']=round(elapsed,1)
    operation=value.get('operation')
    if isinstance(operation,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}',operation):
        result['operation']=operation
    return result


def catalog_query_diagnostic(query,requested):
    """Classify a mismatch without exposing either string or accepting it."""
    if not isinstance(query,str) or not isinstance(requested,str):return {}
    quotes=' \t\r\n\"\'“”‘’「」'
    return {'query_length':len(query),'requested_length':len(requested),
            'query_trim_matches':query.strip()==requested.strip(),
            'query_casefold_matches':query.casefold()==requested.casefold(),
            'query_nfkc_matches':unicodedata.normalize('NFKC',query)==unicodedata.normalize('NFKC',requested),
            'query_normalized_matches':text_key(query)==text_key(requested),
            'query_unquoted_matches':query.strip(quotes)==requested.strip(quotes),
            'query_percent_decoded_matches':unquote(query)==unquote(requested)}


def catalog_request_diagnostic(url,body='',method='GET',body_read=False):
    """Extract operation metadata only; exclude URL, variables and raw body."""
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname!='api-partner.spotify.com':return {}
    endpoint={'/pathfinder/v1/query':'pathfinder_v1','/pathfinder/v2/query':'pathfinder_v2',
              '/pathfinder/query':'pathfinder'}.get(parsed.path)
    if endpoint is None:endpoint='pathfinder_other' if parsed.path.startswith('/pathfinder/') else 'partner_other'
    facts={'phase':'response','endpoint':endpoint,'method':method if method in ('GET','POST') else 'other'}
    request=None
    try:
        if method=='GET':
            request={key:values[0] for key,values in parse_qs(parsed.query).items()}
            facts['request_format']='query'
        elif not body_read:facts['request_format']='unread'
        elif not body:facts['request_format']='empty'
        else:
            request=json.loads(body)
            facts['request_format']='object' if isinstance(request,dict) else 'array' if isinstance(request,list) else 'invalid'
    except (ValueError,TypeError,IndexError):facts['request_format']='invalid'
    if isinstance(request,dict):facts['operation']=request.get('operationName')
    return catalog_diagnostic_record(facts)


def catalog_page_kind(url):
    if not isinstance(url,str):return 'other'
    try:
        parsed=urlparse(url)
        if parsed.hostname=='accounts.spotify.com':return 'login'
        if parsed.hostname!='open.spotify.com':return 'other'
        path=re.sub(r'^/intl-[a-z]{2}(?=/|$)','',parsed.path)
        if path in ('','/'):return 'home'
        if path.startswith('/search'):return 'search'
        if path.startswith('/track/'):return 'track'
        return 'other'
    except (ValueError,TypeError):return 'other'


class SpotifyCatalogError(Exception):
    def __init__(self, message, code='', retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def parse_track_id(value):
    """Accept an exact track ID, URI or HTTPS Spotify track URL, else None."""
    return _parse_spotify_id(value, 'track')


def parse_playlist_id(value):
    """Accept an exact playlist ID, URI or HTTPS Spotify playlist URL, else None."""
    return _parse_spotify_id(value, 'playlist')


def _parse_spotify_id(value, kind):
    if not isinstance(value, str) or len(value) > 2048:return None
    value = value.strip()
    if re.fullmatch(r'[A-Za-z0-9]{22}', value):return value
    match = re.fullmatch(rf'spotify:{kind}:([A-Za-z0-9]{{22}})', value)
    if match:return match[1]
    try:
        parsed = urlparse(value)
        if (parsed.scheme != 'https' or parsed.hostname != 'open.spotify.com'
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return None
        match = re.fullmatch(rf'/(?:intl-[a-z]{{2}}/)?{kind}/([A-Za-z0-9]{{22}})/?', parsed.path)
        return match[1] if match else None
    except ValueError:return None


def _field(song, name, default=''):
    return song.get(name, default) if isinstance(song, dict) else getattr(song, name, default)


def _duration(value):
    try:duration = float(value)
    except (TypeError, ValueError, OverflowError):return 0
    return duration if not isinstance(value, bool) and math.isfinite(duration) and duration > 0 else 0


def _artists(value):
    if not isinstance(value, (list, tuple)):return set()
    return {text_key(name) for name in value if isinstance(name, str) and name.strip()}


def rank_candidates(song, tracks):
    """Rank suggestions only; never confirm a recording or infer its playability."""
    title = text_key(_field(song, 'title'))
    album = text_key(_field(song, 'album'))
    artists = _field(song, 'artists', None)
    if not isinstance(artists, (list, tuple)):
        artists = [_field(song, 'artist')]
    artists = _artists(artists)
    duration = _duration(_field(song, 'duration', 0))
    ranked = []
    for track in tracks:
        if not isinstance(track, dict) or not parse_track_id(track.get('id')):continue
        candidate_title = text_key(track.get('title'))
        exact = bool(title and title == candidate_title)
        score = 60 if exact else round(30 * SequenceMatcher(None, title, candidate_title).ratio())
        reasons = ['제목 일치'] if exact else ['제목 또는 버전 확인 필요']
        if artists and artists & _artists(track.get('artists')):
            score += 20;reasons.append('가창자 일치')
        elif artists:reasons.append('가창자 확인 필요')
        album_conflict = bool(album and text_key(track.get('album'))
                              and album != text_key(track.get('album')))
        if album and album == text_key(track.get('album')):
            score += 15;reasons.append('앨범 일치')
        elif album:reasons.append('앨범 확인 필요')
        candidate_duration = _duration(track.get('duration'))
        if duration and candidate_duration and abs(candidate_duration - duration) <= 2:
            score += 5;reasons.append('길이 일치')
        elif duration:reasons.append('길이 확인 필요')
        ranked.append({**track, 'match_score': score, 'match_reasons': reasons,
                       'version_conflict': not exact or album_conflict})
    return sorted(ranked, key=lambda item: -item['match_score'])
