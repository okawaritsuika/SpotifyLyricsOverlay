"""Personal catalog choices, performer relationships and unbiased discovery."""
import copy
from datetime import date
import json
import math
from pathlib import Path
import random
import re
import unicodedata

from app_paths import CATALOG_RELEASE_STAMP, prefer_catalog_update
from artist_names import artist_label, name_key
from artist_colors import SIDEM_SERIES_COLOR, _artist_ranges, catalog as artist_color_catalog
from song_aliases import search_aliases
from storage import atomic_json, read_json


SERIES = {'': '전체 시리즈', '765': '765 ALLSTARS', 'million': '밀리언 라이브!',
          'cinderella': '신데렐라 걸즈', 'sidem': 'SideM', 'shiny': '샤이니 컬러즈',
          'gakuen': '학원 아이돌마스터', 'other': '기타 · 합동'}
SERIES_COLORS = {'': '#99e3c8', '765': '#ff8598', 'million': '#f5c76d',
                 'cinderella': '#85baff', 'sidem': SIDEM_SERIES_COLOR, 'shiny': '#acd7ef',
                 'gakuen': '#ffb478', 'other': '#cbb5f5'}
SONG_SORTS = {'default': '기본순', 'title_asc': '이름 오름차순', 'title_desc': '이름 내림차순',
              'release_desc': '발매일 최신순', 'release_asc': '발매일 오래된순',
              'live_desc': '라이브 가창 많은순', 'live_asc': '라이브 가창 적은순',
              'artist_asc': '가창자 이름순', 'duration_desc': '재생시간 긴순', 'duration_asc': '재생시간 짧은순'}


def search_key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value))).casefold()


def series_label(song):
    return ' · '.join(SERIES.get(key, key) for key in song.get('series', [])) or '분류 미확인'


def artists_label(song):
    artists = song.get('artists') or []
    return artist_label(artists, 'THE IDOLM@STER') or '가창자 미확인'


def _partial_release_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}(?:-[0-9]{2}){0,2}', value.strip()):
        return None
    parts = [int(part) for part in value.strip().split('-')]
    try:return date(*(parts + [1] * (3 - len(parts))))
    except ValueError:return None


def song_release_date(song):
    """Keep the source's precision; use release entries only without a valid main date."""
    for field in ('first_release_date', 'release_date'):
        value = song.get(field)
        if _partial_release_date(value) is not None:return value.strip()
    candidates = [(parsed, entry['date'].strip()) for entry in song.get('releases', [])
                  if isinstance(entry, dict) and (parsed := _partial_release_date(entry.get('date'))) is not None]
    return min(candidates)[1] if candidates else ''


def sort_songs(songs, order, live_counts=None):
    """Sort a copy, keeping missing dates/durations last in either direction."""
    if not isinstance(order, str) or order not in SONG_SORTS:order = 'default'
    if order == 'default':
        return sorted(songs, key=lambda song: {'song': 0, 'instrumental': 2, 'spoken': 3}.get(
            song.get('kind', song.get('type')), 1))
    # Stable secondary ordering also keeps ties deterministic for descending sorts.
    ordered = sorted(songs, key=lambda song: (search_key(song.get('title', '')), str(song.get('id', ''))))
    if order == 'title_asc':return ordered
    if order == 'title_desc':
        return sorted(ordered, key=lambda song: search_key(song.get('title', '')), reverse=True)
    if order == 'artist_asc':return sorted(ordered, key=lambda song: search_key(artists_label(song)))
    descending = order.endswith('_desc')
    if order.startswith('live_'):
        counts = live_counts or {}
        return sorted(ordered, key=lambda song: counts.get(song.get('id'), 0), reverse=descending)

    def measured_key(song):
        if order.startswith('release_'):
            release = _partial_release_date(song_release_date(song))
            value = release.toordinal() if release is not None else None
        else:
            value = song.get('duration')
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
                    or isinstance(value, float) and not math.isfinite(value)):value = None
        return (value is None, 0 if value is None else -value if descending else value)

    return sorted(ordered, key=measured_key)


def artist_key(name):
    normalized = unicodedata.normalize('NFKC', name)
    return name_key(re.sub(r'\s*\(CV[.:：]?.*?\)', '', normalized, flags=re.I))


def franchise_key(franchise):
    text = str(franchise).lower()
    for token, key in [('million', 'million'), ('cinderella', 'cinderella'),
                       ('shiny', 'shiny'), ('gakuen', 'gakuen'), ('sidem', 'sidem'), ('765', '765')]:
        if token in text:return key
    return 'other'


def _strings(values):
    return list(dict.fromkeys(value.strip() for value in values
                             if isinstance(value, str) and value.strip())) if isinstance(values, list) else []


def _person_names(person):
    return _strings([person.get('name'), person.get('ko'), person.get('display')]
                    + _strings(person.get('aliases')))


def build_performer_lookup(people, kind=None):
    """Index full names and aliases; shared aliases deliberately have no owner."""
    choices = {}
    for person in people:
        if kind is not None and person.get('kind') != kind:continue
        for name in _person_names(person):
            choices.setdefault(artist_key(name), {})[id(person)] = person
    return {key: next(iter(owners.values())) for key, owners in choices.items() if len(owners) == 1}


def lookup_performer(people, name, kind=None):
    return build_performer_lookup(people, kind).get(artist_key(name)) if isinstance(name, str) else None


def resolve_group_members(group_or_name, people, *, idol_lookup=None, group_lookup=None):
    """Resolve an explicit roster to characters without changing song credits.

    A member name refers to a character before a same-named unit. Shared aliases
    between characters remain unresolved. Nested units are expanded once, and
    unknown members or cyclic references remain visible as ``kind='unknown'``.
    The returned records are independent copies, so UI annotations cannot alter
    the public catalog or another group's member data.
    """
    people = people if isinstance(people, (list, tuple)) else list(people)
    idols = build_performer_lookup(people, 'idol') if idol_lookup is None else idol_lookup
    groups = build_performer_lookup(people, 'group') if group_lookup is None else group_lookup
    idol_names = None
    group = groups.get(artist_key(group_or_name)) if isinstance(group_or_name, str) else group_or_name
    if not isinstance(group, dict) or group.get('kind') != 'group':return []
    members, seen = [], set()

    def add(person, member_name, reason=''):
        identity = (person.get('id') or id(person)) if person else ('unknown', artist_key(member_name))
        if identity in seen:return
        seen.add(identity)
        if person:
            members.append(copy.deepcopy(person))
        else:
            members.append({'id': 'unknown:' + artist_key(member_name), 'name': member_name,
                            'display': artist_label([member_name], 'THE IDOLM@STER'),
                            'kind': 'unknown', 'color': '', 'voice_actors': [], 'cv': [],
                            'members': [], 'member_status': reason})

    def visit(unit, ancestors):
        nonlocal idol_names
        identity = unit.get('id') or id(unit)
        ancestors = ancestors | {identity}
        for name in _strings(unit.get('members')):
            key = artist_key(name)
            if key in idols:
                add(idols[key], name)
                continue
            if idol_names is None:
                idol_names = {artist_key(alias) for person in people if person.get('kind') == 'idol'
                              for alias in _person_names(person)}
            if key in idol_names:
                add(None, name, 'ambiguous')
            elif key in groups:
                nested = groups[key]
                if (nested.get('id') or id(nested)) in ancestors:
                    add(None, name, 'cycle')
                elif not nested.get('members'):
                    add(None, name, 'unconfirmed')
                else:visit(nested, ancestors)
            else:add(None, name, 'unconfirmed')

    visit(group, set())
    return members


def _hex_color(value):
    return '#' + value.lstrip('#').lower() if isinstance(value, str) and re.fullmatch(r'#?[0-9a-fA-F]{6}', value) else ''


def valid_people_snapshot(snapshot):
    if (not isinstance(snapshot, dict) or type(snapshot.get('schema_version')) is not int
            or snapshot['schema_version'] != 1):
        return False
    try:
        date.fromisoformat(snapshot['verified_at'])
    except (KeyError, TypeError, ValueError):
        return False
    entries = snapshot.get('characters')
    return isinstance(entries, list) and bool(entries)


def people_database_snapshot(base, database_path=None):
    update = Path(database_path) if database_path is not None else None
    released = None
    for path in (update, base / 'imas_db_people.json'):
        if path is None:continue
        try:
            snapshot = json.loads(path.read_text(encoding='utf-8'))
            if not valid_people_snapshot(snapshot):continue
            if path == update and CATALOG_RELEASE_STAMP in snapshot:
                released = snapshot
                continue
            return released if prefer_catalog_update(snapshot, released) else snapshot
        except (OSError, ValueError, KeyError, TypeError):continue
    return released or {}


def _enrich_people_from_database(people, base, voices, database_path=None):
    """Add optional fan-source evidence only to unambiguous existing identities."""
    snapshot = people_database_snapshot(base, database_path)
    if not snapshot:return
    entries = snapshot['characters']
    verified = date.fromisoformat(snapshot['verified_at'])
    lookup = build_performer_lookup(people, 'idol')
    # These pages contain other adaptations and NPCs too. Exact name resolution
    # must identify an existing idol; the source never creates new identities.
    source_owners = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get('name'), str):
            match_name = entry.get('match_name') or entry['name']
            if isinstance(match_name, str):source_owners.setdefault(artist_key(match_name), []).append(entry)
    for key, owners in source_owners.items():
        person = lookup.get(key)
        if len(owners) != 1 or person is None:continue
        entry = owners[0]
        person['source_urls'] = _strings(person['source_urls'] + _strings(entry.get('identity_source_urls')))
        history, active = [], {}
        for credit in entry.get('voice_actor_history', []) if isinstance(entry.get('voice_actor_history'), list) else []:
            if not isinstance(credit, dict) or not isinstance(credit.get('name'), str):continue
            if credit.get('role_context'):continue
            source = credit.get('source_url')
            if source != 'https://imas-db.jp/misc/cv.html':continue
            voice = voices.get(artist_key(credit['name']))
            name = voice['name'] if voice else credit['name']
            item = {'name': name, 'display': (voice.get('ko') or name) if voice else name,
                    'source_url': source}
            try:
                first = date.fromisoformat(credit['from']) if credit.get('from') else None
                last = date.fromisoformat(credit['until']) if credit.get('until') else None
                if first and last and first > last:continue
            except (ValueError, TypeError):continue
            for label in ('from', 'until'):
                if credit.get(label):item[label] = credit[label]
            if item not in history:history.append(item)
            if (first is None or first <= verified) and (last is None or verified <= last):
                active[artist_key(name)] = name
        if history:
            person['voice_actor_history'] = history
            person['source_urls'] = _strings(person['source_urls'] + ['https://imas-db.jp/misc/cv.html'])
        if len(active) == 1:
            active_key, name = next(iter(active.items()))
            known = {artist_key(voices.get(artist_key(value), {}).get('name', value)) for value in person['cv']}
            if not known:person['cv'] = [name]
            if not known or active_key in known:
                person['voice_actor_sources'] = _strings(person['voice_actor_sources'] + ['https://imas-db.jp/misc/cv.html'])
            else:person['voice_actor_conflict'] = True
        elif len(active) > 1:person['voice_actor_conflict'] = True
        color = _hex_color(entry.get('color'))
        if color and 'https://imas-db.jp/misc/color.html' in _strings(entry.get('source_urls')):
            person['database_color'] = color
            person['database_color_source'] = 'https://imas-db.jp/misc/color.html'
            if not person['color'] and not person['color_sources']:
                person['color'] = color
                person['color_sources'] = ['https://imas-db.jp/misc/color.html']
            person['source_urls'] = _strings(person['source_urls'] + ['https://imas-db.jp/misc/color.html'])


def performers(resource_dir=None, external=(), database_path=None):
    """Membership is presented separately; it is never assigned as song credits."""
    base = Path(resource_dir or Path(__file__).resolve().parent / 'resources')
    colors = {}
    for filename in sorted((base / 'artist_colors').glob('*.json')):
        data = json.loads(filename.read_text(encoding='utf-8'))
        for entry in data.get('entries', []):colors.setdefault(name_key(entry['name']), []).append(entry)
    result = []
    names = []
    for filename in sorted((base / 'artist_names').glob('*.json')):
        data = json.loads(filename.read_text(encoding='utf-8'))
        names.extend(data.get('entries', []))
    voices = build_performer_lookup([entry for entry in names if entry.get('kind') == 'voice_actor'])
    for entry in names:
        if entry.get('kind') not in ('idol', 'group'):continue
        entries = colors.get(name_key(entry['name']), [])
        values = {_hex_color(item.get('color')) for item in entries} - {''}
        members = next((_strings(item.get('members')) for item in entries if item.get('members')), [])
        color_sources = _strings([item.get('source') for item in entries if _hex_color(item.get('color'))])
        membership_sources = _strings([source for item in entries for source in _strings(item.get('membership_sources'))])
        cv = _strings(entry.get('cv'))
        voice_sources = _strings(entry.get('voice_actor_sources')) if cv else []
        result.append({**copy.deepcopy(entry), 'id': entry['kind'] + ':' + name_key(entry['name']),
                       'display': artist_label([entry['name']], 'THE IDOLM@STER'),
                       'series': franchise_key(entry.get('franchise')), 'members': members,
                       'color': next(iter(values)) if len(values) == 1 else '',
                       'color_sources': color_sources, 'membership_sources': membership_sources,
                       'cv': cv, 'voice_actors': [], 'voice_actor_sources': voice_sources,
                       'source_urls': _strings(_strings(entry.get('source_urls')) + color_sources + membership_sources
                                              + voice_sources)})
    known = {}
    def register(person):
        for name in _person_names(person):known.setdefault(artist_key(name), {})[id(person)] = person
    for person in result:register(person)
    for item in external:
        if not isinstance(item, dict) or not item.get('name') or item.get('kind') not in ('idol', 'group'):continue
        aliases = _person_names(item)
        matches = {identity: person for name in aliases for identity, person in known.get(artist_key(name), {}).items()}
        sources = _strings(_strings(item.get('source_urls')) + [item.get('source_url')])
        if len(matches) == 1 and next(iter(matches.values()))['kind'] == item['kind']:
            person = next(iter(matches.values()))
        else:
            series = item.get('series', [])
            if isinstance(series, list):series = series[0] if series else 'other'
            person = {**copy.deepcopy(item), 'display': artist_label([item['name']], 'THE IDOLM@STER'),
                      'series': series if series in SERIES else 'other', 'members': [], 'color': '',
                      'color_sources': [], 'cv': [], 'voice_actors': [], 'voice_actor_sources': [],
                      'source_urls': [], 'membership_sources': [], 'external_ids': []}
            result.append(person)
        person['external_ids'] = _strings(person.get('external_ids', []) + [item.get('id')])
        person['aliases'] = _strings(_strings(person.get('aliases')) + aliases)
        person['source_urls'] = _strings(person.get('source_urls', []) + sources)
        person['provenance'] = person.get('provenance', []) + [copy.deepcopy(value) for value in item.get('provenance', [])
                                                           if value not in person.get('provenance', [])]
        if not person['members'] and item.get('members'):
            person['members'] = _strings(item['members'])
            person['membership_sources'] = _strings(person.get('membership_sources', []) + sources)
        if not person['color'] and not person['color_sources'] and _hex_color(item.get('color')):
            person['color'] = _hex_color(item['color']);person['color_sources'] = sources
        person['cv'] = _strings(person['cv'] + _strings(item.get('cv')))
        if item.get('cv'):person['voice_actor_sources'] = _strings(person['voice_actor_sources'] + sources)
        register(person)
    _enrich_people_from_database(result, base, voices, database_path)
    for person in result:
        seen_voices = set()
        for name in person['cv']:
            voice = voices.get(artist_key(name))
            key = artist_key(voice['name'] if voice else name)
            if key in seen_voices:continue
            seen_voices.add(key)
            person['voice_actors'].append({'name': voice['name'] if voice else name,
                'display': (voice.get('ko') or voice['name']) if voice else name,
                'source_urls': _strings(person['voice_actor_sources'] + (_strings(voice.get('source_urls')) if voice else []))})
    return sorted(result, key=lambda item: (item['kind'] != 'group', item['display']))


def group_duplicate_songs(songs):
    """Group by full title for display, retaining each selectable recording."""
    groups, buckets = [], {}
    for song in songs:
        title = song.get('title')
        duration = song.get('duration')
        invalid_duration = (type(duration) in (int, float) and duration > 604800 and
                            (type(duration) is int or math.isfinite(duration)))
        if not isinstance(title, str) or not title.strip() or invalid_duration:
            groups.append([song]);continue
        key = search_key(title)
        if key not in buckets:
            buckets[key] = [];groups.append(buckets[key])
        buckets[key].append(song)
    return groups


def related_songs(songs, performer, *, known_names=None):
    """Match explicit credit tokens, preserving known unit names and CV blocks."""
    names = {artist_key(value) for value in [performer['name'], performer.get('ko', '')]
             + performer.get('aliases', []) if value}
    known_names = names | (artist_color_catalog().known_names if known_names is None else set(known_names))
    external_ids = set(performer.get('external_ids', []))

    def credited_names(song):
        for original in song.get('artists', []):
            # Remove CV annotations only in the matching copy. A comma inside a
            # voice credit is not a singer separator; unit punctuation remains.
            credit = re.sub(r'\(CV\s*[.:：]?\s*[^()]*\)', '',
                            unicodedata.normalize('NFKC', original), flags=re.I)
            for start, end in _artist_ranges(credit, known_names):
                yield artist_key(credit[start:end])

    return [song for song in songs if names.intersection(credited_names(song))
            or external_ids.intersection(song.get('performer_ids', []))]


def song_search_text(song):
    fields = [song.get('title', ''), song.get('album', ''), artists_label(song), series_label(song)]
    fields += song.get('artists', []) + song.get('aliases', []) + song.get('performer_aliases', [])
    fields += list(search_aliases(song))
    return search_key(' '.join(fields))


def matches_query(song, query):
    if not query.strip():return True
    haystack = song_search_text(song)
    return all(search_key(word) in haystack for word in query.split())


def spotify_url(track):
    track_id = track.get('id', '') if isinstance(track, dict) else ''
    return ('https://open.spotify.com/track/' + track_id
            if isinstance(track_id, str) and re.fullmatch(r'[A-Za-z0-9]{22}', track_id) else '')


class ExplorerLibrary:
    """User data is kept outside the replaceable public catalog."""
    def __init__(self, directory):
        self.path = Path(directory) / 'explorer_library.json'
        value = read_json(self.path, {})
        if not isinstance(value, dict) or value.get('schema_version', 1) != 1:
            raise ValueError('개인 음악 목록 형식을 확인할 수 없습니다. 원본은 보존했습니다.')
        for field, expected in [('favorites', list), ('spotify', dict), ('recent', list)]:
            if not isinstance(value.get(field, expected()), expected):
                raise ValueError('개인 음악 목록을 읽을 수 없습니다. 원본은 보존했습니다.')
        for field in ('favorites', 'recent'):
            if any(not isinstance(item, str) for item in value.get(field, [])):
                raise ValueError('개인 음악 목록의 곡 ID를 읽을 수 없습니다. 원본은 보존했습니다.')
        if not isinstance(value.get('hide_duplicates', False), bool):
            raise ValueError('중복 곡 표시 설정을 읽을 수 없습니다. 원본은 보존했습니다.')
        if (not isinstance(value.get('automatic_link_exclusions', []), list)
                or any(not isinstance(item, str) for item in value.get('automatic_link_exclusions', []))):
            raise ValueError('자동 연결 제외 목록을 읽을 수 없습니다. 원본은 보존했습니다.')
        self.value = {**value, 'schema_version': 1, 'favorites': value.get('favorites', []),
                      'spotify': value.get('spotify', {}), 'recent': value.get('recent', []),
                      'hide_duplicates': value.get('hide_duplicates', False)}

    def _commit(self, value):
        atomic_json(self.path, value)
        self.value = value

    def is_favorite(self, song_id):
        return song_id in self.value['favorites']

    @property
    def hide_duplicates(self):
        return self.value['hide_duplicates']

    def set_hide_duplicates(self, value):
        if not isinstance(value, bool):raise ValueError('중복 곡 표시 설정은 켜기 또는 끄기여야 합니다.')
        updated = copy.deepcopy(self.value)
        updated['hide_duplicates'] = value
        self._commit(updated)

    @property
    def song_sort(self):
        value = self.value.get('song_sort', 'default')
        return value if isinstance(value, str) and value in SONG_SORTS else 'default'

    def set_song_sort(self, value):
        if not isinstance(value, str) or value not in SONG_SORTS:raise ValueError('지원하지 않는 곡 정렬 방식입니다.')
        updated = copy.deepcopy(self.value)
        updated['song_sort'] = value
        self._commit(updated)

    def set_song_view(self, order, hide_duplicates):
        if not isinstance(order, str) or order not in SONG_SORTS:
            raise ValueError('지원하지 않는 곡 정렬 방식입니다.')
        if not isinstance(hide_duplicates, bool):
            raise ValueError('중복 곡 표시 설정은 켜기 또는 끄기여야 합니다.')
        self._commit({**self.value, 'song_sort':order, 'hide_duplicates':hide_duplicates})

    def toggle_favorite(self, song_id):
        value = copy.deepcopy(self.value)
        if song_id in value['favorites']:value['favorites'].remove(song_id)
        else:value['favorites'].append(song_id)
        self._commit(value)

    def linked_track(self, song_id):
        track = self.value['spotify'].get(song_id)
        return track if isinstance(track, dict) else None

    def link_spotify(self, song_id, track):
        if not spotify_url(track):raise ValueError('올바른 Spotify 곡 주소가 필요합니다.')
        value = copy.deepcopy(self.value)
        # Save the explicit selected recording, not its source song's guessed metadata.
        value['spotify'][song_id] = {key: track.get(key) for key in
            ('id', 'title', 'artists', 'album', 'duration', 'is_playable', 'restriction', 'isrc', 'checked_at', 'market', 'verification')}
        value['spotify'][song_id]['url'] = spotify_url(track)
        value['automatic_link_exclusions'] = [item for item in value.get('automatic_link_exclusions', [])
                                             if item != song_id]
        self._commit(value)

    def link_automatic(self, song_id, track):
        """Apply a verified proposal without replacing a user's selection."""
        if (song_id in self.value['spotify']
                or song_id in self.value.get('automatic_link_exclusions', [])):return False
        self.link_spotify(song_id, track)
        return True

    def link_automatic_many(self, matches):
        """Commit a collected batch once, preserving current choices and opt-outs."""
        value = copy.deepcopy(self.value)
        excluded = set(value.get('automatic_link_exclusions', []))
        linked = 0
        for song_id, track in matches.items():
            if song_id in value['spotify'] or song_id in excluded:continue
            if not spotify_url(track):raise ValueError('올바른 Spotify 곡 주소가 필요합니다.')
            value['spotify'][song_id] = {key: track.get(key) for key in
                ('id', 'title', 'artists', 'album', 'duration', 'is_playable', 'restriction', 'isrc', 'checked_at', 'market', 'verification')}
            value['spotify'][song_id]['url'] = spotify_url(track)
            linked += 1
        if linked:self._commit(value)
        return linked

    def unlink_spotify(self, song_id):
        value = copy.deepcopy(self.value)
        value['spotify'].pop(song_id, None)
        value['automatic_link_exclusions'] = list(dict.fromkeys(
            value.get('automatic_link_exclusions', []) + [song_id]))
        self._commit(value)

    def remember(self, song_id):
        value = copy.deepcopy(self.value)
        value['recent'] = ([song_id] + [item for item in value['recent'] if item != song_id])[:100]
        self._commit(value)

    def choose(self, songs, playable=False, rng=None):
        choices = {song['id']: song for song in songs if not playable or
                   (self.linked_track(song['id']) or {}).get('is_playable') is True}
        if not choices:return None
        # Exclude at most half the pool; a small collection must remain discoverable.
        # Only explicit work IDs group versions. Similar names alone are not proof.
        works = {}
        for song in choices.values():works.setdefault(song.get('work_id') or song['id'], []).append(song)
        song_works = {song['id']: work for work, versions in works.items() for song in versions}
        recent_works = list(dict.fromkeys(song_works[item] for item in self.value['recent'] if item in song_works))
        excluded = set(recent_works[:min(20, len(works) // 2)])
        works = {key: versions for key, versions in works.items() if key not in excluded}
        rng = rng or random
        return rng.choice(works[rng.choice(list(works))])


def availability(track):
    if not track:return '미연결'
    if track.get('is_playable') is True:return 'Spotify 가능'
    if track.get('is_playable') is False:return '재생 제한'
    return 'Spotify 링크'


def cached_candidates(directory, song):
    """Observed Spotify metadata is a suggestion, never current availability."""
    try:value = read_json(Path(directory) / 'artist_metadata.json', {})
    except (ValueError, OSError):return []
    if not isinstance(value, dict):return []
    entries = value.get('entries', [])
    if not isinstance(entries, list):return []
    output = []
    for item in entries:
        track = item.get('track', {}) if isinstance(item, dict) else {}
        if not isinstance(track, dict) or not spotify_url(track):continue
        if (not isinstance(track.get('title'), str) or not isinstance(track.get('album', ''), str)
                or not isinstance(track.get('artists', []), list)
                or any(not isinstance(artist, str) for artist in track.get('artists', []))):continue
        duration = track.get('duration')
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float))
                                     or not 0 < duration <= 604800):continue
        if search_key(track['title']) == search_key(song['title']):
            output.append({**track, 'url': spotify_url(track), 'is_playable': None,
                           'checked_at': '', 'match_reasons': ['이 PC에서 재생한 제목과 일치']})
    return output
