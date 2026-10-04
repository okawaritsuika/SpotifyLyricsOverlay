"""Conservative automatic recording links using public Spotify metadata only."""
from copy import deepcopy
from functools import lru_cache
import json
import math
from pathlib import Path
import re
from urllib.parse import urlparse

from artist_metadata import ArtistMetadata, validated_track
from artist_names import name_key
from explorer_library import build_performer_lookup, resolve_group_members
from spotify_api import text_key
from spotify_catalog import parse_track_id


MAX_PUBLIC_BYTES = 16 * 1024 * 1024
MAX_PUBLIC_TRACKS = 20000
_CV = re.compile(r'(.*?)\s*[（(]\s*CV\s*[.:：]?\s*([^()（）]+)[)）]', re.I)
_VERSION_LABEL = re.compile(r'\b(?:version|ver|mix|remix|edit|arrange|instrumental|karaoke|live)\b'
                            r'|off[\s-]*vocal|カラオケ', re.I)
_QUOTES = str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'"})


def recording_title_key(value):
    """Normalize version delimiters, keeping every word of the version label."""
    title = text_key(value).translate(_QUOTES)
    suffix = re.fullmatch(r'(.*?)\s*[\[(]([^()\[\]]+)[\])]', title)
    if suffix and _VERSION_LABEL.search(suffix[2]):
        title = suffix[1].rstrip() + ' - ' + suffix[2].strip()
    return title


def _album_key(value):
    # Keep edition names, years, numbers and punctuation; only equivalent
    # typesetting changes across the catalog's documented releases are ignored.
    value = text_key(value).translate(_QUOTES).replace('【', '(').replace('】', ')')
    return re.sub(r'\s+', '', value)


@lru_cache(maxsize=1)
def _recording_aliases():
    """Reviewed title differences are bound to both recording IDs and full titles."""
    path = Path(__file__).resolve().parent / 'resources' / 'spotify_recording_aliases.json'
    try:value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):return {}
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or not isinstance(value.get('entries'), list)):return {}
    aliases = {}
    for entry in value['entries']:
        if not isinstance(entry, dict):continue
        if not all(isinstance(entry.get(key), str) and entry[key].strip() for key in
                   ('catalog_id', 'catalog_title', 'spotify_id', 'spotify_title', 'source_url')):continue
        if not parse_track_id(entry['spotify_id']) or not entry['source_url'].startswith('https://'):continue
        aliases[entry['catalog_id']] = entry
    return aliases


def _reviewed_title_matches(song, track):
    entry = _recording_aliases().get(song.get('id'))
    return bool(entry and song.get('title') == entry['catalog_title']
                and track.get('id') == entry['spotify_id'] and track.get('title') == entry['spotify_title'])


@lru_cache(maxsize=1)
def _voice_aliases():
    """Use only source-backed voice aliases already in the bundled name data."""
    choices = {}
    directory = Path(__file__).resolve().parent / 'resources' / 'artist_names'
    for filename in ('imas_legacy.json', 'imas_modern.json'):
        try:data = json.loads((directory / filename).read_text(encoding='utf-8'))
        except (OSError, ValueError):continue
        if not isinstance(data, dict) or data.get('schema_version') != 1:continue
        entries = data.get('entries')
        if not isinstance(entries, list):continue
        for entry in entries:
            if (not isinstance(entry, dict) or entry.get('kind') != 'voice_actor'
                    or not isinstance(entry.get('name'), str) or not entry['name'].strip()
                    or not isinstance(entry.get('source_urls'), list) or not entry['source_urls']):continue
            canonical = name_key(entry['name'])
            aliases = entry.get('aliases', [])
            for name in [entry['name'], *(aliases if isinstance(aliases, list) else [])]:
                if isinstance(name, str) and name.strip():choices.setdefault(name_key(name), set()).add(canonical)
    return {key: next(iter(values)) for key, values in choices.items() if len(values) == 1}


def _duration(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):return 0
    try:value = float(value)
    except (OverflowError, ValueError):return 0
    return value if math.isfinite(value) and value > 0 else 0


def _credit_details(names, lookup, voice_aliases=None):
    if not isinstance(names, (list, tuple)):return frozenset(), {}
    voice_aliases = _voice_aliases() if voice_aliases is None else voice_aliases
    def voice_key(name):return voice_aliases.get(name_key(name), name_key(name))
    credits, voices, explicit = set(), set(), {}
    plain_names, known_voices = set(), {}
    for name in names:
        if not isinstance(name, str) or not name.strip():continue
        cv = _CV.fullmatch(name)
        if cv:
            name = cv[1]
            voices.add(voice_key(cv[2]))
        else:plain_names.add(voice_key(name))
        person = lookup.get(name_key(name))
        credit = name_key(person['name']) if person else voice_key(name)
        credits.add(credit)
        if cv:explicit.setdefault(credit, set()).add(voice_key(cv[2]))
        if person:
            person_voices = {voice_key(value) for value in person.get('cv', []) if isinstance(value, str)}
            voices.update(person_voices)
            known_voices.setdefault(credit, set()).update(person_voices)
    for person, person_voices in known_voices.items():
        declared = person_voices & plain_names
        if declared:explicit.setdefault(person, set()).update(declared)
    return frozenset(credits - voices), explicit


def _credits(names, lookup):
    return set(_credit_details(names, lookup)[0])


def _expanded(credits, people, lookup, idol_lookup=None):
    result = set()
    for credit in credits:
        group = lookup.get(credit)
        members = resolve_group_members(group, people, idol_lookup=idol_lookup,
                                        group_lookup=lookup) if group else []
        # Only an explicit complete roster can substitute for a group credit.
        if members and all(member.get('kind') == 'idol' for member in members):
            result.update(name_key(member['name']) for member in members)
        else:result.add(credit)
    return result


class _MatchContext:
    def __init__(self, people):
        self.people = list(people)
        self.lookup = build_performer_lookup(self.people)
        self.groups = build_performer_lookup(self.people, 'group')
        self.idols = build_performer_lookup(self.people, 'idol')
        self.voices = _voice_aliases()
        self.credit_cache = {}
        self.expanded_cache = {}

    def details(self, names):
        key = tuple(name for name in names if isinstance(name, str)) if isinstance(names, (list, tuple)) else ()
        if key not in self.credit_cache:self.credit_cache[key] = _credit_details(key, self.lookup, self.voices)
        return self.credit_cache[key]

    def expanded(self, credits):
        if credits not in self.expanded_cache:
            self.expanded_cache[credits] = frozenset(_expanded(credits, self.people, self.groups, self.idols))
        return self.expanded_cache[credits]

    def same_artists(self, wanted, actual):
        left, left_cv = wanted
        right, right_cv = actual
        # An explicit old CV can explain a duplicate bare actor credit even if
        # the current people database knows only the replacement actor.
        for person in left_cv.keys() & right_cv.keys():
            if left_cv[person] != right_cv[person]:return False
        left = left - {voice for person, voices in right_cv.items() if person in left for voice in voices}
        right = right - {voice for person, voices in left_cv.items() if person in right for voice in voices}
        return bool(left and right) and (left == right or self.expanded(left) == self.expanded(right))


def automatic_candidate(song, tracks, people=()):
    """Return one unambiguous recording; never infer country availability.

    Search aliases are deliberately excluded: translations and stripped version
    labels are useful for discovery, but cannot prove recording identity. Only
    reviewed title differences bound to both recording IDs are exceptions.
    """
    return _candidate(song, tracks, _MatchContext(people))


def _candidate(song, tracks, context):
    if not isinstance(song, dict) or not isinstance(tracks, (list, tuple)) or not tracks:return None
    title = recording_title_key(song.get('title'))
    wanted = context.details(song.get('artists'))
    if not title or not wanted[0]:return None
    albums = {_album_key(song.get('album'))}
    albums.update(_album_key(release.get('title')) for release in song.get('releases', [])
                  if isinstance(release, dict))
    albums.discard('')
    duration = _duration(song.get('duration'))
    matches = {}
    for track in tracks:
        if not isinstance(track, dict) or not parse_track_id(track.get('id')):continue
        if title != recording_title_key(track.get('title')) and not _reviewed_title_matches(song, track):continue
        candidate_album = _album_key(track.get('album'))
        candidate_duration = _duration(track.get('duration'))
        # Missing evidence cannot satisfy an explicit album or length boundary.
        if albums and candidate_album not in albums:continue
        if duration and (not candidate_duration or abs(duration - candidate_duration) > 2):continue
        if not context.same_artists(wanted, context.details(track.get('artists'))):continue
        # Titles/credits alone can conflate a rerecording released under the same
        # title. At least album or length evidence is required for auto linking.
        if not albums and not duration:continue
        candidate = deepcopy(track)
        candidate['url'] = 'https://open.spotify.com/track/' + track['id']
        candidate['verification'] = 'automatic_metadata'
        candidate['is_playable'] = track.get('is_playable') if isinstance(track.get('is_playable'), bool) else None
        matches[track['id']] = candidate
    return next(iter(matches.values())) if len(matches) == 1 else None


def cached_pool(directory):
    """Load only previously validated metadata, without stale playability."""
    tracks = [{**entry['track'], 'is_playable': None, 'checked_at': ''}
            for entry in ArtistMetadata(directory).entries.values()]
    try:
        with (Path(directory) / 'spotify_public_tracks.json').open('rb') as stream:
            data = stream.read(MAX_PUBLIC_BYTES + 1)
        if len(data) > MAX_PUBLIC_BYTES:return tracks
        value = json.loads(data)
        if not isinstance(value, dict) or value.get('schema_version') != 1:return tracks
        entries = value.get('tracks')
        if not isinstance(entries, list) or len(entries) > MAX_PUBLIC_TRACKS:return tracks
        for entry in entries:
            if not isinstance(entry, dict):continue
            source = entry.get('source_url')
            if not isinstance(source, str) or len(source) > 4000:continue
            try:url = urlparse(source)
            except ValueError:continue
            if (url.scheme != 'https' or url.netloc != 'open.spotify.com'
                    or not re.fullmatch(r'/(?:embed/)?(?:track|album|playlist|artist)/[A-Za-z0-9]{22}', url.path)):
                continue
            artists = entry.get('artists')
            if not isinstance(artists, list) or not artists:continue
            request = {name: entry.get(name) for name in ('title', 'album', 'duration')}
            request['artist'] = artists[0]
            track = validated_track(request, entry)
            if track:tracks.append({**track, 'source_url': source, 'is_playable': None, 'checked_at': ''})
    except (OSError, ValueError, TypeError, RecursionError):pass
    return tracks


def local_matches(songs, tracks, people=()):
    """Propose links; the UI commits them against current personal choices."""
    by_title = {};by_id = {}
    for track in tracks:
        if isinstance(track, dict):
            by_title.setdefault(recording_title_key(track.get('title')), []).append(track)
            if isinstance(track.get('id'), str):by_id.setdefault(track['id'], []).append(track)
    result = {}
    context = _MatchContext(people)
    for song in songs:
        candidates = by_title.get(recording_title_key(song.get('title')), [])
        reviewed = _recording_aliases().get(song.get('id'))
        if reviewed and song.get('title') == reviewed['catalog_title']:
            candidates = candidates + by_id.get(reviewed['spotify_id'], [])
        track = _candidate(song, candidates, context)
        if track is not None:result[song['id']] = track
    return result
