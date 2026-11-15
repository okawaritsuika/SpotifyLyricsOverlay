"""Music dictionary operations for the Electron workspace."""
from hashlib import sha256
import json
from pathlib import Path
import queue
import re
import threading
from functools import lru_cache
from tkinter import filedialog
import unicodedata
from urllib.parse import quote, urlsplit
import webbrowser

from artist_colors import _artist_ranges, character_color_spans
from artist_names import artist_label, character_name_only, name_key
from app_paths import resource_dir
from explorer_library import (ExplorerLibrary, SERIES, SERIES_COLORS, SONG_SORTS, artist_key,
    availability, build_performer_lookup, cached_candidates, franchise_key, group_duplicate_songs,
    performers, related_songs,
    resolve_group_members, search_key, series_label, song_release_date, song_search_text, sort_songs)
from group_credits import is_project_credit
from live_catalog import LiveCatalog
from local_library import LocalLibrary
from song_aliases import search_aliases
from song_catalog import PERSONAL_CATALOG_NAME, SongCatalog
from spotify_catalog import SpotifyCatalogError, parse_track_id, rank_candidates
from spotify_desktop import DesktopSpotify


SONG_KINDS = {'': '전체 유형', 'song': '노래', 'instrumental': '연주 · 반주',
              'spoken': '드라마 · 토크', 'unknown': '유형 미확인'}
SERIES_OPTIONS = [dict(id=key,label=label,color=SERIES_COLORS[key])
                  for key,label in SERIES.items() if key]
PAGE_SIZE = 40
GROUP_MEMBER_PREVIEW = 5
GROUP_SONG_PREVIEW = 4
CV_CREDIT = re.compile(r'(.*?)\s*\(\s*CV\s*[.:]?\s*(.*?)\s*\)', re.I)
LIVE_ROLE = re.compile(r'^(サポート|バックダンス|コーラス・バックダンス|バックダンサー|コーラス|演劇パート参加|参加者|ダンスパフォーマンス|ダンス参加|友情出演|途中出演|コール|ギター|指揮|DJ)\s*:\s*', re.I)
LIVE_ROLES = dict(zip(('サポート','バックダンス','コーラス・バックダンス','バックダンサー','コーラス',
                      '演劇パート参加','参加者','ダンスパフォーマンス','ダンス参加','友情出演','途中出演','コール','ギター','指揮','dj'),
                      ('지원','백댄스','코러스 · 백댄스','백댄스','코러스','연극','참가','댄스','댄스','우정 출연','중간 출연','콜','기타','지휘','DJ')))
LIVE_GENERIC = re.compile(r'^(?:(?:ゲスト含む|ほか|他|出演者|キャスト|アンコール参加者|アイドル)?全員|\[?(?:all cast|all members)\]?|会場のプロデューサー|アンサンブル)(?:$|\s|\()', re.I)
LIVE_NAME_NORMALIZATION = str.maketrans({'髙':'高','﨑':'崎','濵':'浜','濱':'浜'})
SHINY_UNITS = ('イルミネーションスターズ', 'アンティーカ', '放課後クライマックスガールズ',
               'アルストロメリア', 'ストレイライト', 'ノクチル', 'シーズ', 'コメティック')
SHINY_UNIT_ORDER = {name: index for index, name in enumerate(SHINY_UNITS)}


def shiny_group_category(person):
    if person.get('series') != 'shiny' or person['kind'] != 'group':
        return None
    if person['name'] in SHINY_UNIT_ORDER:
        return 'unit'
    return 'project' if person['name'] == 'シャイニーカラーズ' else 'collaboration'


@lru_cache(maxsize=1)
def idol_roster_order():
    ranks={}
    # Feature the new generations and DRAMATIC STARS trios, then use the
    # series order recorded in the source-backed character lists.
    for series,names in {'cinderella':('島村卯月','渋谷凛','本田未央'),
                         'sidem':('天道輝','桜庭薫','柏木翼')}.items():
        for name in names:ranks[series,artist_key(name)]=len(ranks)
    directory=Path(__file__).resolve().parent/'resources'/'artist_names'
    for source in sorted(directory.glob('*.json')):
        entries=json.loads(source.read_text(encoding='utf-8')).get('entries',())
        for entry in entries:
            if entry.get('kind')=='idol':
                ranks.setdefault((franchise_key(entry.get('franchise')),artist_key(entry['name'])),len(ranks))
    return ranks


def page_of(rows, page):
    if type(page) is not int:
        raise ValueError('페이지 번호를 확인하세요.')
    total=len(rows)
    page=max(1,min(page,(total+PAGE_SIZE-1)//PAGE_SIZE))
    start=(page-1)*PAGE_SIZE
    return rows[start:start+PAGE_SIZE], dict(total=total,page=page,pageSize=PAGE_SIZE,series=SERIES_OPTIONS)


def search_options(payload):
    query, series = payload.get('query',''),payload.get('series','')
    if not isinstance(query,str) or not isinstance(series,str) or series not in SERIES:
        raise ValueError('검색 조건이 올바르지 않습니다.')
    return query[:200],series,payload.get('page',1)


def string_key(payload, field):
    value = payload.get(field)
    if not isinstance(value,str) or not value or len(value)>256:
        raise ValueError('목록에서 항목을 선택하세요.')
    return value


def modification_time(file):
    try:return file.stat().st_mtime_ns
    except FileNotFoundError:return None


def live_name_key(value):
    return artist_key(value).translate(LIVE_NAME_NORMALIZATION)


def live_qualifiers(value):
    """Split parenthetical source credits without dropping nested CV names."""
    head, tail = [], []
    start = depth = 0
    for index, char in enumerate(value):
        if char == '(':
            if depth == 0:
                head.append(value[start:index])
                start = index + 1
            depth += 1
        if char == ')' and depth:
            depth -= 1
            if depth == 0:
                tail.append(value[start:index].strip())
                start = index + 1
    head.append(value[start:])
    return ' '.join(part.strip() for part in head if part.strip()), tail


def person_row(person):
    name=person['name']
    voices=person.get('voice_actors',())
    brand='valiv' if person.get('franchise')=='vα-liv' else None
    return colored_credit(dict(id=person.get('id') if person['kind']!='unknown' else None,
                               title=name,localizedName=person.get('display') or name,
                               artists=[actor['name'] for actor in voices],
                               localizedVoices=[actor.get('display') or actor['name'] for actor in voices],
                               series=person.get('series',''),brand=brand,
                               kind={'group':'그룹','idol':'아이돌'}.get(person['kind'],''),
                               groupCategory=shiny_group_category(person) or person.get('catalog_group_category'),
                               color=person.get('color',''),memberStatus=person.get('member_status')))


def gakuen_catalog_groups(idols):
    """Expose documented formations alongside the original performer roster."""
    path=resource_dir()/'resources'/'gakuen_groups.json'
    try:
        entries=json.loads(path.read_text(encoding='utf-8'))['groups']
    except FileNotFoundError:
        return []
    groups=[]
    for entry in entries:
        members=[idols.get(artist_key(name)) for name in entry['members']]
        if any(not member or member['series']!='gakuen' for member in members):
            continue
        named=bool(entry.get('name'))
        name=entry.get('name') or ' × '.join(member['name'] for member in members)
        display=entry.get('ko') or ' × '.join(member['display'] for member in members)
        identity=('group:'+artist_key(name) if named else 'gakuen:'+entry['category']+':'
                  +'|'.join(sorted(artist_key(member['name']) for member in members)))
        groups.append(dict(id=identity,name=name,display=display,ko=display,kind='group',
                           series='gakuen',franchise='Gakuen Idolmaster',
                           catalog_group_category=entry['category'],published_name=named,
                           members=[member['name'] for member in members],aliases=entry.get('aliases',[]),
                           source_urls=entry['source_urls'],recording_ids=frozenset(entry.get('recording_ids',[])),
                           live_performances=entry.get('live_performances',[])))
    return groups


def colored_credit(row,album=''):
    if not row.get('color'):
        title=row['title']
        spans=character_color_spans(title,album or 'THE IDOLM@STER')
        if len(spans)==1 and spans[0][:2]==(0,len(title)):
            value=spans[0][2]
            if isinstance(value,tuple):row['palette']=value
            else:row['color']=value
    return row


def spotify_recording(track):
    return bool(track and track.get('is_playable') is not False and not track.get('restriction'))


def live_row(event):
    brand='valiv' if 'vα-liv' in event.get('series_names',()) else None
    return dict(id=event['id'],title=event['title'],artists=[],album=event.get('venue',''),
                series=next(iter(event['series']),'other'),brand=brand,kind=event['date'],date=event['date'])


def local_row(file):
    return {field:file.get(field) for field in ('id','title','artists','album','duration')}


def open_source(url):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('열 수 있는 HTTPS 출처 주소가 없습니다.')
    if not webbrowser.open(url):
        raise ValueError('브라우저에서 출처를 열지 못했습니다.')
    return True


class CatalogWorkspace:
    def __init__(self, app):
        self.app=app
        self.directory=Path(app.store.directory)
        self.catalog=None
        self.album_catalog=None
        self.live_catalog=None
        self.live_people_ref=None
        self.live_credit_cache=lru_cache(maxsize=4096)(self.parse_live_credits)
        self.people_index=None
        self.live_counts=None
        self.local=None
        self.scan_cancel=threading.Event()
        self.scan_state={'busy':False,'message':''}
        self.scan_results=queue.SimpleQueue()
        self.spotify=DesktopSpotify(app)
        self.candidates={}
        self.searching_song=None

    def songs(self):
        source=self.directory/'song_catalog.json'
        personal=self.directory/PERSONAL_CATALOG_NAME
        stamp=(modification_time(source),modification_time(personal))
        if self.catalog is None or stamp!=self.catalog_stamp:
            self.catalog=SongCatalog(update_path=source,personal_path=personal,
                                     spotify_library=ExplorerLibrary(self.directory).value)
            self.catalog_stamp=stamp
            self.song_index={song['id']:song for song in self.catalog.songs}
            self.song_text={}
            self.live_counts=None
            self.people_index=None
        return self.catalog

    def get_song(self,value):
        self.songs()
        song=self.song_index.get(value) if isinstance(value,str) else None
        if not song:
            raise ValueError('음악 사전에서 이 녹음을 찾지 못했습니다.')
        return song

    def albums(self):
        catalog=self.songs()
        if catalog is self.album_catalog:
            return self.album_index
        albums, song_albums, by_title, without_id = {}, {}, {}, []

        def add(identifier, title, release, song):
            album=albums.setdefault(identifier,dict(id=identifier,title=title,series=set(),tracks={},formats=set()))
            album['series'].update(song['series'])
            album['tracks'][song['id']]=song
            album['formats'].update(release.get('formats',()))
            if release.get('date'):
                if not album.get('date') or release.get('date_source_kind') in ('official_label','streaming_service'):
                    album['date']=release['date']
            url=release.get('url') or release.get('source_url')
            if url and '/track/' not in url and not album.get('sourceUrl'):
                album['sourceUrl']=url
            song_albums.setdefault(song['id'],{})[identifier]=album

        for song in catalog.songs:
            releases=song.get('releases') or [{'title':song.get('album')}]
            for release in releases:
                title=(release.get('title') or release.get('album') or '').strip()
                if not title:
                    continue
                release_id=release.get('id') or release.get('mbid')
                url=release.get('url') or release.get('source_url') or ''
                if release_id or '/album/' in url:
                    identifier='album:'+sha256((release_id or url).encode()).hexdigest()[:24]
                    add(identifier,title,release,song)
                    by_title.setdefault(search_key(title),set()).add(identifier)
                else:
                    without_id.append((release,title,song))

        editions={}
        for identifier,album in list(albums.items()):
            title=search_key(album['title'])
            edition=(title,album.get('date'),frozenset(album['tracks']))
            if edition not in editions:
                editions[edition]=identifier
                continue
            canonical=albums[editions[edition]]
            canonical['formats'].update(album['formats'])
            if not canonical.get('sourceUrl') and album.get('sourceUrl'):
                canonical['sourceUrl']=album['sourceUrl']
            by_title[title].remove(identifier)
            canonical['series'].update(album['series'])
            for song_id in album['tracks']:
                song_albums[song_id].pop(identifier)
                song_albums[song_id][canonical['id']]=canonical
            albums.pop(identifier)

        for release,title,song in without_id:
            matches=[identifier for identifier in by_title.get(search_key(title),())
                     if albums[identifier]['series'].intersection(song['series'])]
            if len(matches)==1:
                identifier=matches[0]
            else:
                fallback='\0'.join((search_key(title),*sorted(song['series'])))
                identifier='album-title:'+sha256(fallback.encode()).hexdigest()[:24]
            add(identifier,title,release,song)
        self.album_catalog=catalog
        self.album_index=albums
        self.song_albums=song_albums
        self.album_terms={album['id']:search_key(album['title']) for album in albums.values()}
        return albums

    def album_row(self,album):
        series=next((key for key in SERIES if key in album['series'] and key!='other'),'other')
        return dict(id=album['id'],title=album['title'],series=series,
                    releaseDate=album.get('date',''),trackCount=len(album['tracks']))

    def album_rows(self,payload):
        query,series,page=search_options(payload)
        words=[search_key(word) for word in query.split()]
        rows=[album for album in self.albums().values() if (not series or series in album['series'])
              and all(word in self.album_terms[album['id']] for word in words)]
        rows.sort(key=lambda album:(self.album_terms[album['id']],album.get('date',''),album['id']))
        rows,meta=page_of(rows,page)
        return dict(**meta,items=[self.album_row(album) for album in rows])

    def get_album(self,identifier):
        album=self.albums().get(identifier)
        if album is None:
            raise ValueError('음반을 찾지 못했습니다.')
        return album

    def album_detail(self,album):
        library=ExplorerLibrary(self.directory)
        tracks=sort_songs(album['tracks'].values(),'title_asc')
        return {**self.album_row(album),'tracks':[self.song_row(track,library) for track in tracks],
                'formats':sorted(album['formats']),'sourceUrl':album.get('sourceUrl')}

    def track_for(self,song,library):
        track=library.linked_track(song['id'])
        if track and parse_track_id(track.get('id')):
            return track
        if song['id'] in library.value.get('automatic_link_exclusions',()):
            return None
        track_id=parse_track_id(song.get('spotify_url',''))
        if track_id:
            if recording:=song.get('spotify_recording'):
                return {**recording,'id':track_id}
            return dict(id=track_id,title=song['title'],artists=song['artists'],album=song.get('album',''),
                        duration=song.get('duration') or 0)
        return None

    def remember(self,song_id):
        ExplorerLibrary(self.directory).remember(song_id)

    def matching_songs(self,payload,library,query,series):
        kind=payload.get('kind','')
        duplicates=payload.get('duplicates',library.hide_duplicates)
        if not isinstance(kind,str) or kind not in SONG_KINDS or type(duplicates) is not bool:
            raise ValueError('곡 유형을 확인하세요.')
        songs=self.songs().search('',series)
        words=[search_key(word) for word in query.split()]
        if words:
            def matches(song):
                ident=song['id']
                if ident not in self.song_text:self.song_text[ident]=song_search_text(song)
                return all(word in self.song_text[ident] for word in words)
            songs=[song for song in songs if matches(song)]
        if person_id:=payload.get('performer'):
            person=self.get_person(person_id)
            songs=related_songs(songs,person,known_names=self.known_people)
        if group_id:=payload.get('group'):
            group=self.get_person(group_id)
            if group['kind']!='group':raise ValueError('그룹을 선택하세요.')
            songs=self.group_songs(songs,group)
        if live_id:=payload.get('liveId'):
            songs=self.live_recordings(live_id,payload.get('position'),songs)
        favorites=set(library.value['favorites'])
        if kind:songs=[song for song in songs if song.get('kind',song.get('type','unknown'))==kind]
        if payload.get('favorites') is True:songs=[song for song in songs if song['id'] in favorites]
        if payload.get('playable') is True:
            songs=[song for song in songs if spotify_recording(self.track_for(song,library))]
        if duplicates:
            links=self.local_library().links
            def preference(song):
                track=self.track_for(song,library)
                return (song['id'] not in favorites, not spotify_recording(track),
                        not bool(links.get(song['id'])))
            songs=[min(group,key=preference) for group in group_duplicate_songs(songs)]
        return songs, duplicates

    def song_rows(self,payload):
        query, series, page=search_options(payload)
        if type(page) is not int:raise ValueError('페이지 번호를 확인하세요.')
        library=ExplorerLibrary(self.directory)
        order=payload.get('sort',library.song_sort)
        if not isinstance(order,str) or order not in SONG_SORTS:
            raise ValueError('곡 정렬 방식을 확인하세요.')
        songs, duplicates=self.matching_songs(payload,library,query,series)
        if order!=library.song_sort or duplicates!=library.hide_duplicates:
            library.set_song_view(order,duplicates)
        counts=None
        if order.startswith('live_'):
            lives=self.live_recording_catalog()
            if self.live_counts is None:
                self.live_counts={song['id']:lives.count_for_song(song) for song in self.catalog.songs}
            counts=self.live_counts
        rows, meta=page_of(sort_songs(songs,order,counts),page)
        return dict(**meta,items=[self.song_row(song,library) for song in rows],
                    sort=order,sorts=[dict(id=key,label=label) for key,label in SONG_SORTS.items()],
                    kinds=[dict(id=key,label=label) for key,label in SONG_KINDS.items()],duplicates=duplicates)

    def discover(self,payload):
        query,series,_=search_options(payload)
        library=ExplorerLibrary(self.directory)
        songs,_=self.matching_songs(payload,library,query,series)
        if not payload.get('kind'):
            songs=[song for song in songs if song.get('kind',song.get('type'))=='song']
        song=library.choose(songs)
        if not song:
            raise ValueError('조건에 맞는 곡이 없습니다.')
        library.remember(song['id'])
        return dict(id=song['id'],title=song['title'])

    def search_spotify(self,song):
        self.poll_job()
        if self.scan_state['busy']:
            raise ValueError('현재 작업이 끝난 뒤 다시 시도해 주세요.')
        self.candidates[song['id']]=cached_candidates(self.directory,song)
        if not self.spotify.connected:
            self.searching_song=song['id']
            self.scan_state={'busy':False,'message':'저장된 녹음 후보만 표시합니다.'}
            return self.spotify_candidates(song['id'])
        self.scan_cancel=threading.Event()
        self.searching_song=song['id']

        def work(cancel):
            tracks=self.spotify.search(song['title'],(song.get('artists') or [''])[0],song.get('album',''),cancel=cancel)
            if cancel.is_set():return 'Spotify 검색 취소'
            self.candidates[song['id']]=rank_candidates(song,tracks) or self.candidates[song['id']]
            return f"Spotify 녹음 {len(self.candidates[song['id']])}개"
        self.start_job('Spotify에서 녹음을 찾는 중',work)
        return self.spotify_candidates(song['id'])

    def spotify_candidates(self,ident):
        return dict(items=self.candidates.get(ident,[])[:40],
                    busy=self.scan_state['busy'] and ident==self.searching_song,
                    message=self.scan_state['message'] if ident==self.searching_song else '')

    def song_row(self,song,library):
        track=self.track_for(song,library)
        credits=self.performer_credits(song['artists'],song.get('album',''),song.get('groups',()))
        brand='valiv' if credits and all(credit.get('brand')=='valiv' for credit in credits) else None
        rosters=self.credited_rosters(credits) if song.get('kind')=='song' else []
        roster_preview=list({member.get('id') or member['title']:member for roster in rosters
                             for member in roster['members']}.values())
        return dict(id=song['id'],title=song['title'],artists=song['artists'],album=song.get('album',''),
                    series=next(iter(song['series']),'other'),brand=brand,kind=song.get('kind','unknown'),
                    personal=bool(song.get('personal')),
                    credits=credits,rosterPreview=roster_preview[:5],rosterRemaining=max(0,len(roster_preview)-5),
                    favorite=library.is_favorite(song['id']),spotify=bool(track),availability=availability(track))

    def song_detail(self,song):
        library=ExplorerLibrary(self.directory)
        files=[local_row(file) for file in self.local_library().for_song(song['id'])]
        live=self.live_recording_catalog()
        title=search_key(song['title'])
        recordings=[other for other in self.catalog.songs if other['id']!=song['id'] and search_key(other['title'])==title]
        events=live.for_song(song)
        self.albums()
        row=self.song_row(song,library)
        return {**row,'duration':song.get('duration'),
                'rosters':self.credited_rosters(row['credits']) if row['kind']=='song' else [],
                'releaseDate':song_release_date(song),'liveCount':live.count_for_song(song),
                'liveEvents':[live_row(event) for event in events[:100]],
                'local':bool(files),'localFiles':files[:80],
                'sourceUrl':song.get('source_url') or song.get('url'),
                'recordings':[self.song_row(other,library) for other in recordings[:100]],
                'spotifyLinked':row['spotify'],
                'releases':[self.album_row(album) for album in list(self.song_albums.get(song['id'],{}).values())[:100]]}

    def people(self):
        self.songs()
        source=self.directory/'imas_db_people.json'
        stamp=modification_time(source)
        if self.people_index is None or stamp!=self.people_stamp:
            # The community data includes Luca's early solo project as a
            # one-member group named after herself. Her credits are idol credits.
            external=[person for person in self.catalog.performers
                      if person['id']!='imas:斑鳩ルカ' or person['kind']!='group']
            people=performers(external=external,database_path=source)
            self.idols=build_performer_lookup(people,kind='idol')
            identities={person['id'] for person in people}
            added=[group for group in gakuen_catalog_groups(self.idols) if group['id'] not in identities]
            self.people_index={person['id']:person for person in people+added}
            self.people_stamp=stamp
            credited=people+[group for group in added if group['published_name']]
            self.groups=build_performer_lookup(credited,kind='group')
            self.people_lookup=build_performer_lookup(credited)
            self.known_people=set(self.idols)|set(self.groups)|set(self.people_lookup)
            self.group_members={}
            units={person['name']:person for person in people if shiny_group_category(person)=='unit'}
            self.shiny_units=[units[name] for name in SHINY_UNITS if name in units]
            self.shiny_unit_aliases={key for key, person in self.groups.items()
                                     if shiny_group_category(person)=='unit'}
        return self.people_index

    def group_songs(self,songs,group):
        recording_ids=group.get('recording_ids',frozenset())
        def reviewed_recording(song):
            return (song['id'] in recording_ids
                    or ('spotify:'+song.get('spotify_url','').rsplit('/',1)[-1]) in recording_ids)
        if group.get('catalog_group_category'):
            songs=(song for song in songs if 'gakuen' in song['series'] and song.get('kind')=='song')
            if not group['published_name']:
                return [song for song in songs if reviewed_recording(song)]
        explicit={key for key,value in self.groups.items() if value is group}
        direct=explicit-self.idols.keys()
        external_ids=set(group.get('external_ids',()))
        shiny=shiny_group_category(group)
        return [song for song in songs if (recording_ids and reviewed_recording(song))
                or external_ids.intersection(song.get('performer_ids',()))
                or any(artist_key(name) in explicit for name in song.get('groups',()))
                or any(artist_key(artist[start:end]) in direct for artist in song.get('artists',())
                       for start,end in _artist_ranges(artist,self.known_people))
                or (shiny and any(artist_key(name) in direct for artist in song.get('artists',())
                                  for name in self.shiny_credit_parts(artist)))]

    def shiny_credit_parts(self,credit):
        """Read a combined unit credit only when every piece is a known unit."""
        parts=re.split(r'\s*[×＆&／/]\s*',credit)
        if len(parts)>1 and all(artist_key(name) in self.shiny_unit_aliases for name in parts):
            return parts
        return ()

    def performer_credits(self,artists,album='',groups=()):
        if not artists:return []
        self.people()
        credits, credited_voices = [], set()
        group_ids={self.groups[artist_key(name)]['id'] for name in groups if artist_key(name) in self.groups}
        for artist in (part for credit in artists for part in self.shiny_credit_parts(credit) or (credit,)):
            for start,end in _artist_ranges(artist,self.known_people):
                name=artist[start:end].strip()
                if not name:continue
                explicit=CV_CREDIT.fullmatch(unicodedata.normalize('NFKC',name))
                key=artist_key(explicit[1] if explicit else name)
                group=self.groups.get(key)
                person=group if group and group['id'] in group_ids else self.idols.get(key) or self.people_lookup.get(key)
                if person:
                    row=person_row(person)
                    row['localizedName']=person.get('ko') or row['localizedName']
                    if person['kind']=='idol':
                        voices=person.get('voice_actors',())
                        if explicit:
                            row['artists']=[explicit[2]]
                            credited_voices.add(name_key(explicit[2]))
                            voices=[voice for voice in voices if name_key(explicit[2]) in
                                    {name_key(voice['name']),name_key(voice.get('display') or voice['name'])}]
                            if len(voices)==1:
                                row['artists']=[voices[0]['name']]
                                row['localizedVoices']=[voices[0].get('display') or voices[0]['name']]
                            else:
                                row['localizedVoices']=[artist_label([explicit[2]],'THE IDOLM@STER')]
                        elif len(voices)!=1:
                            row.update(artists=[],localizedVoices=[])
                            voices=[]
                        for voice in voices:
                            credited_voices.update(name_key(label) for label in (voice['name'],voice.get('display')) if label)
                        if character_name_only(row['title']):row.update(artists=[],localizedVoices=[])
                else:
                    name=explicit[1].strip() if explicit else name
                    row=colored_credit(dict(title=name,localizedName=artist_label([name],'THE IDOLM@STER'),
                                            artists=[explicit[2]] if explicit else [],
                                            localizedVoices=[artist_label([explicit[2]],'THE IDOLM@STER')]
                                            if explicit else []),album)
                credits.append(row)
        return [row for row in credits if row.get('id') or name_key(row['title']) not in credited_voices]

    def credited_rosters(self,credits):
        explicit={row['id'] for row in credits if row.get('kind')=='아이돌'}
        rosters=[]
        for row in credits:
            if row.get('kind')!='그룹':continue
            group=self.get_person(row['id'])
            if is_project_credit(group) or row.get('groupCategory')=='project':continue
            members=self.members(group)
            if members and not any(member.get('id') in explicit for member in members):
                rosters.append(dict(group=row,members=[person_row(member) for member in members]))
        return rosters

    def get_person(self,person_id):
        person=self.people().get(person_id) if isinstance(person_id,str) else None
        if not person:
            raise ValueError('가창자 정보를 찾지 못했습니다.')
        return person

    def members(self,person):
        if person['kind']!='group':return []
        people=self.people()
        if person['id'] not in self.group_members:
            self.group_members[person['id']]=resolve_group_members(person,list(people.values()),
                idol_lookup=self.idols,group_lookup=self.groups)
        return self.group_members[person['id']]

    def ordered_members(self,person):
        ranks=idol_roster_order()
        return sorted(self.members(person),key=lambda member:
            ranks.get((member.get('series'),artist_key(member['name'])),len(ranks)))

    def people_rows(self,payload):
        query, series, page=search_options(payload)
        words=[search_key(word) for word in query.split()]
        kind=payload.get('kind','')
        if kind not in ('','idol','group'):
            raise ValueError('가창자 유형을 확인하세요.')
        people=[p for p in self.people().values() if (not series or p['series']==series)
                and (not kind or p['kind']==kind)]
        if words:
            def terms(person):
                text=[SERIES.get(person['series'],'')]
                for member in (person,*self.members(person)):
                    text.extend((member['name'],member.get('display',''),*member.get('aliases',())))
                    for actor in member.get('voice_actors',()):
                        text.extend((actor['name'],actor.get('display') or ''))
                value=search_key(' '.join(text))
                return all(word in value for word in words)
            people=[person for person in people if terms(person)]
        if kind=='group' and series=='shiny':
            people.sort(key=lambda person: SHINY_UNIT_ORDER.get(person['name'],
                len(SHINY_UNITS) + (shiny_group_category(person)!='project')))
        if kind=='idol':
            ranks=idol_roster_order()
            series_order={key:index for index,key in enumerate(SERIES)}
            people.sort(key=lambda person:(series_order.get(person['series'],len(series_order)),
                ranks.get((person['series'],artist_key(person['name'])),len(ranks))))
        rows, meta=page_of(people,page)
        items=[]
        for person in rows:
            row=person_row(person)
            if person['kind']=='group' and not is_project_credit(person):
                members=self.ordered_members(person)
                row.update(memberPreview=[person_row(member) for member in members[:GROUP_MEMBER_PREVIEW]],
                           memberCount=len(members))
            items.append(row)
        return dict(**meta,items=items)

    def lives(self):
        source=self.directory/'live_catalog.json'
        stamp=modification_time(source)
        if self.live_catalog is None or stamp!=self.live_stamp:
            self.live_catalog=LiveCatalog(update_path=source)
            self.live_stamp=stamp
            self.live_counts=None
            self.live_index={event['id']:event for event in self.live_catalog.events}
            self.live_text={}
            self.live_cast={}
        return self.live_catalog

    def live_recording_catalog(self):
        live=self.lives()
        live.index_recordings(self.songs().songs)
        return live

    def get_live(self,event_id):
        self.lives()
        event=self.live_index.get(event_id) if isinstance(event_id,str) else None
        if event is None:raise ValueError('라이브 공연을 찾지 못했습니다.')
        return event

    def live_credit_people(self):
        people=self.people()
        if self.live_people_ref is not people:
            self.live_people_ref=people
            self.live_cast.clear()
            self.live_voices={}
            self.live_credit_cache.cache_clear()
            for person in people.values():
                if person['kind']!='idol':continue
                for voice in person.get('voice_actors',()):
                    for name in {voice['name'],voice.get('display') or voice['name']}:
                        self.live_voices.setdefault(live_name_key(name),{})[person['id']]=(person,voice['name'])
    def parse_live_credits(self,artists,series):
        def parse(value, role='', unit=None):
            value=value.strip(' ,;')
            if not value:return []
            if tag:=LIVE_ROLE.match(value):
                return parse(value[tag.end():],LIVE_ROLES[tag[1].casefold()],unit)
            if LIVE_GENERIC.match(value):
                return [{'title':value,'collective':True,'role':role}]
            key=name_key(value)
            person=self.idols.get(key) or self.groups.get(key)
            if person:
                return [{**person_row(person),'role':role}]

            spans=_artist_ranges(value,self.known_people)
            if len(spans)>1:
                return [credit for start,end in spans for credit in parse(value[start:end],role,unit)]

            with_band=re.split(r'\s+with\s+',value,maxsplit=1,flags=re.I)
            if len(with_band)>1:
                return parse(with_band[0],role,unit)+[{'title':with_band[1],'role':'반주'}]

            head, qualifiers=live_qualifiers(value)
            key=artist_key(head)
            person=self.idols.get(key) or self.groups.get(key)
            voices=self.live_voices.get(live_name_key(head),{})
            candidates=list(voices.values())
            if len(candidates)>1:
                members={member['id'] for member in self.members(unit)} if unit else set()
                narrowed=[pair for pair in candidates if pair[0]['id'] in members]
                if not narrowed:narrowed=[pair for pair in candidates if pair[0]['series'] in series]
                candidates=narrowed or candidates
                for qualifier in qualifiers:
                    hinted=self.idols.get(artist_key(qualifier.removesuffix('役')))
                    if hinted and hinted['id'] in voices:
                        candidates=[voices[hinted['id']]]
                        break
            if len(candidates)==1 and not person:
                person,voice=candidates[0]
                credit={**person_row(person),'artists':[voice],
                        'localizedVoices':[artist_label([voice],'THE IDOLM@STER')],'role':role}
            elif person:
                credit={**person_row(person),'role':role}
            else:
                credit={'title':head or value,'role':role}
                common={pair[0].get('color') for pair in candidates}
                if len(common)==1 and (color:=next(iter(common))):
                    credit['color']=color
            detail=[]
            for qualifier in qualifiers:
                if qualifier.startswith('※') or not qualifier:continue
                if LIVE_ROLE.match(qualifier) or qualifier.lower().startswith('with '):
                    if qualifier.lower().startswith('with '):
                        detail.append({'title':qualifier[5:].strip(),'role':'반주'})
                    else:detail.extend(parse(qualifier,role,unit))
                elif person and person['kind']=='idol':
                    explicit=re.sub(r'^CV\s*[.:]?\s*','',qualifier,flags=re.I)
                    if any(live_name_key(explicit)==live_name_key(actor['name']) for actor in person.get('voice_actors',())):
                        credit.update(artists=[explicit],localizedVoices=[artist_label([explicit],'THE IDOLM@STER')])
                else:
                    members=parse(qualifier,role,person if person and person['kind']=='group' else None)
                    if members and all(member.get('kind')=='아이돌' or member.get('color') for member in members):
                        if not person and head:credit['kind']='그룹'
                        detail.extend(members)
            return ([credit] if head or not qualifiers else [])+detail

        credits={}
        for artist in artists:
            for row in parse(unicodedata.normalize('NFKC',artist)):
                identity=row.get('id') or artist_key(row['title'])
                credits.setdefault((identity,row.get('role','')),row)
        return list(credits.values())

    def live_credits(self,performance,event):
        if not (artists:=performance.get('artists')):return []
        return self.live_credit_cache(tuple(artists),tuple(performance.get('series',event['series'])))

    def live_event_row(self,event,preview=True):
        if event['id'] not in self.live_cast:
            cast={}
            for performance in event['songs']:
                for row in self.live_credits(performance,event):
                    if not row.get('collective'):
                        identity=row.get('id') or artist_key(row['title'])
                        if identity not in cast or cast[identity].get('role') and not row.get('role'):
                            cast[identity]=row
            self.live_cast[event['id']]=list(cast.values())
        cast=self.live_cast[event['id']]
        return {**live_row(event),'credits':cast[:8] if preview else cast,'artistCount':len(cast)}

    def live_rows(self,payload):
        query, series, page=search_options(payload)
        words=[search_key(word) for word in query.split()]
        year=payload.get('year','')
        if not isinstance(year,str) or year and (len(year)!=4 or not year.isdecimal()):
            raise ValueError('공연 연도를 확인하세요.')
        events=self.lives().events
        if words and not self.live_text:
            for event in events:
                text=[event['title'],event['date'],event.get('venue',''),series_label(event),*event.get('series_names',())]
                for item in event.get('songs',()):
                    title=item.get('song_title') or item['title']
                    text.extend([item['title'],title,item.get('performance_title',''),*item.get('artists',())])
                    text.extend(search_aliases(dict(title=title,series=item.get('series') or event['series'])))
                self.live_text[event['id']]=search_key(' '.join(text))
        matches=[event for event in events if (not series or series in event['series'])
                 and (not year or event['date'].startswith(year))
                 and (not words or all(word in self.live_text[event['id']] for word in words))]
        rows, meta=page_of(matches,page)
        if rows:self.live_credit_people()
        return dict(**meta,items=[self.live_event_row(event) for event in rows],
                    years=sorted({event['date'][:4] for event in events},reverse=True))

    def live_detail(self,event):
        entries=self.live_recording_catalog().songs_for_event(event,self.songs().songs)
        self.live_credit_people()
        songs=[dict(title=row['performance'].get('performance_title') or row['performance']['title'],
                    artists=row['performance'].get('artists',[]),matchCount=len(row['matches']),
                    credits=self.live_credits(row['performance'],event),
                    position=row['position'],kind=row['performance'].get('performance_kind',''))
               for row in entries]
        return {**self.live_event_row(event,preview=False),'songs':songs,'sourceUrl':event.get('source_url'),
                'sourceKind':'팬 기록' if event.get('source_kind')=='fan_database' else '공연 자료',
                'dateSourceUrl':event.get('date_source_url'),'setlistSourceUrl':event.get('setlist_source_url'),
                'officialSources':event.get('official_sources',[])}

    def live_recordings(self,event_id,position,songs):
        event=self.get_live(event_id)
        if type(position) is not int or not 1<=position<=len(event['songs']):
            raise ValueError('공연 곡목을 선택하세요.')
        matches=self.live_recording_catalog().songs_for_event(event,songs)[position-1]['matches']
        return matches

    def local_library(self):
        stamp=modification_time(self.directory/'local_library.json')
        if self.local is None or stamp!=self.local_stamp and not self.scan_state['busy']:
            self.local=LocalLibrary(self.directory)
            self.local_stamp=stamp
        return self.local

    def get_local(self,file_id):
        for record in self.local_library().files:
            if record['id']==file_id:return record
        raise ValueError('로컬 음원 목록에서 파일을 선택하세요.')

    def local_rows(self,payload):
        query, _, page=search_options(payload)
        words=[search_key(word) for word in query.split()]
        files=self.local_library().files
        if words:
            def matches(file):
                text=search_key(' '.join((file['title'],file['album'],*file['artists'])))
                return all(word in text for word in words)
            files=[file for file in files if matches(file)]
        rows, meta=page_of(files,page)
        return dict(**meta,items=[local_row(file) for file in rows],scan=self.scan_state.copy())

    def scan(self):
        self.poll_job()
        if self.scan_state['busy']:
            raise ValueError('이미 로컬 음원 폴더를 확인하고 있습니다.')
        library=self.local_library()
        self.scan_state={'busy':True,'message':'음원 폴더 선택 대기'}
        try:folder=filedialog.askdirectory(parent=self.app.root,title='음원이 들어 있는 폴더 선택')
        except Exception:
            self.scan_state={'busy':False,'message':''}
            raise
        if not folder:
            self.scan_state={'busy':False,'message':''}
            return self.scan_state.copy()
        self.scan_cancel=threading.Event()

        def work(cancel):
            result=library.scan(folder,cancel=cancel)
            message=('음원 폴더 확인 취소' if result.get('cancelled') else
                     f"음원 {result.get('scanned',0):,}개 확인 · {len(library.files):,}개 보관")
            return message
        return self.start_job('음원 폴더를 확인하는 중', work)

    def start_job(self,message,work):
        self.scan_state={'busy':True,'message':message}
        cancel=self.scan_cancel

        def run():
            try:result=work(cancel)
            except (OSError,ValueError,SpotifyCatalogError) as error:result=str(error)
            self.scan_results.put(result)
        threading.Thread(target=run,name='catalog-workspace',daemon=True).start()
        return self.scan_state.copy()

    def poll_job(self):
        try:message=self.scan_results.get_nowait()
        except queue.Empty:return
        self.scan_state={'busy':False,'message':message}

    def current(self):
        playback=self.app.playback
        if not playback.title:return dict(query='',id=None)
        library=ExplorerLibrary(self.directory)
        songs=self.songs().songs
        track=self.spotify.current_track()
        if track:
            ids=[song['id'] for song in songs if (self.track_for(song,library) or {}).get('id')==track['id']]
            if len(ids)==1:return dict(query=self.song_index[ids[0]]['title'],id=ids[0])
        title, album=search_key(playback.title),search_key(playback.album)
        artist=artist_key(playback.artist)
        candidates=[]
        for song in songs:
            if title not in {search_key(value) for value in (song['title'],*song.get('aliases',()))}:continue
            albums={search_key(value) for value in (song.get('album',''),
                    *(r.get('title','') for r in song.get('releases',()))) if value}
            artists={artist_key(name) for name in song.get('artists',())}
            if album and albums and album not in albums or artist and artists and artist not in artists:continue
            if (album not in albums and artist not in artists) or (playback.duration and song.get('duration')
                    and abs(song['duration']-playback.duration)>2):continue
            candidates.append(song)
        if len(candidates)==1:return dict(query=candidates[0]['title'],id=candidates[0]['id'])
        return dict(query=playback.title,id=None)

    def dispatch(self,action,payload):
        self.poll_job()
        if action=='list':return self.song_rows(payload)
        if action=='people':return self.people_rows(payload)
        if action=='albums':return self.album_rows(payload)
        if action=='album':return self.album_detail(self.get_album(string_key(payload,'id')))
        if action=='lives':return self.live_rows(payload)
        if action=='locals':return self.local_rows(payload)
        if action=='localStatus':return self.scan_state.copy()
        if action=='localScan':return self.scan()
        if action=='updates':
            self.app.open_updates()
            return True
        if action=='addSong':
            result=self.songs().add_personal_song(payload)
            self.catalog=None
            return dict(id=result['song']['id'],added=result['added'])
        if action=='cancel':
            self.scan_cancel.set()
            return self.scan_state.copy()
        if action=='current':return self.current()
        if action=='discover':return self.discover(payload)
        if action=='source':
            ident=string_key(payload,'id')
            mode=payload.get('mode')
            if mode in ('people','groups'):
                urls=self.get_person(ident).get('source_urls',[])
                url=urls[0] if urls else ''
            elif mode=='albums':
                url=self.get_album(ident).get('sourceUrl','')
            elif mode=='lives':
                event=self.get_live(ident)
                field=payload.get('field')
                if field=='official':
                    index=payload.get('index')
                    sources=event.get('official_sources',())
                    if type(index) is not int or not 0<=index<len(sources):
                        raise ValueError('공식 출처를 선택하세요.')
                    url=sources[index]['url']
                else:url=event.get(field,'') if field in ('date_source_url','setlist_source_url') else event['source_url']
            else:
                song=self.get_song(ident)
                url=song.get('source_url') or song.get('url','')
            return open_source(url)
        if action=='person':
            person=self.get_person(string_key(payload,'id'))
            units=[]
            track_preview=[]
            if shiny_group_category(person)=='project':
                units=self.shiny_units
            elif person['kind']=='idol' and person['series']=='shiny':
                units=[unit for unit in self.shiny_units
                       if any(member['id']==person['id'] for member in self.members(unit))]
            if person['kind']=='group':
                recordings=self.group_songs((song for song in self.songs().songs if song.get('kind')=='song'),person)
                library=ExplorerLibrary(self.directory)
                track_preview=[self.song_row(versions[0],library) for versions in
                               group_duplicate_songs(recordings)[:GROUP_SONG_PREVIEW]]
            live_events=[]
            if performances:=person.get('live_performances'):
                self.lives()
                live_events=[dict(live_row(self.live_index[ident]),performanceTitle=title)
                             for ident,title in performances if ident in self.live_index]
            return {**person_row(person),'members':[person_row(p) for p in self.ordered_members(person)]
                    if not is_project_credit(person) else [],
                    'trackPreview':track_preview,'liveEvents':live_events,
                    'units':[person_row(unit) for unit in units],
                    'sourceUrl':next(iter(person.get('source_urls',())),None),'name':person['name']}
        if action=='live':return self.live_detail(self.get_live(string_key(payload,'id')))
        if action=='localUnlink':
            ident=self.get_song(string_key(payload,'songId'))['id']
            self.local_library().unlink(ident)
            return True
        if action in ('local','localOpen','localLink'):
            file=self.get_local(string_key(payload,'id'))
            if action=='localOpen':self.local_library().open_file(file['id']);return True
            if action=='localLink':
                song=self.get_song(string_key(payload,'songId'))
                self.local_library().link(song['id'],file['id'])
                return True
            return local_row(file)
        song=self.get_song(string_key(payload,'id'))
        if action=='detail':return self.song_detail(song)
        if action=='spotifySearch':return self.search_spotify(song)
        if action=='spotifyCandidates':return self.spotify_candidates(song['id'])
        library=ExplorerLibrary(self.directory)
        if action=='favorite':
            library.toggle_favorite(song['id'])
            return dict(favorite=library.is_favorite(song['id']))
        if action=='spotifyLink':
            value=payload.get('url')
            track_id=parse_track_id(value.strip()) if isinstance(value,str) else None
            if not track_id:raise ValueError('Spotify 곡 주소 또는 트랙 ID를 입력하세요.')
            current=self.spotify.current_track()
            candidate=next((track for track in self.candidates.get(song['id'],()) if track['id']==track_id),None)
            track=candidate or (current if current and current['id']==track_id else {
                'id':track_id,'title':'직접 입력한 Spotify 곡','artists':[],'is_playable':None}
            )
            library.link_spotify(song['id'],track)
            detail=self.song_detail(song)
            self.catalog=None
            return detail
        if action=='spotifyUnlink':
            library.unlink_spotify(song['id'])
            detail=self.song_detail(song)
            self.catalog=None
            return detail
        if action=='spotifyOpen':
            track=self.track_for(song,library)
            if track:self.spotify.open(track)
            else:open_source('https://open.spotify.com/search/'+quote(song['title'],safe=''))
            return True
        raise ValueError('지원하지 않는 음악 사전 동작입니다.')

    def close(self):
        self.scan_cancel.set()
        self.spotify.close()
